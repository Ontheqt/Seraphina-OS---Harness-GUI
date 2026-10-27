"""Tests for the GPU custody client (DESIGN.md section 37, swfactory/custody.py) against a mock register
(tests/mock_custody.py): the adapter's requests, residency first, simultaneous claims, generation fencing, a
crashed requester, restart recovery, outcome unknown and no replay, release that is never evidence of free VRAM,
the provider guard (nothing bypasses custody), placement (a denial follows the overflow path), cancellation
while a model loads, and exact restoration after a swap-back."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from swfactory import activity, custody, routing, settings
from swfactory.chats import Route, place_run
from swfactory.config import Config, ModelConfig, ProviderConfig, deep_merge
from swfactory.custody import (Claims, CustodyClient, CustodyDenied, CustodySettings, Lease, Refusal,
                               RegisterAdapter, RegisterError, lease_problem, needs_lease, parse_answer,
                               parse_settings, server_of)
from swfactory.harness.providers import AbortHandle, ProviderError, make_provider, provider_semaphore
from swfactory.plainhttp import Reply
from swfactory.routing import Build
from swfactory.runners import RunContext
from tests.mock_custody import MockRegister
from tests.mock_llm import MockLLM
from tests.test_orchestrator import REPO, TIMEOUT, Call, until
from tests.test_routing import FakeReach, PlacementTestCase


def settings_for(register: MockRegister, **extra: Any) -> CustodySettings:
    return CustodySettings(url=register.url, timeout=extra.pop("timeout", 3.0), **extra)


def provider_cfg(base_url: str, register: MockRegister | None, name: str = "gpu", **extra: Any) -> ProviderConfig:
    return ProviderConfig(name=name, type="openai", base_url=base_url, api_key_env=None, timeout=10,
                          custody=settings_for(register) if register else None, **extra)


def model_cfg(provider: ProviderConfig, model_id: str = "flash") -> ModelConfig:
    return ModelConfig(key="local", provider=provider, model=model_id, max_tokens=50, stream=False)


class RegisterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.register = MockRegister()
        self.register.start()
        self.addCleanup(self.register.stop)

    def client(self, **kwargs: Any) -> CustodyClient:
        return CustodyClient(settings_for(self.register), **kwargs)

    def install(self, client: CustodyClient) -> CustodyClient:
        """Make client the process-wide one for its register (Claims and leased use client_for)."""
        with custody._CLIENTS_LOCK:
            custody._CLIENTS[client.settings.key()] = client
        self.addCleanup(lambda: custody._CLIENTS.pop(client.settings.key(), None))
        return client


# ---------------------------------------------------------------- settings


class SettingsTest(unittest.TestCase):
    def test_custody_is_off_unless_a_provider_names_a_register(self) -> None:
        config = Config("/nonexistent/config.json")
        for name in ("tabby", "strata", "llamacpp"):
            self.assertIsNone(config.provider(name).custody)
            self.assertFalse(needs_lease(config.provider(name)))

    def test_a_lease_is_needed_only_where_a_request_may_load_or_swap(self) -> None:
        register = CustodySettings(url="http://127.0.0.1:1")
        plain = ProviderConfig(name="p", type="openai", base_url="http://127.0.0.1:2/v1", custody=register)
        self.assertFalse(needs_lease(plain), "custody, but nothing a request does loads a model")
        self.assertTrue(needs_lease(ProviderConfig(name="p", type="openai", base_url="http://x/v1", custody=register,
                                                   lazy_load=True)))
        self.assertTrue(needs_lease(ProviderConfig(name="p", type="openai", base_url="http://x/v1", custody=register,
                                                   allow_model_swap=True)))
        self.assertFalse(needs_lease(ProviderConfig(name="p", type="openai", base_url="http://x/v1", lazy_load=True)),
                         "lazy_load alone: custody is off")

    def test_the_settings_are_checked_by_name(self) -> None:
        good = parse_settings({"url": "http://127.0.0.1:9/custody", "token_file": "/run/custody.token",
                               "host_header": "custody.local", "provider": "tabbyapi", "models": {"flash": "Flash-8"},
                               "timeout": 4}, "providers.tabby.custody")
        self.assertEqual((good.provider, dict(good.models), good.timeout), ("tabbyapi", {"flash": "Flash-8"}, 4.0))
        self.assertEqual(custody.register_names(ProviderConfig(name="tabby", type="openai", base_url="http://x/v1",
                                                               custody=good), "flash"), ("tabbyapi", "Flash-8"))
        for bad, words in [({"url": "ftp://x"}, "url"), ({"url": "http://u:p@x"}, "user or password"),
                           ({"url": "http://x", "token_env": "1BAD"}, "token_env"),
                           ({"url": "http://x", "host_header": "a\r\nb"}, "host_header"),
                           ({"url": "http://x", "timeout": 0}, "timeout"), ({"url": "http://x", "brokers": 1}, "unknown"),
                           ("http://x", "object")]:
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, words):
                parse_settings(bad, "providers.tabby.custody")
        config = Config("/nonexistent/config.json", {"providers": {"tabby": {"custody": {"url": "nope"}}}})
        with self.assertRaisesRegex(ValueError, r"providers\.tabby\.custody\.url"):
            settings.validate_settings(config)
        config = Config("/nonexistent/config.json", {"providers": {"tabby": {"lazy_load": "yes"}}})
        with self.assertRaisesRegex(ValueError, "lazy_load"):
            settings.validate_settings(config)

    def test_the_token_is_a_secret_and_never_in_the_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "custody.token"
            token.write_text("custody-token-0123456789\n")
            config = Config(Path(tmp) / "config.json", {"providers": {"tabby": {
                "lazy_load": True, "custody": {"url": "http://127.0.0.1:9", "token_file": str(token)}}}})
            self.assertIn("custody-token-0123456789", config.secret_values())
            self.assertNotIn("custody-token-0123456789", repr(config.public_view()))


# ---------------------------------------------------------------- the adapter and the client


class AdapterTest(RegisterTestCase):
    def test_requests_carry_the_exact_host_the_bearer_and_no_browser_headers(self) -> None:
        self.register.token = "register-token-0123456789"
        with tempfile.NamedTemporaryFile("w", suffix=".token", delete=False) as handle:
            handle.write("register-token-0123456789\n")
        self.addCleanup(os.unlink, handle.name)
        adapter = RegisterAdapter(settings_for(self.register, token_file=handle.name, host_header="custody.local:7"))
        residency = adapter.residency()
        self.assertEqual(residency.resident, [])
        _, path, headers, _ = self.register.requests[-1]
        self.assertEqual((path, headers["host"], headers["authorization"]),
                         ("/residency", "custody.local:7", "Bearer register-token-0123456789"))
        self.assertFalse([name for name in headers if name == "origin" or name.startswith("sec-fetch")])

    def test_a_refusal_with_an_error_status_is_an_answer_and_other_statuses_are_errors(self) -> None:
        sent: list = []

        def send(method: str, url: str, **kwargs: Any) -> Reply:
            sent.append((method, url))
            body = b'{"granted": false, "generation": 7, "reason_code": "HELD", "holder": "operator"}'
            return Reply(423, {}, body) if "leases" in url else Reply(500, {}, b"oops")

        adapter = RegisterAdapter(CustodySettings(url="http://127.0.0.1:1/c"), send=send)
        answer = adapter.acquire("tabby", "flash", "job", "me")
        self.assertEqual((answer.granted, answer.reason_code, answer.holder, answer.generation),
                         (False, "HELD", "operator", 7))
        with self.assertRaises(custody.RegisterError) as caught:
            adapter.residency()
        self.assertEqual(caught.exception.code, "ERROR")
        self.assertEqual(sent[0], ("POST", "http://127.0.0.1:1/c/leases"))

    def test_a_server_error_to_a_lease_request_is_outcome_unknown_never_a_denial(self) -> None:
        """DESIGN §40: a 5xx says the register failed while handling the POST, not that it granted, renewed or
        released nothing; a 4xx without a refusal's shape is a refusal (ERROR); a GET stays a read."""
        statuses: dict[str, int] = {}
        sent: list[str] = []

        def send(method: str, url: str, **kwargs: Any) -> Reply:
            op = url.rsplit("/", 1)[-1]
            sent.append(op)
            if op in statuses:
                return Reply(statuses[op], {}, b"upstream failed")
            payload = ({"granted": True, "lease_id": "l-1", "until": time.time() + 60, "generation": 2}
                       if method == "POST" else {"resident": [], "generation": 1})
            return Reply(200, body=json.dumps(payload).encode())

        settings = CustodySettings(url="http://127.0.0.1:1/c")
        adapter = RegisterAdapter(settings, send=send)
        for status, code in ((500, "OUTCOME_UNKNOWN"), (502, "OUTCOME_UNKNOWN"), (504, "OUTCOME_UNKNOWN"),
                             (307, "OUTCOME_UNKNOWN"), (308, "OUTCOME_UNKNOWN"), (102, "OUTCOME_UNKNOWN"),
                             (404, "ERROR"), (400, "ERROR")):   # one rule: outcomes.of_status
            statuses["leases"] = status
            with self.subTest(status=status), self.assertRaises(RegisterError) as caught:
                adapter.acquire("gpu", "flash", "job", "me")
            self.assertEqual((caught.exception.code, caught.exception.sent), (code, code == "OUTCOME_UNKNOWN"))
        statuses["leases"] = 503
        client = CustodyClient(settings, adapter=adapter)
        sent.clear()
        with self.assertRaises(CustodyDenied) as caught:
            client.claim("gpu", "flash", "job", "me")
        self.assertEqual(caught.exception.code, "OUTCOME_UNKNOWN")
        self.assertEqual(sent, ["residency", "leases", "residency"], "residency read again, nothing replayed")
        self.assertEqual(client.history[-1]["outcome"], "unknown")
        del statuses["leases"]
        lease = client.claim("gpu", "flash", "job", "me")
        statuses["release"] = 500
        client.release(lease)
        self.assertEqual(lease.state, "unknown", "a release the register failed on is not 'unreleased'")
        self.assertEqual(lease.problem(), "the lease's state is unknown (release: POST /leases/l-1/release answered "
                                          "HTTP 500 without an answer this client can read); residency was read "
                                          "again and nothing was replayed", "no talk of a lost connection")

    def test_an_unknown_reason_code_reads_as_unknown_and_a_grant_needs_an_id_and_until(self) -> None:
        answer = custody.parse_answer({"granted": False, "generation": 3, "reason_code": "SLEEPING"})
        self.assertEqual(answer.reason_code, "UNKNOWN")
        with self.assertRaises(custody.RegisterError) as caught:
            custody.parse_answer({"granted": True, "generation": 3})
        self.assertEqual((caught.exception.code, caught.exception.sent), ("OUTCOME_UNKNOWN", True))


class ClientTest(RegisterTestCase):
    def test_residency_is_read_before_every_claim_and_kept_with_the_lease(self) -> None:
        self.register.load("gpu", "owners-model")
        lease = self.client().claim("gpu", "flash", "job demo/001 builder", "h1")
        self.assertEqual(self.register.ops(), ["residency", "acquire"])
        self.assertEqual(lease.before.on("gpu"), [("gpu", "owners-model")])
        self.assertEqual(self.register.requests[-1][3], {"provider": "gpu", "model": "flash",
                                                         "purpose": "job demo/001 builder", "holder": "h1"})
        self.assertIsNone(lease.problem())

    def test_simultaneous_claims_exactly_one_is_granted(self) -> None:
        clients = [self.client() for _ in range(8)]
        barrier = threading.Barrier(len(clients))
        results: list[Any] = [None] * len(clients)

        def claim(index: int) -> None:
            barrier.wait()
            try:
                results[index] = clients[index].claim("gpu", "flash", "job", f"holder-{index}")
            except CustodyDenied as exc:
                results[index] = exc

        threads = [threading.Thread(target=claim, args=(index,)) for index in range(len(clients))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        granted = [result for result in results if isinstance(result, Lease)]
        denied = [result for result in results if isinstance(result, CustodyDenied)]
        self.assertEqual((len(granted), len(denied)), (1, 7))
        self.assertEqual({exc.code for exc in denied}, {"LEASED"})
        self.assertEqual(len(self.register.leases), 1)

    def test_runs_of_one_process_share_one_lease_released_by_the_last(self) -> None:
        client = self.client()
        first = client.claim("gpu", "flash", "job a", "me")
        second = client.claim("gpu", "flash", "job b", "me")
        self.assertIs(first, second)
        self.assertEqual(self.register.ops().count("acquire"), 1)
        client.release(first, "a ended")
        self.assertEqual(len(self.register.leases), 1, "still used by the other run")
        client.release(second, "b ended")
        self.assertEqual(self.register.leases, {})

    def test_a_stale_generation_is_refused_and_a_stale_grant_given_back(self) -> None:
        client = self.client()
        client.observe()
        self.register.generation += 10      # the client has seen this generation now
        client.observe()
        self.register.stale = 1             # the next lease answer carries an older generation
        with self.assertRaises(CustodyDenied) as caught:
            client.claim("gpu", "flash", "job", "me")
        self.assertEqual(caught.exception.code, "STALE")
        self.assertIn("older than", caught.exception.detail)
        self.assertEqual(self.register.ops()[-1], "release", "the stale grant is released, once")
        self.assertEqual(self.register.leases, {})

    def test_an_answer_whose_until_has_passed_is_stale(self) -> None:
        client = self.client(now=lambda: time.time() + 3600)   # this clock is past every until the register gives
        with self.assertRaises(CustodyDenied) as caught:
            client.claim("gpu", "flash", "job", "me")
        self.assertEqual(caught.exception.code, "STALE")
        self.assertIn("until has passed", caught.exception.detail)

    def test_a_stale_residency_answer_is_refused(self) -> None:
        client = self.client()
        client.observe()
        self.register.generation -= 1
        with self.assertRaises(CustodyDenied) as caught:
            client.claim("gpu", "flash", "job", "me")
        self.assertEqual(caught.exception.code, "STALE")
        self.assertNotIn("acquire", self.register.ops(), "no lease is asked for on a stale view")

    def test_requester_crash_the_lease_expires_and_the_next_requester_sees_residency_first(self) -> None:
        crashed = self.client()
        crashed.claim("gpu", "flash", "job", "harness:111")
        self.register.load("gpu", "flash")          # its model loaded; then the requester died without a release
        later = self.client()
        with self.assertRaises(CustodyDenied) as caught:
            later.claim("gpu", "flash", "job", "harness:222")
        self.assertEqual(caught.exception.code, "LEASED")
        self.assertIn("harness:111", caught.exception.detail)
        self.register.now += self.register.ttl + 1  # the crashed requester's lease expires at the register
        before = len(self.register.requests)
        lease = later.claim("gpu", "flash", "job", "harness:222")
        self.assertEqual(self.register.ops()[before:], ["residency", "acquire"], "residency first")
        self.assertEqual(lease.before.on("gpu"), [("gpu", "flash")],
                         "expiry freed no VRAM: the crashed requester's model is still resident")

    def test_restart_recovery_assumes_no_orphaned_claim(self) -> None:
        before_restart = self.client()
        orphan = before_restart.claim("gpu", "flash", "job", "harness:old")
        after_restart = self.client()                # a new process: no lease held, none assumed
        self.assertEqual(after_restart.view()["leases"], [])
        with self.assertRaises(CustodyDenied):
            after_restart.claim("gpu", "flash", "job", "harness:new")
        self.assertNotIn(f"/leases/{orphan.lease_id}/release", self.register.paths(), "never released by assumption")
        self.assertNotIn(f"/leases/{orphan.lease_id}/renew", self.register.paths(), "nor renewed as its own")
        self.assertIn(orphan.lease_id, self.register.leases, "the register keeps it until it expires")
        self.register.now += self.register.ttl + 1
        lease = after_restart.claim("gpu", "flash", "job", "harness:new")
        self.assertNotEqual(lease.lease_id, orphan.lease_id)

    def test_a_lost_connection_on_a_claim_is_outcome_unknown_and_never_replayed(self) -> None:
        self.register.drop["acquire"] = 1
        client = self.client()
        with self.assertRaises(CustodyDenied) as caught:
            client.claim("gpu", "flash", "job", "me")
        self.assertEqual(caught.exception.code, "OUTCOME_UNKNOWN")
        self.assertIn("not replayed", caught.exception.detail)
        self.assertEqual(self.register.ops(), ["residency", "acquire", "residency"])
        self.assertEqual(client.leases, {}, "nothing is assumed held")

    def test_a_lease_request_is_journaled_before_it_is_sent(self) -> None:
        """DESIGN §40: the intent comes first. A harness that dies while POST /leases is out leaves a request with
        no answer in the journal, and the activity query reads it as outcome unknown; an answered one shows only
        its answer."""

        class Died(BaseException):
            pass

        data = Path(tempfile.mkdtemp(prefix="sf-custody-intent-"))
        self.addCleanup(__import__("shutil").rmtree, data, True)
        custody.set_journal(data)
        self.addCleanup(custody.set_journal, None)
        client = self.client()
        lease = client.claim("gpu", "flash", "job app/001 builder", "me")
        client.release(lease, "done")
        real = client.adapter.acquire

        def granted_then_died(*args: Any, **kwargs: Any) -> Any:
            real(*args, **kwargs)   # the register granted and recorded it
            raise Died()            # the harness died before it read the answer

        client.adapter.acquire = granted_then_died   # type: ignore[method-assign]
        with self.assertRaises(Died):
            client.claim("gpu", "flash", "job app/002 builder", "me")
        lines = [json.loads(line) for path in sorted((data / "custody").glob("journal-*.jsonl"))
                 for line in path.read_text().splitlines()]
        self.assertEqual([(line["op"], line["outcome"]) for line in lines],
                         [("claim", "requested"), ("claim", "granted"), ("release", "requested"),
                          ("release", "released"), ("claim", "requested")])
        self.assertEqual((lines[-1]["purpose"], lines[-1]["holder"]), ("job app/002 builder", "me"))
        self.assertEqual(len(self.register.leases), 1, "the register holds a lease the harness never saw")
        entries = activity._custody_entries(data, "ops", True, None, None)
        self.assertEqual([entry["kind"] for entry in entries],
                         ["lease granted", "lease released", "claim outcome unknown"])
        self.assertIn("requested with no answer recorded: flash on gpu for job app/002 builder", entries[-1]["summary"])
        self.register.leases.clear()

    def test_release_is_never_evidence_that_vram_is_free(self) -> None:
        client = self.client()
        lease = client.claim("gpu", "flash", "job", "me")
        self.register.load("gpu", "flash")
        view = client.release(lease, "the run ended")
        self.assertEqual(view["state"], "released")
        self.assertEqual(self.register.ops()[-2:], ["release", "residency"], "residency is read after the release")
        self.assertEqual(view["resident_after"], [["gpu", "flash"]], "released, and the model is still resident")

    def test_a_lost_connection_on_release_is_unknown_and_never_replayed(self) -> None:
        client = self.client()
        lease = client.claim("gpu", "flash", "job", "me")
        self.register.drop["release"] = 1
        view = client.release(lease, "the run ended")
        self.assertEqual(view["state"], "unknown")
        self.assertEqual(self.register.ops().count("release"), 1)
        self.assertEqual(self.register.ops()[-1], "residency")
        self.assertIn(lease.lease_id, self.register.leases, "not assumed released")

    def test_renewal_extends_a_lease_and_a_refused_renewal_loses_it(self) -> None:
        client = self.client()
        lease = client.claim("gpu", "flash", "job", "me")
        old = lease.until
        self.register.now += 30
        self.assertTrue(client.renew(lease))
        self.assertGreater(lease.until, old)
        self.register.leases.clear()        # the operator revoked it
        self.assertFalse(client.renew(lease))
        self.assertEqual(lease.state, "lost")
        self.assertIn("lost", lease.problem())

    def test_the_keeper_renews_a_held_lease(self) -> None:
        self.register.ttl = 2.0
        client = self.client()
        lease = client.claim("gpu", "flash", "job", "me")
        lease.keep()
        until(lambda: "renew" in self.register.ops(), timeout=5)
        client.release(lease)
        self.assertEqual(lease.state, "released")

    def test_an_unreachable_register_or_maintenance_gives_no_lease(self) -> None:
        self.register.maintenance = True
        with self.assertRaises(CustodyDenied) as caught:
            self.client().claim("gpu", "flash", "job", "me")
        self.assertEqual(caught.exception.code, "MAINTENANCE")
        dead = CustodyClient(CustodySettings(url="http://127.0.0.1:9", timeout=1))
        with self.assertRaises(CustodyDenied) as caught:
            dead.claim("gpu", "flash", "job", "me")
        self.assertEqual(caught.exception.code, "UNREACHABLE")


# ---------------------------------------------------------------- the provider guard


class ProviderGuardTest(RegisterTestCase):
    def test_nothing_bypasses_custody_a_call_without_a_lease_never_goes_out(self) -> None:
        with MockLLM([{"text": "hi"}]) as llm:
            cfg = provider_cfg(llm.base_url, self.register, lazy_load=True)
            with self.assertRaisesRegex(ProviderError, "GPU custody: gpu is under GPU custody and this call holds no "
                                                       "lease"):
                make_provider(cfg).complete(model_cfg(cfg), "", [{"role": "user", "content": "x"}], [])
            refused = provider_cfg(llm.base_url, self.register, lazy_load=True, lease=Refusal("LEASED: busy"))
            with self.assertRaisesRegex(ProviderError, "gave no lease for gpu/flash: LEASED: busy"):
                make_provider(refused).complete(model_cfg(refused), "", [{"role": "user", "content": "x"}], [])
            self.assertEqual(llm.requests, [])
            plain = provider_cfg(llm.base_url, self.register)   # custody, but no request loads a model
            make_provider(plain).complete(model_cfg(plain), "", [{"role": "user", "content": "x"}], [])
            self.assertEqual(len(llm.requests), 1)

    def test_a_call_with_a_valid_lease_for_its_model_goes_out(self) -> None:
        self.install(self.client())
        with MockLLM([{"text": "hi"}, {"text": "again"}]) as llm:
            cfg = provider_cfg(llm.base_url, self.register, lazy_load=True)
            with custody.leased(model_cfg(cfg), "test") as leased_model:
                self.assertIsInstance(leased_model.provider.lease, Lease)
                answer = make_provider(leased_model.provider).complete(leased_model, "", [{"role": "user",
                                                                                           "content": "x"}], [])
                self.assertEqual(answer.text, "hi")
                other = model_cfg(leased_model.provider, "other-model")
                self.assertIn("not gpu/other-model", lease_problem(other.provider, "other-model"))
            self.assertEqual(self.register.leases, {}, "released when the block ends")
            self.assertIn("released", lease_problem(leased_model.provider, "flash"))

    def test_attach_gives_back_a_lease_taken_for_another_build(self) -> None:
        """A run that waited for its slot and then overflowed holds the first build's lease and the overflow's
        until attach() keeps the one it runs on."""
        self.install(self.client())
        first = provider_cfg("http://127.0.0.1:59007/v1", self.register, name="gpu-a", lazy_load=True)
        second = provider_cfg("http://127.0.0.1:59008/v1", self.register, name="gpu-b", lazy_load=True)
        claims = Claims("job demo/001 builder")
        self.assertEqual((claims(first, "flash"), claims(second, "flash")), ((True, ""), (True, "")))
        self.assertEqual(len(self.register.leases), 2)
        placed = claims.attach(model_cfg(second))
        self.assertEqual((placed.provider.lease.harness_provider, claims.info()["custody"]["provider"]),
                         ("gpu-b", "gpu-b"))
        self.assertEqual([lease["provider"] for lease in self.register.leases.values()], ["gpu-b"])
        outside = claims.attach(model_cfg(provider_cfg("http://127.0.0.1:59009/v1", None, name="api")))
        self.assertIsNone(outside.provider.lease, "a server outside custody carries nothing")
        self.assertEqual(self.register.leases, {}, "and every lease went back")

    def test_a_lost_connection_under_a_lease_is_never_replayed_and_residency_is_read_again(self) -> None:
        self.install(self.client())
        errors: list[dict] = []
        with MockLLM([{"text": "late"}], error_sequence=[{"disconnect": True}]) as llm:
            cfg = provider_cfg(llm.base_url, self.register, lazy_load=True)
            with custody.leased(model_cfg(cfg), "test") as leased_model:
                before = len(self.register.requests)
                with self.assertRaisesRegex(ProviderError, "outcome is unknown, so the request was not replayed"):
                    make_provider(leased_model.provider).complete(
                        leased_model, "", [{"role": "user", "content": "x"}], [], on_error=errors.append)
                self.assertEqual(len(llm.requests), 1, "not retried")
                self.assertEqual(self.register.ops()[before:], ["residency"])
                self.assertIsNone(leased_model.provider.lease.problem(), "the lease itself is not assumed lost")
        self.assertEqual((len(errors), errors[0]["connection"], errors[0]["will_retry"]), (1, True, False))

    def test_an_answer_of_500_or_more_under_a_lease_is_unknown_and_never_replayed(self) -> None:
        """DESIGN §40: a gateway's 504 says nothing about the lazy-loading server behind it, so under a lease it
        reads like a lost connection; a refusal below 500 (429) is retried under the same lease."""
        self.install(self.client())
        with MockLLM([{"text": "late"}], error_sequence=[504]) as llm:
            cfg = provider_cfg(llm.base_url, self.register, lazy_load=True)
            with custody.leased(model_cfg(cfg), "test") as leased_model:
                before = len(self.register.requests)
                provider = make_provider(leased_model.provider)
                provider._sleep = lambda seconds: None
                with self.assertRaisesRegex(ProviderError, "HTTP 504.*outcome is unknown, so the request was not "
                                                           "replayed"):
                    provider.complete(leased_model, "", [{"role": "user", "content": "x"}], [])
                self.assertEqual(len(llm.requests), 1, "not replayed")
                self.assertEqual(self.register.ops()[before:], ["residency"])
        with MockLLM([{"text": "second"}], error_sequence=[429]) as llm:
            cfg = provider_cfg(llm.base_url, self.register, lazy_load=True)
            with custody.leased(model_cfg(cfg), "test") as leased_model:
                provider = make_provider(leased_model.provider)
                provider._sleep = lambda seconds: None
                answer = provider.complete(leased_model, "", [{"role": "user", "content": "x"}], [])
                self.assertEqual((answer.text, len(llm.requests)), ("second", 2))

    def test_a_request_never_sent_under_a_lease_is_tried_six_times(self) -> None:
        """The operator confirmed (26 Oct): a connection that was never made sent nothing, so under a lease it is
        tried again as for any provider: six tries in all (MAX_ATTEMPTS: the first and five retries, after 2, 4,
        8, 16 and 32 s), each under the same lease, and nothing reads as outcome unknown."""
        from swfactory.harness import providers
        from tests.test_providers import FakeSleep, closed_port
        self.install(self.client())
        errors: list[dict] = []
        cfg = provider_cfg(f"http://127.0.0.1:{closed_port()}/v1", self.register, lazy_load=True)
        with custody.leased(model_cfg(cfg), "test") as leased_model:
            before = len(self.register.requests)
            sleep = FakeSleep()
            provider = make_provider(leased_model.provider)
            provider._sleep = sleep
            with self.assertRaises(ProviderError) as caught:
                provider.complete(leased_model, "", [{"role": "user", "content": "x"}], [], on_error=errors.append)
            self.assertFalse(caught.exception.sent)
            self.assertNotIn("outcome is unknown", str(caught.exception))
            self.assertEqual(providers.MAX_ATTEMPTS, 6)
            self.assertEqual([error["will_retry"] for error in errors], [True] * 5 + [False])
            self.assertEqual(sleep.total, 62)
            self.assertEqual(self.register.requests[before:], [], "no residency read: nothing was sent")
            self.assertIsNone(leased_model.provider.lease.problem(), "the same lease throughout")

    def test_cancellation_while_a_model_loads_releases_and_reads_residency_again(self) -> None:
        self.install(self.client())
        cancel, abort = threading.Event(), AbortHandle()   # a cancel ends a read blocked on a loading server
        failures: list[BaseException] = []
        with MockLLM([{"text": "loaded at last", "delay": 3}]) as llm:
            cfg = provider_cfg(llm.base_url, self.register, lazy_load=True)
            claims = Claims("chat c-0123456789")
            self.assertEqual(claims(cfg, "flash"), (True, ""))
            leased_model = claims.attach(model_cfg(cfg))

            def call() -> None:
                try:
                    make_provider(leased_model.provider).complete(leased_model, "", [{"role": "user", "content": "x"}],
                                                                  [], cancel=cancel, abort=abort)
                except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
                    failures.append(exc)

            caller = threading.Thread(target=call)
            caller.start()
            until(lambda: llm.requests, timeout=5)     # the server is loading the model
            self.register.load("gpu", "flash")
            cancel.set()
            abort.fire()
            caller.join(10)
            self.assertIn("cancelled", str(failures[0]))
            views = claims.release("cancelled")
        self.assertEqual(self.register.ops()[-2:], ["release", "residency"])
        self.assertEqual((views[0]["state"], views[0]["resident_after"]), ("released", [["gpu", "flash"]]))


# ---------------------------------------------------------------- placement


class CustodyPlacementTest(PlacementTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.register = MockRegister()
        self.register.start()
        self.addCleanup(self.register.stop)
        self.tabby = f"tabby{self.suffix}"

    def custody_config(self, **tabby: Any) -> Config:
        return self.placed_config(providers={self.tabby: {
            "lazy_load": True, "custody": {"url": self.register.url, "timeout": 3}, **tabby}})

    def test_a_granted_lease_is_on_the_run_and_given_back_when_it_ends(self) -> None:
        seen: list[Any] = []

        def build(ctx: RunContext, call: Call) -> None:
            seen.append(ctx.model.provider.lease)
            self.assertIsNone(lease_problem(ctx.model.provider, ctx.model.model), "its calls may go out")
            from tests.test_orchestrator import build as default_build
            default_build(ctx, call)

        job = self.run_job(self.start_placed(self.custody_config(), builder=build))
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertIsInstance(seen[0], Lease)
        start = self.start_event(job, "builder")
        self.assertEqual(start.data["custody"]["lease_id"], seen[0].lease_id)
        self.assertEqual((start.data["provider"], start.data["custody"]["provider"]), (self.tabby, self.tabby))
        run = next(entry for entry in job["runs"] if entry["agent"] == "builder")
        self.assertEqual(run["custody"]["lease_id"], seen[0].lease_id)
        self.assertEqual(self.register.leases, {}, "every lease went back")
        self.assertEqual(self.register.requests[-1][1], "/residency", "residency read after the last release")
        self.assertIn(f"GPU custody lease {seen[0].lease_id} on {self.tabby}/flash-exl3 released", self.logs(job))
        purposes = {body.get("purpose") for _, path, _, body in self.register.requests if path == "/leases"}
        self.assertIn(f"job {REPO}/{job['id']} builder", purposes)

    def test_a_denied_build_is_unavailable_and_the_run_follows_the_overflow_path(self) -> None:
        self.register.leases["held"] = {"id": "held", "provider": self.tabby, "model": "owners-model",
                                        "holder": "operator", "purpose": "maintenance", "until": time.time() + 3600}
        job = self.run_job(self.start_placed(self.custody_config()))
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(self.recorder.of("builder")[0].ctx.model.provider.name, f"strata{self.suffix}")
        self.assertIn(f"build {self.tabby}/flash-exl3 is unavailable: GPU custody: LEASED", self.logs(job))

    def test_an_unreachable_register_makes_the_build_unavailable_too(self) -> None:
        config = self.placed_config(
            providers={self.tabby: {"lazy_load": True, "custody": {"url": "http://127.0.0.1:9", "timeout": 1}},
                       f"strata{self.suffix}": {"lazy_load": True, "custody": {"url": "http://127.0.0.1:9",
                                                                                "timeout": 1}}})
        job = self.run_job(self.start_placed(config))
        ctx = self.recorder.of("builder")[0].ctx
        self.assertEqual((ctx.model.key, ctx.model.provider.name), ("cheap", "api"), "overflow: no lease anywhere")
        self.assertIn("GPU custody: UNREACHABLE", self.start_event(job, "builder").data["overflow"]["why"])

    def test_cancelling_a_run_while_its_model_loads_releases_and_reads_residency(self) -> None:
        started, finish = threading.Event(), threading.Event()

        def build(ctx: RunContext, call: Call) -> None:
            self.register.load(self.tabby, "flash-exl3")   # the first request made the server load it
            started.set()
            ctx.cancel.wait(TIMEOUT)
            finish.set()

        factory = self.start_placed(self.custody_config(), builder=build)
        job = factory.submit(REPO, "x")
        started.wait(TIMEOUT)
        lease_id = next(iter(self.register.leases))
        factory.cancel(REPO, job["id"])
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual(job["stage"], "cancelled")
        paths = self.register.paths()
        self.assertIn(f"/leases/{lease_id}/release", paths, "the release was attempted")
        self.assertEqual(paths[paths.index(f"/leases/{lease_id}/release") + 1], "/residency", "then residency read")
        self.assertIn("released; resident on", self.logs(job))
        self.assertIn("flash-exl3", self.logs(job), "the model is still resident: a release frees nothing")

    def test_exact_restoration_after_a_swap_back(self) -> None:
        tabby = self.tabby
        self.reach.loaded_map[tabby] = "owners-model"
        self.register.load(tabby, "owners-model")

        def handler(body: dict, path: str) -> dict:
            self.register.load(tabby, body["model"])   # the server loads whatever a request names
            return {"text": "OK"}

        with MockLLM(handler=handler) as llm:
            config = self.placed_config(providers={tabby: {
                "kind": "tabby", "allow_model_swap": True, "swap_back": True, "base_url": llm.base_url + "/v1",
                "custody": {"url": self.register.url, "timeout": 3}}})

            def build(ctx: RunContext, call: Call) -> None:
                self.register.load(tabby, "flash-exl3")        # the run's request swapped the model in
                self.reach.loaded_map[tabby] = "flash-exl3"
                from tests.test_orchestrator import build as default_build
                default_build(ctx, call)

            job = self.run_job(self.start_placed(config, builder=build))
            self.assertEqual(job["stage"], "merged", job.get("reason"))
            until(lambda: "matches what it was before the swap" in self.logs(job))
        self.assertEqual([request["model"] for request in llm.requests], ["owners-model"])
        self.assertEqual(self.register.resident[0]["model"], "owners-model")
        purposes = [body.get("purpose") for _, path, _, body in self.register.requests if path == "/leases"]
        self.assertEqual(purposes[-1], f"swap_back {REPO}/{job['id']}", "the swap-back held its own lease")
        self.assertEqual(self.register.leases, {})

    def test_a_restoration_that_does_not_match_is_reported(self) -> None:
        tabby = self.tabby
        self.reach.loaded_map[tabby] = "owners-model"
        self.register.load(tabby, "owners-model")
        with MockLLM(handler=lambda body, path: {"text": "OK"}) as llm:   # the server never loads it back
            config = self.placed_config(providers={tabby: {
                "kind": "tabby", "allow_model_swap": True, "swap_back": True, "base_url": llm.base_url + "/v1",
                "custody": {"url": self.register.url, "timeout": 3}}})

            def build(ctx: RunContext, call: Call) -> None:
                self.register.load(tabby, "flash-exl3")
                self.reach.loaded_map[tabby] = "flash-exl3"
                from tests.test_orchestrator import build as default_build
                default_build(ctx, call)

            job = self.run_job(self.start_placed(config, builder=build))
            until(lambda: "differs from before the swap" in self.logs(job))
        self.assertIn("before owners-model, now flash-exl3", self.logs(job))

    def test_a_cli_route_that_gets_no_lease_fails_its_step(self) -> None:
        self.register.maintenance = True
        config = self.custody_config()
        config = Config(config.path, deep_merge(config.raw, {"agents": {"builder": {"driver": "qwen"}}}))
        job = self.run_job(self.start_placed(config))
        self.assertEqual(job["stage"], "failed")
        self.assertIn("GPU custody: MAINTENANCE", job.get("reason") or "")
        self.assertEqual(self.recorder.of("builder"), [])


class LivePlacementCustodyTest(RegisterTestCase):
    def test_a_chat_turn_takes_a_lease_and_its_release_gives_it_back(self) -> None:
        config = Config(Path(tempfile.mkdtemp()) / "config.json", {
            "providers": {"gpu": {"type": "openai", "base_url": "http://127.0.0.1:59003/v1", "api_key_env": None,
                                  "lazy_load": True, "custody": {"url": self.register.url, "timeout": 3}}},
            "models": {"local": {"provider": "gpu", "model": "flash"}}})
        events: list = []
        placed = place_run(config, Route("builtin", "local", {}, None), FakeReach(), threading.Event(),
                           lambda kind, data: events.append((kind, data)), "chat c-0123456789")
        model, info, _, release = placed
        self.assertIsInstance(model.provider.lease, Lease)
        self.assertEqual(info["custody"]["lease_id"], model.provider.lease.lease_id)
        self.assertEqual(len(self.register.leases), 1)
        release()
        self.assertEqual(self.register.leases, {})
        self.assertTrue(any("released" in data.get("message", "") for kind, data in events if kind == "log"))

    def test_a_model_test_probe_holds_a_lease_or_says_why_not(self) -> None:
        with MockLLM([{"text": "OK"}, {"text": "", "tool_calls": [{"name": "get_time", "arguments": {}}]}]) as llm:
            raw = {"providers": {"gpu": {"type": "openai", "base_url": llm.base_url, "api_key_env": None,
                                         "lazy_load": True, "custody": {"url": self.register.url, "timeout": 3}}},
                   "models": {"local": {"provider": "gpu", "model": "flash", "stream": False}}}
            config = Config(Path(tempfile.mkdtemp()) / "config.json", raw)
            result = settings.test_model(config, "local")
            self.assertTrue(result["ok"], result)
            purposes = [body.get("purpose") for _, path, _, body in self.register.requests if path == "/leases"]
            self.assertEqual(purposes, ["model test local"])
            self.assertEqual(self.register.leases, {})
            self.register.maintenance = True
            result = settings.test_model(config, "local")
            self.assertFalse(result["ok"])
            self.assertIn("GPU custody: MAINTENANCE", result["error"])


class SurfaceTest(RegisterTestCase):
    def test_the_factory_view_and_the_doctor(self) -> None:
        from swfactory import doctor
        from swfactory.events import EventBus
        from swfactory.orchestrator import Factory

        self.register.load("gpu", "flash")
        tmp = Path(tempfile.mkdtemp())
        config = Config(tmp / "config.json", {"data_dir": str(tmp / "data"), "providers": {
            "gpu": {"type": "openai", "base_url": "http://127.0.0.1:59004/v1", "api_key_env": None, "lazy_load": True,
                    "custody": {"url": self.register.url, "timeout": 3}},
            "idle": {"type": "openai", "base_url": "http://127.0.0.1:59005/v1", "api_key_env": None,
                     "custody": {"url": self.register.url, "timeout": 3}},
            "gone": {"type": "openai", "base_url": "http://127.0.0.1:59006/v1", "api_key_env": None, "lazy_load": True,
                     "custody": {"url": "http://127.0.0.1:9", "timeout": 1}}}})
        view = Factory(config, EventBus()).custody(refresh=True)
        rows = {row["provider"]: row for row in view["providers"]}
        self.assertEqual(sorted(rows), ["gone", "gpu", "idle"])
        self.assertEqual((rows["gpu"]["needs_lease"], rows["idle"]["needs_lease"]), (True, False))
        self.assertEqual(rows["gpu"]["register"]["residency"]["resident"][0]["model"], "flash")
        self.assertIsNone(rows["gone"]["register"]["residency"])
        checks = {name: doctor._custody_check(config, name)[0] for name in ("gpu", "idle", "gone")}
        self.assertEqual((checks["gpu"]["ok"], checks["gpu"]["level"]), (True, "info"))
        self.assertIn("generation", checks["gpu"]["detail"])
        self.assertEqual(checks["idle"]["level"], "warn")
        self.assertEqual((checks["gone"]["ok"], checks["gone"]["level"]), (False, "error"))
        self.assertIn("follows its overflow", checks["gone"]["detail"])


class WakeCustodyTest(RegisterTestCase):
    def test_a_wake_turn_holds_a_lease_and_is_deferred_without_one(self) -> None:
        from tests.test_wakes import NONE
        from tests.wake_helpers import WakeTestCase

        case = WakeTestCase("setUp")
        case.setUp()
        try:
            with MockLLM([NONE, NONE]) as llm:
                raw = {"providers": {"local": {"type": "openai", "base_url": llm.base_url, "api_key_env": None,
                                               "lazy_load": True,
                                               "custody": {"url": self.register.url, "timeout": 3}}}}
                manager = case.make(llm.base_url, raw=raw)
                manager.request("seraphina", "poke", reason="look around", by="spool")
                case.run_tick()
                self.assertEqual(len(llm.requests), 1, "the turn ran under its lease")
                purposes = [body.get("purpose") for _, path, _, body in self.register.requests if path == "/leases"]
                self.assertTrue(purposes and purposes[0].startswith("wake seraphina"), purposes)
                self.assertEqual(self.register.leases, {}, "given back when the turn ended")
                log = manager.store.run_log("seraphina", "01-wake")
                self.assertEqual([row["state"] for row in log if row.get("type") == "custody"], ["released"])
                self.register.maintenance = True
                case.time.advance(3600)
                manager.request("seraphina", "poke", reason="again", by="spool")
                case.run_tick()
                self.assertEqual(len(llm.requests), 1, "no lease, no call")
                [pending] = manager.state()["queue"]
                self.assertEqual((pending["state"], pending["gate_words"]),
                                 ("pending", "Not now: the GPU custody register gave no lease."))
        finally:
            case.doCleanups()


# ---------------------------------------------------------------- the review round


class StreamCutUnderLeaseTest(RegisterTestCase):
    def test_a_stream_cut_off_mid_answer_under_a_lease_is_never_replayed(self) -> None:
        """A server that streams part of an answer and then drops the connection (a lazy-load server that died
        loading): one request only, residency read again, the call not retried."""
        self.install(self.client())
        errors: list[dict] = []
        with MockLLM([{"text": "half an answer", "truncate": True}, {"text": "second answer"}]) as llm:
            cfg = provider_cfg(llm.base_url, self.register, lazy_load=True)
            model = ModelConfig(key="local", provider=cfg, model="flash", max_tokens=50, stream=True)
            with custody.leased(model, "test") as leased_model:
                before = len(self.register.requests)
                provider = make_provider(leased_model.provider)
                provider._sleep = lambda seconds: None
                with self.assertRaisesRegex(ProviderError, "ended before the model finished; GPU custody: the "
                                                           "outcome is unknown, so the request was not replayed"):
                    provider.complete(leased_model, "", [{"role": "user", "content": "x"}], [], on_error=errors.append)
                self.assertEqual(len(llm.requests), 1, "not replayed to a server that may load the model again")
                self.assertEqual(self.register.ops()[before:], ["residency"])
        self.assertEqual([(e["connection"], e["will_retry"], e["http_status"]) for e in errors], [(True, False, 200)])
        self.assertEqual(self.register.ops()[-2:], ["release", "residency"])


class AnswerBoundsTest(RegisterTestCase):
    def test_an_until_that_cannot_be_a_time_is_outcome_unknown(self) -> None:
        for until in (1e20, float("inf"), float("nan"), -5, "99999-01-01T00:00:00Z"):
            with self.subTest(until=until), self.assertRaises(RegisterError) as caught:
                parse_answer({"granted": True, "lease_id": "l-1", "until": until, "generation": 2})
            self.assertEqual((caught.exception.code, caught.exception.sent), ("OUTCOME_UNKNOWN", True))
        self.assertEqual(parse_answer({"granted": True, "lease_id": "l-1", "until": "9999-12-31T23:59:59Z",
                                       "generation": 2}).until, 253402300799.0)

        def send(method: str, url: str, **kwargs: Any) -> Reply:   # a register whose grant says until 1e20
            payload = ({"granted": True, "lease_id": "l-1", "until": 1e20, "generation": 2} if url.endswith("/leases")
                       else {"resident": [], "generation": 1})
            sent.append(url.rsplit("/", 1)[-1])
            return Reply(200, body=json.dumps(payload).encode())

        sent: list[str] = []
        client = CustodyClient(CustodySettings(url="http://127.0.0.1:1/c"),
                               adapter=RegisterAdapter(CustodySettings(url="http://127.0.0.1:1/c"), send=send))
        with self.assertRaises(CustodyDenied) as caught:
            client.claim("gpu", "flash", "job", "me")
        self.assertEqual(caught.exception.code, "OUTCOME_UNKNOWN")
        self.assertEqual(sent, ["residency", "leases", "residency"], "residency read again, nothing replayed")


class FenceTest(RegisterTestCase):
    def test_a_register_that_restarts_with_a_new_epoch_starts_the_fence_over(self) -> None:
        self.register.epoch = "boot-1"
        self.register.generation = 500
        client = self.client()
        held = client.claim("gpu", "flash", "job a", "me")
        self.register.restart("boot-2")                   # it forgot its leases and counts from 1 again
        lease = client.claim("gpu-b", "flash", "job b", "me")
        self.assertIsNone(lease.problem())
        self.assertEqual((client.epoch, client.generation, client.fenced), ("boot-2", self.register.generation, None))
        self.assertEqual(held.state, "lost", "the earlier epoch's lease is gone at the register")
        self.assertIn("the register restarted (epoch boot-1 became boot-2)", held.problem())
        self.assertIn("epoch", [entry["op"] for entry in client.history])

    def test_without_an_epoch_a_generation_that_went_back_is_reported_not_silent(self) -> None:
        from swfactory import doctor

        self.register.generation = 500
        client = self.install(self.client())
        client.observe()
        self.register.restart()                           # no epoch: the client cannot tell a restart from a replay
        with self.assertRaises(CustodyDenied) as caught:
            client.claim("gpu", "flash", "job", "me")
        self.assertEqual(caught.exception.code, "STALE")
        fenced = client.view()["fenced"]
        self.assertEqual((fenced["seen"], fenced["answered"]), (500, 1))
        config = Config(Path(tempfile.mkdtemp()) / "config.json", {"providers": {"gpu": {
            "type": "openai", "base_url": "http://127.0.0.1:59010/v1", "api_key_env": None, "lazy_load": True,
            "custody": {"url": self.register.url, "timeout": 3}}}})
        [check] = doctor._custody_check(config, "gpu")
        self.assertEqual((check["ok"], check["level"]), (False, "error"))
        self.assertIn("generation went back from 500 to 1", check["detail"])
        self.assertIn("send an epoch", check["detail"])
        self.register.generation = 501                    # past the highest one seen: believed again
        self.assertIsNotNone(client.reread())
        self.assertIsNone(client.view()["fenced"])


class OneServerManyEntriesTest(RegisterTestCase):
    def test_two_provider_entries_for_one_server_share_one_lease(self) -> None:
        self.install(CustodyClient(CustodySettings(url=self.register.url, timeout=3.0, provider="gpu0")))
        settings = CustodySettings(url=self.register.url, timeout=3.0, provider="gpu0")
        tabby = ProviderConfig(name="tabby", type="openai", base_url="http://127.0.0.1:59011/v1", api_key_env=None,
                               custody=settings, lazy_load=True)
        longer = ProviderConfig(name="tabby-long", type="openai", base_url="http://127.0.0.1:59011/v1",
                                api_key_env=None, timeout=1800, custody=settings, lazy_load=True)
        run1, run2 = Claims("job demo/001 builder"), Claims("chat c-1")
        self.assertEqual(run1(tabby, "flash"), (True, ""))
        first = run1.attach(model_cfg(tabby))
        self.assertEqual(run2(longer, "flash"), (True, ""))
        second = run2.attach(model_cfg(longer))
        self.assertIs(second.provider.lease, first.provider.lease)
        self.assertIsNone(lease_problem(second.provider, "flash"))
        self.assertIsNone(lease_problem(first.provider, "flash"), "the first run's lease was not given back")
        self.assertEqual(run2.info()["custody"]["provider"], "tabby-long", "recorded as the run uses it")
        self.assertEqual((len(self.register.leases), first.provider.lease.users), (1, 2))
        run1.release("done")
        self.assertEqual(len(self.register.leases), 1, "still used by the second run")
        run2.release("done")
        self.assertEqual(self.register.leases, {})

    def test_a_lease_that_lapsed_before_the_run_started_says_so(self) -> None:
        self.install(self.client())
        cfg = provider_cfg("http://127.0.0.1:59012/v1", self.register, lazy_load=True)
        claims = Claims("job demo/002 builder", keep=False)
        self.assertEqual(claims(cfg, "flash"), (True, ""))
        claims.leases[0].state, claims.leases[0].error = "lost", "renew refused: LEASED"
        placed = claims.attach(model_cfg(cfg))
        self.assertIsInstance(placed.provider.lease, Refusal)
        problem = lease_problem(placed.provider, "flash")
        self.assertIn("the lease taken for gpu/flash at placement is no longer valid: the lease was lost", problem)
        self.assertNotIn("no lease was asked", problem)

    def test_custody_follows_the_server_not_the_provider_name(self) -> None:
        from swfactory import doctor
        from swfactory.events import EventBus
        from swfactory.orchestrator import Factory

        self.assertEqual(server_of("http://localhost:5000/v1/"), server_of("http://127.0.0.1:5000"))
        self.assertNotEqual(server_of("http://127.0.0.1:5000/tabby/v1"), server_of("http://127.0.0.1:5000/llama/v1"))
        with MockLLM([{"text": "loaded and answered"}]) as llm:
            tmp = Path(tempfile.mkdtemp())
            config = Config(tmp / "config.json", {"data_dir": str(tmp / "data"), "providers": {
                "gpu0": {"type": "openai", "base_url": llm.base_url, "api_key_env": None, "lazy_load": True,
                         "custody": {"url": self.register.url, "timeout": 3}},
                "gpu0-long": {"type": "openai", "base_url": llm.base_url + "/", "api_key_env": None, "timeout": 1800}},
                "models": {"local": {"provider": "gpu0", "model": "flash"}}})
            settings.validate_settings(config)
            sibling = config.provider("gpu0-long")
            self.assertTrue(needs_lease(sibling))
            self.assertEqual(sibling.custody.provider, "gpu0", "the register's name for the server")
            factory = Factory(config, EventBus())
            old = factory._provider_from_entry("local", {"type": "openai", "base_url": llm.base_url,
                                                         "api_key_env": None, "timeout": 900})
            self.assertTrue(needs_lease(old), "an older snapshot of a renamed provider is not outside custody")
            for cfg in (sibling, old):
                with self.assertRaisesRegex(ProviderError, "holds no lease"):
                    make_provider(cfg).complete(model_cfg(cfg), "", [{"role": "user", "content": "x"}], [])
            self.assertEqual(llm.requests, [], "nothing reached the lazy-load server without a lease")
            self.install(self.client())
            run1, run2 = Claims("job a"), Claims("job b")
            self.assertEqual((run1(config.provider("gpu0"), "flash"), run2(sibling, "flash")), ((True, ""), (True, "")))
            self.assertEqual(self.register.ops().count("acquire"), 1, "one server, one lease")
            run1.release()
            run2.release()
            rows = {row["provider"]: row for row in factory.custody()["providers"]}
            self.assertEqual((rows["gpu0"]["custody_from"], rows["gpu0-long"]["custody_from"]), (None, "gpu0"))
            names = [name for name, _ in doctor._custody_jobs(config)]
            self.assertEqual(names, ["custody gpu0", "custody gpu0-long"])
            self.assertIn("it follows gpu0's custody", doctor._custody_check(config, "gpu0-long")[0]["detail"])
            config.raw["providers"]["gpu0-long"]["custody"] = {"url": self.register.url, "provider": "other"}
            with self.assertRaisesRegex(ValueError, "are the same server .* different custody register"):
                settings.validate_settings(config)
            config.raw["providers"]["gpu0-long"]["custody"] = {"url": self.register.url, "provider": "gpu0"}
            settings.validate_settings(config)   # the same register and name: one server, as it should be

    def test_the_cli_says_which_entry_a_provider_follows_and_when_the_register_is_fenced(self) -> None:
        import argparse
        import io
        from contextlib import redirect_stdout

        from swfactory import __main__ as cli

        view = {"providers": [{"provider": "gpu0-long", "needs_lease": True, "custody_from": "gpu0", "register": {
            "generation": 500, "fenced": {"seen": 500, "answered": 1, "since": "2026-10-22T09:00:00.000Z"},
            "residency": None, "leases": []}}]}
        client = mock.Mock()
        client.return_value.get.return_value = view
        out = io.StringIO()
        with mock.patch.object(cli, "Client", client), mock.patch.object(cli, "load_config"), redirect_stdout(out):
            self.assertEqual(cli.cmd_custody(argparse.Namespace(refresh=False, json=False)), 0)
        self.assertIn("(follows gpu0: the same server)", out.getvalue())
        self.assertIn("the register's generation went back from 500 to 1", out.getvalue())
        self.assertIn("HELD (the card is held by someone else): no wait;", out.getvalue())

    def test_the_cli_says_what_held_and_full_do_and_the_last_refusals_in_words(self) -> None:
        import argparse
        import io
        from contextlib import redirect_stdout

        from swfactory import __main__ as cli

        history = [{"at": "2026-10-22T09:00:00.000Z", "op": "claim", "outcome": "denied", "reason": "FULL",
                    "words": "the card cannot fit this model now", "model": "big", "purpose": "job demo/001 builder"},
                   {"at": "2026-10-22T09:01:00.000Z", "op": "claim", "outcome": "granted", "model": "small"}]
        view = {"providers": [{"provider": "gpu0", "needs_lease": True, "held_wait_seconds": 120, "register": {
            "generation": 7, "residency": None, "leases": [], "history": history}}]}
        client = mock.Mock()
        client.return_value.get.return_value = view
        out = io.StringIO()
        with mock.patch.object(cli, "Client", client), mock.patch.object(cli, "load_config"), redirect_stdout(out):
            self.assertEqual(cli.cmd_custody(argparse.Namespace(refresh=False, json=False)), 0)
        self.assertIn("HELD (the card is held by someone else): another free card at once, else waits up to 120s; "
                      "FULL (the card cannot fit the model now): the next build at once", out.getvalue())
        self.assertIn("refused 2026-10-22T09:00:00.000Z  big  FULL (the card cannot fit this model now)  "
                      "(job demo/001 builder)", out.getvalue())
        self.assertNotIn("small", out.getvalue(), "only refusals are listed")

    def test_custody_ca_file_is_a_setting_and_part_of_the_register_key(self) -> None:
        parsed = parse_settings({"url": "https://127.0.0.1:8444", "ca_file": "/etc/register-ca.pem"}, "c")
        self.assertEqual(parsed.to_dict()["ca_file"], "/etc/register-ca.pem")
        self.assertNotEqual(parsed.key(), parse_settings({"url": "https://127.0.0.1:8444"}, "c").key())
        with self.assertRaisesRegex(ValueError, "ca_file"):
            parse_settings({"url": "https://127.0.0.1:8444", "ca_file": ""}, "c")
        with self.assertRaises(RegisterError) as caught:   # a CA file that cannot be loaded: not reached, not sent
            RegisterAdapter(parse_settings({"url": "https://127.0.0.1:9", "ca_file": "/nonexistent/ca.pem"},
                                           "c")).residency()
        self.assertEqual((caught.exception.code, caught.exception.sent), ("UNREACHABLE", False))
        self.assertIn("ca_file", caught.exception.detail)


class SetupFailureTest(RegisterTestCase):
    def test_a_wake_turn_whose_setup_fails_gives_its_lease_back(self) -> None:
        from tests.test_wakes import NONE
        from tests.wake_helpers import WakeTestCase

        case = WakeTestCase("setUp")
        case.setUp()
        try:
            with MockLLM([NONE]) as llm:
                raw = {"providers": {"local": {"type": "openai", "base_url": llm.base_url, "api_key_env": None,
                                               "lazy_load": True, "custody": {"url": self.register.url, "timeout": 3}}}}
                manager = case.make(llm.base_url, raw=raw)

                def broken(cfg: Any) -> Any:
                    raise ValueError("cannot build the provider client")

                manager.provider_factory = broken
                manager.request("seraphina", "poke", reason="look around", by="spool")
                case.run_tick()
                self.assertIn("acquire", self.register.ops(), "the lease was granted before the setup failed")
                self.assertEqual(self.register.leases, {}, "and given back")
                self.assertEqual(self.register.ops()[-2:], ["release", "residency"])
                self.assertFalse([lease for client in custody.known_clients() for lease in client.leases.values()])
        finally:
            case.doCleanups()

    def test_a_live_placement_that_fails_after_its_slot_wait_gives_back_the_lease(self) -> None:
        config = Config(Path(tempfile.mkdtemp()) / "config.json", {
            "providers": {"gpu": {"type": "openai", "base_url": "http://127.0.0.1:59013/v1", "api_key_env": None,
                                  "lazy_load": True, "custody": {"url": self.register.url, "timeout": 3}}},
            "models": {"local": {"provider": "gpu", "model": "flash"}}})
        with mock.patch("swfactory.chats.placement_reason", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                place_run(config, Route("builtin", "local", {}, None), FakeReach(), threading.Event(),
                          lambda kind, data: None, "chat c-0123456789")
        self.assertIn("acquire", self.register.ops())
        self.assertEqual(self.register.leases, {})

    def test_a_job_placement_that_fails_gives_back_any_lease_it_took(self) -> None:
        from swfactory.events import EventBus
        from swfactory.orchestrator import Factory

        tmp = Path(tempfile.mkdtemp())
        config = Config(tmp / "config.json", {"data_dir": str(tmp / "data"), "providers": {
            "gpu": {"type": "openai", "base_url": "http://127.0.0.1:59014/v1", "api_key_env": None,
                    "lazy_load": True, "custody": {"url": self.register.url, "timeout": 3}}}})
        factory = Factory(config, EventBus())

        def failing(job: Any, agent: str, route: dict, round_: Any, claims: Claims) -> Any:
            self.assertEqual(claims(config.provider("gpu"), "flash"), (True, ""))
            raise RuntimeError("the placement broke after its claim")

        with mock.patch.object(factory, "_place_with", failing), self.assertRaisesRegex(RuntimeError, "broke"):
            factory._place(None, "builder", {}, purpose="job demo/003 builder")
        self.assertEqual(self.register.leases, {})


# ---------------------------------------------------------------- HELD waits, FULL does not (the operator's decision)


class CardIdentityTest(unittest.TestCase):
    """A card's index is not its identity: the order changed between two boots on the owner's server. Per-card
    residency fields are kept by GPU UUID only, and the same cards in another index order read the same."""

    A = "GPU-d1107200-1111-2222-3333-444455556666"
    B = "GPU-45071570-aaaa-bbbb-cccc-ddddeeeeffff"

    def residency(self, gpus: Any) -> custody.Residency:
        return custody.parse_residency({"generation": 3, "resident": [
            {"provider": "gpu0", "model": "flash", "vram_mib": 21840, "gpus": gpus}]})

    def test_cards_by_uuid_and_never_by_index(self) -> None:
        first, swapped = self.residency([self.A, self.B]), self.residency([self.B, self.A])
        self.assertEqual(first.view()["resident"], swapped.view()["resident"], "the index order changes nothing")
        self.assertEqual(first.view()["resident"][0]["gpus"], sorted([self.A, self.B]))
        self.assertNotIn("cards_note", first.view()["resident"][0])
        by_index = self.residency([0, "1", self.A]).view()["resident"][0]
        self.assertEqual(by_index["gpus"], [self.A], "an index names no card")
        self.assertIn("by its number, which can change between boots", by_index["cards_note"])
        plain = self.residency(None).view()["resident"][0]
        self.assertNotIn("gpus", plain, "a register that names no cards: the view is as before")
        self.assertEqual(self.residency(self.A).view()["resident"][0]["gpus"], [self.A], "one card, not in a list")
        self.assertEqual(first.on("gpu0"), [("gpu0", "flash")], "exact restoration still compares provider/model")


class CardIdentityOverTheWireTest(RegisterTestCase):
    """The same through a register: the cards listed in another index order after a reboot read the same, and a
    card named by its number is left out with a note."""

    def test_an_index_swapped_register_reads_the_same(self) -> None:
        client = RegisterAdapter(settings_for(self.register))
        a, b = CardIdentityTest.A, CardIdentityTest.B
        self.register.load("gpu0", "flash", gpus=[a, b])
        before = client.residency()
        self.register.load("gpu0", "flash", gpus=[b, a])   # the next boot lists the cards the other way round
        after = client.residency()
        self.assertEqual(before.view()["resident"][0]["gpus"], sorted([a, b]))
        self.assertEqual(before.view()["resident"][0]["gpus"], after.view()["resident"][0]["gpus"])
        self.assertTrue(custody.residency_matches(before, after, "gpu0"))
        self.register.load("gpu0", "flash", gpus=[1, a])
        by_index = client.residency().view()["resident"][0]
        self.assertEqual(by_index["gpus"], [a])
        self.assertIn("by its number", by_index["cards_note"])


class ReasonCodeTest(unittest.TestCase):
    def test_full_is_a_reason_code_and_unknown_codes_still_read_as_unknown(self) -> None:
        for sent, read in (("FULL", "FULL"), ("full", "FULL"), ("HELD", "HELD"), ("SLEEPING", "UNKNOWN"),
                           ("", "UNKNOWN"), (None, "UNKNOWN")):
            with self.subTest(sent=sent):
                self.assertEqual(parse_answer({"granted": False, "generation": 3, "reason_code": sent}).reason_code,
                                 read)
        self.assertIn("FULL", custody.REASON_CODES)
        self.assertEqual(custody.refusal_words("FULL"), "the card cannot fit this model now")
        self.assertEqual(custody.refusal_words("HELD"), "the card is held by someone else")
        self.assertEqual(custody.refusal_words("NOT_RESIDENT", "flash"),
                         "flash is not resident and may not be loaded now")
        self.assertEqual(custody.refusal_words("SLEEPING"), "refused")
        for code in (*custody.REASON_CODES, *custody.NO_ANSWER_CODES):
            self.assertNotIn(" (", custody.refusal_words(code), "an overflow's why is cut at its first bracket")


class HeldAndFullClaimTest(RegisterTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.install(self.client())
        patcher = mock.patch.object(custody, "HELD_RETRY_SECONDS", 0.05)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cfg(self, **extra: Any) -> ProviderConfig:
        return provider_cfg("http://127.0.0.1:59020/v1", self.register, lazy_load=True, **extra)

    def acquires(self) -> int:
        return self.register.ops().count("acquire")

    def test_held_waits_until_the_hold_ends(self) -> None:
        self.register.held["gpu"] = "operator"
        waits: list = []
        claims = Claims("job demo/010 builder", keep=False, cancel=threading.Event(),
                        on_wait=lambda *args: waits.append(args))
        threading.Timer(0.3, self.register.held.clear).start()
        self.assertEqual(claims(self.cfg(overflow_after_seconds=10), "flash"), (True, ""))
        self.assertEqual(len(claims.leases), 1)
        self.assertGreater(self.acquires(), 1, "it asked again while it waited")
        [(provider, seconds, denied)] = waits
        self.assertEqual((provider, seconds, denied.code), ("gpu", 10.0, "HELD"))
        self.assertIn("held by operator", denied.detail)
        notes = [(entry["op"], entry["outcome"]) for entry in custody.client_for(self.cfg().custody).history]
        self.assertEqual(notes.count(("claim", "denied")), 1, "a HELD asked again is not noted again")
        self.assertEqual([note for note in notes if note[0] == "wait"], [("wait", "waiting"), ("wait", "granted")])
        claims.release()

    def test_a_held_asked_again_in_a_wait_is_answered_in_the_journal_and_never_reads_as_unknown(self) -> None:
        """Each ask is journaled before it is sent (§40); an ask HELD again during a wait is not noted, so the
        journal answers it quietly. Without that, every ask but the last would read as "claim outcome unknown"."""
        data = Path(tempfile.mkdtemp(prefix="sf-custody-quiet-"))
        self.addCleanup(__import__("shutil").rmtree, data, True)
        custody.set_journal(data)
        self.addCleanup(custody.set_journal, None)
        self.register.held["gpu"] = "operator"
        claims = Claims("job demo/015 builder", keep=False, cancel=threading.Event())
        threading.Timer(0.3, self.register.held.clear).start()
        self.assertEqual(claims(self.cfg(overflow_after_seconds=10), "flash"), (True, ""))
        claims.release()
        lines = [json.loads(line) for path in sorted((data / "custody").glob("journal-*.jsonl"))
                 for line in path.read_text().splitlines()]
        claim_lines = [line for line in lines if line["op"] == "claim"]
        quiet = [line for line in claim_lines if line.get("quiet")]
        self.assertGreaterEqual(len(quiet), 1, "it asked again while it waited")
        self.assertEqual({(line["outcome"], line["reason"]) for line in quiet}, {("denied", "HELD")})
        requested = sum(1 for line in claim_lines if line["outcome"] == "requested")
        self.assertEqual(requested, len(claim_lines) - requested, "every request line has its answer")
        kinds = [entry["kind"] for entry in activity._custody_entries(data, "ops", True, None, None)]
        self.assertNotIn("claim outcome unknown", kinds)
        self.assertEqual(kinds.count("lease denied"), 1, "the quiet answers are not entries")
        self.assertEqual([kind for kind in kinds if kind.startswith("lease wait")], ["lease wait", "lease wait ended"])
        self.assertIn("lease granted", kinds)

    def test_held_waits_at_most_overflow_after_seconds(self) -> None:
        self.register.held["gpu"] = "operator"
        claims = Claims("job demo/011 builder", keep=False, cancel=threading.Event())
        started = time.monotonic()
        granted, why = claims(self.cfg(overflow_after_seconds=0.4), "flash")
        self.assertFalse(granted)
        self.assertGreaterEqual(time.monotonic() - started, 0.35)
        self.assertIn("HELD", why)
        self.assertIn("waited 0.4s (overflow_after_seconds)", why)
        self.assertEqual(claims.info()["custody_refused"],
                         [{"provider": "gpu", "model": "flash", "code": "HELD",
                           "words": "the card is held by someone else"}])

    def test_held_does_not_wait_without_overflow_after_seconds_or_a_cancel_event(self) -> None:
        self.register.held["gpu"] = "operator"
        for claims, cfg in ((Claims("a", keep=False, cancel=threading.Event()), self.cfg()),
                            (Claims("b", keep=False), self.cfg(overflow_after_seconds=30))):
            before = self.acquires()
            started = time.monotonic()
            self.assertFalse(claims(cfg, "flash")[0])
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertEqual(self.acquires() - before, 1, "asked once")
            self.assertFalse(claims.waited)

    def test_full_never_waits(self) -> None:
        self.register.full.add(("gpu", "flash"))
        claims = Claims("job demo/012 builder", keep=False, cancel=threading.Event())
        started = time.monotonic()
        granted, why = claims(self.cfg(overflow_after_seconds=30), "flash")
        self.assertFalse(granted)
        self.assertLess(time.monotonic() - started, 2.0, "FULL does not wait")
        self.assertEqual(self.acquires(), 1)
        self.assertIn("FULL: no lease for flash on gpu: the card cannot fit this model now", why)
        self.assertEqual(claims.common_refusal([("gpu", "flash")]), "the card cannot fit this model now")
        history = custody.client_for(self.cfg().custody).history
        self.assertEqual((history[-1]["reason"], history[-1]["words"]), ("FULL", "the card cannot fit this model now"))

    def test_a_held_wait_ends_at_once_when_the_answer_turns_full(self) -> None:
        self.register.held["gpu"] = "operator"

        def turn_full() -> None:
            self.register.held.clear()
            self.register.full.add(("gpu", "flash"))

        threading.Timer(0.2, turn_full).start()
        claims = Claims("job demo/013 builder", keep=False, cancel=threading.Event())
        started = time.monotonic()
        granted, why = claims(self.cfg(overflow_after_seconds=30), "flash")
        self.assertFalse(granted)
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertIn("FULL", why)

    def test_a_cancel_ends_the_wait_and_nothing_more_is_asked(self) -> None:
        self.register.held["gpu"] = "operator"
        cancel = threading.Event()
        claims = Claims("job demo/014 builder", keep=False, cancel=cancel)
        threading.Timer(0.2, cancel.set).start()
        granted, why = claims(self.cfg(overflow_after_seconds=30), "flash")
        self.assertFalse(granted)
        self.assertIn("cancelled while it waited", why)
        before = len(self.register.requests)
        self.assertEqual(claims(self.cfg(overflow_after_seconds=30), "other"), (False, "the run was cancelled"))
        self.assertEqual(len(self.register.requests), before, "a cancelled run asks the register nothing more")

    def test_one_placement_waits_once_however_many_builds_answer_held(self) -> None:
        """Three cards HELD (two builds and the overflow), overflow_after_seconds 0.5 each: the placement waits
        about 0.5 s in all, not 0.5 s per build."""
        cards = [provider_cfg(f"http://127.0.0.1:5910{i}/v1", self.register, name=f"card{i}", lazy_load=True,
                              overflow_after_seconds=0.5) for i in range(3)]
        for card in cards:
            self.register.held[card.name] = "operator"
        claims = Claims("job demo/015 builder", keep=False, cancel=threading.Event())
        started = time.monotonic()
        choice = routing.choose([Build(cards[0], "big"), Build(cards[1], "big")], [Build(cards[2], "small")],
                                FakeReach(), "small", claims)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.9, "one wait per placement")
        self.assertGreaterEqual(elapsed, 0.45)
        self.assertFalse(choice.overflowed)
        notes = " ".join(choice.notes)
        self.assertIn("waited 0.5s (overflow_after_seconds)", notes)
        self.assertEqual(notes.count("no wait left"), 2, notes)
        self.assertEqual([entry["outcome"] for entry in custody.client_for(cards[0].custody).history
                          if entry["op"] == "wait"], ["waiting", "gave_up"])

    def test_a_later_held_answer_waits_only_for_what_is_left(self) -> None:
        first, second = (provider_cfg(f"http://127.0.0.1:5911{i}/v1", self.register, name=f"slot{i}", lazy_load=True,
                                      overflow_after_seconds=seconds) for i, seconds in ((0, 1.5), (1, 30)))
        self.register.held.update({first.name: "operator", second.name: "operator"})

        def first_turns_full() -> None:   # the first card's wait ends early, with time left in the placement's wait
            del self.register.held[first.name]
            self.register.full.add((first.name, "flash"))

        threading.Timer(0.2, first_turns_full).start()
        waits: list = []
        claims = Claims("job demo/016 builder", keep=False, cancel=threading.Event(),
                        on_wait=lambda *args: waits.append(args[:2]))
        self.assertTrue(claims(first, "flash")[1].startswith("FULL"))
        threading.Timer(0.1, self.register.held.clear).start()
        started = time.monotonic()
        self.assertEqual(claims(second, "flash"), (True, ""), "it still waits for the time left, and gets the card")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(waits[0], ("slot0", 1.5))
        self.assertEqual(waits[1][0], "slot1")
        self.assertLessEqual(waits[1][1], 1.4, "what was left of the placement's one wait, not slot1's 30 s")
        self.assertGreater(waits[1][1], 0)
        claims.release()

    def test_after_a_slot_wait_that_ran_out_a_held_answer_does_not_wait(self) -> None:
        self.register.held["gpu"] = "operator"
        claims = Claims("job demo/017 builder", keep=False, cancel=threading.Event())
        claims.stop_waiting("waited 120s for a slot of tabby")
        started = time.monotonic()
        granted, why = claims(self.cfg(overflow_after_seconds=30), "flash")
        self.assertFalse(granted)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertIn("no wait left (the run waited 120s for a slot of tabby already", why)
        self.assertFalse(claims.waited)

    def test_another_lease_on_the_card_ends_a_held_wait_at_once(self) -> None:
        """Two runs of this harness wait out one HELD card for different models, and the hold ends: one gets the
        card, and the other's wait ends at once with LEASED (another lease covers the card), which does not wait.
        Whether LEASED should wait like HELD is the operator's to decide (DESIGN §37, Open)."""
        cfg = self.cfg(overflow_after_seconds=30)
        self.register.held["gpu"] = "operator"
        results: dict[str, tuple] = {}

        def run(name: str, model: str) -> None:
            claims = Claims(f"job demo/{name} builder", keep=False, cancel=threading.Event())
            started = time.monotonic()
            granted, why = claims(cfg, model)
            results[name] = (granted, time.monotonic() - started, why)
            self.addCleanup(claims.release)

        threads = [threading.Thread(target=run, args=("a", "flash")), threading.Thread(target=run, args=("b", "pro"))]
        for thread in threads:
            thread.start()
        time.sleep(0.3)
        self.register.held.clear()
        for thread in threads:
            thread.join(20)
        outcomes = sorted(results.values(), key=lambda item: not item[0])
        self.assertEqual([granted for granted, _, _ in outcomes], [True, False], results)
        self.assertLess(outcomes[1][1], 10.0, "the other run went on at once, long before 30 s")
        self.assertTrue(outcomes[1][2].startswith("LEASED: "), outcomes[1][2])


class NeverWaits(threading.Event):
    """A cancel event that fails the test if anything waits on it: a placement that never sleeps."""

    def wait(self, timeout: float | None = None) -> bool:
        raise AssertionError(f"the placement waited ({timeout}s)")


class HeldWhileAnotherCardIsFreeTest(RegisterTestCase):
    """The operator (26 Oct): HELD while another card is free does not wait, the run goes to the free card at
    once; only when no other build can take the run now is the HELD answer waited out, as before. LEASED is
    unchanged: never waited for."""

    def setUp(self) -> None:
        super().setUp()
        self.install(self.client())
        patcher = mock.patch.object(custody, "HELD_RETRY_SECONDS", 0.05)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cards = [provider_cfg(f"http://127.0.0.1:5912{i}/v1", self.register, name=f"card{i}", lazy_load=True,
                                   overflow_after_seconds=600) for i in range(2)]
        self.builds = [Build(card, "flash") for card in self.cards]

    def asked(self) -> list[str]:
        return [body["provider"] for _, path, _, body in self.register.requests if path == "/leases"]

    def test_held_on_the_first_card_with_the_second_free_goes_there_without_waiting(self) -> None:
        self.register.held["card0"] = "operator"
        claims = Claims("job demo/020 builder", keep=False, cancel=NeverWaits())
        index, _, notes = routing.choose_build(self.builds, FakeReach(), claims)
        self.assertEqual(index, 1)
        self.assertEqual(self.asked(), ["card0", "card1"], "card0 asked once, then card1 at once")
        self.assertFalse(claims.waited)
        self.assertEqual(notes, ["build card0/flash is unavailable: GPU custody: HELD: no lease for flash on card0: "
                                 "the card is held by someone else (held by operator)"])
        self.assertEqual(claims.info()["custody_refused"][0]["code"], "HELD")
        self.assertNotIn("wait", [entry["op"] for entry in custody.client_for(self.cards[0].custody).history])
        claims.release()

    def test_held_on_every_card_waits_for_the_first_as_before(self) -> None:
        self.register.held.update({"card0": "operator", "card1": "operator"})
        threading.Timer(0.3, lambda: self.register.held.pop("card0", None)).start()
        waits: list = []
        claims = Claims("job demo/021 builder", keep=False, cancel=threading.Event(),
                        on_wait=lambda *args: waits.append(args[:2]))
        index, _, notes = routing.choose_build(self.builds, FakeReach(), claims)
        self.assertEqual(index, 0, "the first card, waited for")
        self.assertEqual(waits, [("card0", 600.0)])
        asked = self.asked()
        self.assertEqual(asked[:2], ["card0", "card1"], "both asked before the wait")
        self.assertEqual(set(asked[2:]), {"card0"}, "the wait asks the first card again")
        self.assertEqual(len(notes), 1)
        self.assertIn("card1/flash is unavailable: GPU custody: HELD", notes[0])
        claims.release()

    def test_held_with_the_other_card_full_or_leased_still_waits(self) -> None:
        for other in ("FULL", "LEASED"):
            with self.subTest(other=other):
                self.register.held = {"card0": "operator"}
                self.register.full, self.register.leases = set(), {}
                if other == "FULL":
                    self.register.full.add(("card1", "flash"))
                else:
                    self.register.leases["x"] = {"id": "x", "provider": "card1", "model": "pro", "holder": "another",
                                                 "purpose": "job", "until": self.register.now + 3600}
                threading.Timer(0.3, self.register.held.clear).start()
                claims = Claims("job demo/022 builder", keep=False, cancel=threading.Event())
                index, _, notes = routing.choose_build(self.builds, FakeReach(), claims)
                self.assertEqual(index, 0, "no other card could take the run now: card0 was waited for")
                self.assertTrue(claims.waited)
                self.assertIn(f"GPU custody: {other}", notes[0])
                claims.release()

    def test_leased_is_unchanged_it_never_waits(self) -> None:
        for card in self.cards:
            self.register.leases[card.name] = {"id": card.name, "provider": card.name, "model": "pro",
                                               "holder": "another", "purpose": "job", "until": self.register.now + 3600}
        claims = Claims("job demo/023 builder", keep=False, cancel=NeverWaits())
        index, _, notes = routing.choose_build(self.builds, FakeReach(), claims)
        self.assertIsNone(index)
        self.assertEqual(self.asked(), ["card0", "card1"], "each asked once")
        self.assertTrue(all("GPU custody: LEASED" in note for note in notes), notes)
        del self.register.leases["card1"]
        claims = Claims("job demo/024 builder", keep=False, cancel=NeverWaits())
        self.assertEqual(routing.choose_build(self.builds, FakeReach(), claims)[0], 1, "LEASED: the next card")
        self.assertFalse(claims.waited)
        claims.release()

    def test_without_a_cancel_event_a_held_card_is_passed_over_at_once(self) -> None:
        """A placement that cannot wait (a wake turn, a Test probe) puts nothing off: HELD is refused at once."""
        self.register.held.update({"card0": "operator", "card1": "operator"})
        claims = Claims("wake kate W-1", keep=False)
        self.assertEqual(routing.choose_build(self.builds, FakeReach(), claims)[0], None)
        self.assertEqual(self.asked(), ["card0", "card1"])
        self.assertEqual(claims.put_off, {})


class HeldAndFullPlacementTest(PlacementTestCase):
    """The operator's decisions on a job's run: HELD while another card is free goes to the free card at once;
    HELD with no other card free waits (overflow_after_seconds); FULL goes on at once to the next build (another
    card, or a smaller build), then the overflow."""

    def setUp(self) -> None:
        super().setUp()
        self.register = MockRegister()
        self.register.start()
        self.addCleanup(self.register.stop)
        patcher = mock.patch.object(custody, "HELD_RETRY_SECONDS", 0.05)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tabby, self.strata = f"tabby{self.suffix}", f"strata{self.suffix}"

    custody_config = CustodyPlacementTest.custody_config

    def both_config(self, seconds: float | None = None) -> Config:
        extra = {"overflow_after_seconds": seconds} if seconds else {}
        under = {"lazy_load": True, "custody": {"url": self.register.url, "timeout": 3}, **extra}
        return self.placed_config(providers={self.tabby: under, self.strata: dict(under)})

    def acquires(self, provider: str) -> int:
        return sum(1 for _, path, _, body in self.register.requests if path == "/leases"
                   and body.get("provider") == provider)

    def test_held_on_every_card_waits_and_the_run_goes_to_the_first_card_it_waited_for(self) -> None:
        self.register.held.update({self.tabby: "operator", self.strata: "operator"})
        threading.Timer(0.4, lambda: self.register.held.pop(self.tabby, None)).start()
        job = self.run_job(self.start_placed(self.both_config(seconds=20)))
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(self.recorder.of("builder")[0].ctx.model.provider.name, self.tabby, "it waited for tabby")
        waiting = [e for e in self.events(job, "agent.waiting") if e.agent == "builder"]
        self.assertEqual((waiting[0].data["provider"], waiting[0].data["custody"],
                          waiting[0].data["overflow_after_seconds"]), (self.tabby, "HELD", 20.0))
        start = self.start_event(job, "builder")
        self.assertEqual(waiting[0].data["run"], start.data["run"], "the wait is the run's own event")
        self.assertIn(f"builder waits up to 20s for {self.tabby}: GPU custody: HELD", self.logs(job))
        self.assertGreater(self.acquires(self.tabby), 1)
        self.assertEqual(self.acquires(self.strata), 1, "strata was asked before the wait, and not waited for")
        self.assertEqual([(r["provider"], r["code"]) for r in start.data["custody_refused"]], [(self.strata, "HELD")],
                         "the build it ran on was granted in the end")

    def test_held_with_another_card_free_goes_to_the_free_card_at_once(self) -> None:
        """The operator (26 Oct): HELD while another card is free does not wait; the run goes to the free card at
        once, however long overflow_after_seconds is."""
        self.register.held[self.tabby] = "operator"
        job = self.run_job(self.start_placed(self.custody_config(overflow_after_seconds=600)))
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(self.recorder.of("builder")[0].ctx.model.provider.name, self.strata)
        self.assertEqual(self.acquires(self.tabby), 1, "asked once, not waited for")
        self.assertFalse([e for e in self.events(job, "agent.waiting") if e.data.get("custody")])
        self.assertNotIn("waits up to", self.logs(job))
        self.assertIn(f"build {self.tabby}/flash-exl3 is unavailable: GPU custody: HELD: no lease for flash-exl3 on "
                      f"{self.tabby}: the card is held by someone else", self.logs(job))
        start = self.start_event(job, "builder")
        self.assertEqual([(r["provider"], r["code"]) for r in start.data["custody_refused"]], [(self.tabby, "HELD")])
        self.assertEqual(start.data["build"]["index"], 1)

    def test_held_on_every_card_past_overflow_after_seconds_waits_once_then_overflows(self) -> None:
        self.register.held.update({self.tabby: "operator", self.strata: "operator"})
        started = time.monotonic()
        job = self.run_job(self.start_placed(self.both_config(seconds=0.4)))
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertGreaterEqual(time.monotonic() - started, 0.35)
        ctx = self.recorder.of("builder")[0].ctx
        self.assertEqual((ctx.model.key, ctx.model.provider.name), ("cheap", "api"))
        self.assertIn("waited 0.4s (overflow_after_seconds)", self.logs(job))
        self.assertIn("no wait left (the run waited out a HELD answer for up to 0.4s already", self.logs(job))
        waiting = [e.data["provider"] for e in self.events(job, "agent.waiting") if e.data.get("custody")]
        self.assertEqual(waiting, [self.tabby], "one wait, for the first card")
        self.assertTrue(self.start_event(job, "builder").data["overflow"]["why"].startswith(
            "the card is held by someone else ("), "every card refused with HELD: the overflow says so")

    def test_full_skips_to_the_next_build_without_waiting(self) -> None:
        self.register.full.add((self.tabby, "flash-exl3"))
        started = time.monotonic()
        job = self.run_job(self.start_placed(self.custody_config(overflow_after_seconds=30)))
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertLess(time.monotonic() - started, 20.0)
        ctx = self.recorder.of("builder")[0].ctx
        self.assertEqual((ctx.model.provider.name, ctx.model.model), (self.strata, "flash-gguf"), "the smaller build")
        self.assertEqual(self.acquires(self.tabby), 1, "asked once: FULL does not wait")
        self.assertFalse([e for e in self.events(job, "agent.waiting") if e.data.get("custody")])
        start = self.start_event(job, "builder")
        self.assertEqual(start.data["custody_refused"],
                         [{"provider": self.tabby, "model": "flash-exl3", "code": "FULL",
                           "words": "the card cannot fit this model now"}])
        self.assertEqual(start.data["build"]["index"], 1)
        run = next(entry for entry in job["runs"] if entry["agent"] == "builder")
        self.assertEqual(run["custody_refused"][0]["code"], "FULL")
        self.assertIn(f"build {self.tabby}/flash-exl3 is unavailable: GPU custody: FULL: no lease for flash-exl3 on "
                      f"{self.tabby}: the card cannot fit this model now", self.logs(job))

    def test_full_on_the_last_build_overflows_and_the_reason_says_why(self) -> None:
        self.register.full.update({(self.tabby, "flash-exl3"), (self.strata, "flash-gguf")})
        job = self.run_job(self.start_placed(self.both_config(seconds=30)))
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        ctx = self.recorder.of("builder")[0].ctx
        self.assertEqual((ctx.model.key, ctx.model.provider.name), ("cheap", "api"))
        start = self.start_event(job, "builder")
        self.assertTrue(start.data["overflow"]["why"].startswith("the card cannot fit this model now ("))
        self.assertEqual(start.data["reason"], {"kind": "time",
                                                "text": "overflow from flash: the card cannot fit this model now"})
        self.assertEqual(sorted(r["provider"] for r in start.data["custody_refused"]),
                         sorted([self.tabby, self.strata]))
        self.assertEqual((self.acquires(self.tabby), self.acquires(self.strata)), (1, 1))

    def test_mixed_refusals_overflow_as_no_build_answered(self) -> None:
        self.register.full.add((self.tabby, "flash-exl3"))
        self.register.held[self.strata] = "operator"
        job = self.run_job(self.start_placed(self.both_config()))
        start = self.start_event(job, "builder")
        self.assertTrue(start.data["overflow"]["why"].startswith("no build answered ("))

    def test_after_a_slot_wait_that_ran_out_the_overflow_does_not_wait_out_a_hold(self) -> None:
        """The run waited overflow_after_seconds for a slot of its build: the overflow it moves to is HELD, and
        it does not wait a second time (one wait per placement); with nowhere else to go, it keeps waiting for
        the slot, as without an overflow."""
        tabby = f"tabby{self.suffix}"
        self.register.held["api"] = "operator"
        config = self.placed_config(providers={tabby: {"overflow_after_seconds": 0.3}, "api": {
            "lazy_load": True, "overflow_after_seconds": 30, "custody": {"url": self.register.url, "timeout": 3}}})
        slots = provider_semaphore(tabby, 1)
        held = threading.Event()

        def hold() -> None:   # another run holds tabby's one slot for a while
            slots.acquire()
            held.set()
            time.sleep(1.5)
            slots.release()

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        self.addCleanup(holder.join, 5)
        held.wait(5)
        started = time.monotonic()
        job = self.run_job(self.start_placed(config))
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertLess(time.monotonic() - started, 20.0, "the overflow's HELD answer did not wait 30 s")
        self.assertEqual(self.recorder.of("builder")[0].ctx.model.provider.name, tabby)
        self.assertEqual(self.acquires("api"), 1, "asked once")
        self.assertIn(f"no wait left (the run waited 0.3s for a slot of {tabby} already", self.logs(job))
        self.assertFalse([e for e in self.events(job, "agent.waiting") if e.data.get("custody")])

    def test_cancelling_a_run_while_it_waits_out_a_hold(self) -> None:
        self.register.held.update({self.tabby: "operator", self.strata: "operator"})
        factory = self.start_placed(self.both_config(seconds=60))
        job = factory.submit(REPO, "x")
        until(lambda: [e for e in self.events(job, "agent.waiting") if e.data.get("custody") == "HELD"])
        factory.cancel(REPO, job["id"])
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual(job["stage"], "cancelled")
        self.assertEqual(self.recorder.of("builder"), [], "the run never started")
        self.assertEqual(self.register.leases, {})


class LiveHeldTest(RegisterTestCase):
    def live_config(self, port: int, seconds: float) -> Config:
        return Config(Path(tempfile.mkdtemp()) / "config.json", {
            "providers": {"gpu": {"type": "openai", "base_url": f"http://127.0.0.1:{port}/v1", "api_key_env": None,
                                  "lazy_load": True, "overflow_after_seconds": seconds,
                                  "custody": {"url": self.register.url, "timeout": 3}}},
            "models": {"local": {"provider": "gpu", "model": "flash"}}})

    def test_a_chat_turn_waits_out_a_hold_and_says_so(self) -> None:
        self.register.held["gpu"] = "operator"
        events: list = []
        threading.Timer(0.3, self.register.held.clear).start()
        with mock.patch.object(custody, "HELD_RETRY_SECONDS", 0.05):
            model, _, _, release = place_run(self.live_config(59021, 10), Route("builtin", "local", {}, None),
                                             FakeReach(), threading.Event(),
                                             lambda kind, data: events.append((kind, data)), "chat c-0123456789")
        self.assertIsInstance(model.provider.lease, Lease)
        self.assertIn(("agent.waiting", {"provider": "gpu", "overflow_after_seconds": 10.0, "custody": "HELD"}),
                      events)
        release()
        self.assertEqual(self.register.leases, {})

    def test_a_chat_turn_cancelled_while_it_waits_is_not_placed(self) -> None:
        self.register.held["gpu"] = "operator"
        cancel = threading.Event()
        threading.Timer(0.2, cancel.set).start()
        with mock.patch.object(custody, "HELD_RETRY_SECONDS", 0.05):
            placed = place_run(self.live_config(59022, 30), Route("builtin", "local", {}, None), FakeReach(), cancel,
                               lambda kind, data: None, "chat c-0123456789")
        self.assertIsNone(placed)
        self.assertEqual(self.register.leases, {})


class CustodyViewAndDoctorTest(RegisterTestCase):
    def test_the_view_gives_the_codes_in_words_and_the_hold_wait(self) -> None:
        from swfactory.events import EventBus
        from swfactory.orchestrator import Factory

        tmp = Path(tempfile.mkdtemp())
        config = Config(tmp / "config.json", {"data_dir": str(tmp / "data"), "providers": {
            "gpu": {"type": "openai", "base_url": "http://127.0.0.1:59023/v1", "api_key_env": None, "lazy_load": True,
                    "overflow_after_seconds": 45, "custody": {"url": self.register.url, "timeout": 3}},
            "gpu2": {"type": "openai", "base_url": "http://127.0.0.1:59024/v1", "api_key_env": None,
                     "lazy_load": True, "custody": {"url": self.register.url, "timeout": 3}}}})
        view = Factory(config, EventBus()).custody()
        self.assertEqual(view["reason_codes"]["FULL"], "the card cannot fit this model now")
        rows = {row["provider"]: row for row in view["providers"]}
        self.assertEqual((rows["gpu"]["held_wait_seconds"], rows["gpu2"]["held_wait_seconds"]), (45.0, None))

    def test_the_doctor_says_what_held_and_full_do_and_warns_of_a_model_with_nowhere_to_go(self) -> None:
        from swfactory import doctor

        config = Config(Path(tempfile.mkdtemp()) / "config.json", {
            "providers": {"gpu": {"type": "openai", "base_url": "http://127.0.0.1:59025/v1", "api_key_env": None,
                                  "lazy_load": True, "overflow_after_seconds": 45,
                                  "custody": {"url": self.register.url, "timeout": 3}},
                          "api": {"type": "openai", "base_url": "https://api.example.invalid/v1", "api_key_env": None}},
            "models": {"alone": {"provider": "gpu", "model": "flash"},
                       "spill": {"provider": "gpu", "model": "flash", "overflow": "remote"},
                       "remote": {"provider": "api", "model": "remote-1"}}})
        checks = doctor._custody_check(config, "gpu")
        self.assertEqual((checks[0]["ok"], checks[0]["level"]), (True, "info"))
        self.assertIn("a HELD answer (the card is held by someone else) goes at once to another build that is free, "
                      "else waits up to 45s", checks[0]["detail"])
        self.assertIn("a FULL answer (the card cannot fit the model now) goes straight to the next build",
                      checks[0]["detail"])
        [fallback] = [entry for entry in checks if entry["name"] == "custody gpu fallback"]
        self.assertEqual((fallback["ok"], fallback["level"]), (False, "warn"))
        self.assertTrue(fallback["detail"].startswith("alone runs only on gpu, with no other build and no overflow"))
        self.assertNotIn("spill", fallback["detail"])


class ActivityWaitTest(unittest.TestCase):
    def test_wait_lines_and_refusal_words_read_plainly(self) -> None:
        from swfactory.activity import _custody_summary

        base = {"provider": "gpu", "model": "flash", "purpose": "job demo/001 builder"}
        self.assertEqual(_custody_summary({**base, "op": "wait", "outcome": "waiting", "reason": "HELD",
                                           "seconds": 45.0}),
                         ("lease wait", "waits up to 45.0s for flash on gpu for job demo/001 builder: the card is held "
                                        "by someone else"))
        self.assertEqual(_custody_summary({**base, "op": "wait", "outcome": "gave_up", "seconds": 45.0})[0],
                         "lease wait gave up")
        self.assertEqual(_custody_summary({**base, "op": "claim", "outcome": "denied", "reason": "FULL",
                                           "words": "the card cannot fit this model now"}),
                         ("lease denied", "lease denied: flash on gpu for job demo/001 builder: FULL (the card cannot "
                                          "fit this model now)"))


# ---------------------------------------------------------------- the epoch


class EpochTest(RegisterTestCase):
    def test_a_new_epoch_may_start_again_from_generation_0(self) -> None:
        self.register.epoch = "boot-a:1"
        self.register.generation = 50
        client = self.client()
        held = client.claim("gpu", "flash", "job a", "me")
        self.register.restart("boot-a:2")             # the register restarted on the same boot: its counter moved on
        self.register.generation = 0
        found = client.observe()
        self.assertEqual((found.generation, client.generation, client.epoch, client.fenced), (0, 0, "boot-a:2", None))
        self.assertEqual(held.state, "lost", "a lease of the earlier epoch is lost")
        self.assertIn("epoch boot-a:1 became boot-a:2", held.problem())
        self.assertNotIn(f"/leases/{held.lease_id}/release", self.register.paths(), "nothing released by assumption")
        lease = client.claim("gpu", "flash", "job b", "me")
        self.assertIsNone(lease.problem())

    def test_the_same_epoch_with_a_lower_generation_is_refused_as_stale(self) -> None:
        self.register.epoch = "boot-a:1"
        self.register.generation = 50
        client = self.client()
        client.observe()
        self.register.generation = 10                  # the same epoch, an older generation: a replay, not a restart
        with self.assertRaises(custody.StaleAnswer):
            client.observe()
        with self.assertRaises(CustodyDenied) as caught:
            client.claim("gpu", "flash", "job", "me")
        self.assertEqual(caught.exception.code, "STALE")
        self.assertEqual((client.fenced["seen"], client.fenced["answered"], client.epoch), (50, 10, "boot-a:1"))
        self.assertNotIn("acquire", self.register.ops(), "no lease is asked for on a stale view")

    def test_a_lease_of_an_earlier_epoch_is_never_released_at_the_new_register(self) -> None:
        """The register's new instance does not know the lease, and may have given its id to another holder: the
        run that ends sends no release for it, keeps it lost with its error, and reads residency."""
        self.register.epoch = "boot-a:1"
        client = self.client()
        held = client.claim("gpu", "flash", "job a", "me")
        self.register.restart("boot-a:2")
        client.observe()
        before = len(self.register.requests)
        view = client.release(held, "the run ended")
        self.assertEqual(self.register.ops()[before:], ["residency"], "no release went to the new instance")
        self.assertEqual((view["state"], view["epoch"]), ("lost", "boot-a:1"))
        self.assertEqual(view["error"], "the register restarted (epoch boot-a:1 became boot-a:2)")
        self.assertIsNotNone(view["resident_after"], "residency was read again")
        last = client.history[-1]
        self.assertEqual((last["op"], last["outcome"]), ("release", "lost"))
        self.assertIn("not sent: the register restarted", last["reason"])

    def test_a_restart_not_seen_yet_is_seen_before_a_release_or_a_renewal_goes_out(self) -> None:
        self.register.epoch = "boot-a:1"
        client = self.client()
        renewed = client.claim("gpu", "flash", "job a", "me")
        released = client.claim("gpu2", "pro", "job b", "me")
        self.register.restart("boot-a:2")             # the client has seen no answer from the new instance
        before = len(self.register.requests)
        self.assertFalse(client.renew(renewed))
        self.assertEqual(self.register.ops()[before:], ["residency"], "residency first: no renew was sent")
        self.assertEqual(renewed.state, "lost")
        before = len(self.register.requests)
        view = client.release(released)
        self.assertEqual(self.register.ops()[before:], ["residency"], "the residency read first is the one kept")
        self.assertEqual((view["state"], view["error"]),
                         ("lost", "the register restarted (epoch boot-a:1 became boot-a:2)"))
        self.assertNotIn("release", self.register.ops())

    def test_within_one_epoch_a_release_reads_residency_first_and_then_releases(self) -> None:
        self.register.epoch = "boot-a:1"
        client = self.client()
        held = client.claim("gpu", "flash", "job a", "me")
        before = len(self.register.requests)
        view = client.release(held)
        self.assertEqual(self.register.ops()[before:], ["residency", "release", "residency"])
        self.assertEqual((view["state"], self.register.leases), ("released", {}))
        unnamed = self.client()                       # a register that sends no epoch: no read first
        self.register.epoch = None
        lease = unnamed.claim("gpu", "flash", "job b", "me")
        before = len(self.register.requests)
        unnamed.release(lease)
        self.assertEqual(self.register.ops()[before:], ["release", "residency"])

    def test_a_register_that_starts_to_send_an_epoch_restarts_the_fence(self) -> None:
        self.register.generation = 500
        client = self.client()
        client.observe()
        self.register.restart("boot-b:1")              # its new version sends an epoch, and counts from 1
        found = client.observe()
        self.assertEqual((found.generation, client.epoch, client.fenced), (1, "boot-b:1", None))
        self.assertIn(("epoch", "it now sends epoch boot-b:1"),
                      [(entry["op"], entry.get("reason")) for entry in client.history])


class EpochOnRenewAndReleaseTest(RegisterTestCase):
    """The operator's answer (26 Oct): lease ids are unique across epochs, and a renew and a release carry the
    epoch the lease was granted in, so the register refuses a mismatch (EPOCH_MISMATCH). Reading residency first
    is a courtesy; the refusal is the enforcement."""

    def bodies(self, action: str) -> list:
        return [body for verb, path, _, body in self.register.requests if path.endswith("/" + action)]

    def test_renew_and_release_carry_the_epoch_and_none_without_one(self) -> None:
        self.register.epoch = "boot-a:1"
        client = self.client()
        lease = client.claim("gpu", "flash", "job a", "me")
        self.assertTrue(client.renew(lease))
        self.assertEqual(client.release(lease)["state"], "released")
        self.assertEqual((self.bodies("renew"), self.bodies("release")),
                         ([{"epoch": "boot-a:1"}], [{"epoch": "boot-a:1"}]))
        self.register.requests.clear()
        self.register.epoch = None                     # a register that sends no epoch: none is sent
        unnamed = self.client()
        lease = unnamed.claim("gpu", "flash", "job b", "me")
        self.assertTrue(unnamed.renew(lease))
        unnamed.release(lease)
        self.assertEqual((self.bodies("renew"), self.bodies("release")), ([{}], [{}]))

    def test_the_mock_register_refuses_a_renew_or_release_of_another_epoch(self) -> None:
        self.register.epoch = "boot-a:2"
        adapter = RegisterAdapter(settings_for(self.register))
        for epoch in ("boot-a:1", None):
            with self.subTest(epoch=epoch):
                answer = adapter.renew("l-1", epoch)
                self.assertEqual((answer.granted, answer.reason_code, answer.epoch),
                                 (False, "EPOCH_MISMATCH", "boot-a:2"))
                self.assertEqual(adapter.release("l-1", epoch).reason_code, "EPOCH_MISMATCH")
        self.assertEqual(custody.parse_answer({"granted": False, "generation": 1,
                                               "reason_code": "epoch_mismatch"}).reason_code, "EPOCH_MISMATCH")

    def test_a_refused_epoch_loses_the_lease_and_nothing_is_sent_again(self) -> None:
        """The courtesy read left out (a restart in the moment between it and the request): the register's
        refusal alone loses the lease. Lost, never "unknown", and no renew or release is sent again."""
        self.register.epoch = "boot-a:1"
        client = self.client()
        renewed = client.claim("gpu", "flash", "job a", "me")
        released = client.claim("gpu2", "pro", "job b", "me")
        self.register.restart("boot-a:2")
        with mock.patch.object(client, "_granted_earlier", return_value=(False, None)):
            self.assertFalse(client.renew(renewed))
            self.assertEqual(renewed.state, "lost")
            self.assertTrue(renewed.problem().startswith("the lease was lost (renew refused: EPOCH_MISMATCH"))
            self.assertIn("EPOCH_MISMATCH: the lease is of an earlier instance of the register (granted under "
                          "epoch boot-a:1; the register is at epoch boot-a:2)", renewed.error)
            self.assertFalse(client.renew(renewed), "a lost lease is never renewed again")
            before = len(self.register.requests)
            view = client.release(released, "the run ended")
            self.assertEqual(self.register.ops()[before:], ["release", "residency"], "refused once, not sent again")
        self.assertEqual(self.register.ops().count("renew"), 1)
        self.assertEqual(view["state"], "lost", "the register answered: lost, not unknown")
        self.assertIn("release refused: EPOCH_MISMATCH", view["error"])
        last = client.history[-1]
        self.assertEqual((last["op"], last["outcome"]), ("release", "lost"))
        self.assertIn("EPOCH_MISMATCH", last["reason"])
        self.assertIn("the run ended", last["reason"])

    def test_the_keeper_stops_after_an_epoch_refusal(self) -> None:
        self.register.ttl = 2.0
        self.register.epoch = "boot-a:1"
        client = self.client()
        lease = client.claim("gpu", "flash", "job a", "me")
        self.register.restart("boot-a:2")
        with mock.patch.object(client, "_granted_earlier", return_value=(False, None)):
            lease.keep()
            until(lambda: lease.state == "lost", timeout=5)
            lease._keeper.join(5)
        self.assertFalse(lease._keeper.is_alive(), "no retry: the keeper ended")
        self.assertEqual(self.register.ops().count("renew"), 1)

    def test_a_lease_id_granted_again_under_another_epoch_is_not_used(self) -> None:
        """Lease ids are unique across epochs (the contract): a register that hands out an id it gave under an
        earlier epoch breaks it, and the grant is refused with why; nothing more is sent for that id."""
        self.register.epoch = "boot-a:1"
        self.register.ids = ["L-7"]
        client = self.client()
        client.release(client.claim("gpu", "flash", "job a", "me"))
        self.register.restart("boot-a:2")
        self.register.ids = ["L-7"]
        with self.assertRaises(CustodyDenied) as caught:
            client.claim("gpu", "flash", "job b", "me")
        self.assertEqual(caught.exception.code, "CONTRACT")
        self.assertIn("granted lease id L-7 again under epoch boot-a:2, after epoch boot-a:1 granted it: lease ids "
                      "must be unique across epochs", caught.exception.detail)
        last = client.history[-1]
        self.assertEqual((last["op"], last["outcome"], last["reason"], last["lease_id"]),
                         ("claim", "denied", "CONTRACT", "L-7"))
        self.assertEqual(client.leases, {}, "not used")
        before = len(self.register.requests)
        self.assertEqual(Claims("job demo/x builder", keep=False).release(), [])
        self.assertIn("L-7", self.register.leases, "nothing was released for it: it expires at the register")
        self.assertEqual(self.register.ops()[before:], [])
        self.assertEqual(custody.refusal_words("CONTRACT"), "the custody register broke its contract")
        self.register.ids = ["L-8"]                    # a fresh id in the new epoch is used as ever
        self.register.leases.clear()
        self.assertIsNone(client.claim("gpu", "flash", "job c", "me").problem())


# ---------------------------------------------------------------- systemd credentials


class CredentialTest(RegisterTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.creds = Path(tempfile.mkdtemp())
        patcher = mock.patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(self.creds)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_credential_name_is_a_plain_name(self) -> None:
        from swfactory.plainhttp import check_credential_name

        for good in ("register-token", "checker_token", "a", "tok.en@2+x", "x" * 128):
            self.assertIsNone(check_credential_name(good), good)
        for bad in ("", "a/b", "../x", "a..b", ".hidden", "-x", "a\x00b", "x" * 129, "a b", "a:b", None, 5):
            self.assertIsNotNone(check_credential_name(bad), bad)

    def test_the_credential_is_read_again_for_every_request(self) -> None:
        self.register.token = "first-token-0123456789"
        (self.creds / "register-token").write_text("first-token-0123456789\n")
        adapter = RegisterAdapter(parse_settings({"url": self.register.url, "token_credential": "register-token"},
                                                 "c"))
        adapter.residency()
        self.assertEqual(self.register.requests[-1][2]["authorization"], "Bearer first-token-0123456789")
        self.register.token = "second-token-0123456789"   # rotated: the next request reads the new copy
        (self.creds / "register-token").write_text("second-token-0123456789\n")
        adapter.residency()
        self.assertEqual(self.register.requests[-1][2]["authorization"], "Bearer second-token-0123456789")

    def test_a_missing_directory_a_link_or_a_bad_name_gives_no_token(self) -> None:
        from swfactory.plainhttp import read_bearer

        (self.creds / "real").write_text("real-token-0123456789\n")
        os.symlink(self.creds / "real", self.creds / "linked")
        self.assertEqual(read_bearer(None, None, "real"), "real-token-0123456789")
        self.assertIsNone(read_bearer(None, None, "linked"), "never through a symbolic link")
        self.assertIsNone(read_bearer(None, None, "../real"))
        with mock.patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": "relative/dir"}):
            self.assertIsNone(read_bearer(None, None, "real"))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(read_bearer(None, None, "real"))

    def test_the_settings_take_a_credential_name_alone(self) -> None:
        parsed = parse_settings({"url": "http://127.0.0.1:9", "token_credential": "register-token"}, "c")
        self.assertEqual((parsed.token_credential, parsed.to_dict()["token_credential"]),
                         ("register-token", "register-token"))
        self.assertNotEqual(parsed.key(), parse_settings({"url": "http://127.0.0.1:9"}, "c").key())
        for extra in ({"token_file": "/run/x.token"}, {"token_env": "REGISTER_TOKEN"}):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, "token_credential cannot be set "
                                                                               "together with token_file or token_env"):
                parse_settings({"url": "http://127.0.0.1:9", "token_credential": "register-token", **extra}, "c")
        for bad in ("a/b", "..", "", 7):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, r"c\.token_credential"):
                parse_settings({"url": "http://127.0.0.1:9", "token_credential": bad}, "c")

    def test_a_token_read_from_a_credential_is_a_secret(self) -> None:
        (self.creds / "register-token").write_text("custody-credential-0123456789\n")
        (self.creds / "checker-token").write_text("drift-credential-0123456789\n")
        config = Config(Path(tempfile.mkdtemp()) / "config.json", {
            "providers": {"tabby": {"lazy_load": True, "custody": {"url": "http://127.0.0.1:9",
                                                                   "token_credential": "register-token"}}},
            "drift_sources": {"checker": {"url": "http://127.0.0.1:9/drift", "token_credential": "checker-token",
                                          "schema_versions": ["1"]}}})
        settings.validate_settings(config)
        secrets = config.secret_values()
        self.assertIn("custody-credential-0123456789", secrets)
        self.assertIn("drift-credential-0123456789", secrets)
        self.assertNotIn("custody-credential-0123456789", repr(config.public_view()))

    def test_the_doctor_names_the_credential_and_the_line_to_add(self) -> None:
        from swfactory import doctor

        config = Config(Path(tempfile.mkdtemp()) / "config.json", {"providers": {"gpu": {
            "type": "openai", "base_url": "http://127.0.0.1:59026/v1", "api_key_env": None, "lazy_load": True,
            "custody": {"url": self.register.url, "timeout": 3, "token_credential": "register-token"}}}})
        with mock.patch.dict(os.environ, {}, clear=True):   # the running server, started without the credential
            [check] = doctor._custody_check(config, "gpu", in_server=True)
        self.assertEqual((check["ok"], check["level"]), (False, "error"))
        self.assertIn("the systemd credential register-token, but CREDENTIALS_DIRECTORY is not set", check["detail"])
        self.assertIn("LoadCredential=register-token:<path to the token file>", check["detail"])
        self.assertIn("GET /api/doctor", check["detail"])
        for in_server in (True, False):   # the directory is set but the file is missing: an error, shell or not
            with self.subTest(in_server=in_server):
                [check] = doctor._custody_check(config, "gpu", in_server)
                self.assertEqual(check["level"], "error")
                self.assertIn(f"{self.creds / 'register-token'} does not exist", check["detail"])
                self.assertIn("LoadCredential=register-token:<path to the token file>", check["detail"])
        (self.creds / "register-token").write_text("register-token-0123456789\n")
        [check] = doctor._custody_check(config, "gpu", in_server=True)
        self.assertEqual((check["ok"], check["level"]), (True, "info"))

    def test_a_doctor_in_a_shell_warns_about_a_credential_and_still_asks_the_register(self) -> None:
        """A shell never has the harness's systemd credentials: factory doctor there says it cannot check the
        credential (a warning, so it does not fail), still asks the register, and fails only when the register
        cannot be reached at all."""
        from swfactory import doctor

        def config_for(url: str) -> Config:
            return Config(Path(tempfile.mkdtemp()) / "config.json", {"providers": {"gpu": {
                "type": "openai", "base_url": "http://127.0.0.1:59027/v1", "api_key_env": None, "lazy_load": True,
                "custody": {"url": url, "timeout": 1, "token_credential": "register-token"}}}})

        with mock.patch.dict(os.environ, {}, clear=True):
            checks = {entry["name"]: entry for entry in doctor._custody_check(config_for(self.register.url), "gpu")}
            note = checks["custody gpu credential"]
            self.assertEqual((note["ok"], note["level"]), (False, "warn"))
            self.assertIn("CREDENTIALS_DIRECTORY is not set", note["detail"])
            self.assertIn("GET /api/doctor", note["detail"])
            self.assertIn("LoadCredential=register-token:<path to the token file>", note["detail"])
            self.assertEqual(checks["custody gpu"]["level"], "info", "a register that needs no token still answers")
            self.assertIn("residency", self.register.ops())
            self.register.token = "register-token-0123456789"   # a register that wants the token: 401 without it
            checks = {entry["name"]: entry for entry in doctor._custody_check(config_for(self.register.url), "gpu")}
            self.assertEqual((checks["custody gpu"]["ok"], checks["custody gpu"]["level"]), (False, "warn"))
            self.assertIn("asked without its token, which only the running server has", checks["custody gpu"]["detail"])
            checks = {entry["name"]: entry for entry in doctor._custody_check(config_for("http://127.0.0.1:9"), "gpu")}
            self.assertEqual(checks["custody gpu"]["level"], "error", "a register that is down is an error anyway")


if __name__ == "__main__":
    unittest.main()
