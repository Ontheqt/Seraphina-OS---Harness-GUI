"""``sf-browser mcp``: the harness-owned browser MCP server (BROWSER_AND_CODEX.md §2.1 to §2.5, §2.13).

It runs inside the namespace ``bin/sf-browser`` builds (``namespace.py``), speaks MCP on stdio to whichever agent
started it, and drives the pinned Playwright MCP through ``McpClient.call_result``. What it adds to Playwright:

- its own small tool set (``observe``, ``read``, ``navigate``, ``click``, ``fill``, ``press``, ``wait_for``,
  ``screenshot``, ``close``; under 4,000 characters of schemas), never Playwright's;
- observations with ids, built by the shim after every action, with every page-controlled string inside one
  ``<factory-data>`` block, cleaned and capped (``swfactory/framing.py``);
- the ref rule (K16): an older observation's ref is accepted for a read or an input while role, name and URL still
  hold; a click, a commit or a submitting key needs the latest observation;
- action classes from the role, then from what the browser sent (``browser_network_requests``, internally, read
  again once a dialog is dismissed): a link click or an input that sent anything but a GET or HEAD, or whose
  request list cannot be read, becomes a commit after the fact (K15), and a click on that link again is a commit;
- the action log: an intent line before every non-read action is sent, an outcome line after (``ok``,
  ``tool_error``, ``unknown``, ``not_sent``, ``cancelled``; a commit whose call failed or was stopped after it may
  have sent is ``unknown``), values and URLs only as keyed hashes;
- duplicate commits refused per page, a hold per origin after an unknown or reclassified commit on every action that
  may send (DESIGN §40);
- URL rules, origin rules after every action, one tab (popups closed), dialogs dismissed, downloads recorded and
  never kept, login pages never shown nor acted on, screenshots written by the shim and never sent to a model;
- typed errors (``{"error": code, ...}``), and Stop: ``notifications/cancelled``, stdin EOF and SIGTERM cancel the
  call in flight, which is recorded before the shim closes Playwright and exits.

A reader thread answers the handshake and routes cancellations while one worker thread runs the tool calls in order.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from .. import framing
from ..harness.mcp import CallResult, McpClient
from . import CHROME_PIN, PLAYWRIGHT_MCP_PIN, SHIM_VERSION, TOOLS, UNTRUSTED_LABEL
from . import rules as R
from .session_locks import (ACTION_LOG, FIXTURE_LOG, ActionLog, GrantError, StaleLockNumber, read_grant, utc_now)

PROTOCOL_VERSION = "2025-06-18"
DOWNLOADS_DIR = "/downloads"          # the --size capped tmpfs Playwright's output goes to (§2.8)
OBSERVATIONS_KEPT = 64
MARGIN_SECONDS = 5.0
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
EXPECT_CHECK = "text in the target's accessible subtree, not hidden by CSS"
TRUNCATED_HINT = ("the page is larger than the observation cap: this is the top of the tree to depth 3; call observe "
                  "with scope (a ref), depth or find to see the rest")
CANCEL_WAIT = 4.0                     # seconds the worker gets to record a cancelled call before the shim exits


def _int(value: Any, name: str, low: int, high: int, default: int | None = None) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value \
            or not low <= int(value) <= high:
        raise ShimError("tool_refused", argument=name, detail=f"a whole number from {low} to {high}")
    return int(value)


class ShimError(Exception):
    """A typed refusal or failure: the MCP error result's text is {"error": code, ...fields}."""

    def __init__(self, code: str, **fields: Any) -> None:
        super().__init__(code)
        self.code = code
        self.fields = fields

    def payload(self) -> dict:
        return {"error": self.code, **self.fields}


# ---------------------------------------------------------------- the tool set offered to a model (§2.2)

_REF = {"type": "string", "description": "a ref from an observation"}
_OBS = {"type": "string", "description": "the id of the observation the ref was read from"}


def tool_schemas() -> list[dict]:
    def tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
        return {"name": name, "description": description,
                "inputSchema": {"type": "object", "properties": properties, "required": required,
                                "additionalProperties": False}}
    return [
        tool("observe", "A fresh view of the page.", {"scope": _REF, "depth": {"type": "integer"},
                                                      "find": {"type": "string", "description": "literal text"}}, []),
        tool("read", "One element's text, capped.", {"observation": _OBS, "ref": _REF,
                                                    "max_chars": {"type": "integer"}}, ["observation", "ref"]),
        tool("navigate", "Open a URL on an allowed origin.", {"url": {"type": "string"}}, ["url"]),
        tool("click", "Click an element; expect checks a text afterwards.",
             {"observation": _OBS, "ref": _REF, "again": {"type": "boolean"},
              "expect": {"type": "object", "properties": {"text": {"type": "string"}, "ref": _REF},
                         "required": ["text"]}}, ["observation", "ref"]),
        tool("fill", "Fill fields.", {"observation": _OBS, "fields": {
            "type": "array", "items": {"type": "object", "properties": {"ref": _REF, "value": {"type": "string"}},
                                       "required": ["ref", "value"]}}}, ["observation", "fields"]),
        tool("press", "Press one key (Enter, Tab, Escape, arrows, Backspace, Delete or a character).",
             {"observation": _OBS, "key": {"type": "string"}, "ref": _REF, "again": {"type": "boolean"}},
             ["observation", "key"]),
        tool("wait_for", "Wait for a text to appear or go, at most 15 seconds.",
             {"text": {"type": "string"}, "gone": {"type": "string"}, "seconds": {"type": "integer"}}, ["seconds"]),
        tool("screenshot", "Save a screenshot for the owner (you do not see it).",
             {"name": {"type": "string", "description": "a short slug"}, "full_page": {"type": "boolean"}},
             ["name"]),
        tool("close", "End the browser session.", {}, []),
    ]


# ---------------------------------------------------------------- observations


@dataclass
class Observation:
    id: str
    number: int
    page: R.Page
    origin: str
    origin_ok: bool
    path: str
    page_key: str
    at: str
    events: list[dict] = field(default_factory=list)
    block_lines: list[str] = field(default_factory=list)
    truncated: bool = False
    login: bool = False
    tabs: int = 1

    @property
    def nodes(self) -> dict[str, R.Node]:
        return self.page.nodes


class Shim:
    """One session lock's browser. grant: the grant's path; session: {session_lock, lock_number, folder,
    number_file}. playwright: an argv that replaces the one built from the grant (tests' fake server)."""

    def __init__(self, grant_path: Path | str, session: dict, *, playwright: list[str] | None = None,
                 downloads: str = DOWNLOADS_DIR, record: str | None = None) -> None:
        self.grant_path = Path(grant_path)
        self.grant = read_grant(self.grant_path)
        self.session_lock = str(session["session_lock"])
        self.lock_number = int(session["lock_number"])
        self.folder = Path(session["folder"])
        self.log = ActionLog(self.folder / ACTION_LOG, self.lock_number, Path(session["number_file"]))
        self.limits = self.grant["limits"]
        self.read_origins = set(self.grant["origins"].get("read") or [])
        self.write_origins = set(self.grant["origins"].get("write") or [])
        self.fixture_origins = set(self.grant.get("fixture_origins") or [])
        self.settings = self.grant.get("profile_settings") or {}
        self.playwright_argv = playwright
        self.downloads = downloads
        self.key = os.urandom(32)          # the per-session HMAC key: in memory only (§2.5)
        self.started = time.monotonic()
        self.observations: dict[str, Observation] = {}
        self.latest: Observation | None = None
        self._obs_count = 0
        self._action_count = 0
        self._download_count = 0
        self.commits: list[dict] = []      # {page, action_id, outcome, after}
        self.holds: dict[str, dict] = {}   # origin -> {since_action_id, after}
        self.disguised: set[tuple[str, str, str]] = set()   # (page, role, name) of clicks reclassified as commits
        self.pw: McpClient | None = None
        self.fixture: subprocess.Popen | None = None
        self.cancel = threading.Event()    # set to cancel the call in flight
        self.closing = threading.Event()
        self._artifact_names: set[str] = set()
        # tests only: every Playwright call and its answer, appended as JSON lines (the recorded 0.0.83 responses)
        self.record = Path(record) if record else None
        self.preview_socket = session.get("preview_socket")   # a job's preview, in its sibling namespace (§2.9)
        self.relay = None

    # -- lifecycle -------------------------------------------------------------------

    def call_seconds(self) -> float:
        return float(self.limits.get("call_seconds") or 20)

    def commit_seconds(self) -> float:
        return min(float(self.limits.get("commit_seconds") or self.call_seconds()), self.call_seconds())

    def _start_fixture(self) -> None:
        site = (self.grant.get("paths") or {}).get("fixture")
        if not site or self.fixture is not None:
            return
        root = str(Path(__file__).resolve().parents[2])
        argv = [sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv.pop(1)); "
                "from swfactory.browser.fixture import main; sys.exit(main())", root, "site", "--dir", str(site),
                "--log", str(self.folder / FIXTURE_LOG),
                "--shop-delay", str(float(self.settings.get("shop_delay") or 10))]
        if self.settings.get("fixture_port"):
            argv += ["--port", str(int(self.settings["fixture_port"]))]
        self.fixture = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, start_new_session=False)
        line = self.fixture.stdout.readline().decode("utf-8", "replace") if self.fixture.stdout else ""
        if "listening" not in line:
            raise ShimError("worker_unavailable", reason="the fixture site did not start")

    def _playwright_argv(self) -> list[str]:
        if self.playwright_argv:
            return list(self.playwright_argv)
        paths = self.grant.get("paths") or {}
        node, package, chrome = paths.get("node"), paths.get("playwright_mcp"), paths.get("chrome")
        if not (node and package and chrome):
            raise ShimError("worker_unavailable", reason="the grant names no Node, Playwright MCP or Chrome")
        work = self.folder / "work"
        config = work / "playwright-config.json"
        config.write_text(json.dumps({"browser": {"launchOptions": {"args": ["--no-proxy-server"]}}}),
                          encoding="utf-8")
        argv = [str(node), str(Path(package) / "cli.js"), "--headless", "--isolated", "--no-webmcp",
                "--image-responses", "allow", "--output-dir", self.downloads,
                "--timeout-action", "5000", "--timeout-navigation", "15000",
                "--allowed-origins", ";".join(sorted(self.read_origins)), "--executable-path", str(chrome),
                "--caps", "testing", "--config", str(config)]
        if self.settings.get("chrome_sandbox") is not True:
            argv.append("--no-sandbox")   # O11: BC1's fixture pages, inside the shim's own namespace
        seed = self.settings.get("seed_cookie_seconds")
        if seed:
            from .fixture import storage_state

            origin = sorted(self.fixture_origins or self.read_origins)[0]
            argv += ["--storage-state", str(storage_state(work / "storage-state.json", origin, float(seed)))]
        return argv

    def start(self) -> None:
        """Start the fixture (when the grant names one), the relay to a preview (when there is one) and Playwright
        MCP. ShimError worker_unavailable."""
        self._start_fixture()
        if self.preview_socket and self.relay is None:
            from .fixture import relay_tcp_to_unix
            from .namespace import PREVIEW_PORT

            port = int((self.grant.get("preview") or {}).get("port") or PREVIEW_PORT)
            try:
                self.relay = relay_tcp_to_unix(port, str(self.preview_socket))
            except OSError as exc:
                raise ShimError("worker_unavailable", reason=f"the preview relay did not start ({exc.strerror})") \
                    from None
        argv = self._playwright_argv()
        cfg = SimpleNamespace(name="playwright", command=argv[0], args=argv[1:], env={}, cwd=None,
                              timeout=self.call_seconds(), tools=None, startup_timeout=60.0)
        self.pw = McpClient(cfg, self.folder / "work")
        try:
            self.pw.start()
        except Exception as exc:  # noqa: BLE001 - McpError or a bad path: the browser is unavailable
            self.pw = None
            raise ShimError("worker_unavailable", reason=framing.cap(str(exc), 300)) from None
        if self.record is not None:
            with open(self.record, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"tools": self.pw.tools()}, ensure_ascii=False) + "\n")

    def stop(self) -> None:
        if self.relay is not None:
            self.relay.close()
            self.relay = None
        if self.pw is not None:
            self.pw.close()
            self.pw = None
        if self.fixture is not None:
            self.fixture.terminate()
            try:
                self.fixture.wait(3)
            except subprocess.TimeoutExpired:
                self.fixture.kill()
            self.fixture = None

    # -- the hashes (§2.5) ------------------------------------------------------------

    def keyed(self, value: Any) -> str:
        text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
        return hmac.new(self.key, text.encode("utf-8"), hashlib.sha256).hexdigest()

    # -- Playwright calls -----------------------------------------------------------

    def _pw(self, tool: str, arguments: dict, timeout: float | None = None,
            cancel: threading.Event | None = None) -> CallResult:
        if self.pw is None:
            self.start()
        started = time.monotonic()
        result = self.pw.call_result(tool, arguments, timeout or self.call_seconds(), cancel)
        if self.record is not None:
            line = {"tool": tool, "arguments": arguments, "status": result.status, "content": result.content,
                    "seconds": round(time.monotonic() - started, 2)}
            with open(self.record, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=False) + "\n")
        return result

    def _inner(self, tool: str, arguments: dict) -> CallResult:
        """An internal call (the request list, tabs, snapshots, dialogs): ended at once when the session closes
        (stdin EOF, SIGTERM, a revoke), so the action's outcome line is written within the harness's close grace."""
        return self._pw(tool, arguments, None, self.closing)

    def _requests(self) -> list[dict] | None:
        """The browser's request list (internal, fixed arguments); None when it cannot be read."""
        result = self._inner("browser_network_requests", {"static": True})
        return R.requests_of(result.text) if result.status == "ok" else None

    def _tidy_tabs(self, events: list[dict]) -> int:
        """Rule 1: one tab. Every page the session did not open is closed (internally) and reported as a popup.
        Returns how many tabs are left open."""
        result = self._inner("browser_tabs", {"action": "list"})
        if result.status != "ok":
            return 0
        tabs = R.parse_response(result.text).tabs
        left = len(tabs)
        for tab in sorted(tabs, key=lambda t: -t["index"]):
            if tab["current"] or len(tabs) <= 1:
                continue
            closed = self._inner("browser_tabs", {"action": "close", "index": tab["index"]})
            left -= closed.status == "ok"
            events.append({"kind": "popup", "closed": closed.status == "ok",
                           "_text": f"popup: {framing.cap(tab['url'], R.URL_CHARS)}"})
        return left

    def _next_obs(self) -> tuple[str, int]:
        self._obs_count += 1
        return f"O-{self._obs_count}", self._obs_count

    def observe_page(self, events: list[dict] | None = None, scope: str | None = None, depth: int | None = None,
                     find: str | None = None) -> Observation:
        """Take a fresh observation: a snapshot (dialogs dismissed, extra tabs closed), parsed, origin-checked."""
        events = list(events or [])
        tabs = self._tidy_tabs(events)
        args: dict = {}
        if scope:
            args["target"] = scope
        if depth:
            args["depth"] = depth
        page = R.Page()
        blind = False   # one dismissal of a modal the response did not let us read
        for _ in range(3):
            result = self._inner("browser_snapshot", args)
            page = R.parse_response(result.text)
            if page.modal in ("alert", "confirm", "prompt"):
                kind = page.modal
                dismissed = self._inner("browser_handle_dialog", {"accept": False})
                events.append({"kind": "dialog", "type": kind, "dismissed": dismissed.status == "ok",
                               "_text": f"dialog ({kind}): {framing.cap(page.dialog_text, R.DIALOG_CHARS)}"})
                continue
            if result.status != "ok" and not page.has_snapshot and not blind and R.MODAL_ERROR in result.text:
                blind = True
                dismissed = self._inner("browser_handle_dialog", {"accept": False})
                events.append({"kind": "dialog", "type": "unknown", "dismissed": dismissed.status == "ok",
                               "_text": "dialog (unknown): its text could not be read"})
                continue
            if result.status != "ok" and not page.has_snapshot:
                # never Playwright's own text here: it can carry the page's (a dialog's message, a title)
                raise ShimError("worker_unavailable", reason="the browser returned no page")
            break
        url = page.url or R.BLANK
        try:
            parsed = R.parse_url(url)
        except R.UrlRefused:
            parsed = R.ParsedUrl("unparseable", "", "unparseable")
        ok = parsed.origin == R.BLANK or parsed.origin in self.read_origins
        obs_id, number = self._next_obs()
        lines = page.snapshot
        truncated = False
        if find:
            lines = R.find_lines(lines, find)
        if sum(len(line) + 1 for line in lines) > int(self.limits.get("page_chars") or 8000):
            lines, truncated = R.depth_cut(lines, 3), True
        login = ok and self._login_page(parsed)
        obs = Observation(obs_id, number, page, parsed.origin, ok, parsed.path, parsed.page, utc_now(), events,
                          lines, truncated, login, tabs)
        self.observations[obs_id] = obs
        while len(self.observations) > OBSERVATIONS_KEPT:
            self.observations.pop(next(iter(self.observations)))
        self.latest = obs
        return obs

    def _login_page(self, parsed: R.ParsedUrl) -> bool:
        login = self.settings.get("login")
        prefix = login.get("path") if isinstance(login, dict) else None
        return bool(prefix) and parsed.path.split("?", 1)[0].startswith(str(prefix))

    def render(self, obs: Observation) -> dict:
        """The observation as the model sees it: code-derived fields outside, page text inside one data block."""
        if not obs.origin_ok:
            raise ShimError("origin_not_allowed", origin=framing.frame("browser-page", obs.origin, R.URL_CHARS),
                            allowed=sorted(self.read_origins), observation=obs.id)
        if obs.login:
            self.log.write({"kind": "login_required", "origin": obs.origin, "observation": obs.id})
            raise ShimError("login_required", origin=obs.origin, observation=obs.id)
        head = [f"url: {framing.cap(obs.page.url or R.BLANK, R.URL_CHARS)}",
                f"title: {framing.cap(obs.page.title or '', R.TITLE_CHARS)}"]
        head += [event["_text"] for event in obs.events if event.get("_text")]
        body = "\n".join(head + obs.block_lines)
        cap = int(self.limits.get("page_chars") or 8000) + 1200
        events = [{k: v for k, v in event.items() if not k.startswith("_")} for event in obs.events]
        out = {"observation": obs.id, "session_lock": self.session_lock, "lock_number": self.lock_number,
               "at": obs.at, "origin": obs.origin, "origin_ok": True, "modal": obs.page.modal,
               "events": events, "tabs": obs.tabs, "refs": len(obs.nodes), "truncated": obs.truncated,
               "page": framing.frame("browser-page", body, cap)}
        if obs.truncated:
            out["hint"] = TRUNCATED_HINT
        return out

    # -- refs and checks --------------------------------------------------------------

    def _check_session(self) -> None:
        try:
            grant = read_grant(self.grant_path)
        except GrantError:
            grant = {"revoked": {"at": utc_now()}}
        if grant.get("revoked"):
            revoked = grant["revoked"]
            raise ShimError("session_lock_revoked", at=revoked.get("at") if isinstance(revoked, dict) else utc_now())
        if time.monotonic() - self.started > float(self.limits.get("max_minutes") or 60) * 60:
            raise ShimError("session_lock_expired", at=utc_now())

    def _current(self) -> Observation:
        return self.latest if self.latest is not None else self.observe_page()

    def _stale(self) -> ShimError:
        obs = self.observe_page()
        try:
            rendered = self.render(obs)
        except ShimError as exc:
            rendered = exc.payload()
        return ShimError("stale_ref", observation=rendered)

    def target(self, cited: Any, ref: Any, latest_only: bool) -> R.Node:
        """K16: the ref of cited, checked against the latest observation. latest_only (a click, a commit, a
        submitting key): cited must be the latest observation the shim returned."""
        if not isinstance(ref, str) or not R.REF_RE.match(ref):
            raise ShimError("tool_refused", argument="ref", detail="a ref from an observation, such as e7")
        if not isinstance(cited, str):
            raise ShimError("tool_refused", argument="observation", detail="the observation id the ref came from")
        latest = self._current()
        if not latest.origin_ok or latest.login:
            self.render(latest)   # raises origin_not_allowed or login_required before any ref is looked at
        node = latest.nodes.get(ref)
        if node is None or (latest_only and cited != latest.id):
            raise self._stale()
        if cited != latest.id:
            old = self.observations.get(cited)
            before = old.nodes.get(ref) if old is not None else None
            if before is None or (before.role, before.name) != (node.role, node.name) or old.path != latest.path:
                raise self._stale()
        return node

    def _origin_for_action(self, write: bool) -> Observation:
        """The latest observation, for an action on its page: never on a page off the origins or a login page (the
        owner logs in himself, §2.7; navigating away stays allowed), and an input or commit on a write origin."""
        latest = self._current()
        if not latest.origin_ok or latest.login:
            self.render(latest)   # raises origin_not_allowed or login_required
        if write and latest.origin not in self.write_origins:
            raise ShimError("write_origin_required", origin=latest.origin)
        return latest

    def _cited_number(self, cited: Any) -> int:
        """The number of the observation an action cites (0 when it names none the shim holds)."""
        obs = self.observations.get(cited) if isinstance(cited, str) else None
        return obs.number if obs is not None else 0

    def _hold_check(self, latest: Observation, cited_number: int) -> None:
        """DESIGN §40: after an unknown or reclassified commit, every further action that may send on that origin
        (a click of any role, an input, a key) is held until the caller cites an observation taken after the
        action's own; a link or a field can be the disguised commit itself (K15)."""
        hold = self.holds.get(latest.origin)
        if hold is not None:
            if cited_number <= hold["after"]:
                raise ShimError("commit_hold", since_action_id=hold["since_action_id"])
            self.holds.pop(latest.origin, None)   # cleared by a fresh observation the caller took after it

    def _commit_checks(self, latest: Observation, cited_number: int, again: bool) -> None:
        self._hold_check(latest, cited_number)
        prior = [entry for entry in self.commits if entry["page"] == latest.page_key]
        if prior and not (again is True and cited_number >= prior[-1]["after"]):
            raise ShimError("duplicate_commit", previous_action_id=prior[-1]["action_id"],
                            outcome=prior[-1]["outcome"])

    def _refuse(self, tool: str, error: ShimError) -> None:
        if error.code in ("tool_refused", "stale_ref", "duplicate_commit", "commit_hold", "write_origin_required",
                          "url_refused", "origin_not_allowed"):
            try:
                self.log.write({"kind": "refusal", "tool": tool, "code": error.code})
            except (OSError, StaleLockNumber):
                pass

    # -- the action path --------------------------------------------------------------

    def _next_action(self) -> str:
        self._action_count += 1
        return f"A-{self._action_count}"

    def act(self, tool: str, klass: str, node: R.Node | None, cited: Observation | None, calls: list[tuple[str, dict]],
            *, timeout: float, values: Any = None, expect: dict | None = None, url: str | None = None) -> dict:
        """One non-read action: intent, the call(s), the effect check, a fresh observation, the outcome line."""
        latest = self._current()
        action_id = self._next_action()
        target = None
        if node is not None:
            target = {"ref": node.ref, "role": node.role, "name_hmac": self.keyed(node.name),
                      "observation": cited.id if cited else latest.id,
                      "name_block": framing.frame("browser-page", node.name, R.NAME_CHARS)}
        intent = {"class": klass, "tool": tool, "target": target, "origin": latest.origin,
                  "path_hash": self.keyed(url or latest.page.url or ""), "observation_before": latest.id}
        if values is not None:
            intent["values_hmac"] = self.keyed(values)
        try:
            self.log.intent(action_id, **intent)
        except StaleLockNumber as exc:
            raise ShimError("session_lock_expired", at=utc_now(), detail=str(exc)) from None
        before = self._requests()
        result: CallResult | None = None
        downloads: list[dict] = []
        went = False   # whether any of the calls left for Playwright
        for name, arguments in calls:
            result = self._pw(name, arguments, timeout, self.cancel)
            went = went or result.sent
            downloads += R.parse_response(result.text).downloads
            if result.status != "ok":
                break
        assert result is not None
        outcome = {"ok": "ok", "tool_error": "tool_error", "timeout_sent": "unknown",
                   "timeout_unsent": "not_sent"}.get(result.status)
        if result.status == "transport_failed":
            outcome = "unknown" if result.sent else "not_sent"
        elif result.status == "cancelled":
            outcome = "unknown" if (result.sent and klass == "commit") else "cancelled"
        stale = outcome == "tool_error" and "not found in the current page snapshot" in result.text
        acted = went and outcome != "not_sent" and not stale   # the call may have done something on the page
        after = self._requests() if outcome != "not_sent" and not self.closing.is_set() else None
        events: list[dict] = []
        for item in downloads:
            self._download_count += 1
            size = None
            try:
                size = os.stat(os.path.join(self.downloads, os.path.basename(item["path"]))).st_size
            except OSError:
                pass
            events.append({"kind": "download", "id": f"D-{self._download_count}", "bytes": size,
                           "_text": f"download D-{self._download_count}: {framing.cap(item['name'], R.FILE_NAME_CHARS)}"})
        obs: Observation | None = None
        if not self.closing.is_set():
            try:
                obs = self.observe_page(events)
            except ShimError:
                obs = None
        # The effect check (K15). While a dialog is open 0.0.83 refuses the request list, and a dismissed dialog's
        # handler can send something itself: so the list is read again once the observation has dismissed it.
        dismissed = obs is not None and any(event.get("kind") == "dialog" for event in obs.events)
        if (after is None or dismissed) and outcome != "not_sent":
            # Once the session closes (Stop, a revoke, run end) no further call is made, so the outcome line is
            # written within the close grace: the list it would have read again is a list not read.
            after = self._requests() if not self.closing.is_set() else None
        # A list that still cannot be read after a call that left is an unknown effect, never "nothing was sent";
        # so is one the closing session cut short or never let the shim read: by K15 a link or an input then counts
        # as sent. A typed navigate {url} is the model's own load of a URL the shim checked, and the owner's Stop
        # while its page loads is no commit: it keeps its outcome, its list still recorded as not read.
        unread = after is None and acted
        stopped_load = unread and tool == "navigate" and self.closing.is_set()
        sent = R.new_requests(before or [], after) if after is not None else []
        effect = (unread and not stopped_load) or R.sent_something(sent)
        final = klass
        reclassified = klass in ("navigate", "input") and effect
        if reclassified:
            final = "commit"
        if final == "commit" and acted and effect and outcome in ("tool_error", "cancelled"):
            outcome = "unknown"   # §2.5: the call failed or was stopped, but the commit may have gone out
        elif reclassified and unread and obs is None and outcome == "ok":
            outcome = "unknown"   # nothing after the call was read (no list, no page): what it did is not known
        expect_record = None
        if expect is not None and obs is not None and outcome == "ok":
            expect_record = self._expect(obs, expect, node)
        response: dict = {"tool_status": result.status,
                          "requests": None if unread else [{"method": entry["method"], "status": entry["status"]}
                                                           for entry in sent]}
        if unread:
            response["requests_unread"] = True
        line = {"class": final, "reclassified": reclassified, "observation_after": obs.id if obs else None,
                "response": response, "url_after_hmac": self.keyed(obs.page.url or "") if obs else None,
                "origin_after": obs.origin if obs else None}
        if expect_record is not None:
            line["expect"] = expect_record
        if outcome in ("tool_error", "not_sent", "cancelled"):
            line["error"] = self._error_words(result.text, values)
        try:
            self.log.outcome(action_id, outcome, **line)
        except StaleLockNumber:
            pass
        after_number = obs.number if obs else self._obs_count
        if final == "commit" and outcome in ("ok", "unknown"):
            self.commits.append({"page": latest.page_key, "action_id": action_id, "outcome": outcome,
                                 "after": after_number})
        if final == "commit" and (outcome == "unknown" or reclassified):
            self.holds[latest.origin] = {"since_action_id": action_id, "after": after_number}
        if reclassified and tool == "click" and node is not None:   # a click on it again is a commit (§2.5)
            self.disguised.add((latest.page_key, node.role, node.name))
        if stale:
            raise self._stale()
        payload: dict = {"action_id": action_id, "class": final, "reclassified": reclassified, "outcome": outcome}
        if reclassified:
            why = "the shim could not read what the page sent: this counts as a commit" if unread else \
                "the page sent data: this was a commit"
            payload["note"] = why + ", and the origin is on hold until you observe"
        if obs is not None:
            try:
                payload["observation"] = self.render(obs)
            except ShimError as exc:
                payload["observation"] = exc.payload()
        if expect_record is not None:
            payload["expect"] = expect_record
        if outcome == "unknown":
            raise ShimError("outcome_unknown", action_id=action_id, observation=payload.get("observation"))
        refused = payload.get("observation") if isinstance(payload.get("observation"), dict) else {}
        if refused.get("error") in ("origin_not_allowed", "login_required"):
            raise ShimError(refused["error"], action_id=action_id,
                            **{k: v for k, v in refused.items() if k != "error"})
        if outcome == "not_sent":
            raise ShimError("not_sent", action_id=action_id)
        if outcome == "cancelled":
            raise ShimError("cancelled", action_id=action_id)
        if outcome == "tool_error":
            payload["error"] = framing.frame("browser-page", result.text, 300)
        return payload

    @staticmethod
    def _error_words(text: str, values: Any) -> str:
        """The first line of Playwright's error, without any value the action typed (the log keeps no value)."""
        words = (R.parse_response(text).error or text or "").strip().split("\n")[0]
        for value in values if isinstance(values, list) else [values]:
            if isinstance(value, str) and value:
                words = words.replace(value, "***")
        return framing.cap(words, 200)

    def _expect(self, obs: Observation, expect: dict, node: R.Node | None) -> dict:
        text = expect.get("text")
        scope = expect.get("ref") or (node.ref if node is not None else None)
        record = {"scope": scope, "matched": False, "check": EXPECT_CHECK}
        target = obs.nodes.get(scope) if isinstance(scope, str) else None
        if not isinstance(text, str) or not text or target is None:
            return record
        inside = text in R.element_text(obs.page.snapshot, target)
        if inside:
            visible = self._inner("browser_verify_text_visible", {"text": text})
            record["matched"] = visible.status == "ok"
        return record

    # -- the tools ----------------------------------------------------------------------

    def tool_observe(self, args: dict) -> dict:
        scope = args.get("scope")
        find = args.get("find")
        depth = _int(args.get("depth"), "depth", 1, 30)
        if find is not None and (not isinstance(find, str) or not find or len(find) > 200):
            raise ShimError("tool_refused", argument="find", detail="a literal text of at most 200 characters")
        if scope is not None:
            latest = self._current()
            if not isinstance(scope, str) or scope not in latest.nodes:
                raise self._stale()
        return self.render(self.observe_page(scope=scope, depth=depth, find=find))

    def tool_read(self, args: dict) -> dict:
        limit = _int(args.get("max_chars"), "max_chars", 1, 500, 200)
        node = self.target(args.get("observation"), args.get("ref"), latest_only=False)
        latest = self.latest
        self.render(latest)   # an origin or login refusal applies to reads too
        value = R.element_text(latest.page.snapshot, node)
        return {"value": framing.frame("browser-page", value, limit), "ref": node.ref, "observation": latest.id,
                "chars": min(len(framing.cap(value, limit)), limit)}

    def tool_navigate(self, args: dict) -> dict:
        url = args.get("url")
        try:
            parsed = R.parse_url(url)
        except R.UrlRefused as exc:
            raise ShimError("url_refused", reason=exc.reason) from None
        if parsed.origin != R.BLANK and parsed.origin not in self.read_origins:
            raise ShimError("origin_not_allowed", origin=framing.frame("browser-page", parsed.origin, R.URL_CHARS),
                            allowed=sorted(self.read_origins))
        self._current()
        return self.act("navigate", "navigate", None, None, [("browser_navigate", {"url": url})],
                        timeout=self.call_seconds(), url=url)

    def tool_click(self, args: dict) -> dict:
        node = self.target(args.get("observation"), args.get("ref"), latest_only=True)
        klass = R.click_class(node.role)
        if (self._current().page_key, node.role, node.name) in self.disguised:
            klass = "commit"   # this link already sent data on this page (K15): the duplicate rule applies
        latest = self._origin_for_action(write=klass == "commit")
        expect = args.get("expect")
        if expect is not None and (not isinstance(expect, dict) or not isinstance(expect.get("text"), str)
                                   or len(expect["text"]) > 200):
            raise ShimError("tool_refused", argument="expect", detail="{text, ref?} with a text of at most 200")
        if klass == "commit":
            self._commit_checks(latest, latest.number, args.get("again") is True)
        else:
            self._hold_check(latest, latest.number)
        timeout = self.commit_seconds() if klass == "commit" else self.call_seconds()
        return self.act("click", klass, node, latest, [("browser_click", {"target": node.ref})], timeout=timeout,
                        expect=expect)

    def tool_fill(self, args: dict) -> dict:
        fields = args.get("fields")
        if not isinstance(fields, list) or not 1 <= len(fields) <= 20:
            raise ShimError("tool_refused", argument="fields", detail="1 to 20 fields")
        cited = args.get("observation")
        nodes = []
        for entry in fields:
            if not isinstance(entry, dict) or not isinstance(entry.get("value"), str) or len(entry["value"]) > 2000:
                raise ShimError("tool_refused", argument="fields", detail="each {ref, value} with a value of at most "
                                                                          "2,000 characters")
            node = self.target(cited, entry.get("ref"), latest_only=False)
            if node.role not in R.FILL_TYPES:
                raise ShimError("tool_refused", argument="ref", detail=f"{entry.get('ref')} is not a field")
            nodes.append((node, entry["value"]))
        latest = self._origin_for_action(write=True)
        self._hold_check(latest, self._cited_number(cited))
        form = [{"target": node.ref, "name": node.role, "type": R.FILL_TYPES[node.role], "value": value}
                for node, value in nodes]
        return self.act("fill", "input", nodes[0][0] if len(nodes) == 1 else None, latest,
                        [("browser_fill_form", {"fields": form})], timeout=self.call_seconds(),
                        values=[value for _, value in nodes])

    def tool_press(self, args: dict) -> dict:
        key = args.get("key")
        if not R.valid_key(key):
            raise ShimError("tool_refused", argument="key", detail="Enter, Tab, Escape, an arrow, Backspace, Delete "
                                                                   "or one character; no modifiers")
        klass = R.press_class(key)
        ref = args.get("ref")
        node = None
        if ref is not None:
            node = self.target(args.get("observation"), ref, latest_only=klass == "commit")
            if node.role not in R.EDITABLE_ROLES:
                raise ShimError("tool_refused", argument="ref", detail=f"{ref} is not an editable field")
        elif klass == "commit" and args.get("observation") != self._current().id:
            raise self._stale()
        latest = self._origin_for_action(write=True)
        if klass == "commit":
            self._commit_checks(latest, latest.number, args.get("again") is True)
        else:
            self._hold_check(latest, self._cited_number(args.get("observation")))
        calls =([("browser_click", {"target": node.ref})] if node is not None else []) + \
            [("browser_press_key", {"key": key})]
        timeout = self.commit_seconds() if klass == "commit" else self.call_seconds()
        return self.act("press", klass, node, latest, calls, timeout=timeout, values=key)

    def tool_wait_for(self, args: dict) -> dict:
        seconds = _int(args.get("seconds"), "seconds", 1, 15)
        text, gone = args.get("text"), args.get("gone")
        for name, value in (("text", text), ("gone", gone)):
            if value is not None and (not isinstance(value, str) or not value or len(value) > 200):
                raise ShimError("tool_refused", argument=name, detail="a text of at most 200 characters")
        arguments: dict = {"time": seconds} if text is None and gone is None else (
            {"text": text} if text is not None else {"textGone": gone})
        self._current()
        result = self._pw("browser_wait_for", arguments, seconds + MARGIN_SECONDS, self.cancel)
        if result.status == "cancelled":
            try:
                self.log.write({"kind": "cancelled", "tool": "wait_for"})
            except (OSError, StaleLockNumber):
                pass
            raise ShimError("cancelled", action_id=None)
        waited = "done" if result.status == "ok" else "timed_out"
        out = self.render(self.observe_page())
        return {"waited": waited, "observation": out}

    def tool_screenshot(self, args: dict) -> dict:
        name = args.get("name")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise ShimError("tool_refused", argument="name", detail="a slug of at most 40 characters: a-z, 0-9, -")
        latest = self._current()
        self.render(latest)   # never of a page outside the origins, or a login page
        arguments: dict = {"scale": "css", "type": "png"}
        if args.get("full_page") is True:
            arguments["fullPage"] = True
        result = self._pw("browser_take_screenshot", arguments, self.call_seconds(), self.cancel)
        image = next((part for part in result.content if isinstance(part, dict) and part.get("type") == "image"
                      and part.get("mimeType") == "image/png"), None)
        if result.status != "ok" or image is None:
            raise ShimError("worker_unavailable", reason="no screenshot came back")
        try:
            data = base64.b64decode(str(image.get("data") or ""), validate=True)
        except ValueError:
            raise ShimError("worker_unavailable", reason="the screenshot was not valid base64") from None
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ShimError("worker_unavailable", reason="the screenshot was not a PNG")
        artifacts = self.folder / "artifacts"
        stem, number = name, 1
        while True:
            filename = f"{stem}.png" if number == 1 else f"{stem}-{number}.png"
            try:
                fd = os.open(artifacts / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                break
            except FileExistsError:
                number += 1
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        digest = hashlib.sha256(data).hexdigest()
        try:
            self.log.write({"kind": "artifact", "artifact": filename, "sha256": digest, "bytes": len(data),
                            "observation": latest.id, "origin": latest.origin})
        except StaleLockNumber:
            pass
        return {"artifact": filename, "sha256": digest, "bytes": len(data),
                "note": "saved for the owner; the image is not sent to you"}

    def tool_close(self, args: dict) -> dict:
        if self.pw is not None:
            self._pw("browser_close", {})
        try:
            self.log.write({"kind": "closed"})
        except (OSError, StaleLockNumber):
            pass
        self.closing.set()
        return {"closed": True, "session_lock": self.session_lock}

    def call(self, name: str, args: Any) -> tuple[dict, bool]:
        """One tools/call: (payload, is_error)."""
        if name not in TOOLS:
            return ShimError("tool_refused", argument="name", detail=f"unknown tool {framing.cap(name, 60)}").payload(), True
        if not isinstance(args, dict):
            return ShimError("tool_refused", argument="arguments").payload(), True
        try:
            self._check_session()
            if self.pw is None:
                self.start()
            payload = getattr(self, f"tool_{name}")(args)
            return payload, False
        except ShimError as exc:
            self._refuse(name, exc)
            if exc.code in ("session_lock_revoked", "session_lock_expired"):
                self.closing.set()
            return exc.payload(), True


# ---------------------------------------------------------------- the stdio server


class StdioServer:
    """The MCP side: a reader thread for the handshake and cancellations, a worker thread for tool calls."""

    def __init__(self, handle: Callable[[str, Any], tuple[dict, bool]], *, on_close: Callable[[], None] = lambda: None,
                 shim: Shim | None = None, instructions: str = "") -> None:
        self.handle = handle
        self.on_close = on_close
        self.shim = shim
        self.instructions = instructions
        self.out_lock = threading.Lock()
        self.calls: queue.Queue = queue.Queue()
        self.current: Any = None
        self.done = threading.Event()

    def send(self, message: dict) -> None:
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        with self.out_lock:
            try:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            except (OSError, ValueError):
                pass

    def _answer(self, message: dict) -> None:
        method, request_id = message.get("method"), message.get("id")
        if method == "initialize":
            self.send({"jsonrpc": "2.0", "id": request_id, "result": {
                "protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "sf-browser", "version": SHIM_VERSION},
                "instructions": self.instructions or UNTRUSTED_LABEL}})
        elif method == "tools/list":
            self.send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": tool_schemas()}})
        elif method == "ping":
            self.send({"jsonrpc": "2.0", "id": request_id, "result": {}})
        elif method == "tools/call":
            self.calls.put(message)
        elif request_id is not None:
            self.send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}})

    def stop_now(self, why: str) -> None:
        """stdin EOF, SIGTERM or a revoke: cancel the call in flight and let the worker record it."""
        if self.shim is not None:
            self.shim.closing.set()
            self.shim.cancel.set()
        self.calls.put(None)

    def read(self) -> None:
        for raw in iter(sys.stdin.buffer.readline, b""):
            try:
                message = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("method") == "notifications/cancelled":
                params = message.get("params") if isinstance(message.get("params"), dict) else {}
                current = self.current
                if current is not None and params.get("requestId") == current.get("id") and self.shim is not None:
                    self.shim.cancel.set()
                continue
            if "method" in message:
                self._answer(message)
        self.stop_now("stdin closed")

    def work(self) -> None:
        while True:
            message = self.calls.get()
            if message is None:
                break
            self.current = message
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            if self.shim is not None:
                self.shim.cancel.clear()   # before the check: stop_now sets closing first, then cancel
            if self.shim is not None and self.shim.closing.is_set():
                # Stop, a revoke or run end came while this call waited in the queue: it is never run, so nothing
                # reaches the browser after the owner stopped the session
                payload, is_error = ShimError("cancelled", action_id=None).payload(), True
            else:
                try:
                    payload, is_error = self.handle(str(params.get("name") or ""), params.get("arguments") or {})
                except Exception as exc:  # noqa: BLE001 - a shim bug answers an error, never kills the session
                    payload, is_error = {"error": "worker_unavailable", "reason": f"{type(exc).__name__}"}, True
            self.current = None
            self.send({"jsonrpc": "2.0", "id": message.get("id"), "result": {
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "isError": is_error}})
            if self.shim is not None and self.shim.closing.is_set():
                break
        self.done.set()

    def run(self) -> int:
        reader = threading.Thread(target=self.read, name="shim-reader", daemon=True)
        worker = threading.Thread(target=self.work, name="shim-worker", daemon=True)
        signal.signal(signal.SIGTERM, lambda *_: self.stop_now("SIGTERM"))
        reader.start()
        worker.start()
        while not self.done.wait(0.2):
            if not reader.is_alive() and self.calls.empty() and self.current is None:
                break
        worker.join(CANCEL_WAIT)
        self.on_close()
        return 0


def refusing(code: str, **fields: Any) -> int:
    """Serve MCP and answer every tool call with one typed error (no_grant, session_lock_busy, worker_unavailable):
    the run sees why it has no browser, in the browser's own words."""
    payload = {"error": code, **fields}
    server = StdioServer(lambda name, args: (payload, True))
    return server.run()


def main(argv: list[str] | None = None) -> int:
    """shim <grant> <session json>: run inside the namespace bin/sf-browser built. The session json is
    {session_lock, lock_number, folder, number_file, playwright?}."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        return refusing("no_grant")
    try:
        session = json.loads(argv[1])
        shim = Shim(argv[0], session, playwright=session.get("playwright"),
                    downloads=session.get("downloads") or DOWNLOADS_DIR, record=session.get("record"))
    except (GrantError, ValueError, KeyError, TypeError):
        return refusing("no_grant")
    os.umask(0o077)
    server = StdioServer(shim.call, on_close=shim.stop, shim=shim)
    return server.run()


def worker_versions(grant: dict) -> dict:
    """The worker line of a receipt: the shim, Playwright MCP and Chrome versions the grant's runtime recorded."""
    worker = grant.get("worker") if isinstance(grant.get("worker"), dict) else {}
    return {"shim": SHIM_VERSION, "playwright_mcp": worker.get("playwright_mcp") or PLAYWRIGHT_MCP_PIN,
            "browser": worker.get("browser") or f"chrome-headless-shell {CHROME_PIN} (pin, not read)"}


if __name__ == "__main__":
    sys.exit(main())
