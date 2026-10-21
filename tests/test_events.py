from __future__ import annotations

import collections
import json
import os
import queue
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from swfactory import events
from swfactory.events import Event, EventBus, redact, truncate, utc_now

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def drain(q: queue.Queue) -> list[Event]:
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            return items


class IsolatedEnvTestCase(unittest.TestCase):
    """Runs every test with no credential-named variables (*_API_KEY, *_TOKEN, ...) in the environment."""

    def setUp(self) -> None:
        clean = {k: v for k, v in os.environ.items() if not events.is_credential_name(k)}
        patcher = mock.patch.dict(os.environ, clean, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)


class UtcNowTest(unittest.TestCase):
    def test_format_is_utc_seconds_with_z(self) -> None:
        self.assertRegex(utc_now(), TS_RE)


class TruncateTest(unittest.TestCase):
    def test_short_text_unchanged(self) -> None:
        self.assertEqual(truncate("hello", 10), "hello")

    def test_text_at_limit_unchanged(self) -> None:
        self.assertEqual(truncate("x" * 10, 10), "x" * 10)

    def test_long_text_keeps_head_and_tail(self) -> None:
        text = "".join(chr(ord("a") + i % 26) for i in range(100))
        result = truncate(text, 10)
        self.assertEqual(result, text[:6] + "\n… [90 chars truncated] …\n" + text[-4:])

    def test_zero_limit_keeps_only_marker(self) -> None:
        self.assertEqual(truncate("abcdef", 0), "\n… [6 chars truncated] …\n")

    def test_negative_limit_treated_as_zero(self) -> None:
        self.assertEqual(truncate("abc", -5), "\n… [3 chars truncated] …\n")

    def test_odd_split_rounds_head_down(self) -> None:
        result = truncate("0123456789ABCDEFGHIJ", 7)
        self.assertTrue(result.startswith("0123\n"))
        self.assertTrue(result.endswith("\nHIJ"))
        self.assertIn("[13 chars truncated]", result)


class RedactTest(unittest.TestCase):
    def test_replaces_every_occurrence_recursively(self) -> None:
        secret = "sk-abcdef123456"
        data = {
            "text": f"key={secret} again {secret}",
            "nested": {"list": [secret, "plain", 3, None], "tuple": (secret, 1)},
            "number": 42,
        }
        result = redact(data, [secret])
        self.assertEqual(result["text"], "key=*** again ***")
        self.assertEqual(result["nested"]["list"], ["***", "plain", 3, None])
        self.assertEqual(result["nested"]["tuple"], ("***", 1))
        self.assertEqual(result["number"], 42)

    def test_does_not_mutate_input_and_returns_new_containers(self) -> None:
        secret = "password-1234"
        data = {"a": [secret], "b": {"c": secret}}
        result = redact(data, [secret])
        self.assertEqual(data, {"a": [secret], "b": {"c": secret}})
        self.assertIsNot(result, data)
        self.assertIsNot(result["a"], data["a"])
        self.assertIsNot(result["b"], data["b"])

    def test_copies_even_without_secrets(self) -> None:
        data = {"a": [1, 2]}
        result = redact(data, [])
        self.assertEqual(result, data)
        self.assertIsNot(result["a"], data["a"])

    def test_short_and_non_string_secrets_ignored(self) -> None:
        data = {"text": "short abc1234 value"}
        self.assertEqual(redact(data, ["abc1234", "", None, 12345678]), data)

    def test_eight_char_secret_is_redacted(self) -> None:
        self.assertEqual(redact("x12345678y", ["12345678"]), "x***y")

    def test_longer_secret_wins_over_contained_shorter_one(self) -> None:
        long_secret, short_secret = "abcdefgh-ijklmnop", "abcdefgh"
        result = redact(f"{long_secret} {short_secret}", [short_secret, long_secret])
        self.assertEqual(result, "*** ***")

    def test_keys_are_redacted(self) -> None:
        secret = "topsecretkey"
        self.assertEqual(redact({secret: secret, f"k-{secret}": 1, 7: secret}, [secret]),
                         {"***": "***", "k-***": 1, 7: "***"})

    def test_tuple_and_dict_subclasses_become_plain_containers(self) -> None:
        pair = collections.namedtuple("Pair", "a b")
        secret = "secret-value-1"
        data = {"p": pair(secret, 2), "o": collections.OrderedDict(k=secret)}
        result = redact(data, [secret])
        self.assertEqual(result, {"p": ("***", 2), "o": {"k": "***"}})
        self.assertIs(type(result["p"]), tuple)
        self.assertIs(type(result["o"]), dict)


class EnvSecretValuesTest(unittest.TestCase):
    def test_matches_suffixes_only(self) -> None:
        env = {"DEEPSEEK_API_KEY": "a", "GH_TOKEN": "b", "APP_SECRET": "c",
               "my_token": "d", "PATH": "e", "TOKEN_FILE": "f"}
        self.assertEqual(sorted(events.env_secret_values(env)), ["a", "b", "c", "d"])

    def test_common_credential_names(self) -> None:
        env = {"AWS_SECRET_ACCESS_KEY": "a", "STRIPE_SECRET_KEY": "b", "PGPASSWORD": "c", "DB_PASSWORD": "d",
               "GOOGLE_APPLICATION_CREDENTIALS": "e", "ANTHROPIC_BASE_URL": "f", "FACTORY_CONFIG": "g",
               "PWD": "h", "GIT_AUTHOR_NAME": "i"}
        self.assertEqual(sorted(events.env_secret_values(env)), ["a", "b", "c", "d", "e"])
        bus = EventBus()
        with mock.patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "aws-secret-value-0001"}):
            event = bus.publish("log", {"message": "key aws-secret-value-0001 leaked"})
        self.assertEqual(event.data["message"], "key *** leaked")


class EventTest(unittest.TestCase):
    def test_to_dict_has_every_field(self) -> None:
        event = Event(3, "2026-10-20T22:30:00Z", "log", "repo", "001-x", "builder", {"k": 1})
        self.assertEqual(event.to_dict(), {
            "seq": 3, "ts": "2026-10-20T22:30:00Z", "type": "log", "repo": "repo",
            "job": "001-x", "agent": "builder", "data": {"k": 1},
        })
        json.dumps(event.to_dict())


class PublishTest(IsolatedEnvTestCase):
    def test_seq_starts_at_one_and_increases(self) -> None:
        bus = EventBus()
        first = bus.publish("log", {"level": "info", "message": "a"})
        second = bus.publish("repos.updated")
        self.assertEqual((first.seq, second.seq), (1, 2))
        self.assertRegex(first.ts, TS_RE)

    def test_fields_and_defaults(self) -> None:
        bus = EventBus()
        event = bus.publish("agent.text", {"text": "hi"}, repo="r", job="001-a", agent="builder")
        self.assertEqual((event.type, event.repo, event.job, event.agent), ("agent.text", "r", "001-a", "builder"))
        self.assertEqual(event.data, {"text": "hi"})
        bare = bus.publish("repos.updated")
        self.assertEqual(bare.data, {})
        self.assertIsNone(bare.repo)
        self.assertIsNone(bare.job)
        self.assertIsNone(bare.agent)

    def test_data_becomes_plain_json_values(self) -> None:
        bus = EventBus()
        event = bus.publish("log", collections.OrderedDict(
            tup=(1, 2.5, True, None), num=float("nan"), inf=float("-inf"), ints={1: "a"},
            utf8=b"caf\xc3\xa9", bad=b"\xff", nested={"t": ("x",)}))
        self.assertEqual(event.data, {
            "tup": [1, 2.5, True, None], "num": "nan", "inf": "-inf", "ints": {"1": "a"},
            "utf8": "café", "bad": "\ufffd", "nested": {"t": ["x"]},
        })
        self.assertIs(type(event.data), dict)
        json.dumps(event.to_dict(), allow_nan=False)

    def test_data_is_snapshotted(self) -> None:
        bus = EventBus()
        job = {"stage": "queued", "reviews": {"ux": "pending"}}
        event = bus.publish("job.updated", {"job": job})
        job["stage"] = "build"
        job["reviews"]["ux"] = "PASS"
        self.assertEqual(event.data, {"job": {"stage": "queued", "reviews": {"ux": "pending"}}})
        self.assertEqual(bus.since(0)[0].data["job"]["stage"], "queued")

    def test_concurrent_publish_gives_unique_ordered_seqs(self) -> None:
        bus = EventBus(ring_size=10000)
        subscriber = bus.subscribe()
        threads = [threading.Thread(target=lambda: [bus.publish("log") for _ in range(300)])
                   for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        received = [event.seq for event in drain(subscriber)]
        self.assertEqual(received, list(range(1, 2401)))
        self.assertEqual([e.seq for e in bus.since(0)], list(range(1, 2401)))


class SubscribeTest(IsolatedEnvTestCase):
    def test_subscriber_gets_only_later_events(self) -> None:
        bus = EventBus()
        bus.publish("log", {"message": "before"})
        subscriber = bus.subscribe()
        bus.publish("log", {"message": "after"})
        self.assertEqual([e.data["message"] for e in drain(subscriber)], ["after"])

    def test_every_subscriber_gets_every_event(self) -> None:
        bus = EventBus()
        first, second = bus.subscribe(), bus.subscribe()
        for i in range(3):
            bus.publish("log", {"i": i})
        self.assertEqual([e.seq for e in drain(first)], [1, 2, 3])
        self.assertEqual([e.seq for e in drain(second)], [1, 2, 3])

    def test_unsubscribe_stops_delivery(self) -> None:
        bus = EventBus()
        subscriber = bus.subscribe()
        bus.publish("log")
        bus.unsubscribe(subscriber)
        bus.publish("log")
        self.assertEqual([e.seq for e in drain(subscriber)], [1])

    def test_unsubscribe_unknown_queue_is_ignored(self) -> None:
        bus = EventBus()
        bus.unsubscribe(queue.Queue())
        subscriber = bus.subscribe()
        bus.unsubscribe(subscriber)
        bus.unsubscribe(subscriber)

    def test_default_queue_is_bounded(self) -> None:
        self.assertEqual(EventBus().subscribe().maxsize, 10000)

    def test_slow_subscriber_drops_oldest_without_blocking(self) -> None:
        with mock.patch.object(events, "SUBSCRIBER_QUEUE_SIZE", 3):
            bus = EventBus()
            slow = bus.subscribe()
        for _ in range(10):
            bus.publish("log")
        self.assertEqual([e.seq for e in drain(slow)], [8, 9, 10])
        self.assertEqual(len(bus.since(0)), 10)

    def test_full_real_size_queue_never_blocks(self) -> None:
        bus = EventBus(ring_size=10)
        slow = bus.subscribe()
        done = threading.Event()

        def publish_many() -> None:
            for _ in range(10050):
                bus.publish("log")
            done.set()

        thread = threading.Thread(target=publish_many, daemon=True)
        thread.start()
        self.assertTrue(done.wait(30), "publisher blocked on a full subscriber queue")
        self.assertEqual(slow.qsize(), 10000)
        self.assertEqual(slow.get_nowait().seq, 51)


class SinceTest(IsolatedEnvTestCase):
    def test_returns_events_after_seq(self) -> None:
        bus = EventBus()
        for _ in range(5):
            bus.publish("log")
        self.assertEqual([e.seq for e in bus.since(3)], [4, 5])
        self.assertEqual([e.seq for e in bus.since(0)], [1, 2, 3, 4, 5])
        self.assertEqual([e.seq for e in bus.since(-1)], [1, 2, 3, 4, 5])
        self.assertEqual(bus.since(5), [])
        self.assertEqual(bus.since(99), [])

    def test_ring_keeps_only_the_newest(self) -> None:
        bus = EventBus(ring_size=3)
        for _ in range(5):
            bus.publish("log")
        self.assertEqual([e.seq for e in bus.since(0)], [3, 4, 5])

    def test_default_ring_size_is_5000(self) -> None:
        bus = EventBus()
        for _ in range(5003):
            bus.publish("log")
        replay = bus.since(0)
        self.assertEqual(len(replay), 5000)
        self.assertEqual(replay[0].seq, 4)


class SecretRedactionTest(IsolatedEnvTestCase):
    def test_provider_secrets_redacted_from_data(self) -> None:
        secret = "sk-live-0123456789"
        bus = EventBus(secrets_provider=lambda: [secret, "short"])
        subscriber = bus.subscribe()
        event = bus.publish("agent.tool_result", {
            "output": f"Authorization: Bearer {secret}", "short": "short",
            "input": {"cmd": ["echo", secret]}, "ok": True,
        })
        expected = {"output": "Authorization: Bearer ***", "short": "short",
                    "input": {"cmd": ["echo", "***"]}, "ok": True}
        self.assertEqual(event.data, expected)
        self.assertEqual(drain(subscriber)[0].data, expected)
        self.assertEqual(bus.since(0)[0].data, expected)

    def test_provider_is_consulted_on_every_publish(self) -> None:
        secrets = ["first-secret-1"]
        bus = EventBus(secrets_provider=lambda: list(secrets))
        self.assertEqual(bus.publish("log", {"m": "first-secret-1"}).data["m"], "***")
        secrets[:] = ["second-secret-2"]
        self.assertEqual(bus.publish("log", {"m": "first-secret-1 second-secret-2"}).data["m"],
                         "first-secret-1 ***")

    def test_env_secrets_redacted_without_provider(self) -> None:
        with mock.patch.dict(os.environ, {"MY_API_KEY": "env-api-key-1", "GH_TOKEN": "ghp_tokenvalue",
                                          "DB_SECRET": "db-secret-val", "HOME_DIR": "notsecret99"}):
            event = EventBus().publish("log", {"message": "env-api-key-1 ghp_tokenvalue db-secret-val notsecret99"})
        self.assertEqual(event.data["message"], "*** *** *** notsecret99")

    def test_secrets_in_non_string_values_are_redacted(self) -> None:
        secret = "sk-supersecretvalue123"

        class Opaque:
            def __str__(self) -> str:
                return f"Opaque({secret})"

        bus = EventBus(secrets_provider=lambda: [secret])
        subscriber = bus.subscribe()
        event = bus.publish("agent.end", {
            "ok": False,
            "error": RuntimeError(f"401 for key {secret}"),
            "set": {secret},
            "bytes": secret.encode(),
            "bytearray": bytearray(f"x {secret}".encode()),
            "path": Path("/keys") / secret,
            "object": Opaque(),
            "nested": ({"deep": [ValueError(secret)]},),
            secret.encode(): "bytes key",
        })
        expected = {
            "ok": False, "error": "401 for key ***", "set": "{'***'}", "bytes": "***",
            "bytearray": "x ***", "path": "/keys/***", "object": "Opaque(***)",
            "nested": [{"deep": ["***"]}], "b'***'": "bytes key",
        }
        self.assertEqual(event.data, expected)
        self.assertEqual(drain(subscriber)[0].data, expected)
        self.assertNotIn(secret, json.dumps(event.to_dict()))

    def test_provider_errors_propagate_and_nothing_is_published(self) -> None:
        def broken() -> list[str]:
            raise RuntimeError("boom")

        bus = EventBus(secrets_provider=broken)
        subscriber = bus.subscribe()
        with self.assertRaises(RuntimeError):
            bus.publish("log", {"m": "x"})
        self.assertEqual(bus.since(0), [])
        self.assertTrue(subscriber.empty())


class LogPathTest(IsolatedEnvTestCase):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def read_lines(self, path: Path) -> list[dict]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_appends_json_lines_and_creates_parents(self) -> None:
        bus = EventBus()
        log = self.tmp / "jobs" / "r" / "001-x" / "logs" / "01-builder.jsonl"
        first = bus.publish("agent.text", {"text": "héllo"}, repo="r", job="001-x", agent="builder", log_path=log)
        bus.publish("log", {"message": "not logged"})
        second = bus.publish("agent.end", {"ok": True}, log_path=str(log))
        lines = self.read_lines(log)
        self.assertEqual(lines, [first.to_dict(), second.to_dict()])
        self.assertIn("héllo", log.read_text(encoding="utf-8"))

    def test_log_lines_are_redacted(self) -> None:
        bus = EventBus(secrets_provider=lambda: ["hunter2hunter2"])
        log = self.tmp / "run.jsonl"
        bus.publish("agent.output", {"line": "pw hunter2hunter2"}, log_path=log)
        self.assertNotIn("hunter2hunter2", log.read_text(encoding="utf-8"))
        self.assertEqual(self.read_lines(log)[0]["data"]["line"], "pw ***")

    def test_non_json_values_are_stringified(self) -> None:
        bus = EventBus()
        log = self.tmp / "run.jsonl"
        event = bus.publish("log", {"path": Path("/x/y")}, log_path=log)
        self.assertEqual(event.data["path"], "/x/y")
        self.assertEqual(self.read_lines(log)[0]["data"]["path"], "/x/y")

    def test_secrets_inside_objects_never_reach_the_log(self) -> None:
        secret = "sk-supersecretvalue123"
        bus = EventBus(secrets_provider=lambda: [secret])
        log = self.tmp / "run.jsonl"
        bus.publish("agent.end", {"error": RuntimeError(f"401 for key {secret}"), "s": {secret},
                                  "b": secret.encode()}, log_path=log)
        self.assertNotIn(secret, log.read_text(encoding="utf-8"))
        self.assertEqual(self.read_lines(log)[0]["data"],
                         {"error": "401 for key ***", "s": "{'***'}", "b": "***"})

    def test_unwritable_log_raises_after_delivery(self) -> None:
        bus = EventBus()
        subscriber = bus.subscribe()
        blocker = self.tmp / "not-a-dir"
        blocker.write_text("x")
        with self.assertRaises(OSError):
            bus.publish("agent.text", {"text": "hi"}, log_path=blocker / "run.jsonl")
        self.assertEqual([e.data for e in drain(subscriber)], [{"text": "hi"}])
        self.assertEqual(len(bus.since(0)), 1)

    def test_concurrent_writers_produce_whole_lines_in_seq_order(self) -> None:
        bus = EventBus(ring_size=10000)
        log = self.tmp / "logs" / "events.jsonl"
        other = self.tmp / "logs" / "other.jsonl"

        def worker(index: int) -> None:
            for i in range(100):
                bus.publish("agent.text", {"text": f"{index}-{i}" * 50},
                            log_path=log if i % 2 else other)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        main_seqs = [line["seq"] for line in self.read_lines(log)]
        other_seqs = [line["seq"] for line in self.read_lines(other)]
        self.assertEqual(len(main_seqs), 300)
        self.assertEqual(len(other_seqs), 300)
        self.assertEqual(main_seqs, sorted(main_seqs))
        self.assertEqual(other_seqs, sorted(other_seqs))
        self.assertEqual(sorted(main_seqs + other_seqs), list(range(1, 601)))


if __name__ == "__main__":
    unittest.main()
