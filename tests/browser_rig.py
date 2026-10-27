"""The browser adapter's test rig (BROWSER_AND_CODEX.md §4.6): grants, the shim started directly or through
bin/sf-browser, a driver that calls the shim's tools as a model would, the scenarios the acceptance tests run, and
the recorder that writes the real Playwright MCP 0.0.83's responses to tests/fixtures/browser/playwright-0.0.83/.

Live tests need the harness's own runtime, found from the environment (never from PATH):

- ``SF_BROWSER_RUNTIME``: a directory laid out as browser.runtime.dir (node/bin/node,
  playwright-mcp/node_modules/@playwright/mcp, and the Chrome build), or
- ``SF_BROWSER_NODE``, ``SF_BROWSER_PLAYWRIGHT_MCP`` (the @playwright/mcp package folder) and ``SF_BROWSER_CHROME``.

They skip when these are absent, as on a fresh checkout. ``SF_BROWSER_RECORD=1`` rewrites the recordings from a live
run (after a pin upgrade); otherwise the contract test compares a live run with the recordings.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from swfactory.browser import namespace as NS
from swfactory.browser.fixture import FIXTURE_ORIGIN
from swfactory.browser.runs import FIXTURE_SITE, RUNTIME_LAYOUT
from swfactory.browser.session_locks import take_session_lock, write_grant
from swfactory.harness.mcp import McpClient
from swfactory.harness.sandbox import bwrap_available

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SF_BROWSER = ROOT / "bin" / "sf-browser"
FAKE = HERE / "fake_playwright.py"
RECORDINGS = HERE / "fixtures" / "browser" / "playwright-0.0.83"
# A 1x1 PNG stands in for every recorded screenshot (the recordings stay small; the shim checks the PNG header).
TINY_PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
_TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z")


def runtime() -> dict | None:
    """{node, playwright_mcp, chrome} of the harness's own runtime for the live tests, or None (they skip)."""
    root = os.environ.get("SF_BROWSER_RUNTIME")
    found = {}
    for key, env in (("node", "SF_BROWSER_NODE"), ("playwright_mcp", "SF_BROWSER_PLAYWRIGHT_MCP"),
                     ("chrome", "SF_BROWSER_CHROME")):
        value = os.environ.get(env) or (str(Path(root) / RUNTIME_LAYOUT[key]) if root else None)
        if not value or not os.path.exists(value):
            return None
        found[key] = value
    return found


def live_reason() -> str | None:
    """Why the live tests skip here, or None when they can run."""
    if not bwrap_available():
        return "bubblewrap is not usable here"
    if runtime() is None:
        return "the harness's own browser runtime is not installed (SF_BROWSER_RUNTIME, DESIGN §43)"
    return None


def make_grant(data_dir: Path, *, profile: str = "fixture", origins: list[str] | None = None,
               settings: dict | None = None, limits: dict | None = None, kind: str = "test", ref: str = "rig",
               run: str = "01-turn", fake: Path | None = None, record: bool = False, live: bool = True,
               fake_log: Path | None = None, write: bool = True) -> Path:
    """A grant as the harness core writes one, for the rig: the live runtime, or a fake Playwright replaying a
    recording (no fixture site then: nothing would reach it)."""
    origins = origins or [FIXTURE_ORIGIN]
    paths: dict[str, Any] = {}
    if fake is not None:
        argv = [os.path.realpath(sys.executable), str(FAKE), str(fake)]
        if fake_log is not None:
            argv += ["--log", str(fake_log)]
        paths["playwright_argv"] = argv
    elif live:
        paths.update(runtime() or {})
        paths["fixture"] = str(FIXTURE_SITE)
    if record:
        paths["record"] = True
    _, path = write_grant(data_dir, locked_by={"kind": kind, "ref": ref, "run": run, "agent": "kate-browser"},
                          profile=profile, origins={"read": origins, "write": origins if write else []},
                          fixture_origins=origins,
                          paths=paths, limits=limits, profile_settings=settings or {},
                          worker={"playwright_mcp": "0.0.83", "browser": "rig"})
    return path


def client_config(argv: list[str], timeout: float = 60.0) -> SimpleNamespace:
    return SimpleNamespace(name="browser", command=argv[0], args=argv[1:], env={}, cwd=None, timeout=timeout,
                           tools=None, startup_timeout=60.0)


def start_launcher(grant: Path, cwd: Path, log: Callable[[str], None] | None = None) -> McpClient:
    """bin/sf-browser mcp <grant>, as the harness starts it (the session lock, then bwrap)."""
    client = McpClient(client_config([str(SF_BROWSER), "mcp", str(grant)]), cwd, log=log)
    client.start()
    return client


def start_direct(grant: Path, data_dir: Path, cwd: Path, log: Callable[[str], None] | None = None
                 ) -> tuple[McpClient, Any]:
    """The shim without a namespace (unit tests on any machine): the session lock is taken in this process, as
    bin/sf-browser takes it. Returns (client, the session lock); close both."""
    from swfactory.browser.session_locks import read_grant

    grant_data = read_grant(grant)
    lock = take_session_lock(data_dir, grant_data)
    session = {"session_lock": lock.session_lock, "lock_number": lock.lock_number, "folder": str(lock.folder),
               "number_file": str(lock.number_file), "downloads": str(lock.folder / "work")}
    paths = grant_data.get("paths") or {}
    if paths.get("playwright_argv"):
        session["playwright"] = paths["playwright_argv"]
    if paths.get("record"):
        session["record"] = str(lock.folder / "playwright-calls.jsonl")
    argv = [sys.executable, "-I", "-c", NS.BOOTSTRAP, str(ROOT), str(grant), json.dumps(session)]
    client = McpClient(client_config(argv), cwd, log=log)
    client.start()
    return client, lock


# ---------------------------------------------------------------- the driver and the scenarios


class Step(SimpleNamespace):
    """One tool call: tool, args, payload (the parsed JSON), error (True for an error result)."""

    @property
    def code(self) -> str | None:
        return self.payload.get("error") if self.error else None

    @property
    def obs(self) -> dict:
        """The observation this step returned (an action's, an error's, or observe's own)."""
        if "page" in self.payload and "observation" in self.payload and isinstance(self.payload["observation"], str):
            return self.payload
        inner = self.payload.get("observation")
        return inner if isinstance(inner, dict) else {}


class Driver:
    """Calls the shim's tools as a model would, and keeps every step."""

    def __init__(self, client: McpClient, timeout: float = 60.0) -> None:
        self.client = client
        self.timeout = timeout
        self.steps: list[Step] = []
        self.cancel: threading.Event | None = None

    def __call__(self, tool: str, **args: Any) -> Step:
        result = self.client.call_result(tool, args, self.timeout, self.cancel)
        try:
            payload = json.loads(result.text)
        except ValueError:
            payload = {"error": "not_json", "text": result.text, "status": result.status}
        step = Step(tool=tool, args=args, payload=payload if isinstance(payload, dict) else {"value": payload},
                    error=result.is_error, status=result.status)
        self.steps.append(step)
        return step

    @staticmethod
    def ref(obs: dict, role: str, name: str | None = None) -> str:
        """The ref of the first line of role (and accessible name) in an observation's page block."""
        pattern = re.escape(role) + (r' "' + re.escape(name) + r'"' if name is not None else "") + \
            r"[^\n]*?\[ref=((?:f\d+)?e\d+)\]"
        match = re.search(pattern, str(obs.get("page") or ""))
        if match is None:
            raise AssertionError(f"no {role} {name!r} in {obs.get('observation')}:\n{obs.get('page')}")
        return match.group(1)


def basic(d: Driver) -> dict:
    """T1 to T6, the K16 ref rule and the duplicate guard on the fixture page."""
    out: dict = {}
    out["open"] = d("navigate", url=f"{FIXTURE_ORIGIN}/index.html")
    first = out["open"].obs
    paragraph = d.ref(first, "paragraph")
    out["read"] = d("read", observation=first["observation"], ref=paragraph)
    out["observe"] = d("observe")
    out["read_old"] = d("read", observation=first["observation"], ref=paragraph)   # K16: still accepted
    textbox = d.ref(first, "textbox", "Name")
    out["fill"] = d("fill", observation=first["observation"], fields=[{"ref": textbox, "value": "Kate"}])
    submit = d.ref(first, "button", "Submit")
    out["stale_commit"] = d("click", observation=first["observation"], ref=submit)   # a commit needs the latest
    latest = out["stale_commit"].obs
    result = d.ref(latest, "status")
    out["submit"] = d("click", observation=latest["observation"], ref=d.ref(latest, "button", "Submit"),
                      expect={"text": "Hello, Kate", "ref": result})
    out["screenshot"] = d("screenshot", name="greeting")
    after = out["submit"].obs
    out["again_refused"] = d("click", observation=after["observation"], ref=d.ref(after, "button", "Submit"))
    out["observe2"] = d("observe")
    fresh = out["observe2"].obs
    out["again_allowed"] = d("click", observation=fresh["observation"], ref=d.ref(fresh, "button", "Submit"),
                             again=True)
    now = out["again_allowed"].obs
    out["rename"] = d("click", observation=now["observation"], ref=d.ref(now, "button", "Rename the button"),
                      again=True)   # a commit on the same page: again, citing the latest observation
    renamed = out["rename"].obs
    out["renamed_refused"] = d("click", observation=renamed["observation"],
                               ref=d.ref(renamed, "button", "Send now"))
    return out


def stale(d: Driver) -> dict:
    """T8: a re-render replaces the form; the old ref is refused and the new one works."""
    out: dict = {}
    out["open"] = d("navigate", url=f"{FIXTURE_ORIGIN}/index.html")
    first = out["open"].obs
    old_submit = d.ref(first, "button", "Submit")
    out["rerender"] = d("click", observation=first["observation"], ref=d.ref(first, "button", "Re-render"))
    now = out["rerender"].obs
    out["old_ref"] = d("click", observation=first["observation"], ref=old_submit)
    out["old_ref_latest"] = d("click", observation=now["observation"], ref=old_submit)
    current = out["old_ref_latest"].obs
    out["new_ref"] = d("click", observation=current["observation"], ref=d.ref(current, "button", "Submit"),
                       again=True)   # the re-render was a commit on this page too
    return out


def enter(d: Driver) -> dict:
    """T11: Enter in a textbox of an unnamed form is a commit, and its repeat is refused."""
    out: dict = {}
    out["open"] = d("navigate", url=f"{FIXTURE_ORIGIN}/decoy.html")
    first = out["open"].obs
    textbox = d.ref(first, "textbox", "Name")
    out["fill"] = d("fill", observation=first["observation"], fields=[{"ref": textbox, "value": "Kate"}])
    filled = out["fill"].obs
    out["enter"] = d("press", observation=filled["observation"], key="Enter", ref=textbox,
                     )
    after = out["enter"].obs
    out["enter_again"] = d("press", observation=after["observation"], key="Enter", ref=textbox)
    out["decoy_expect"] = None
    return out


def effects(d: Driver) -> dict:
    """T11 and K15: links that post are reclassified as commits and hold the origin (the link itself too), and a
    click on such a link again is a commit, refused as a duplicate; T18: a redirect off the origin set. The refused
    steps send nothing to Playwright."""
    out: dict = {}
    out["open"] = d("navigate", url=f"{FIXTURE_ORIGIN}/effects.html")
    first = out["open"].obs
    out["link"] = d("click", observation=first["observation"], ref=d.ref(first, "link", "Read more"))
    held = out["link"].obs
    out["held"] = d("click", observation=held["observation"], ref=d.ref(held, "button", "Save"))
    out["link_held"] = d("click", observation=held["observation"], ref=d.ref(held, "link", "Read more"))
    out["observe"] = d("observe")
    looked = out["observe"].obs
    out["link_again"] = d("click", observation=looked["observation"], ref=d.ref(looked, "link", "Read more"))
    out["div"] = d("click", observation=looked["observation"], ref=d.ref(looked, "link", "Next page"))
    later = out["div"].obs
    out["away_held"] = d("click", observation=later["observation"], ref=d.ref(later, "link", "Leave the fixture"))
    out["observe2"] = d("observe")
    looked = out["observe2"].obs
    out["away"] = d("click", observation=looked["observation"], ref=d.ref(looked, "link", "Leave the fixture"))
    out["after_away"] = d("observe")
    out["back"] = d("navigate", url=f"{FIXTURE_ORIGIN}/index.html")
    return out


def shop(d: Driver) -> dict:
    """T11: the slow shop's order times out at the shim's 2-second commit limit: outcome_unknown, a fresh
    observation that shows the order, and a repeat on commit_hold."""
    out: dict = {}
    out["open"] = d("navigate", url=f"{FIXTURE_ORIGIN}/shop.html")
    first = out["open"].obs
    out["order"] = d("click", observation=first["observation"], ref=d.ref(first, "button", "Place order"))
    seen = out["order"].obs
    out["repeat"] = d("click", observation=seen["observation"], ref=d.ref(seen, "button", "Place order"))
    return out


def popups(d: Driver) -> dict:
    """T18: a target=_blank link and a window.open button each open a popup that is closed at once."""
    out: dict = {}
    out["open"] = d("navigate", url=f"{FIXTURE_ORIGIN}/popup.html")
    first = out["open"].obs
    out["blank"] = d("click", observation=first["observation"],
                     ref=d.ref(first, "link", "Open the start in a new tab"))
    now = out["blank"].obs
    out["window"] = d("click", observation=now["observation"], ref=d.ref(now, "button", "Open a window"))
    out["observe"] = d("observe")
    return out


def injection(d: Driver) -> dict:
    """T12: hidden texts, a title, a dialog and a download name stay inside the data block, cleaned and capped."""
    out: dict = {}
    out["open"] = d("navigate", url=f"{FIXTURE_ORIGIN}/injection.html")
    first = out["open"].obs
    out["dialog"] = d("click", observation=first["observation"], ref=d.ref(first, "button", "Show a message"))
    now = out["dialog"].obs
    out["download"] = d("click", observation=now["observation"], ref=d.ref(now, "link", "Download the report"))
    out["find"] = d("observe", find="Plain text")
    return out


def login(d: Driver, wait: float = 3.0) -> dict:
    """T9: the seeded cookie expires (2 seconds); the private page answers login_required, without its content."""
    out: dict = {}
    out["open"] = d("navigate", url=f"{FIXTURE_ORIGIN}/index.html")   # the browser starts, the cookie is seeded
    time.sleep(wait)
    out["private"] = d("navigate", url=f"{FIXTURE_ORIGIN}/private.html")
    return out


def urls(d: Driver) -> dict:
    """T18: the URL rules refuse before anything is sent."""
    out: dict = {}
    for name, url in (("javascript", "javascript:alert(1)"), ("data", "data:text/html,<p>x</p>"),
                      ("file", "file:///etc/passwd"), ("view_source", f"view-source:{FIXTURE_ORIGIN}/"),
                      ("userinfo", "http://127.0.0.1:18937@evil.example/"),
                      ("elsewhere", "http://127.0.0.2:18937/index.html")):
        out[name] = d("navigate", url=url)
    out["variant"] = d("navigate", url="HTTP://127.0.0.1:18937/index.html")
    return out


SCENARIOS: dict[str, dict] = {
    "basic": {"run": basic},
    "stale": {"run": stale},
    "enter": {"run": enter},
    "effects": {"run": effects},
    "shop": {"run": shop, "limits": {"commit_seconds": 2}, "settings": {"shop_delay": 10}},
    "popups": {"run": popups},
    "injection": {"run": injection},
    "login": {"run": login, "profile": "fixture-login",
              "settings": {"seed_cookie_seconds": 2, "login": {"path": "/login"}}},
    "urls": {"run": urls},
}


# ---------------------------------------------------------------- recordings


_DOWNLOADS_RE = re.compile(r"(?:\.\./)+downloads/")


def normalize_text(text: str, folder: str) -> str:
    """A response's text without what depends on this machine: the session folder, the relative path to the
    in-namespace downloads tmpfs (its depth follows the session folder's) and the times in file names."""
    text = text.replace(folder, "<session>")
    text = _DOWNLOADS_RE.sub("<downloads>/", text)
    return _TIME_RE.sub("<time>", text)


def normalize_calls(path: Path, folder: Path) -> dict:
    """The shim's record of its Playwright calls, with local paths and times replaced by placeholders and every
    screenshot replaced by a 1x1 PNG."""
    tools: list = []
    calls: list = []
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        if "tools" in entry:
            tools = entry["tools"]
            continue
        content = []
        for part in entry.get("content") or []:
            if part.get("type") == "image":
                content.append({"type": "image", "mimeType": part.get("mimeType"), "data": TINY_PNG})
            elif part.get("type") == "text":
                content.append({"type": "text", "text": normalize_text(part.get("text") or "", str(folder))})
            else:
                content.append(part)
        arguments = json.loads(normalize_text(json.dumps(entry.get("arguments") or {}), str(folder)))
        call = {"tool": entry["tool"], "arguments": arguments, "status": entry["status"], "content": content}
        calls.append(call)
    return {"playwright_mcp": "0.0.83", "tools": tools, "calls": calls}


def comparable(recording: dict) -> list:
    """What the contract test compares: tool, arguments, status and the texts (images by type only)."""
    out = []
    for call in recording["calls"]:
        texts = [part.get("text") if part.get("type") == "text" else part.get("type") for part in call["content"]]
        out.append((call["tool"], json.dumps(call["arguments"], sort_keys=True), call["status"], texts))
    return out


def run_live(name: str, data_dir: Path, *, record: bool = True) -> tuple[dict, Driver, Path]:
    """Run one scenario live through bin/sf-browser; (its result, the driver, the session folder)."""
    spec = SCENARIOS[name]
    grant = make_grant(data_dir, profile=spec.get("profile", "fixture"), settings=spec.get("settings"),
                       limits=spec.get("limits"), record=record)
    client = start_launcher(grant, data_dir)
    driver = Driver(client)
    try:
        result = spec["run"](driver)
    finally:
        client.close()
    from swfactory.browser.session_locks import session_of_grant

    return result, driver, session_of_grant(data_dir, grant.stem)


def recording_of(name: str) -> dict:
    return json.loads((RECORDINGS / f"{name}.json").read_text(encoding="utf-8"))


def write_recording(name: str, recording: dict, meta: dict) -> Path:
    RECORDINGS.mkdir(parents=True, exist_ok=True)
    path = RECORDINGS / f"{name}.json"
    path.write_text(json.dumps({**meta, **recording}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def run_fake(name: str, data_dir: Path, *, recording: Path | None = None, limits: dict | None = None,
             settings: dict | None = None) -> tuple[dict, Driver, Path, Path]:
    """Run one scenario against the fake Playwright replaying its recording (no namespace, no browser);
    (its result, the driver, the session folder, the fake's call log)."""
    spec = SCENARIOS[name]
    fake_log = data_dir / f"fake-{name}.jsonl"
    grant = make_grant(data_dir, profile=spec.get("profile", "fixture"),
                       settings={**(spec.get("settings") or {}), **(settings or {})},
                       limits={**(spec.get("limits") or {}), **(limits or {})}, live=False,
                       fake=recording or RECORDINGS / f"{name}.json", fake_log=fake_log)
    client, lock = start_direct(grant, data_dir, data_dir)
    driver = Driver(client)
    try:
        result = spec["run"](driver) if name != "login" else login(driver, wait=0)
    finally:
        client.close()
        lock.close()
    return result, driver, lock.folder, fake_log


def main(argv: list[str]) -> int:
    """python3 -m tests.browser_rig record [scenario ...]: rewrite the recordings from live runs."""
    import tempfile

    if not argv or argv[0] != "record":
        print("usage: python3 -m tests.browser_rig record [scenario ...]")
        return 2
    reason = live_reason()
    if reason:
        print(f"cannot record: {reason}")
        return 1
    names = argv[1:] or list(SCENARIOS)
    for name in names:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, folder = run_live(name, Path(tmp))
            recording = normalize_calls(folder / "playwright-calls.jsonl", folder)
            meta = {"scenario": name, "recorded_with": os.environ.get("SF_BROWSER_RECORDED_WITH", ""),
                    "note": "recorded by tests/browser_rig.py from the real Playwright MCP; local paths and times "
                            "are placeholders, screenshots a 1x1 PNG"}
            print(write_recording(name, recording, meta))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
