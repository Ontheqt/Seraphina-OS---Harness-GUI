"""A mock SnapRAID Daemon 2.1 for the daemon route's tests (docs/seraphina/BACKUPS_SNAPRAID.md §12.6).

A standard-library HTTP server on 127.0.0.1 that answers the routes the link and the harness use, seeded from the
recorded exchanges in tests/fixtures/snapraidd21 (daemon lab C, with placeholder paths, no host name and no
credential), and driven by the test through Python methods rather than HTTP:

- the login: HTTP Basic with one credential line; the last accepted header is cached and skips the check; any other
  header is checked at most once a second (else 429 with ``Retry-After: 1`` and an empty body); a request with no
  header is 401 before the rate limit; ``/metrics`` exactly and every OPTIONS need no login; ``auth=False`` opens
  every route (``net_auth_credential`` empty);
- paths are exact and lowercase (404 "REST endpoint paths must be lowercase", 404 "Resource not found");
- the five actions with the daemon's bodies, its 400 "Unrecognized json", its 409s, a POST without a JSON content
  type (415) or without Content-Length; ``fail_with`` answers 500 "SnapRAID executable not found" or 503 "Daemon is
  terminating" before anything is queued;
- scenarios: ``run_all`` runs the queue (a freeze, a failed step, a stop), ``set_scan`` sets the last scan's change
  list, ``fire_window`` (a window, or a skip used up), ``restart`` (uptime back to 0, counters re-based, the queue
  lost, a running task ``signaled 9``, ``hold_off`` off, numbers used again), ``dead`` (connection refused) and
  ``reset_after_request`` (the request is read, then the connection closes with no answer) and ``drop_answer`` (a
  POST is carried out, then the connection closes with no answer).

Every request is recorded in ``requests`` ({method, path, body, auth}), so a test can assert that nothing outside
the link's table was ever sent (T27). The credential is never written anywhere but the server's memory.
"""

from __future__ import annotations

import base64
import calendar
import copy
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "snapraidd21"
HEALTH_NUMBERS = {"pending": 0, "passed": 1, "corrupt": 2, "degraded": 3, "prefail": 4, "failing": 5}
GET_ROUTES = ("/snapraid/v1/state", "/snapraid/v1/tasks", "/snapraid/v1/activity", "/snapraid/v1/array",
              "/snapraid/v2/disks", "/snapraid/v2/config", "/snapraid/v1/system", "/snapraid/v1/disks",
              "/snapraid/v1/config")
POST_ROUTES = ("/snapraid/v1/maintenance", "/snapraid/v1/heal", "/snapraid/v1/undelete", "/snapraid/v1/stop",
               "/snapraid/v1/hold_off", "/snapraid/v1/schedule", "/snapraid/v1/clear", "/snapraid/v1/report",
               "/snapraid/v1/refresh", "/snapraid/v1/suspend_idle")
BODY_FIELDS = {"maintenance": {"ignore_thresholds": bool, "spindown_on_finish": bool},
               "heal": {"spindown_on_finish": bool},
               "undelete": {"filters": list, "filter_disks": list, "spindown_on_finish": bool},
               "stop": {}, "hold_off": {"enabled": bool}}
RUNNING = ("starting", "processing", "finalizing", "stopping")


def fixture(phase: str, name: str) -> dict:
    """One recorded exchange: {method, path, body, status, headers, json | text}."""
    return json.loads((FIXTURES / phase / f"{name}.json").read_text(encoding="utf-8"))


def limited(messages: list[dict], limit: int) -> list[dict]:
    """limit_messages as the daemon applies it (rest.c:2234-2292): fatal messages always, the others from the start
    of the list up to the limit."""
    out = []
    for message in messages:
        if message.get("level") == "fatal":
            out.append(message)
        elif limit > 0:
            out.append(message)
            limit -= 1
    return out


def stamp(when: float) -> str:
    """The daemon's time format: local wall time, no zone (the mock uses UTC)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(when))


class MockDaemon:
    """One mock daemon. credential: "user:password"; auth False opens every route; phase: the lab fixture folder the
    state is seeded from; clock: time.time (tests may step it)."""

    def __init__(self, credential: str = "seraphina:canary-password-0001", *, auth: bool = True,
                 phase: str = "lab/04-after-maintenance", clock: Callable[[], float] = time.time,
                 rate_seconds: float = 1.0) -> None:
        self.credential = credential
        self.auth = auth
        self.clock = clock
        self.rate_seconds = rate_seconds
        self.lock = threading.RLock()
        self.requests: list[dict] = []
        self.dead = False
        self.reset_after_request = False
        self.drop_answer = False                  # the request is carried out, then the connection closes unanswered
        self.fail_with: int | None = None       # 500 or 503 on the action routes
        self.notify_fails = False               # a report's notify_result fails: terminated -1 and a fatal message
        self._cached: str | None = None
        self._last_check = 0.0
        state = fixture(phase, "GET_v1_state")["json"]
        tasks = fixture(phase, "GET_v1_tasks")["json"]
        self.array = fixture(phase, "GET_v1_array")["json"]
        self.config = fixture(phase, "GET_v2_config")["json"]
        self.disks = fixture(phase, "GET_v2_disks")["json"]
        self.health = state.get("health", "passed")
        self.health_reason = state.get("health_reason")
        self.pulse = {key: value for key, value in state["pulse"].items() if key != "current_at"}
        self.pending: list[dict] = copy.deepcopy(tasks.get("pending") or [])
        self.active: list[dict] = copy.deepcopy(tasks.get("active") or [])
        self.history: list[dict] = copy.deepcopy(tasks.get("history") or [])     # newest first, as the daemon
        self.next_number = 1 + max([t["number"] for t in self.pending + self.active + self.history] or [0])
        self.started = self.clock() - 60.0
        self.engine_version = self.array.get("engine_version", "15.1")
        self.daemon_version = self.array.get("daemon_version", "2.1")
        self.array.setdefault("diffs", [])
        self.array.setdefault("fixes", [])
        self.pid = 4000
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    # -- the server --------------------------------------------------------------------------------------------

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> MockDaemon:
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                return

            def _serve(self) -> None:
                daemon.handle(self)

            do_GET = do_POST = do_PATCH = do_DELETE = do_OPTIONS = _serve

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                                        name="mock-snapraidd", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def kill(self) -> None:
        """The daemon is down: connections are refused (the port stays known)."""
        self.dead = True
        self.stop()

    def revive(self) -> None:
        self.dead = False
        if self._server is None:
            self.start()

    # -- one request --------------------------------------------------------------------------------------------

    def handle(self, h: BaseHTTPRequestHandler) -> None:
        length = h.headers.get("Content-Length")
        raw = h.rfile.read(int(length)) if length and length.isdigit() else b""
        parts = urlsplit(h.path)
        auth = h.headers.get("Authorization")
        with self.lock:
            self.requests.append({"method": h.command, "path": h.path, "body": raw.decode("utf-8", "replace"),
                                  "auth": "none" if not auth else ("ok" if self._valid(auth) else "wrong")})
        if self.reset_after_request:
            h.close_connection = True
            with_socket = h.connection
            try:
                with_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return
        status, body, headers = self.answer(h.command, parts.path, parts.query, raw, h.headers, auth)
        if self.drop_answer and h.command == "POST":
            h.close_connection = True
            try:
                h.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else b"")
        h.send_response(status)
        content = headers.pop("Content-Type", "application/json" if body is not None and not isinstance(body, bytes)
                              else "text/plain; charset=utf-8")
        h.send_header("Content-Type", content)
        for key, value in headers.items():
            h.send_header(key, value)
        h.send_header("Content-Length", str(len(data)))
        h.send_header("Connection", "close")
        h.end_headers()
        h.wfile.write(data)
        h.close_connection = True

    def _valid(self, header: str) -> bool:
        expected = "Basic " + base64.b64encode(self.credential.encode()).decode()
        return header == expected

    def answer(self, method: str, path: str, query: str, raw: bytes, headers: Any,
               auth: str | None) -> tuple[int, Any, dict]:
        if method == "OPTIONS":
            return 204, b"", {}
        if path != "/metrics" and self.auth:
            if not auth:
                return 401, b"", {"WWW-Authenticate": 'Basic realm="SnapRAID Daemon REST API"'}
            if auth != self._cached:
                now = time.monotonic()
                if now - self._last_check < self.rate_seconds:
                    return 429, b"", {"Retry-After": "1"}
                self._last_check = now
                if not self._valid(auth):
                    return 401, b"", {"WWW-Authenticate": 'Basic realm="SnapRAID Daemon REST API"'}
                self._cached = auth
        if path != path.lower():
            return 404, {"success": False, "message": "REST endpoint paths must be lowercase"}, {}
        if path == "/metrics":
            if method != "GET":
                return 405, {"success": False, "message": "Method not allowed"}, {}
            return 200, self.metrics().encode(), {"Content-Type": "text/plain; version=0.0.4; charset=utf-8"}
        if method == "GET" and path in GET_ROUTES:
            try:
                limits = {key: int(values[0]) for key, values in parse_qs(query).items()}
            except ValueError:
                return 400, {"success": False, "message": "Invalid limit parameter"}, {}
            if any(value < 0 or value > 100000 for value in limits.values()):
                return 400, {"success": False, "message": "Invalid limit parameter"}, {}
            with self.lock:
                return self.get(path, limits)
        if method == "POST" and path in POST_ROUTES:
            if "application/json" not in (headers.get("Content-Type") or ""):
                return 415, {"success": False, "message": "Unsupported Media Type"}, {}
            if headers.get("Transfer-Encoding"):
                return 411, {"success": False, "message": "Length Required"}, {}
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                return 400, {"success": False, "message": "Unrecognized json"}, {}
            with self.lock:
                return self.post(path.rsplit("/", 1)[1], body)
        if path in GET_ROUTES or path in POST_ROUTES or path == "/snapraid/v2/config":
            return 405, {"success": False, "message": "Method not allowed"}, {}
        return 404, {"success": False, "message": "Resource not found"}, {}

    # -- GET routes ---------------------------------------------------------------------------------------------

    def pulse_view(self) -> dict:
        return {**self.pulse, "current_at": stamp(self.clock())}

    def get(self, path: str, limits: dict) -> tuple[int, Any, dict]:
        if path == "/snapraid/v1/state":
            out = {"pulse": self.pulse_view(), "health": self.health, "instance": "lab"}
            if self.health_reason:
                out["health_reason"] = self.health_reason
            if self.active:
                out.update(active_command=self.active[0]["command"], progress=self.active[0].get("progress", 0),
                           eta_seconds=self.active[0].get("eta_seconds", 0))
            if self.pending:
                out["next_command"] = self.pending[-1]["command"]
            return 200, out, {}
        if path == "/snapraid/v1/tasks":
            keep = limits.get("limit_history", 10000)
            messages = limits.get("limit_messages", 5000)
            def trim(task: dict) -> dict:
                return {**task, "messages": limited(task.get("messages") or [], messages)}
            return 200, {"pulse": self.pulse_view(), "pending": [trim(t) for t in self.pending],
                         "active": [trim(t) for t in self.active],
                         "history": [trim(t) for t in self.history[:keep]]}, {}
        if path == "/snapraid/v1/activity":
            latest = self.latest()
            if latest is None:
                return 204, b"", {}
            messages = limits.get("limit_messages", 5000)
            return 200, {**latest, "messages": limited(latest.get("messages") or [], messages),
                         "pulse": self.pulse_view()}, {}
        if path == "/snapraid/v1/array":
            out = {key: value for key, value in self.array.items() if key not in ("diffs", "fixes")}
            out.update(pulse=self.pulse_view(), engine_version=self.engine_version,
                       daemon_version=self.daemon_version, health=self.health,
                       diffs=self.array["diffs"][:limits.get("limit_diffs", 100000)],
                       fixes=self.array["fixes"][:limits.get("limit_fixes", 100000)])
            return 200, out, {}
        if path == "/snapraid/v2/disks":
            return 200, {**self.disks, "pulse": self.pulse_view()}, {}
        if path == "/snapraid/v2/config":
            return 200, {**self.config, "pulse": self.pulse_view()}, {}
        return 200, {"pulse": self.pulse_view(), "hostname": "<host>"}, {}

    def latest(self) -> dict | None:
        """The daemon's runner.latest: the running task; else the last task that ran, where a run's report points
        back to the last executed, not cancelled, task of its run (runner.c:610-613); queued and cancelled tasks
        never."""
        if self.active:
            return self.active[0]
        ran = [t for t in self.history if t.get("status") != "canceled"]
        if not ran:
            return None
        if ran[0]["command"] == "report":
            earlier = [t for t in ran[1:] if t["scheduled_at"] == ran[0]["scheduled_at"]
                       and t["high_command"] == ran[0]["high_command"]]
            if earlier:
                return earlier[0]
        return ran[0]

    # -- POST routes ----------------------------------------------------------------------------------------------

    def post(self, name: str, body: Any) -> tuple[int, Any, dict]:
        fields = BODY_FIELDS.get(name)
        if fields is None:
            return 200, {"success": True, "message": "accepted by the mock (never used by Seraphina)"}, {}
        if not isinstance(body, dict) or set(body) - set(fields) or \
                any(not isinstance(value, fields[key]) for key, value in body.items()):
            return 400, {"success": False, "message": "Expected empty JSON object" if name == "stop"
                         else "Unrecognized json"}, {}
        if name == "hold_off":
            if "enabled" not in body:
                return 400, {"success": False, "message": "Unrecognized json"}, {}
            self.array["hold_off"] = body["enabled"]
            self.pulse["array"] += 1
            return 200, {"success": True, "message": "Hold off enabled" if body["enabled"] else "Hold off disabled"}, {}
        if name == "stop":
            return self.stop_running()
        if self.fail_with == 500:
            return 500, {"success": False, "message": "SnapRAID executable not found"}, {}
        if self.fail_with == 503:
            return 503, {"success": False, "message": "Daemon is terminating"}, {}
        if any(t["high_command"] == name for t in self.pending + self.active):
            return 409, {"success": False, "message": f"{name.capitalize()} already running or scheduled"}, {}
        if name == "maintenance":
            steps = ["up", "sync"] + (["scrub"] if int(self.config.get("scrub_percentage") or 0) > 0 else [])
            self.ignored_thresholds = bool(body.get("ignore_thresholds"))
        elif name == "heal":
            steps = ["up", "fix", "scrub"]
        else:
            steps = ["up", "fix"]
            self.undelete_filters = (list(body.get("filters") or []), list(body.get("filter_disks") or []))
        self.queue(name, steps + ["report"])
        return 202, {"success": True}, {}

    def stop_running(self) -> tuple[int, Any, dict]:
        if not self.active:
            return 409, {"success": False, "message": "No task running"}, {}
        task = self.active[0]
        self.pid += 1
        task.setdefault("messages", []).append({"level": "error", "type": "soft", "text": "Stop requested"})
        task["_stop"] = True            # 15.1 traps SIGTERM: the task stays "processing" while it saves (lab C 05)
        self.pulse["tasks"] += 1
        return 202, {"success": True, "message": "Signal sent", "number": task["number"], "pid": self.pid}, {}

    # -- scenarios (called by the tests) --------------------------------------------------------------------------

    def task(self, command: str, high: str, scheduled: str) -> dict:
        task = {"number": self.next_number, "command": command, "high_command": high, "status": "queued",
                "health": "pending", "scheduled_at": scheduled, "messages": [], "log_file": "/var/log/x.log"}
        self.next_number += 1
        return task

    def queue(self, high: str, steps: list[str]) -> list[dict]:
        scheduled = stamp(self.clock())
        group = [self.task(step, high, scheduled) for step in steps]
        self.pending = list(reversed(group)) + self.pending    # the next to run is the last element
        self.pulse["tasks"] += 1
        return group

    def begin(self) -> dict | None:
        """The next queued task starts (status processing)."""
        with self.lock:
            if self.active or not self.pending:
                return self.active[0] if self.active else None
            task = self.pending.pop()
            task.update(status="processing", started_at=stamp(self.clock()), progress=37, eta_seconds=60)
            self.active = [task]
            self.pulse["tasks"] += 1
            self.pulse["activity"] += 1
            return task

    def finish(self, *, exit_code: int = 0, health: str = "passed") -> dict | None:
        """The running task ends; a failure cancels the rest of its run except report, a stop keeps only report."""
        with self.lock:
            if not self.active:
                return None
            task = self.active.pop()
            stopped = bool(task.pop("_stop", False))
            if stopped and exit_code == 0:
                exit_code = 1
            if task["command"] == "report" and self.notify_fails:
                exit_code = -1
                task.setdefault("messages", []).append({"level": "fatal", "type": "soft",
                                                        "text": "Notification failed (check syslog for details)"})
            task.update(status="terminated", exit_code=exit_code, finished_at=stamp(self.clock()), health=health)
            self.history.insert(0, task)
            if task["command"] == "sync":
                if exit_code == 0 or (exit_code == 1 and stopped):
                    self.array["last_sync_at"] = stamp(self.clock())
                    self.array["blocks_unsynced"] = 0 if exit_code == 0 else 63
                    if exit_code == 0:
                        for key in [k for k in self.array if k.startswith("diff_") and k != "diff_equal"]:
                            self.array[key] = 0
                        self.array["diffs"] = []
                        self.array["fixes"] = []
                        self.array["fix_recovered"] = self.array["fix_unrecoverable"] = 0
                self.array["last_command"], self.array["last_command_at"] = "sync", stamp(self.clock())
                self.pulse["array"] += 1
            if task["command"] == "fix" and exit_code in (0, 1):
                self.array["last_command"], self.array["last_command_at"] = "fix", stamp(self.clock())
                if task["high_command"] == "heal":
                    self.array["blocks_bad"] = 0
                    self.health = "passed"
                else:
                    restored = [{"disk": d, "path": p.lstrip("/"), "result": "recovered"} for d, p in self._restorable()]
                    self.array["fixes"] = self.array["fixes"] + restored
                    self.array["fix_recovered"] = int(self.array.get("fix_recovered") or 0) + len(restored)
                self.pulse["array"] += 1
            if exit_code != 0 and task["command"] != "report":
                keep = ("report",) if stopped else ("report", "down", "shutdown")
                why = (f"The preceding {task['command']} operation was canceled" if stopped else
                       f"The preceding {task['command']} operation failed with exit code {exit_code}")
                for other in list(self.pending):
                    if other["scheduled_at"] == task["scheduled_at"] and other["high_command"] == task["high_command"] \
                            and other["command"] not in keep:
                        self.pending.remove(other)
                        other.update(status="canceled", exit_msg=why, started_at=stamp(self.clock()),
                                     finished_at=stamp(self.clock()), health="passed")
                        self.history.insert(0, other)
            self.pulse["tasks"] += 1
            self.pulse["activity"] += 1
            return task

    def _restorable(self) -> list[tuple[str, str]]:
        filters, disks = getattr(self, "undelete_filters", ([], []))
        out = []
        for entry in self.array.get("diffs") or []:
            if entry.get("change") != "removed" or (disks and entry.get("disk") not in disks):
                continue
            path = "/" + entry["path"]
            for pattern in filters:
                plain = pattern.replace("\\", "")
                if path == plain or (plain.endswith("/") and path.startswith(plain)):
                    out.append((entry["disk"], path))
                    break
        return out

    def run_all(self, outcomes: dict | None = None) -> None:
        """Run the queue to its end. outcomes: {command: exit code} (sync 2 is a freeze: the scan's counts stay)."""
        outcomes = outcomes or {}
        while self.pending or self.active:
            task = self.begin()
            if task is None:
                break
            self.finish(exit_code=int(outcomes.get(task["command"], 0)))

    def set_scan(self, removed: dict[str, list[str]] | None = None, updated: int = 0, added: int = 0) -> None:
        """The last scan's change list: removed files per disk (their paths), and counts of updated and added."""
        with self.lock:
            diffs = [{"change": "removed", "disk": disk, "path": path} for disk, paths in (removed or {}).items()
                     for path in paths]
            diffs += [{"change": "updated", "disk": "d1", "path": f"upd/u-{i:04d}.bin"} for i in range(updated)]
            diffs += [{"change": "added", "disk": "d2", "path": f"new/a-{i:04d}.bin"} for i in range(added)]
            self.array["diffs"] = diffs
            self.array["diff_removed"] = sum(len(p) for p in (removed or {}).values())
            self.array["diff_updated"] = updated
            self.array["diff_added"] = added
            self.array["last_command"], self.array["last_command_at"] = "diff", stamp(self.clock())
            self.array["last_diff_at"] = stamp(self.clock())
            self.pulse["array"] += 1

    def freeze(self, removed: dict[str, list[str]], updated: int = 0) -> None:
        """A window whose sync the thresholds stopped: the scan's list stays, last_sync_at does not move."""
        self.set_scan(removed, updated)
        self.queue("maintenance", ["up", "sync", "scrub", "report"])
        self.run_all({"sync": 2})

    def fire_window(self) -> bool:
        """The daemon's window comes: skipped once when hold_off is on (the switch resets), else a maintenance."""
        with self.lock:
            if self.array.get("hold_off"):
                self.array["hold_off"] = False
                self.pulse["array"] += 1
                return False
            if any(t["high_command"] == "maintenance" for t in self.pending + self.active):
                return False
            self.queue("maintenance", ["up", "sync", "scrub", "report"])
            return True

    def restart(self) -> None:
        """A restart: uptime from 0, counters re-based (config 1), the queue lost, a running task signaled 9, tasks
        without a log (start, report, cancelled) gone, numbers used again, hold_off off, a start-up probe."""
        with self.lock:
            self.started = self.clock()
            for task in self.active:
                task.pop("_stop", None)
                task.update(status="signaled", exit_sig=9, finished_at=stamp(self.clock()))
                self.history.insert(0, task)
            self.active, self.pending = [], []
            self.history = [t for t in self.history if t["command"] not in ("start", "report")
                            and t["status"] != "canceled"]
            self.next_number = 1 + max([t["number"] for t in self.history] or [0])
            self.array["hold_off"] = False
            self.pulse = {"array": 3, "config": 1, "disks": 5, "tasks": 7, "activity": 9}
            self._cached = None
            probe = self.task("probe", "startup", stamp(self.clock()))
            probe.update(status="terminated", exit_code=0, started_at=stamp(self.clock()),
                         finished_at=stamp(self.clock()), health="passed")
            self.history.insert(0, probe)

    # -- /metrics ---------------------------------------------------------------------------------------------------

    def metrics(self) -> str:
        with self.lock:
            a = self.array
            lines = [
                f"snapraid_array_health {HEALTH_NUMBERS.get(self.health, 0)}",
                f'snapraid_build_info{{daemon_version="{self.daemon_version}",engine_version="{self.engine_version}"}} 1',
                f"snapraid_array_block_size_bytes {a.get('block_size_bytes', 0)}",
                f"snapraid_array_data_disks_count {a.get('data_disks_count', 0)}",
                f"snapraid_array_parity_disks_count {a.get('parity_disks_count', 0)}",
                f"snapraid_array_files {a.get('files_count', 0)}",
                f"snapraid_array_blocks {a.get('blocks_count', 0)}",
                f"snapraid_blocks_bad {a.get('blocks_bad', 0)}",
                f"snapraid_blocks_unsynced {a.get('blocks_unsynced', 0)}",
                f"snapraid_blocks_unscrubbed {a.get('blocks_unscrubbed', 0)}",
            ]
            for command, key in (("sync", "last_sync_at"), ("scrub", "last_scrub_at"), ("diff", "last_diff_at")):
                if a.get(key):
                    when = calendar.timegm(time.strptime(a[key], "%Y-%m-%dT%H:%M:%S"))
                    lines.append(f'snapraid_last_command_timestamp_seconds{{command="{command}"}} {int(when)}')
            for change in ("equal", "added", "removed", "updated", "moved", "copied", "relocated", "restored"):
                lines.append(f'snapraid_diff_files{{change="{change}"}} {a.get("diff_" + change, 0)}')
            lines += [f"snapraid_config_sync_threshold_deletes {self.config.get('sync_threshold_deletes', 0)}",
                      f"snapraid_config_sync_threshold_updates {self.config.get('sync_threshold_updates', 0)}",
                      f"snapraid_config_scrub_percentage {self.config.get('scrub_percentage', 0)}",
                      f"snapraid_config_hold_off {1 if a.get('hold_off') else 0}"]
            for group, role in (("data_disks", "data"), ("parity_disks", "parity")):
                for disk in self.disks.get(group) or []:
                    lines.append(f'snapraid_disk_health{{disk="{disk["name"]}",role="{role}"}} '
                                 f'{HEALTH_NUMBERS.get(disk.get("health"), 0)}')
            lines.append(f"snapraid_task_queue_depth {len(self.pending)}")
            for task in self.active:
                lines += [f'snapraid_task_active{{command="{task["command"]}",high_command="{task["high_command"]}"}} 1',
                          f"snapraid_task_active_progress_percent {task.get('progress', 0)}",
                          f"snapraid_task_active_eta_seconds {task.get('eta_seconds', 0)}",
                          f"snapraid_task_active_elapsed_seconds 1"]
            last: dict[str, int] = {}
            for task in reversed(self.history):
                if task.get("status") == "terminated":
                    last[task["command"]] = task.get("exit_code", 0)
            for command, code in last.items():
                lines.append(f'snapraid_task_last_exit_code{{command="{command}"}} {code}')
            for name, value in self.pulse.items():
                lines.append(f'snapraid_exporter_last_pulse{{subsystem="{name}"}} {value}')
            lines.append(f"snapraid_daemon_uptime_seconds {int(self.clock() - self.started)}")
            lines.append("snapraid_exporter_up 1")
            return "\n".join(lines) + "\n"
