"""Tests for swfactory.channels: ntfy over a Unix socket and TCP, its three-topic stream, Telegram long polling,
two-step pairing, the boot checks, token hygiene and the privacy class (Wake & Messages §2.7, §5.9, §5.10, S30)."""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from swfactory import channels as ch
from swfactory.channels import (Button, Inbound, NtfyChannel, Rendered, TelegramChannel, channel_privacy,
                                could_be_tailnet, make_channel, new_capability, ntfy_forwarders, parse_tap)
from tests.mock_ntfy import HARNESS_TOKEN, PHONE_TOKEN, REPLY_TOKEN, MockNtfy
from tests.mock_telegram import OWNER_ID, STRANGER_ID, TOKEN, MockTelegram

TAILNET = {"suffix": "tail0123.ts.net", "addresses": ["100.101.102.103", "100.90.1.2"]}


def ntfy_entry(publish_url: str, **extra: Any) -> dict:
    return {"type": "ntfy", "enabled": True, "publish_url": publish_url,
            "public_url": "https://host.tail0123.ts.net:8445", "topic": "seraphina", "taps_topic": "seraphina-taps",
            "chat_topic": "seraphina-chat", "token_env": "SERAPHINA_NTFY_TOKEN",
            "reply_token_env": "SERAPHINA_NTFY_REPLY_TOKEN", **extra}


ENV = {"SERAPHINA_NTFY_TOKEN": HARNESS_TOKEN, "SERAPHINA_NTFY_REPLY_TOKEN": REPLY_TOKEN,
       "SERAPHINA_TELEGRAM_BOT_TOKEN": TOKEN}


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, kind: str, data: dict) -> None:
        self.events.append((kind, data))

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.events]


class Collect:
    """Run channel.receive in a thread and collect what it yields."""

    def __init__(self, channel: ch.Channel) -> None:
        self.channel = channel
        self.stop = threading.Event()
        self.items: list[Inbound] = []
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        for inbound in self.channel.receive(self.stop):
            self.items.append(inbound)

    def wait(self, count: int, timeout: float = 5.0) -> list[Inbound]:
        deadline = time.monotonic() + timeout
        while len(self.items) < count and time.monotonic() < deadline:
            time.sleep(0.02)
        return list(self.items)

    def close(self) -> None:
        self.stop.set()
        self.channel.close()
        self.thread.join(10)


class TempDir(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-channels-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)


class NtfySendTests(TempDir):
    def channel(self, ntfy: MockNtfy, **extra: Any) -> NtfyChannel:
        entry = ntfy_entry(ntfy.publish_url, **extra)
        return NtfyChannel("phone", lambda: entry, ENV, self.tmp / "phone.json",
                           privacy=lambda e: channel_privacy(e, TAILNET))

    def test_publish_over_a_unix_socket_carries_headers_and_json(self) -> None:
        with MockNtfy(unix=True) as ntfy:
            channel = self.channel(ntfy)
            self.assertEqual(channel.privacy, "tailnet")
            cap = new_capability()
            rendered = Rendered(title="Waiting for your decision", text="A job (003) is waiting for your decision.",
                                why="Job 003 has waited since 11:10", priority="default",
                                buttons=[Button("Got it", "ack", cap), Button("Snooze 1 h", "snooze_1h", new_capability())],
                                sequence="M-000412")
            result = channel.send(rendered, "M-000412:phone")
            self.assertEqual(result.state, "sent", result.error)
            published = ntfy.publishes[-1]
            self.assertEqual(published["headers"]["Authorization"], f"Bearer {HARNESS_TOKEN}")
            body = published["body"]
            self.assertEqual(body["topic"], "seraphina")
            self.assertEqual(body["priority"], 3)
            self.assertEqual(body["sequence_id"], "M-000412")
            self.assertEqual(body["message"], "A job (003) is waiting for your decision.\nJob 003 has waited since 11:10")
            ack = body["actions"][0]
            self.assertEqual((ack["action"], ack["method"], ack["url"]),
                             ("http", "POST", "https://host.tail0123.ts.net:8445/seraphina-taps"))
            self.assertEqual(ack["headers"], {"Authorization": f"Bearer {REPLY_TOKEN}"})
            self.assertEqual(json.loads(ack["body"]), {"v": 1, "verb": "ack", "cap": cap})
            self.assertTrue(ack["clear"])
            self.assertNotIn("clear", body["actions"][1])
            self.assertTrue(channel.is_own(result.remote_id))
            self.assertTrue(channel.health()["ok"])

    def test_publish_over_tcp_and_failures(self) -> None:
        with MockNtfy() as ntfy:
            channel = self.channel(ntfy)
            plain = Rendered(title="t", text="x", why="", priority="low")
            self.assertEqual(channel.send(plain, "k").state, "sent")
            self.assertEqual(ntfy.publishes[-1]["body"]["priority"], 2)
            ntfy.fail_publish.append(401)
            failed = channel.send(plain, "k")
            self.assertEqual(failed.state, "failed")
            self.assertIn("401", failed.error)
            self.assertFalse(channel.health()["ok"])
            ntfy.fail_publish.append(429)
            self.assertEqual(channel.send(plain, "k").retry_after, 60.0)

    def test_a_server_error_after_sending_is_unknown_never_failed(self) -> None:
        """DESIGN §40: a 5xx says ntfy failed while handling the publish, not that nothing went out (it may have
        reached the phone before the error), so the notice is never sent again by itself; a refusal is failed."""
        with MockNtfy() as ntfy:
            channel = self.channel(ntfy)
            plain = Rendered(title="t", text="x", why="", priority="low")
            for status in (500, 502, 503, 504):
                ntfy.fail_publish.append(status)
                result = channel.send(plain, "k")
                self.assertEqual((status, result.state), (status, "unknown"))
                self.assertIn(f"HTTP {status}", result.error)
            for status in (400, 403, 413, 429):
                ntfy.fail_publish.append(status)
                self.assertEqual((status, channel.send(plain, "k").state), (status, "failed"))

    def test_a_timeout_after_sending_is_unknown_and_a_refused_connection_is_failed(self) -> None:
        with MockNtfy() as ntfy:
            entry = ntfy_entry(ntfy.publish_url)
            channel = NtfyChannel("phone", lambda: entry, ENV, self.tmp / "phone.json")
            ntfy.hang_publish = True
            original = ch.SEND_TIMEOUT
            ch.SEND_TIMEOUT = 0.5
            try:
                result = channel.send(Rendered(title="t", text="x", why="", priority="low"), "k")
            finally:
                ch.SEND_TIMEOUT = original
            self.assertEqual(result.state, "unknown")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        entry = ntfy_entry(f"http://127.0.0.1:{port}")
        closed = NtfyChannel("phone", lambda: entry, ENV, self.tmp / "other.json")
        self.assertEqual(closed.send(Rendered(title="t", text="x", why="", priority="low"), "k").state, "failed")

    def test_no_buttons_without_the_reply_token(self) -> None:
        with MockNtfy() as ntfy:
            entry = ntfy_entry(ntfy.publish_url)
            channel = NtfyChannel("phone", lambda: entry, {"SERAPHINA_NTFY_TOKEN": HARNESS_TOKEN}, self.tmp / "p.json")
            payload = channel.publish_payload(Rendered(title="t", text="x", why="", priority="default",
                                                       buttons=[Button("Got it", "ack", new_capability())]))
            self.assertNotIn("actions", payload)

    def test_health_check_and_poll(self) -> None:
        with MockNtfy(unix=True) as ntfy:
            channel = self.channel(ntfy)
            self.assertEqual(channel.check_health(), (True, "/v1/health answered HTTP 200"))
            self.assertEqual(channel.poll("seraphina-taps"), 200)
            self.assertEqual(channel.poll("seraphina-taps", token=REPLY_TOKEN), 403)   # write-only


class NtfyReceiveTests(TempDir):
    def test_stream_by_topic_dedupe_own_resume(self) -> None:
        with MockNtfy(unix=True, keepalive=0.1) as ntfy:
            entry = ntfy_entry(ntfy.publish_url)
            state = self.tmp / "phone.json"
            channel = NtfyChannel("phone", lambda: entry, ENV, state)
            own = channel.send(Rendered(title="Your server", text="x", why="", priority="low"), "k")
            self.assertEqual(own.state, "sent")
            collect = Collect(channel)
            try:
                cap = new_capability()
                ntfy.post("seraphina-taps", json.dumps({"v": 1, "verb": "ack", "cap": cap}), token=REPLY_TOKEN)
                ntfy.post("seraphina-taps", "snooze", token=REPLY_TOKEN)
                ntfy.post("seraphina-chat", "  hello there  ", token=PHONE_TOKEN)
                ntfy.post("seraphina", "Your bank needs you: click here", token=HARNESS_TOKEN, title="Urgent")
                items = collect.wait(4)
            finally:
                collect.close()
            kinds = [(i.kind, i.topic) for i in items]
            self.assertEqual(kinds, [("button", "seraphina-taps"), ("forged", "seraphina-taps"),
                                     ("text", "seraphina-chat"), ("foreign", "seraphina")])
            self.assertEqual((items[0].verb, items[0].cap), ("ack", cap))
            self.assertEqual(items[2].text, "hello there")
            self.assertTrue(items[2].sent_at)
            last = json.loads(state.read_text())["last_id"]
            self.assertEqual(last, items[-1].remote_id)
            # a restarted receiver resumes from since=<last id> and sees nothing twice
            again = NtfyChannel("phone", lambda: entry, ENV, state)
            collect = Collect(again)
            try:
                ntfy.post("seraphina-chat", "second", token=PHONE_TOKEN)
                more = collect.wait(1)
            finally:
                collect.close()
            self.assertEqual([i.text for i in more], ["second"])
            self.assertEqual(ntfy.subscriptions[-1]["since"], last)
            self.assertEqual(ntfy.subscriptions[-1]["topics"], ["seraphina", "seraphina-taps", "seraphina-chat"])

    def test_own_notice_seen_before_send_returns_is_not_foreign(self) -> None:
        channel = NtfyChannel("phone", lambda: ntfy_entry("unix:/nonexistent"), ENV, self.tmp / "p.json")
        channel._remember_text("Your server", "a body")
        line = json.dumps({"id": "abc", "event": "message", "topic": "seraphina", "title": "Your server",
                           "message": "a body", "time": 1}).encode()
        self.assertIsNone(channel._line_to_inbound(line, channel.topics()))
        other = json.dumps({"id": "abd", "event": "message", "topic": "seraphina", "title": "Your server",
                            "message": "another body", "time": 1}).encode()
        self.assertEqual(channel._line_to_inbound(other, channel.topics()).kind, "foreign")
        keepalive = json.dumps({"id": "k", "event": "keepalive", "topic": "seraphina"}).encode()
        self.assertIsNone(channel._line_to_inbound(keepalive, channel.topics()))
        self.assertIsNone(channel._line_to_inbound(other, channel.topics()))   # deduped by id

    def test_parse_tap(self) -> None:
        cap = new_capability()
        self.assertEqual(parse_tap(json.dumps({"v": 1, "verb": "ack", "cap": cap})), ("ack", cap))
        for bad in ("ack", json.dumps({"v": 2, "verb": "ack", "cap": cap}),
                    json.dumps({"v": 1, "verb": "ack", "cap": cap, "x": 1}), json.dumps({"v": 1, "verb": "ack"}),
                    json.dumps({"v": 1, "verb": "ACK!", "cap": cap}), json.dumps({"v": 1, "verb": "ack", "cap": "short"}),
                    None, "x" * 300):
            self.assertIsNone(parse_tap(bad), bad)


class TelegramTests(TempDir):
    def channel(self, tg: MockTelegram, **kwargs: Any) -> TelegramChannel:
        entry = {"type": "telegram", "enabled": True, "token_env": "SERAPHINA_TELEGRAM_BOT_TOKEN"}
        channel = TelegramChannel("telegram", lambda: entry, ENV, self.tmp / "telegram.json", api_base=tg.api_base,
                                  poll_seconds=1, http_timeout=5, backoff=(0.05, 0.1), **kwargs)
        self.recorder = Recorder()
        channel.on_event = self.recorder
        return channel

    def pair(self, tg: MockTelegram, channel: TelegramChannel, user: int = OWNER_ID) -> None:
        code = channel.start_pairing()["code"]
        tg.add_message(user, f"/start {code}")
        collect = Collect(channel)
        try:
            self.assertTrue(tg.wait_for(lambda: tg.sent))
            collect.wait(1)
        finally:
            collect.close()
        confirm = tg.sent[-1]["text"].split()[3].rstrip(".")
        self.assertEqual(channel.confirm_pairing(confirm)["state"], "paired")

    def test_two_step_pairing(self) -> None:
        with MockTelegram() as tg:
            channel = self.channel(tg)
            started = channel.start_pairing()
            self.assertEqual(len(started["code"]), 12)
            self.assertEqual(started["link"], f"https://t.me/quiet_helper_bot?start={started['code']}")
            self.assertEqual(started["text"], f"/start {started['code']}")
            self.assertNotIn(started["code"], (self.tmp / "telegram.json").read_text())   # only its sha256
            tg.add_message(STRANGER_ID, "/start wrongcode123")
            tg.add_message(OWNER_ID, f"/start {started['code']}")
            tg.add_message(STRANGER_ID, f"/start {started['code']}")   # single use: the second one is a stranger
            collect = Collect(channel)
            try:
                items = collect.wait(3)
            finally:
                collect.close()
            self.assertEqual([i.kind for i in items], ["stranger", "pairing", "stranger"])
            self.assertEqual(channel.paired(), [])   # nothing paired until the code is typed in the cockpit
            self.assertEqual(channel.pair_status()["state"], "confirm")
            self.assertEqual(channel.pair_status()["account"]["id"], OWNER_ID)
            self.assertEqual(len(tg.sent), 1)
            self.assertEqual(tg.sent[0]["chat_id"], OWNER_ID)
            code = tg.sent[0]["text"].split()[3].rstrip(".")
            self.assertTrue(code.isdigit() and len(code) == 6)
            self.assertEqual(channel.confirm_pairing("000000" if code != "000000" else "111111")["state"], "confirm")
            result = channel.confirm_pairing(code)
            self.assertEqual(result["state"], "paired")
            self.assertEqual([a["id"] for a in channel.paired()], [OWNER_ID])
            self.assertEqual(channel.pair_status()["state"], "paired")
            self.assertTrue(tg.called("setMyCommands"))
            self.assertIn("pairing", self.recorder.kinds())
            self.assertTrue(channel.unpair(OWNER_ID))
            self.assertEqual(channel.paired(), [])

    def test_three_wrong_confirmation_codes_cancel_and_codes_expire(self) -> None:
        with MockTelegram() as tg:
            now = [1_000_000.0]
            channel = self.channel(tg, now=lambda: now[0])
            code = channel.start_pairing()["code"]
            tg.add_message(OWNER_ID, f"/start {code}")
            collect = Collect(channel)
            try:
                collect.wait(1)
            finally:
                collect.close()
            right = tg.sent[0]["text"].split()[3].rstrip(".")
            wrong = "123456" if right != "123456" else "654321"
            for _ in range(3):
                result = channel.confirm_pairing(wrong)
            self.assertEqual(result["state"], "expired")
            self.assertEqual(channel.confirm_pairing(right)["state"], "expired")
            late = channel.start_pairing()["code"]
            now[0] += 11 * 60
            tg.add_message(OWNER_ID, f"/start {late}")
            collect = Collect(channel)
            try:
                items = collect.wait(1)
            finally:
                collect.close()
            self.assertEqual(items[0].kind, "stranger")

    def test_filters_strangers_groups_other_chats_and_non_text(self) -> None:
        with MockTelegram() as tg:
            channel = self.channel(tg)
            self.pair(tg, channel)
            tg.add_message(STRANGER_ID, "hello")
            tg.add_message(OWNER_ID, "in a group", chat_id=-100123, chat_type="group")
            tg.add_message(OWNER_ID, "other chat", chat_id=STRANGER_ID)
            tg.add_message(OWNER_ID, None, photo=True)
            tg.add_message(OWNER_ID, "look at https://x.example/a", reply_to=77)
            tg.add_message(OWNER_ID, "forwarded words", forward=True)
            tg.add_callback(OWNER_ID, f"v1:ack:{new_capability()}", 77)
            tg.add_callback(STRANGER_ID, f"v1:stop:{new_capability()}", 77)
            collect = Collect(channel)
            try:
                items = collect.wait(8)
            finally:
                collect.close()
            self.assertEqual([i.kind for i in items], ["stranger", "stranger", "stranger", "ignored", "text", "text",
                                                       "button", "stranger"])
            self.assertTrue(items[4].forwarded)
            self.assertEqual(items[4].reply_to, f"{OWNER_ID}:77")
            self.assertTrue(items[5].forwarded)
            self.assertEqual((items[6].verb, items[6].reply_to, items[6].sender), ("ack", "77", str(OWNER_ID)))
            self.assertIsNone(items[0].sender)

    def test_offsets_advance_only_after_handling_and_survive_a_restart(self) -> None:
        with MockTelegram() as tg:
            channel = self.channel(tg)
            self.pair(tg, channel)
            state = self.tmp / "telegram.json"
            offset_before = json.loads(state.read_text())["offset"]
            update = tg.add_message(OWNER_ID, "one")
            stop = threading.Event()
            stream = channel.receive(stop)
            first = next(stream)
            self.assertEqual(first.text, "one")
            self.assertEqual(json.loads(state.read_text())["offset"], offset_before)   # not handled yet
            tg.add_message(OWNER_ID, "two")
            second = next(stream)   # resuming means the first was handled
            self.assertEqual(second.text, "two")
            self.assertEqual(json.loads(state.read_text())["offset"], update["update_id"] + 1)
            stop.set()
            stream.close()
            again = self.channel(tg)
            tg.add_message(OWNER_ID, "three")
            collect = Collect(again)
            try:
                items = collect.wait(2)
            finally:
                collect.close()
            # "two" was yielded but its handling never finished (the reader stopped first): it comes again
            self.assertEqual([i.text for i in items], ["two", "three"])
            self.assertIn(update["update_id"] + 1, tg.offsets)

    def test_a_malformed_update_is_an_error_and_the_offset_moves_past_it(self) -> None:
        with MockTelegram() as tg:
            channel = self.channel(tg)
            self.pair(tg, channel)
            bad = tg._queue({"message": {"message_id": 1, "from": {"id": OWNER_ID},
                                         "chat": {"id": OWNER_ID, "type": "private"}, "date": "not a number",
                                         "text": 5}})
            original = channel._to_inbound

            def explode(update: dict, first: bool) -> Inbound | None:
                if update.get("update_id") == bad["update_id"]:
                    raise RuntimeError("a bug in a verb")
                return original(update, first)
            channel._to_inbound = explode   # type: ignore[method-assign]
            tg.add_message(OWNER_ID, "after")
            collect = Collect(channel)
            try:
                items = collect.wait(2)
            finally:
                collect.close()
            self.assertEqual([i.kind for i in items], ["error", "text"])
            self.assertGreater(json.loads((self.tmp / "telegram.json").read_text())["offset"], bad["update_id"])

    def test_conflict_and_retry_after(self) -> None:
        with MockTelegram() as tg:
            channel = self.channel(tg)
            tg.conflict = True

            class Stop(threading.Event):
                waits: list[float] = []

                def wait(self, timeout: float | None = None) -> bool:
                    self.waits.append(timeout)
                    self.set()
                    return True
            stop = Stop()
            list(channel.receive(stop))
            self.assertEqual(stop.waits, [10])
            self.assertIn("conflict", self.recorder.kinds())
            self.assertEqual(channel.health()["detail"], "Another program is reading this bot's messages.")
            tg.conflict = False
            tg.fail["getUpdates"] = (429, "Too Many Requests: retry after 7", 7)
            stop = Stop()
            stop.waits = []
            list(channel.receive(stop))
            self.assertEqual(stop.waits, [7.0])

    def test_a_conflict_after_stop_is_not_reported(self) -> None:
        with MockTelegram() as tg:
            channel = self.channel(tg)
            stop = threading.Event()
            original = channel.call

            def call(method: str, params: dict | None = None, http_timeout: float = 5) -> Any:
                if method == "getUpdates":
                    stop.set()
                    raise ch.TelegramError(method, "409 Conflict", status=409)
                return original(method, params, http_timeout)
            channel.call = call   # type: ignore[method-assign]
            list(channel.receive(stop))
            self.assertNotIn("conflict", self.recorder.kinds())

    def test_boot_check_alarms(self) -> None:
        with MockTelegram() as tg:
            channel = self.channel(tg)
            channel.boot_check()
            self.assertEqual(self.recorder.kinds(), [])
            tg.bot = {**tg.bot, "id": 1}
            tg.webhook_url = "https://elsewhere.example/hook"
            channel.boot_check()
            alarms = [data["kind"] for kind, data in self.recorder.events if kind == "alarm"]
            self.assertEqual(alarms, ["bot_changed", "webhook"])

    def test_send_finish_and_the_token_never_shows(self) -> None:
        with MockTelegram() as tg:
            channel = self.channel(tg)
            self.pair(tg, channel)
            cap = new_capability()
            result = channel.send(Rendered(title="Heads-up", text="A job is waiting for your decision. Open the cockpit.",
                                           why="Waiting since 11:10", priority="low",
                                           buttons=[Button("Got it", "ack", cap)]), "k")
            self.assertEqual(result.state, "sent")
            body = tg.sent[-1]
            self.assertTrue(body["protect_content"])
            self.assertTrue(body["disable_notification"])
            self.assertEqual(body["link_preview_options"], {"is_disabled": True})
            self.assertNotIn("parse_mode", body)
            self.assertEqual(body["reply_markup"]["inline_keyboard"][0][0]["callback_data"], f"v1:ack:{cap}")
            self.assertLessEqual(len(f"v1:snooze_morning:{cap}".encode()), 64)
            self.assertEqual(result.remote_id, f"{OWNER_ID}:{body['message_id']}")
            tap = Inbound(channel="telegram", kind="button", sender=str(OWNER_ID), verb="ack", cap=cap,
                          reply_to=str(body["message_id"]), remote_id="cb-1")
            channel.finish(tap, "Done.")
            self.assertEqual(tg.called("answerCallbackQuery")[-1], {"callback_query_id": "cb-1", "text": "Done."})
            self.assertEqual(tg.called("editMessageReplyMarkup")[-1]["reply_markup"], {"inline_keyboard": []})
            text = Inbound(channel="telegram", kind="text", sender=str(OWNER_ID), text="hi", remote_id="5555")
            channel.finish(text, None)
            self.assertEqual(tg.called("deleteMessage")[-1], {"chat_id": OWNER_ID, "message_id": 5555})
            self.assertTrue(channel.update(result.remote_id, Rendered(title="t", text="Snoozed until 15:07", why="",
                                                                      priority="low")).state == "sent")
            self.assertEqual(tg.called("editMessageText")[-1]["text"], "t\nSnoozed until 15:07")
            # errors name the method only; the token is nowhere
            tg.fail["sendMessage"] = (401, f"Unauthorized for bot{TOKEN}", None)
            failed = channel.send(Rendered(title="t", text="x", why="", priority="low"), "k")
            self.assertEqual(failed.state, "failed")
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            offline = TelegramChannel("tg2", channel.entry, ENV, self.tmp / "tg2.json",
                                      api_base=f"http://127.0.0.1:{port}")
            errors = []
            for attempt in (lambda: offline.call("getMe"), lambda: channel.call("nosuch", {"x": TOKEN})):
                try:
                    attempt()
                except ch.ChannelError as exc:
                    errors.append(exc)
            tg.fail["getMe"] = (500, f"oops {TOKEN}", None)
            try:
                channel.call("getMe")
            except ch.ChannelError as exc:
                errors.append(exc)
            texts = [str(e) for e in errors] + [repr(e) for e in errors] + [repr(channel), failed.error or "",
                                                                             (self.tmp / "telegram.json").read_text(),
                                                                             json.dumps(self.recorder.events)]
            for text in texts:
                self.assertNotIn(TOKEN, text)
                self.assertNotIn(TOKEN.split(":")[1], text)
            self.assertIsNone(errors[0].__cause__)
            self.assertTrue(errors[0].__suppress_context__)


class TelegramOutcomeTests(TempDir):
    """DESIGN §40 for Telegram: a server error after sending is unknown; a retry never reaches an account twice."""

    channel = TelegramTests.channel

    def test_a_server_error_from_telegram_is_unknown(self) -> None:
        with MockTelegram() as tg:
            channel = self.channel(tg)
            channel.update_state(lambda state: state.__setitem__("paired", [{"id": OWNER_ID}]))
            tg.fail["sendMessage"] = (502, "Bad Gateway", None)
            result = channel.send(Rendered(title="t", text="x", why="", priority="low"), "k")
            self.assertEqual(result.state, "unknown")
            tg.fail["sendMessage"] = (429, "Too Many Requests: retry after 7", 7)
            refused = channel.send(Rendered(title="t", text="x", why="", priority="low"), "k")
            self.assertEqual((refused.state, refused.retry_after), ("failed", 7.0))

    def test_a_retry_skips_the_accounts_already_reached(self) -> None:
        second = OWNER_ID + 1
        with MockTelegram() as tg:
            channel = self.channel(tg)
            channel.update_state(lambda state: state.__setitem__("paired", [{"id": OWNER_ID}, {"id": second}]))
            real = channel.call
            refusals = [second]

            def call(method: str, params: dict | None = None, **kwargs: Any) -> Any:
                if method == "sendMessage" and (params or {}).get("chat_id") in refusals:
                    refusals.remove(params["chat_id"])
                    raise ch.TelegramError(method, "429 Too Many Requests", status=429, retry_after=3)
                return real(method, params, **kwargs)

            channel.call = call   # type: ignore[method-assign]
            plain = Rendered(title="t", text="x", why="", priority="low")
            first = channel.send(plain, "k")
            self.assertEqual(first.state, "failed")
            self.assertEqual([body["chat_id"] for body in tg.sent], [OWNER_ID])
            self.assertTrue(first.remote_id.startswith(f"{OWNER_ID}:"))
            again = channel.send(plain, "k2", already=first.remote_id)
            self.assertEqual(again.state, "sent")
            self.assertEqual([body["chat_id"] for body in tg.sent], [OWNER_ID, second], "the owner got it once")
            self.assertEqual(again.remote_id.split(",")[0], first.remote_id)


    def two_accounts(self, tg: MockTelegram) -> tuple[TelegramChannel, int, ch.SendResult]:
        second = OWNER_ID + 1
        channel = self.channel(tg)
        channel.update_state(lambda state: state.__setitem__("paired", [{"id": OWNER_ID}, {"id": second}]))
        first = channel.send(Rendered(title="t", text="one", why="", priority="low"), "k")
        self.assertEqual(first.state, "sent")
        return channel, second, first

    def test_an_edit_with_no_answer_is_unknown_and_sends_nothing_new(self) -> None:
        """DESIGN §40: an edit answered 502 or with no answer may have been applied, so no new message follows it;
        with two accounts, the one already edited never gets a second copy."""
        two = Rendered(title="t", text="two", why="", priority="low")
        with MockTelegram() as tg:
            channel = self.channel(tg)
            channel.update_state(lambda state: state.__setitem__("paired", [{"id": OWNER_ID}]))
            first = channel.send(Rendered(title="t", text="one", why="", priority="low"), "k")
            before = len(tg.sent)
            tg.fail["editMessageText"] = (502, "Bad Gateway", None)
            result = channel.update(first.remote_id, two)
            self.assertEqual(result.state, "unknown")
            self.assertIn("may have been applied", result.error)
            self.assertEqual(len(tg.sent), before, "no new message after an edit whose outcome is unknown")
        with MockTelegram() as tg:
            channel, second, first = self.two_accounts(tg)
            before = len(tg.sent)
            real = channel.call

            def call(method: str, params: dict | None = None, **kwargs: Any) -> Any:
                if method == "editMessageText" and (params or {}).get("chat_id") == second:
                    raise ch._NoAnswer("Telegram editMessageText: no answer from Telegram (TimeoutError)")
                return real(method, params, **kwargs)

            channel.call = call   # type: ignore[method-assign]
            result = channel.update(first.remote_id, two)
            self.assertEqual(result.state, "unknown")
            self.assertEqual(len(tg.sent), before)
            self.assertEqual([body["chat_id"] for body in tg.called("editMessageText")], [OWNER_ID])

    def test_a_refused_edit_falls_back_to_one_new_message_for_that_account_only(self) -> None:
        two = Rendered(title="t", text="two", why="", priority="low")
        with MockTelegram() as tg:
            channel, second, first = self.two_accounts(tg)
            before = len(tg.sent)
            real = channel.call

            def call(method: str, params: dict | None = None, **kwargs: Any) -> Any:
                if method == "editMessageText" and (params or {}).get("chat_id") == second:
                    raise ch.TelegramError(method, "400 Bad Request: message to edit not found", status=400,
                                           retryable=False)
                return real(method, params, **kwargs)

            channel.call = call   # type: ignore[method-assign]
            result = channel.update(first.remote_id, two)
            self.assertEqual(result.state, "sent")
            self.assertEqual([body["chat_id"] for body in tg.sent[before:]], [second], "the edited owner got no copy")
            parts = result.remote_id.split(",")
            self.assertEqual(parts[0], first.remote_id.split(",")[0])
            self.assertTrue(parts[1].startswith(f"{second}:"))
            self.assertNotEqual(parts[1], first.remote_id.split(",")[1])


class NtfyBookkeepingTests(TempDir):
    def test_a_failed_state_write_after_a_200_still_reads_sent(self) -> None:
        """DESIGN §40: once ntfy confirmed the publish, a full disk while the channel writes its own state (the
        own-id list, health, last_sent) is bookkeeping and never turns the send into an error."""
        with MockNtfy() as ntfy:
            entry = ntfy_entry(ntfy.publish_url)
            channel = NtfyChannel("phone", lambda: entry, ENV, self.tmp / "phone.json")

            def full_disk(change: Any) -> Any:
                raise OSError(28, "No space left on device")

            channel.update_state = full_disk   # type: ignore[method-assign]
            result = channel.send(Rendered(title="t", text="x", why="", priority="low"), "k")
            self.assertEqual(result.state, "sent")
            self.assertEqual(len(ntfy.publishes), 1)
            self.assertTrue(channel.is_own(result.remote_id), "the id is kept in memory")


class TelegramBacklogAndMenuTests(TempDir):
    """The first batch is only the backlog (S34); the bot's description follows the Notifier's text."""

    channel = TelegramTests.channel
    pair = TelegramTests.pair

    def test_the_backlog_is_drained_first_and_only_it_is_the_first_batch(self) -> None:
        with MockTelegram(max_wait=1.0) as tg:
            channel = self.channel(tg)
            channel.update_state(lambda state: state.__setitem__("paired", [{"id": OWNER_ID}]))
            tg.add_message(OWNER_ID, "sent while nothing listened")
            collect = Collect(channel)
            try:
                first = collect.wait(1)
                self.assertTrue(first and first[0].first_batch, "the backlog is the first batch")
                self.assertTrue(tg.wait_for(lambda: [p["timeout"] for p in tg.called("getUpdates")][:3] == [0, 0, 1]),
                                "drained with timeout 0 until empty, then long polls")
                time.sleep(0.3)   # inside the first long poll
                tg.add_callback(OWNER_ID, "v1:ack:" + "A" * 26, 5001)
                items = collect.wait(2)
            finally:
                collect.close()
            self.assertEqual(len(items), 2)
            self.assertEqual(items[1].kind, "button")
            self.assertFalse(items[1].first_batch, "a tap made during the first long poll is fresh")

    def test_the_bot_description_comes_from_describe_and_is_refreshed(self) -> None:
        with MockTelegram() as tg:
            channel = self.channel(tg)
            channel.describe = lambda: "This server sleeps 01:00-06:00."
            self.pair(tg, channel)
            self.assertEqual(tg.called("setMyDescription")[-1]["description"], "This server sleeps 01:00-06:00.")
            self.assertTrue(channel.menu_current())
            channel.describe = lambda: "This server sleeps 02:00-05:00."
            self.assertFalse(channel.menu_current())
            self.assertTrue(channel.refresh_menu())
            self.assertEqual(tg.called("setMyDescription")[-1]["description"], "This server sleeps 02:00-05:00.")
            self.assertTrue(channel.menu_current())


class PrivacyTests(unittest.TestCase):
    def test_only_plain_urls_pass(self) -> None:
        """They are pasted into server.yml and into commands the owner runs as root."""
        from swfactory.channels import local_publish_url, public_url_ok

        for url in ("unix:/run/ntfy/ntfy.sock", "http://127.0.0.1:2586", "http://localhost", "http://[::1]:2586/"):
            self.assertTrue(local_publish_url(url), url)
        for url in ('unix:/run/ntfy/ntfy.sock"\nupstream-base-url: "https://ntfy.sh', "unix:/run/../etc/x",
                    "unix:relative", "unix:/a b", "http://127.0.0.1:2586/x?y", "http://user@127.0.0.1",
                    "http://10.0.0.1:2586", "https://127.0.0.1"):
            self.assertFalse(local_publish_url(url), url)
        for url in ("https://host.tail0123.ts.net:8445", "https://host.tail0123.ts.net/", "https://100.90.1.2:8445"):
            self.assertTrue(public_url_ok(url), url)
        for url in ('https://host.tail0123.ts.net:8445"\nfirebase-key-file: "/x', "https://h.ts.net:8445;rm -rf ~",
                    "https://h.ts.net/path", "https://h.ts.net:0", "https://u@h.ts.net", "http://h.ts.net",
                    "https://h.ts.net:99999", None):
            self.assertFalse(public_url_ok(url), url)

    def test_channel_privacy_by_url_form(self) -> None:
        good = ntfy_entry("unix:/run/ntfy/ntfy.sock")
        self.assertEqual(channel_privacy(good, TAILNET), "tailnet")
        self.assertEqual(channel_privacy(ntfy_entry("http://127.0.0.1:2586"), TAILNET), "tailnet")
        cases = {
            "another tailnet's name": ntfy_entry("unix:/run/ntfy/ntfy.sock", public_url="https://host.other99.ts.net"),
            "a CGNAT address that is not a peer": ntfy_entry("unix:/s", public_url="https://100.64.0.9:8445"),
            "plain http": ntfy_entry("unix:/s", public_url="http://host.tail0123.ts.net:8445"),
            "a remote publish URL": ntfy_entry("https://ntfy.sh"),
            "a public server": ntfy_entry("unix:/s", public_url="https://ntfy.sh"),
            "telegram": {"type": "telegram"},
        }
        for label, entry in cases.items():
            with self.subTest(label):
                self.assertEqual(channel_privacy(entry, TAILNET), "third_party")
        self.assertEqual(channel_privacy(ntfy_entry("unix:/s", public_url="https://100.90.1.2:8445"), TAILNET), "tailnet")
        self.assertEqual(channel_privacy(good, None), "third_party")   # Tailscale could not be asked
        self.assertEqual(channel_privacy(good, {"suffix": None, "addresses": []}), "third_party")

    def test_forwarders_downgrade(self) -> None:
        good = ntfy_entry("unix:/run/ntfy/ntfy.sock")
        self.assertEqual(ntfy_forwarders("firebase-key-file: /x.json\n# smtp-sender-addr: x\nupstream-base-url: ''\n"),
                         ["firebase-key-file"])
        self.assertEqual(channel_privacy(good, TAILNET, "firebase-key-file: /x.json"), "third_party")
        relay = 'upstream-base-url: "https://ntfy.sh"\n'
        self.assertEqual(channel_privacy(good, TAILNET, relay), "third_party")
        self.assertEqual(channel_privacy({**good, "ios_relay": True}, TAILNET, relay), "tailnet")
        self.assertEqual(channel_privacy(good, TAILNET, "web-root: disable\n"), "tailnet")

    def test_could_be_tailnet_is_static(self) -> None:
        self.assertTrue(could_be_tailnet(ntfy_entry("unix:/s", public_url="https://host.other.ts.net")))
        self.assertFalse(could_be_tailnet({"type": "telegram"}))
        self.assertFalse(could_be_tailnet(ntfy_entry("unix:/s", public_url="https://ntfy.sh")))

    def test_make_channel(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="sf-make-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        channel = make_channel("phone", lambda: ntfy_entry("unix:/s"), ENV, tmp, api_base="x", stream_timeout=3)
        self.assertIsInstance(channel, NtfyChannel)
        self.assertEqual(channel.stream_timeout, 3)
        tg = make_channel("telegram", lambda: {"type": "telegram", "token_env": "SERAPHINA_TELEGRAM_BOT_TOKEN"},
                          ENV, tmp, api_base="http://127.0.0.1:1", stream_timeout=3)
        self.assertEqual(tg.api_base, "http://127.0.0.1:1")
        with self.assertRaises(ValueError):
            make_channel("x", lambda: {"type": "whatsapp"}, ENV, tmp)
        self.assertEqual(set(tg.secrets()), {TOKEN})
        self.assertEqual(set(channel.secrets()), {HARNESS_TOKEN, REPLY_TOKEN})


if __name__ == "__main__":
    unittest.main()
