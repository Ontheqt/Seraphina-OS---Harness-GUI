"""Tests for drift sources (DESIGN.md section 38, swfactory/driftsources.py) against a mock source that behaves
like the operator's checker: GET only (405 otherwise), the Host must match, the Bearer token is compared in
constant time (401), an Origin header gets 403, and a call while another is in progress gets 429.

What is checked: the exact request (Host, Bearer, no Origin or Sec-Fetch-*), every refusal and the back-off,
validForSeconds and observedAt, an unknown schemaVersion as "cannot read" (never "no drift"), findings as
system-scope tracker items (source drift:<name>; deduplicated, reopened when they recur, closed only with drift
evidence), the role manifest from profiles (in the data dir only), the Factory and HTTP surface."""

from __future__ import annotations

import hmac
import json
import os
import shutil
import ssl
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

from swfactory import custody, driftsources, settings
from swfactory.config import Config
from swfactory.driftsources import DriftSources, fetch, parse_source
from swfactory.events import EventBus
from swfactory.housekeeping import HousekeepingManager
from swfactory.plainhttp import HttpFailure, request
from tests.test_server import ServerTestCase

SOURCE_TOKEN = "drift-source-token-0123456789"
HOST = "checker.internal:8443"


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


PROFILES = [{"base_model_id": "flash-next", "providerKind": "tabby", "providerModelId": "Flash-Next-exl3",
             "routeKind": "local", "versionIdAtSnapshot": "v-41", "toolPackageCount": 3, "filterPackageCount": 1,
             "limitations": ["no vision"]},
            {"base_model_id": "sonnet", "providerKind": "openai", "providerModelId": "claude-sonnet-5-5",
             "routeKind": "api", "versionIdAtSnapshot": "v-7", "toolPackageCount": 0, "filterPackageCount": 0,
             "limitations": []}]


class MockSource:
    """The operator's checker as the harness sees it (see the module docstring)."""

    def __init__(self) -> None:
        self.payload: dict = {}
        self.delay = 0.0
        self.status: int | None = None        # answer this status instead of the payload
        self.retry_after: str | None = None
        self.requests: list[tuple[str, str, dict]] = []
        self.clock: list[float] = [time.time()]   # observedAt follows the test's clock
        self.busy = False
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.source = self   # type: ignore[attr-defined]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/drift"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def observe(self, findings: list[dict] | None = None, *, at: float | None = None, valid: float = 3600,
                version: Any = "1", profiles: list[dict] | None = None) -> None:
        payload: dict[str, Any] = {"schemaVersion": version, "observedAt": iso(self.clock[0] if at is None else at),
                                   "validForSeconds": valid}
        if findings is not None:
            payload["findings"] = findings
        if profiles is not None:
            payload["profiles"] = profiles
        self.payload = payload


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, message_format: str, *args: Any) -> None:
        pass

    def _send(self, status: int, payload: dict, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _any(self, method: str) -> None:
        source: MockSource = self.server.source   # type: ignore[attr-defined]
        headers = {name.lower(): value for name, value in self.headers.items()}
        source.requests.append((method, self.path, headers))
        if method != "GET":
            self._send(405, {"error": "GET only"}, {"Allow": "GET"})
            return
        if headers.get("host") != HOST:
            self._send(403, {"error": "wrong host"})
            return
        if "origin" in headers:
            self._send(403, {"error": "no browsers"})
            return
        given = headers.get("authorization", "")
        if not hmac.compare_digest(given.encode(), f"Bearer {SOURCE_TOKEN}".encode()):
            self._send(401, {"error": "unauthorized"})
            return
        with source.lock:
            if source.busy:
                busy = True
            else:
                busy, source.busy = False, True
        if busy:
            self._send(429, {"error": "check in progress"}, {"Retry-After": "7"})
            return
        try:
            time.sleep(source.delay)
            if source.status is not None:
                self._send(source.status, {"error": "scripted"},
                           {"Retry-After": source.retry_after} if source.retry_after else None)
            else:
                self._send(200, source.payload)
        finally:
            with source.lock:
                source.busy = False

    def do_GET(self) -> None:
        self._any("GET")

    def do_POST(self) -> None:
        self._any("POST")


class DriftTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-drift-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.source = MockSource()
        self.addCleanup(self.source.stop)
        self.token_file = self.tmp / "drift.token"
        self.token_file.write_text(SOURCE_TOKEN + "\n")
        self.bus = EventBus()
        self.clock = [time.time()]
        self.source.clock = self.clock

    def entry(self, **extra: Any) -> dict:
        return {"url": self.source.url, "token_file": str(self.token_file), "host_header": HOST,
                "schema_versions": ["1"], "interval": 600, **extra}

    def manager(self, **extra: Any) -> DriftSources:
        config = Config(self.tmp / "config.json", {"data_dir": str(self.tmp / "data"),
                                                   "drift_sources": {"checker": self.entry(**extra)}})
        system = HousekeepingManager(config.data_dir, self.bus)
        self.system = system
        return DriftSources(config, system, self.bus, clock=lambda: self.clock[0])

    def items(self) -> list[dict]:
        return self.system.scope_items()["items"]


# ---------------------------------------------------------------- settings and the request


class SettingsTest(DriftTestCase):
    def test_a_source_is_checked_by_name(self) -> None:
        parsed = parse_source("checker", self.entry(timeout=5))
        self.assertEqual((parsed.host_header, parsed.schema_versions, parsed.interval), (HOST, ("1",), 600.0))
        self.assertEqual(parsed.view()["token_from"], "file")
        for bad, words in [({"url": "file:///x"}, "url"), ({"token_file": None, "token_env": None}, "needs token"),
                           ({"schema_versions": []}, "schema_versions"), ({"interval": 5}, "interval"),
                           ({"host_header": "a\nb"}, "host_header"), ({"timeout": 99}, "timeout"),
                           ({"method": "POST"}, "unknown keys")]:
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, words):
                parse_source("checker", {**self.entry(), **bad})
        with self.assertRaisesRegex(ValueError, "invalid name"):
            parse_source("Bad Name", self.entry())
        config = Config(self.tmp / "config.json", {"drift_sources": {"checker": {"url": "http://x"}}})
        with self.assertRaisesRegex(ValueError, "drift_sources.checker needs token_file"):
            settings.validate_settings(config)
        self.assertIn("drift_sources", settings.UPDATABLE_SECTIONS)

    def test_the_token_is_a_secret(self) -> None:
        config = Config(self.tmp / "config.json", {"drift_sources": {"checker": self.entry()}})
        self.assertIn(SOURCE_TOKEN, config.secret_values())
        self.assertNotIn(SOURCE_TOKEN, json.dumps(driftsources.parse_source("checker", self.entry()).view()))

    def test_a_systemd_credential_is_read_for_every_check_and_named_alone(self) -> None:
        from swfactory import doctor

        creds = self.tmp / "credentials"
        creds.mkdir()
        entry = {**self.entry(token_file=None), "token_credential": "checker-token"}
        parsed = parse_source("checker", entry)
        self.assertEqual((parsed.token_credential, parsed.view()["token_from"]), ("checker-token", "credential"))
        for extra in ({"token_file": str(self.token_file)}, {"token_env": "CHECKER_TOKEN"}):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, "token_credential cannot be set"):
                parse_source("checker", {**entry, **extra})
        with self.assertRaisesRegex(ValueError, r"drift_sources\.checker\.token_credential"):
            parse_source("checker", {**entry, "token_credential": "../checker-token"})
        self.source.observe([])
        config = Config(self.tmp / "config.json", {"data_dir": str(self.tmp / "data"),
                                                   "drift_sources": {"checker": entry}})
        with mock.patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(creds)}):
            [check] = doctor._drift_checks(config)
            self.assertEqual((check["ok"], check["level"]), (False, "error"))
            self.assertIn("LoadCredential=checker-token:<path to the token file>", check["detail"])
            self.assertEqual(fetch(parsed).status, "cannot_read", "no token, no request")
            self.assertEqual(self.source.requests, [])
            (creds / "checker-token").write_text(SOURCE_TOKEN + "\n")
            fetched = fetch(parsed)
            self.assertEqual(fetched.status, "ok", fetched.detail)
            self.assertEqual(self.source.requests[-1][2]["authorization"], f"Bearer {SOURCE_TOKEN}")
            self.assertIn(SOURCE_TOKEN, config.secret_values())
            [check] = doctor._drift_checks(config)
            self.assertTrue(check["ok"], check["detail"])
        with mock.patch.dict(os.environ, {}, clear=True):
            [check] = doctor._drift_checks(config, in_server=True)   # the server, started without the credential
            self.assertEqual(check["level"], "error")
            self.assertIn("CREDENTIALS_DIRECTORY is not set", check["detail"])
            note, state = doctor._drift_checks(config)   # a shell: it cannot check the credential, and says so
        self.assertEqual((note["name"], note["ok"], note["level"]), ("drift source checker credential", False, "warn"))
        self.assertIn("GET /api/doctor", note["detail"])
        self.assertEqual((state["name"], state["ok"]), ("drift source checker", True), "the last check is still shown")

    def test_a_token_file_inside_a_systemd_credentials_directory_is_refused(self) -> None:
        """The harness reads only its own copy of a token (token_credential), never another service's
        credentials directory, also not through a symbolic link."""
        from swfactory.plainhttp import in_credentials_dir, read_bearer

        other = "/run/credentials/other-checker.service/checker-token"
        link = self.tmp / "checker.token"
        os.symlink(other, link)
        own = self.tmp / "own-credentials"
        own.mkdir()
        (own / "checker-token").write_text(SOURCE_TOKEN + "\n")
        with mock.patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(own)}):
            for path in (other, str(link), "/run/user/1000/credentials/x.service/t", str(own / "checker-token")):
                with self.subTest(path=path):
                    self.assertTrue(in_credentials_dir(path))
                    with self.assertRaisesRegex(ValueError, "inside a systemd credentials directory.*token_credential"):
                        parse_source("checker", self.entry(token_file=path))
                    with self.assertRaisesRegex(ValueError, "inside a systemd credentials directory"):
                        custody.parse_settings({"url": "http://127.0.0.1:9", "token_file": path}, "c")
            self.assertIsNone(read_bearer(str(own / "checker-token"), None), "not read as a token_file either")
            self.assertEqual(read_bearer(None, None, "checker-token"), SOURCE_TOKEN, "as the harness's own credential")
        for path in (str(self.token_file), "/run/x.token", "/run/credentialsx/t"):
            self.assertFalse(in_credentials_dir(path), path)


class FetchTest(DriftTestCase):
    def test_get_only_the_exact_host_the_bearer_and_no_origin_or_sec_fetch(self) -> None:
        self.source.observe([])
        fetched = fetch(parse_source("checker", self.entry()))
        self.assertEqual(fetched.status, "ok", fetched.detail)
        method, path, headers = self.source.requests[-1]
        self.assertEqual((method, path, headers["host"], headers["authorization"]),
                         ("GET", "/drift", HOST, f"Bearer {SOURCE_TOKEN}"))
        self.assertFalse([name for name in headers if name == "origin" or name.startswith("sec-fetch")])
        # the mock really refuses what the client never sends
        browser = request("GET", self.source.url, {"Host": HOST, "Origin": "http://evil.example",
                                                   "Authorization": f"Bearer {SOURCE_TOKEN}"})
        self.assertEqual(browser.status, 403)
        self.assertEqual(request("POST", self.source.url, {"Host": HOST}, body={}).status, 405)

    def test_refusals_are_cannot_read_and_429_is_check_in_progress(self) -> None:
        wrong_host = fetch(parse_source("checker", self.entry(host_header="other.internal")))
        self.assertEqual((wrong_host.status, wrong_host.http), ("cannot_read", 403))
        self.token_file.write_text("not-the-token-at-all\n")
        refused = fetch(parse_source("checker", self.entry()))
        self.assertEqual((refused.status, refused.http), ("cannot_read", 401))
        self.assertNotIn("not-the-token-at-all", refused.detail)
        self.token_file.write_text(SOURCE_TOKEN)
        self.source.status = 405
        self.assertIn("405", fetch(parse_source("checker", self.entry())).detail)
        self.source.status, self.source.retry_after = 429, "42"
        busy = fetch(parse_source("checker", self.entry()))
        self.assertEqual((busy.status, busy.retry_after, busy.detail), ("busy", 42.0, "check in progress (429)"))

    def test_concurrent_calls_get_429_and_the_harness_never_asks_twice_at_once(self) -> None:
        self.source.observe([])
        self.source.delay = 0.6
        results: list = []
        threads = [threading.Thread(target=lambda: results.append(fetch(parse_source("checker", self.entry()))))
                   for _ in range(2)]
        for thread in threads:
            thread.start()
            time.sleep(0.1)
        for thread in threads:
            thread.join(5)
        self.assertEqual(sorted(result.status for result in results), ["busy", "ok"])
        manager = self.manager()
        before = len(self.source.requests)
        first: list = []
        runner = threading.Thread(target=lambda: first.append(manager.check("checker")))
        runner.start()
        time.sleep(0.2)
        second = manager.check("checker")
        runner.join(5)
        self.assertEqual(second["status"], "busy")
        self.assertEqual(len(self.source.requests) - before, 1, "one call at a time per source")
        self.assertEqual(first[0]["status"], "ok")

    def test_unreachable(self) -> None:
        fetched = fetch(parse_source("checker", self.entry(url="http://127.0.0.1:9/drift", timeout=1)))
        self.assertEqual(fetched.status, "unreachable")


def private_certificate(folder: Path, name: str) -> tuple[Path, Path] | None:
    """A self-signed certificate for DNS name only (no IP address), made with openssl; None without openssl."""
    cert, key = folder / "cert.pem", folder / "key.pem"
    try:
        done = subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                               "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "2", "-subj", f"/CN={name}",
                               "-addext", f"subjectAltName=DNS:{name}"], capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return (cert, key) if done.returncode == 0 and cert.is_file() else None


class TlsSourceTest(DriftTestCase):
    """An https source with a private certificate: ca_file trusts it, and with a Host header the certificate must
    name that host (https://127.0.0.1:<port> with Host: checker.internal)."""

    def setUp(self) -> None:
        super().setUp()
        made = private_certificate(self.tmp, "checker.internal")
        if made is None:
            self.skipTest("openssl is not available to make a test certificate")
        self.cert, key = made
        self.source.observe([])
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, key)
        self.secure = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.secure.daemon_threads = True
        self.secure.source = self.source   # type: ignore[attr-defined]
        self.secure.socket = context.wrap_socket(self.secure.socket, server_side=True)
        threading.Thread(target=self.secure.serve_forever, daemon=True).start()
        self.addCleanup(self.secure.server_close)
        self.addCleanup(self.secure.shutdown)
        self.url = f"https://127.0.0.1:{self.secure.server_address[1]}/drift"

    def test_ca_file_trusts_a_private_certificate_checked_against_the_host_header(self) -> None:
        trusted = fetch(parse_source("checker", self.entry(url=self.url, ca_file=str(self.cert),
                                                           host_header="checker.internal:8443")))
        self.assertEqual(trusted.status, "ok", trusted.detail)
        self.assertEqual(self.source.requests[-1][2]["host"], "checker.internal:8443")
        untrusted = fetch(parse_source("checker", self.entry(url=self.url, host_header="checker.internal:8443")))
        self.assertEqual(untrusted.status, "unreachable")
        self.assertIn("CERTIFICATE_VERIFY_FAILED", untrusted.detail)
        other = fetch(parse_source("checker", self.entry(url=self.url, ca_file=str(self.cert),
                                                         host_header="other.internal")))
        self.assertEqual(other.status, "unreachable", "the certificate does not name the Host header's host")
        with self.assertRaisesRegex(HttpFailure, "TLS"):   # without a Host header, the URL's host: not in it
            request("GET", self.url, {}, timeout=3, ca_file=str(self.cert))
        with self.assertRaisesRegex(HttpFailure, "ca_file .* cannot be loaded"):
            request("GET", self.url, {"Host": "checker.internal"}, timeout=3, ca_file=str(self.tmp / "missing.pem"))
        self.assertEqual(parse_source("checker", self.entry(ca_file=str(self.cert))).view()["ca_file"], str(self.cert))
        with self.assertRaisesRegex(ValueError, "ca_file"):
            parse_source("checker", self.entry(ca_file=""))


# ---------------------------------------------------------------- observations and items


class ObservationTest(DriftTestCase):
    def test_findings_become_system_items_deduplicated_closed_with_evidence_and_reopened(self) -> None:
        manager = self.manager()
        self.source.observe([{"key": "openwebui/flash-next/context", "title": "Context differs",
                              "detail": "Tabby has 65536, the profile 262144.", "severity": "high"}])
        result = manager.check("checker")
        self.assertEqual(result["status"], "ok", result.get("detail"))
        [item] = self.items()
        self.assertEqual((item["status"], item["source"]["type"], item["source"]["key"], item["kind"]),
                         ("open", "drift:checker", "openwebui/flash-next/context", "commitment"))
        self.assertIn("Context differs", item["title"])
        self.assertEqual(result["result"]["opened"], [f"system:{item['id']}"])
        self.clock[0] += 700
        self.source.observe([{"key": "openwebui/flash-next/context", "title": "Context differs"}])
        self.assertEqual(manager.check("checker")["result"]["kept"], [f"system:{item['id']}"])
        self.assertEqual(len(self.items()), 1, "deduplicated by its stable key")
        self.clock[0] += 700
        self.source.observe([])
        closed = manager.check("checker")["result"]["closed"]
        [item] = self.items()
        self.assertEqual((closed, item["status"], item["verified_by"]), ([f"system:{item['id']}"], "verified", "factory"))
        evidence = item["evidence"][-1]
        self.assertEqual((evidence["kind"], evidence["check"]), ("drift", "ok"))
        self.assertTrue(evidence["ref"].startswith("drift:checker@"))
        self.clock[0] += 700
        self.source.observe([{"key": "openwebui/flash-next/context", "title": "Context differs"}])
        self.assertEqual(manager.check("checker")["result"]["reopened"], [f"system:{item['id']}"])
        [item] = self.items()
        self.assertEqual(item["status"], "open", "the same item comes back: it recurred")
        self.assertIn("reports it again", item["reason"])

    def test_a_person_closing_an_item_while_it_is_reported_holds_until_it_recurs(self) -> None:
        manager = self.manager()
        self.source.observe([{"key": "k1", "title": "Accepted difference"}])
        manager.check("checker")
        [item] = self.items()
        self.system.item_action(item["id"], "drop", reason="this difference is intended")
        self.clock[0] += 700
        self.source.observe([{"key": "k1", "title": "Accepted difference"}])
        manager.check("checker")
        self.assertEqual(self.items()[0]["status"], "dropped", "still reported: the person's decision stands")
        self.clock[0] += 700
        self.source.observe([])
        manager.check("checker")
        self.assertEqual(self.items()[0]["status"], "dropped", "a dropped item is not proven by the drift source")
        self.clock[0] += 700
        self.source.observe([{"key": "k1", "title": "Accepted difference"}])
        manager.check("checker")
        self.assertEqual(self.items()[0]["status"], "open", "it went away and came back: reopened")

    def test_an_unknown_schema_version_is_cannot_read_never_no_drift(self) -> None:
        manager = self.manager()
        self.source.observe([{"key": "k1", "title": "Something drifted"}])
        manager.check("checker")
        self.clock[0] += 700
        self.source.observe([], version="2")      # an empty findings list, in a schema this harness cannot read
        result = manager.check("checker")
        self.assertEqual(result["status"], "cannot_read")
        self.assertIn("unknown schemaVersion '2'", result["detail"])
        self.assertIn('not "no drift"', result["detail"])
        self.assertEqual(self.items()[0]["status"], "open", "nothing was closed")

    def test_validity_and_observed_at_are_honoured(self) -> None:
        manager = self.manager()
        self.source.observe([{"key": "k1"}])
        manager.check("checker")
        self.clock[0] += 700
        self.source.observe([], at=self.clock[0] - 100, valid=60)   # observed long enough ago to be stale
        result = manager.check("checker")
        self.assertEqual(result["status"], "stale")
        self.assertEqual(self.items()[0]["status"], "open", "a stale observation proves nothing")
        self.source.observe([], at=self.clock[0] + 3600)            # from the future: a clock problem
        self.assertEqual(manager.check("checker")["status"], "cannot_read")
        old = self.clock[0] - 50
        self.source.observe([], at=old, valid=3600)
        manager.check("checker")
        self.assertEqual(self.items()[0]["status"], "verified")
        self.source.observe([{"key": "k1"}], at=old - 10, valid=3600)   # an older observation than one acted on
        self.assertEqual(manager.check("checker")["result"]["findings"], "unchanged")
        self.assertEqual(self.items()[0]["status"], "verified")
        state = manager.state("checker")
        self.assertLessEqual(driftsources._parse_iso(state["next_at"]), old + 3600 + 1, "re-checked when it expires")

    def test_a_malformed_finding_makes_the_observation_unreadable(self) -> None:
        manager = self.manager()
        self.source.observe([{"key": "k1"}])
        manager.check("checker")
        self.clock[0] += 700
        self.source.observe([{"title": "no key"}])
        self.assertEqual(manager.check("checker")["status"], "cannot_read")
        self.assertEqual(self.items()[0]["status"], "open")

    def test_backing_off(self) -> None:
        manager = self.manager()
        self.source.status, self.source.retry_after = 429, "7"
        result = manager.check("checker")
        self.assertEqual((result["status"], result["detail"]), ("busy", "check in progress (429)"))
        self.assertEqual(driftsources._parse_iso(result["next_at"]), int(self.clock[0]) + 7)
        self.source.status, self.source.retry_after = 401, None
        first = manager.check("checker")
        second = manager.check("checker")
        self.assertEqual((first["status"], first["failures"], second["failures"]), ("cannot_read", 2, 3))
        self.assertGreater(driftsources._parse_iso(second["next_at"]), driftsources._parse_iso(first["next_at"]))
        self.source.status = None
        self.source.observe([])
        ok = manager.check("checker")
        self.assertEqual((ok["status"], ok["failures"]), ("ok", 0))

    def test_profiles_produce_a_role_manifest_in_the_data_dir_only(self) -> None:
        manager = self.manager()
        self.source.observe(None, profiles=PROFILES)   # an inventory only: no statement about drift
        result = manager.check("checker")
        self.assertEqual((result["status"], result["result"]["findings"], result["result"]["profiles"]), ("ok", None, 2))
        self.assertEqual(self.items(), [])
        path = self.tmp / "data" / "drift-sources" / "checker" / "role-manifest.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        manifest = manager.manifest("checker")
        self.assertEqual(manifest["roles"][0], {"base_model_id": "flash-next", "provider_kind": "tabby",
                                                "provider_model_id": "Flash-Next-exl3", "route_kind": "local",
                                                "version_id": "v-41", "tool_packages": 3, "filter_packages": 1,
                                                "limitations": ["no vision"]})
        self.assertIn("never applies it", manifest["note"])
        written = sorted(str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*") if p.is_file())
        month = driftsources._iso(self.clock[0])[:7]
        self.assertEqual([p for p in written if "drift-sources" in p],
                         [f"data/drift-sources/checker/checks-{month}.jsonl",   # the check's record (§36 activity)
                          "data/drift-sources/checker/role-manifest.json", "data/drift-sources/checker/state.json"])
        self.assertEqual(manager.views()[0]["manifest"], True)


class ReviewRoundTest(DriftTestCase):
    """The review round's cases: out-of-range times, sub-second observedAt, an older inventory, a lost state."""

    def test_an_out_of_range_validity_is_cannot_read_saved_and_backed_off(self) -> None:
        manager = self.manager()
        for valid in (1e300, 400 * 86400, float("inf"), float("nan")):
            self.source.observe([{"key": "k1"}])
            self.source.payload["validForSeconds"] = valid
            result = manager.check("checker")
            self.assertEqual(result["status"], "cannot_read", valid)
            self.assertIn("validForSeconds", result["detail"])
        state = manager.state("checker")
        self.assertEqual((state["status"], state["failures"]), ("cannot_read", 4))
        self.assertGreater(driftsources._parse_iso(state["next_at"]), self.clock[0], "backed off, and saved")
        self.assertEqual(self.items(), [])

    def test_whatever_fails_in_a_check_its_state_is_saved_with_a_back_off(self) -> None:
        manager = self.manager()
        self.source.observe([{"key": "k1"}])

        def broken(*args: Any, **kwargs: Any) -> Any:
            raise OverflowError("timestamp out of range for platform time_t")

        original = driftsources.read_observation
        driftsources.read_observation = broken
        self.addCleanup(setattr, driftsources, "read_observation", original)
        result = manager.check("checker")
        self.assertEqual(result["status"], "error")
        self.assertIn("OverflowError", result["detail"])
        due = driftsources._parse_iso(manager.state("checker")["next_at"])
        self.assertGreaterEqual(due, self.clock[0] + driftsources.DOWN_BACKOFF - 1)
        before = len(self.source.requests)
        original_poll = driftsources.POLL_SECONDS
        driftsources.POLL_SECONDS = 0.05
        self.addCleanup(setattr, driftsources, "POLL_SECONDS", original_poll)
        manager.start()
        time.sleep(0.5)
        manager.stop(1)
        self.assertEqual(len(self.source.requests), before, "the background thread waits for the back-off")

    def test_an_observation_no_newer_to_the_microsecond_changes_nothing(self) -> None:
        manager = self.manager()
        self.clock[0] = datetime(2026, 10, 22, 9, 1, tzinfo=timezone.utc).timestamp()

        def serve(fraction: str, findings: list[dict]) -> dict:
            self.source.payload = {"schemaVersion": "1", "observedAt": f"2026-10-22T09:00:00.{fraction}Z",
                                   "validForSeconds": 3600, "findings": findings}
            return manager.check("checker")

        serve("200", [{"key": "k1", "title": "Context differs"}])
        self.assertEqual(serve("700", [])["result"]["closed"], [f"system:{self.items()[0]['id']}"])
        self.assertEqual(manager.state("checker")["acted_observed_at"], "2026-10-22T09:00:00.700000Z")
        again = serve("200", [{"key": "k1", "title": "Context differs"}])   # the older one, from a cache
        self.assertEqual(again["result"]["findings"], "unchanged")
        self.assertEqual(self.items()[0]["status"], "verified", "an older observation reopens nothing")
        self.assertEqual(serve("900", [{"key": "k1"}])["result"]["reopened"], [f"system:{self.items()[0]['id']}"])

    def test_an_older_inventory_never_replaces_the_role_manifest(self) -> None:
        manager = self.manager()
        self.source.observe([], at=self.clock[0] - 10, profiles=[dict(PROFILES[0], versionIdAtSnapshot="v-42")])
        manager.check("checker")
        self.source.observe([], at=self.clock[0] - 300, profiles=[dict(PROFILES[0], versionIdAtSnapshot="v-41")])
        result = manager.check("checker")
        self.assertEqual((result["result"]["profiles"], result["result"]["findings"]), ("unchanged", "unchanged"))
        self.assertEqual(manager.manifest("checker")["roles"][0]["version_id"], "v-42")
        self.source.observe([], at=self.clock[0] - 5, profiles=[dict(PROFILES[0], versionIdAtSnapshot="v-43")])
        self.assertEqual(manager.check("checker")["result"]["profiles"], 1)
        self.assertEqual(manager.manifest("checker")["roles"][0]["version_id"], "v-43")

    def test_a_lost_state_still_closes_open_items_and_reopens_proven_ones(self) -> None:
        manager = self.manager()
        state_file = self.tmp / "data" / "drift-sources" / "checker" / "state.json"
        self.source.observe([{"key": "k1", "title": "Context differs"}, {"key": "k2", "title": "Accepted"}])
        manager.check("checker")
        k1, k2 = sorted(self.items(), key=lambda item: item["source"]["key"])
        self.system.item_action(k2["id"], "drop", reason="intended")
        state_file.write_text("{not json")             # a restore, a reset, or a crash before the state was saved
        self.clock[0] += 700
        self.source.observe([])
        result = manager.check("checker")
        self.assertEqual(result["result"]["closed"], [f"system:{k1['id']}"], "proven from the tracker's own record")
        statuses = {item["source"]["key"]: item["status"] for item in self.items()}
        self.assertEqual(statuses, {"k1": "verified", "k2": "dropped"})
        state_file.unlink()
        self.clock[0] += 700
        self.source.observe([{"key": "k1", "title": "Context differs"}, {"key": "k2", "title": "Accepted"}])
        result = manager.check("checker")
        self.assertEqual(result["result"]["reopened"], [f"system:{k1['id']}"],
                         "it recurred after the factory proved it")
        statuses = {item["source"]["key"]: item["status"] for item in self.items()}
        self.assertEqual(statuses, {"k1": "open", "k2": "dropped"}, "a person's decision still stands")


# ---------------------------------------------------------------- the Factory and the HTTP surface


class DriftHttpTest(ServerTestCase):
    def test_the_routes_reach_the_factory(self) -> None:
        calls: list = []
        self.factory.drift_sources = lambda: [{"name": "checker", "status": "ok"}]   # type: ignore[attr-defined]
        self.factory.check_drift_source = lambda name: calls.append(name) or {"name": name, "status": "ok"}  # type: ignore
        self.factory.drift_manifest = lambda name: {"roles": []}   # type: ignore[attr-defined]
        self.factory.custody = lambda refresh=False: {"providers": [], "refresh": refresh}   # type: ignore
        self.assertEqual(self.request("GET", "/api/drift-sources").json(), {"sources": [{"name": "checker",
                                                                                          "status": "ok"}]})
        self.assertEqual(self.request("POST", "/api/drift-sources/checker/check", {}).json()["status"], "ok")
        self.assertEqual(calls, ["checker"])
        self.assertEqual(self.request("GET", "/api/drift-sources/checker/manifest").json(), {"manifest": {"roles": []}})
        self.assertEqual(self.request("GET", "/api/custody?refresh=1").json(), {"providers": [], "refresh": True})
        self.assertEqual(self.request("GET", "/api/drift-sources/Bad%20Name/manifest").status, 400)
        self.assertEqual(self.request("GET", "/api/custody", token=None).status, 401)


class FactoryDriftTest(DriftTestCase):
    def test_the_factory_checks_a_source_and_its_items_are_system_items(self) -> None:
        from swfactory.orchestrator import Factory

        config = Config(self.tmp / "config.json", {"data_dir": str(self.tmp / "data"),
                                                   "drift_sources": {"checker": self.entry()}})
        factory = Factory(config, self.bus)
        self.source.observe([{"key": "k1", "title": "Tabby context differs"}])
        result = factory.check_drift_source("checker")
        self.assertEqual(result["status"], "ok")
        rows = factory.drift_sources()
        self.assertEqual((rows[0]["name"], rows[0]["present"], rows[0]["token_from"]), ("checker", ["k1"], "file"))
        self.assertNotIn(SOURCE_TOKEN, json.dumps(rows))
        [item] = factory.items(scope="system")["items"]
        self.assertEqual((item["source"]["type"], item["status"]), ("drift:checker", "open"))
        with self.assertRaises(KeyError):
            factory.check_drift_source("nope")
        self.assertTrue(any(e.type == "drift.checked" for e in self.bus.since(0)))


if __name__ == "__main__":
    unittest.main()
