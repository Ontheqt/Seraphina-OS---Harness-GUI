#!/usr/bin/env python3
"""Mock Software Factory backend for GUI development and the Playwright smoke test.

Serves swfactory/web/ and implements every HTTP endpoint of DESIGN.md sections 14, 19,
22, 23 and 24 with in-memory state (the shapes of swfactory/server.py, orchestrator.py,
settings.py and plans.py), token auth, SSE with replay, and a scenario thread that keeps
the factory line busy: a job that goes through a CHANGES round, a resume and a PASS round
to merged; one that the approver escalates to needs-human; one that fails. Agent events
(text, reasoning, tool calls/results, CLI output) carry run ids like the real
orchestrator; runs on a provider with max_concurrency and concurrency_scope "run" hold its
slot for the whole run and publish agent.waiting {provider} while they queue. Jobs carry
the routing snapshot of section 23; plans (section 24) run a simulated lead and move
planning -> ready -> running -> final-review -> done. Seed data includes hostile
markdown, diffs and job text to exercise the GUI's escaping.

    python3 tests/gui_mock_server.py --port 0 [--scenario-speed 2.0] [--ping-interval 15]

Prints "READY http://127.0.0.1:<port>" on stdout once listening. Token: test-token.
Section 25 (Welcome, Grid Card, guided setup): GET /api/setup (computed from the live state, so it changes as
the wizard applies things; host.model_servers only after POST /api/setup/look-around, which keeps the real rate
limit), GET/POST /api/secrets (names only; values are never stored or logged), GET
/api/docs/procedures(.html), delivery modes (merge.delivery/output_dir, per-job delivery, stage delivered, a
seeded delivered folder job and branch job), presets as {name, description} objects, chats with an agent
(coder or analyst), agent.updated events and the plan log endpoint. --fresh starts from a fresh install
(no repos, no keys, no jobs).

Test hooks: POST /__mock/drop-sse, POST /__mock/publish {type, data, repo?, job?} (any event on the stream),
GET /__mock/requests, POST /__mock/look-around {finds?, error?, gap?, forget?} (what the next look for model servers
finds, or the error it breaks with; a fresh budget, gap: seconds between looks; forget drops the last look), POST
/__mock/hold {provider,
seconds} (occupy a provider slot so the next run on it waits), POST /__mock/stopped-job {repo,
feature, routing, stage, delivery} (a job that is needs-human or failed at once). Standard library only.

Seraphina OS Harness areas (the GUI's view modules): each area's endpoints live in its own module,
tests/gui_mock/<area>.py (routing, tracker, personas, messages, operator), loaded here; it seeds its own state,
answers /api/... before the core routes and owns /__mock/<area>/<name>. tests/gui_mock/__init__.py has the hooks.
The view modules themselves are served from swfactory/web/views/<name>.js, as by the real server.
"""

from __future__ import annotations

import argparse
import copy
import json
import queue
import re
import sys
import threading
import time
import urllib.parse
from collections import deque
from datetime import datetime, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "swfactory" / "web"
if str(ROOT) not in sys.path:   # run as a script: tests/ is on the path, the repository root is not
    sys.path.insert(0, str(ROOT))
from tests import gui_mock  # noqa: E402  (the per-area mock modules)

AREAS = gui_mock.load()
CORE = sys.modules[__name__]   # what the area modules get as `core` (this module, also when run as __main__)
AGENTS_DIR = ROOT / "agents"
PRESETS_DIR = ROOT / "examples"
USER_AGENTS_DIR = "/home/ai-dev/.config/software-factory/agents"
DATA_DIR = "/srv/ai-work/factory"
TOKEN = "test-token"
VERSION = "0.2.0"
SPEED = 1.0
PING_INTERVAL = 15.0
CANCEL_TOKEN = object()

HOSTILE_MD = """ESCALATE

## Why
The notes renderer still allows raw HTML: <img src=x onerror=alert('decision')> and <script>alert("xss")</script>.
A human should decide whether **markdown in user notes** is in scope.

- [ ] Sanitize *before* rendering
- [x] Escape `<b>` tags in previews
- [Click me](javascript:alert('link')) and [a safe link](https://example.com/docs?a=1&b=2)
- <a href="javascript:alert(1)">raw anchor</a> and <svg onload=alert(2)>
- ![tracker](https://evil.example/pixel.png)

> Quoted from review-security.md: <iframe src="https://evil.example"></iframe>

| Check | Result |
| --- | :---: |
| XSS in title | <b>fails</b> |
| Links | `javascript:` blocked |

```html
<script>alert("in a code block")</script>
<img src=x onerror=alert('code')>
```

---
Done: "quotes" & 'apostrophes' & ampersands &amp; entities &lt;b&gt;.
"""

# Markdown the renderer once looped on forever (nested emphasis and brackets): kept in the seed data so
# the smoke test renders it on every run.
NESTED_MD = """VERDICT: PASS

## Round 1: code
Use *args and **kwargs in *the wrapper*. Returns [dict[str, int]](https://example.com/typing).
See [RFC 6749 [section 4.1]](https://example.com/rfc6749) and the footnote [see [1]](https://example.com/notes).

- *Note: *all* reviewers must pass first.*
- **Warning: **do not** merge this.**
- ~~a ~~b~~ c~~ and [a [b] c](javascript:alert('nested'))
- update [the [config] docs](https://example.com/config)
"""

HOSTILE_DIFF = """diff --git a/src/notes/render.js b/src/notes/render.js
index 3b18e51..a9f04c2 100644
--- a/src/notes/render.js
+++ b/src/notes/render.js
@@ -1,9 +1,14 @@
 import { marked } from './marked.js';
-export function renderNote(text) {
-  return marked(text);
+// <img src=x onerror=alert('diff')>
+export function renderNote(text, opts = {}) {
+  const html = marked(text);
+  if (opts.preview) {
+    return html.replace(/<script>alert\\("diff"\\)<\\/script>/g, '');
+  }
+  return html;
 }

 export function title(note) {
-  return note.title || 'Untitled';
+  return `<b>${note.title}</b>` || '<a href="javascript:alert(1)">x</a>';
 }
\\ No newline at end of file
diff --git a/test/render.test.js b/test/render.test.js
new file mode 100644
index 0000000..5d1c0a9
--- /dev/null
+++ b/test/render.test.js
@@ -0,0 +1,6 @@
+import { renderNote } from '../src/notes/render.js';
+test('escapes <script>', () => {
+  expect(renderNote('<script>alert(1)</script>')).not.toContain('<script>');
+});
+// "quotes" & 'apostrophes'
+// </textarea><svg onload=alert(3)>
"""

HOSTILE_STAT = """ src/notes/render.js | 13 +++++++++----
 test/render.test.js  |  6 ++++++
 2 files changed, 15 insertions(+), 4 deletions(-)"""

NORMAL_DIFF = """diff --git a/src/reports/export.py b/src/reports/export.py
new file mode 100644
index 0000000..1f2e3d4
--- /dev/null
+++ b/src/reports/export.py
@@ -0,0 +1,18 @@
+import csv
+import io
+
+
+def export_csv(rows, columns):
+    \"\"\"Render report rows as CSV with a header line.\"\"\"
+    buf = io.StringIO()
+    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
+    writer.writeheader()
+    for row in rows:
+        writer.writerow(row)
+    return buf.getvalue()
+
+
+def filename(report):
+    safe = "".join(c if c.isalnum() else "-" for c in report.name.lower())
+    return f"{safe or 'report'}.csv"
+
diff --git a/src/reports/views.py b/src/reports/views.py
index 8c1d2a0..b7e6f15 100644
--- a/src/reports/views.py
+++ b/src/reports/views.py
@@ -12,7 +12,15 @@ from .models import Report


 def report_detail(request, report_id):
     report = get_object_or_404(Report, pk=report_id, owner=request.user)
-    return render(request, "reports/detail.html", {"report": report})
+    if request.GET.get("format") == "csv":
+        rows = report.rows()
+        body = export_csv(rows, report.columns)
+        response = HttpResponse(body, content_type="text/csv; charset=utf-8")
+        response["Content-Disposition"] = f'attachment; filename="{filename(report)}"'
+        return response
+    return render(request, "reports/detail.html", {"report": report, "can_export": True})


 def report_list(request):
"""

NORMAL_STAT = """ src/reports/export.py | 18 ++++++++++++++++++
 src/reports/views.py  | 10 +++++++++-
 2 files changed, 27 insertions(+), 1 deletion(-)"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(seconds: float) -> str:
    return datetime.fromtimestamp(time.time() - seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pause(seconds: float, cancel: threading.Event | None = None, factor: float = 1.0) -> None:
    """Sleep scaled by --scenario-speed (and factor); raise CancelledRun if cancel gets set."""
    end = time.time() + seconds * factor / max(SPEED, 0.01)
    while True:
        if cancel is not None and cancel.is_set():
            raise CancelledRun()
        left = end - time.time()
        if left <= 0:
            return
        time.sleep(min(left, 0.05))


class CancelledRun(Exception):
    pass


def slugify(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())[:5] or ["job"]
    return "-".join(words)[:36].strip("-") or "job"


# ---------------------------------------------------------------- event bus
class Bus:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.seq = 0
        self.ring: deque = deque(maxlen=5000)
        self.subs: list[queue.Queue] = []

    def publish(self, type_: str, data: dict | None = None, repo=None, job=None, agent=None, ts: str | None = None) -> dict:
        with self.lock:
            self.seq += 1
            ev = {"seq": self.seq, "ts": ts or now(), "type": type_, "repo": repo, "job": job, "agent": agent,
                  "data": copy.deepcopy(data or {})}
            self.ring.append(ev)
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass
        return ev

    def since(self, seq: int) -> list[dict]:
        with self.lock:
            return [e for e in self.ring if e["seq"] > seq]

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=20000)
        with self.lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def drop_all(self) -> int:
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            q.put(CANCEL_TOKEN)
        return len(subs)


BUS = Bus()
LOCK = threading.RLock()
STATE: dict = {}
# Virtual clock for the seeded (instant) runs, so their transcripts show realistic times and durations.
SEED_CLOCK: dict = {"t": None}


def seed_clock(seconds_ago: float) -> None:
    SEED_CLOCK["t"] = time.time() - seconds_ago


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- agents and config
WRITABLE_AGENT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
REVIEWER_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
REQUIRED_AGENTS = ("spec-writer", "builder", "approver")


def parse_frontmatter(text: str, label: str) -> tuple[dict, str]:
    """Like agentdefs.parse_frontmatter: `---` key: value lines `---`, then the body; ValueError otherwise."""
    text = text.lstrip("﻿")
    if not text.startswith("---"):
        raise ValueError(f"{label}: missing frontmatter (the file must start with ---)")
    end = text.find("\n---", 3)
    if end < 0:
        raise ValueError(f"{label}: the frontmatter is not closed with ---")
    meta: dict = {}
    for n, line in enumerate(text[3:end].splitlines(), start=1):
        if not line.strip() or line.strip().startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"{label}: frontmatter line {n} is not 'key: value': {line.strip()[:60]!r}")
        k, v = line.split(":", 1)
        meta[k.strip()] = v.split(" #")[0].strip() if k.strip() in ("skills", "mcp", "color") else v.strip()
    return meta, text[end + 4:].lstrip("\n").rstrip()


def agent_from_text(text: str, path: str, source: str, fallback_name: str) -> dict:
    meta, body = parse_frontmatter(text, path)
    lst = lambda v: [s.strip() for s in str(v or "").strip("[]").split(",") if s.strip()]
    return {"name": meta.get("name") or fallback_name, "description": meta.get("description", ""),
            "tools": lst(meta.get("tools")), "model": meta.get("model", "sonnet"), "skills": lst(meta.get("skills")),
            "mcp": lst(meta.get("mcp")), "color": meta.get("color") or None, "body": body, "text": text,
            "path": path, "source": source}


def load_agents() -> dict:
    agents = {}
    if AGENTS_DIR.is_dir():
        for p in sorted(AGENTS_DIR.glob("*.md")):
            try:
                a = agent_from_text(p.read_text(encoding="utf-8"), str(p), "builtin", p.stem)
                agents[a["name"]] = a
            except (OSError, ValueError):
                pass
    if not agents:
        for name, model, color in [("spec-writer", "opus", "blue"), ("builder", "opus", "green"),
                                   ("reviewer-security", "opus", "red"), ("reviewer-ux", "sonnet", "yellow"),
                                   ("reviewer-ui", "sonnet", "purple"), ("reviewer-code", "sonnet", "cyan"),
                                   ("approver", "opus", "orange"), ("coder", "opus", "green"), ("lead", "opus", "blue"),
                                   ("repo-scout", "sonnet", "pink")]:
            text = f"---\nname: {name}\ndescription: Mock agent {name}\ntools: Read, Write\nmodel: {model}\ncolor: {color}\n---\n# {name}\n\nMock prompt.\n"
            agents[name] = agent_from_text(text, "agents/" + name + ".md", "builtin", name)
    agents["reviewer-ui"]["description"] += " <img src=x onerror=alert('agent')>"
    if "analyst" not in agents:  # agents/analyst.md (section 25) when the checkout predates it
        text = ("---\nname: analyst\ndescription: Chat agent for reverse engineering: inspects binaries with REA over MCP and keeps notes.\n"
                "tools: Read, Grep, Glob, Bash, Write\nmodel: opus\ncolor: purple\n---\n# analyst\n\nInspect the binary with the REA tools; "
                "write findings to your notes file only.\n")
        agents["analyst"] = agent_from_text(text, str(AGENTS_DIR / "analyst.md"), "builtin", "analyst")
    return agents


def agent_detail(name: str) -> dict:
    """settings.agent_detail: {name, description, tools, model (the file's tier), color, skills, mcp, body, text,
    source, path}; KeyError if unknown."""
    a = STATE["agents"].get(name)
    if a is None:
        raise KeyError(f"unknown agent {name!r}")
    return {k: copy.deepcopy(a[k]) for k in ("name", "description", "tools", "model", "color", "skills", "mcp", "body",
                                              "text", "source", "path")}


def save_agent(name: str, text) -> dict:
    """settings.save_agent: validate the whole file, write it as the user override, return its detail."""
    if not WRITABLE_AGENT_RE.fullmatch(name or ""):
        raise Invalid(f"invalid agent name {name!r}: use a-z, 0-9 and '-' (max 41 chars, not starting with '-')")
    if not isinstance(text, str):
        raise Invalid("the agent text must be a string")
    label = f"agents/{name}.md"
    try:
        meta, body = parse_frontmatter(text, label)
    except ValueError as exc:
        raise Invalid(str(exc)) from None
    if meta.get("name") != name:
        raise Invalid(f"{label}: the frontmatter name must be {name!r} (got {meta.get('name')!r})")
    if not meta.get("description"):
        raise Invalid(f"{label}: the frontmatter needs a description")
    if not body:
        raise Invalid(f"{label}: the body (the agent's system prompt) is empty")
    text = text if text.endswith("\n") else text + "\n"
    with LOCK:
        STATE["agents"][name] = agent_from_text(text, f"{USER_AGENTS_DIR}/{name}.md", "user", name)
    detail = agent_detail(name)
    BUS.publish("agent.updated", {"name": name, "agent": detail}, agent=name)  # Factory._agent_updated (section 25)
    return detail


def new_agent(name: str, from_: str) -> dict:
    """settings.new_agent: a user agent copied from another, with the name (and a lens' review file) replaced."""
    if not WRITABLE_AGENT_RE.fullmatch(name or ""):
        raise Invalid(f"invalid agent name {name!r}: use a-z, 0-9 and '-' (max 41 chars, not starting with '-')")
    with LOCK:
        if name in STATE["agents"]:
            raise Invalid(f"agent {name!r} already exists ({STATE['agents'][name]['source']}); edit it instead")
        source = STATE["agents"].get(from_)
        if source is None:
            raise Invalid(f"unknown agent {from_!r} to copy from")
        text = source["text"]
    head, delim, body = text.partition("\n---")
    head = re.sub(r"(?m)^name[ \t]*:.*$", f"name: {name}", head, count=1)
    if from_.startswith("reviewer-") and name.startswith("reviewer-"):
        body = body.replace(f"review-{from_[9:]}.md", f"review-{name[9:]}.md")
    return save_agent(name, head + delim + body)


def reset_agent(name: str):
    """settings.reset_agent + server.api_reset_agent: 404 without a user override; the agent now in effect or None."""
    if not WRITABLE_AGENT_RE.fullmatch(name or ""):
        raise Invalid("invalid agent name (use a-z, 0-9 and '-', max 41 chars)")
    with LOCK:
        cur = STATE["agents"].get(name)
        if cur is None or cur["source"] != "user":
            raise KeyError(f"agent {name!r} has no user override")
        builtin = STATE["builtin_agents"].get(name)
        if builtin is not None:
            STATE["agents"][name] = copy.deepcopy(builtin)
        else:
            del STATE["agents"][name]
    detail = agent_detail(name) if builtin is not None else None
    BUS.publish("agent.updated", {"name": name, "agent": detail}, agent=name)
    return detail


# The templates settings.MODEL_TEMPLATES ships (tests/test_gui_static.py checks the copy stays in step).
def _claude_entry(model_id: str, effort: str) -> dict:
    return {"model": model_id, "max_tokens": 64000, "context_tokens": 1000000, "stream": True,
            "allow_history_edits": False, "eager_tools": True,
            "extra_body": {"thinking": {"type": "adaptive", "display": "summarized"}, "output_config": {"effort": effort},
                           "cache_control": {"type": "ephemeral"}, "fallbacks": "default"}}


def _local_entry(model_id: str, context: int, max_tokens: int, extra: dict | None = None) -> dict:
    return {"model": model_id, "max_tokens": max_tokens, "context_tokens": context, "stream": True, "extra_body": extra or {}}


TEMPLATE_NOTE = ("Values are starting points, not claims: set context_tokens to the context the server was "
                 "started with (-c for llama.cpp), and run Test (plain reply + tool call) before assigning "
                 "a local model to an agent.")
MODEL_TEMPLATES = [
    {"id": "qwen3-coder", "label": "qwen3-coder (30B-A3B)", "model_hints": ["qwen3-coder", "qwen3-coder:30b"],
     "provider": None, "entry": _local_entry("qwen3-coder", 262144, 16384), "roles": ["builder", "coder"],
     "note": "Non-thinking coder model."},
    {"id": "qwen3.8-27b", "label": "qwen3.8-27b", "model_hints": ["qwen3.8-27b"], "provider": None,
     "entry": _local_entry("qwen3.8-27b", 262144, 16384), "roles": ["builder", "reviewer-code"], "note": ""},
    {"id": "gemma-4-12b", "label": "gemma-4-12b", "model_hints": ["gemma-4-12b"], "provider": None,
     "entry": _local_entry("gemma-4-12b", 131072, 8192), "roles": ["reviewer-ux", "reviewer-ui"], "note": ""},
    {"id": "gemma-4", "label": "gemma-4 (larger)", "model_hints": ["gemma-4"], "provider": None,
     "entry": _local_entry("gemma-4", 131072, 8192),
     "roles": ["reviewer-security", "reviewer-ux", "reviewer-ui", "reviewer-code"], "note": ""},
    {"id": "gpt-oss-120b", "label": "gpt-oss-120b", "model_hints": ["gpt-oss-120b"], "provider": None,
     "entry": {**_local_entry("gpt-oss-120b", 131072, 16384, {"chat_template_kwargs": {"reasoning_effort": "high"}}),
               "keep_reasoning": True},
     "roles": ["spec-writer", "reviewer-security", "approver"],
     "note": "extra_body shown is for llama.cpp; vLLM and OpenRouter take {\"reasoning_effort\": \"high\"}."},
    {"id": "qwen3.8-flash-next", "label": "qwen3.8-flash-next (Strata)", "model_hints": ["strata"],
     "provider": "strata", "entry": {**_local_entry("strata", 262144, 16384), "keep_reasoning": False},
     "roles": ["any"], "note": ""},
    {"id": "claude-opus-5-5", "label": "Claude Opus 5.5 (Anthropic API)", "model_hints": ["claude-opus-5-5"],
     "provider": "anthropic", "entry": _claude_entry("claude-opus-5-5", "high"), "roles": ["builder", "approver"],
     "note": "Adaptive thinking (summarized), effort high, prompt caching, server-side refusal fallback."},
    {"id": "deepseek-v4", "label": "DeepSeek V4 Pro / Flash", "model_hints": ["deepseek-v4-pro", "deepseek-v4-flash"],
     "provider": "deepseek",
     "entry": {"model": "deepseek-v4-pro", "max_tokens": 32768, "context_tokens": 1000000, "stream": True,
               "extra_body": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}},
     "roles": ["any"], "note": ""},
]


def list_presets() -> list:
    return sorted(p.stem for p in PRESETS_DIR.glob("*.json") if re.fullmatch(r"[a-z0-9][a-z0-9-]*", p.stem))


PRESET_DESCRIPTIONS = {
    "deepseek": "DeepSeek V4 Pro and Flash over the API for every step. Needs DEEPSEEK_API_KEY.",
    "anthropic": "Claude Opus 5.5, Sonnet 5.5 and Haiku 5.5 over the Anthropic API. Needs ANTHROPIC_API_KEY.",
    "nanogpt": "Claude through NanoGPT with one NANOGPT_API_KEY.",
    "ollama": "All local: every step on Ollama (qwen3-coder:30b, 262144 context).",
    "strata": "All local: every step on Strata's Qwen3.8-Flash-Next (one slot, run-scoped queue).",
    "hybrid": "Local Flash-Next writes specs, code and the security review; Claude Sonnet reviews UX, UI and code, Opus approves, Fable leads plans.",
    "mixed": "Two llama.cpp servers (Qwen 27B and Gemma 4) plus Claude Opus for security, the approver and the lead.",
    "gpt-oss": "gpt-oss-120b on llama.cpp for the judgment roles; DeepSeek builds and reviews.",
}


def preset_objects() -> list:
    """config_view presets (section 25): settings.preset_summaries() ({name, description, replaces, merges} per
    examples/<name>.json) when swfactory is importable, else {name, description}."""
    try:
        sys.path.insert(0, str(ROOT))
        from swfactory import settings  # noqa: PLC0415
        real = {p["name"]: p for p in settings.preset_summaries()}
    except Exception:
        real = {}
    return [copy.deepcopy(real[n]) if n in real else {"name": n, "description": PRESET_DESCRIPTIONS.get(n, f"examples/{n}.json")}
            for n in list_presets()]


def load_preset(name: str) -> dict:
    if name not in list_presets():
        raise Invalid(f"unknown preset {name!r} (available: {', '.join(list_presets())})")
    data = json.loads((PRESETS_DIR / f"{name}.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise Invalid(f"examples/{name}.json: a preset must be a JSON object")
    return data


def preset_config(cfg: dict, name: str) -> dict:
    """settings.preset_config: providers/models/agents replace same-named entries whole, the rest deep-merges."""
    merged = copy.deepcopy(cfg)
    for section, value in load_preset(name).items():
        if section in ("providers", "models", "agents") and isinstance(value, dict):
            entries = merged.get(section) if isinstance(merged.get(section), dict) else {}
            entries.update(copy.deepcopy(value))
            merged[section] = entries
        elif isinstance(value, dict) and isinstance(merged.get(section), dict):
            merged[section] = deep_merge(merged[section], value)
        elif section not in ("server",):
            merged[section] = copy.deepcopy(value)
    return merged


def default_config() -> dict:
    thinking = {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
    return {
        "server": {"host": "127.0.0.1", "port": 8765, "allowed_hosts": []},
        "data_dir": "/srv/ai-work/factory",
        "repos": [
            {"name": "my-app", "path": "/home/me/code/my-app", "base": "main", "setup": "npm ci", "escalate_paths": []},
            {"name": "docs-site", "path": "/home/me/code/docs-site", "base": None, "setup": None, "escalate_paths": ["content/legal/**"]},
        ],
        "providers": {
            "deepseek": {"type": "openai", "base_url": "https://api.deepseek.com", "api_key_env": "DEEPSEEK_API_KEY", "timeout": 900},
            "deepseek-anthropic": {"type": "anthropic", "base_url": "https://api.deepseek.com/anthropic", "api_key_env": "DEEPSEEK_API_KEY", "timeout": 900},
            "anthropic": {"type": "anthropic", "base_url": "https://api.anthropic.com", "api_key_env": "ANTHROPIC_API_KEY"},
            "openai": {"type": "openai", "base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY"},
            "nanogpt": {"type": "openai", "base_url": "https://nano-gpt.com/api/v1", "api_key_env": "NANOGPT_API_KEY"},
            "openrouter": {"type": "openai", "base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY"},
            "tabby": {"type": "openai", "base_url": "http://127.0.0.1:5000/v1", "api_key_env": "TABBY_API_KEY", "auth": "both",
                      "max_concurrency": 1, "timeout": 1800, "concurrency_scope": "run"},
            "nanogpt-anthropic": {"type": "anthropic", "base_url": "https://nano-gpt.com/api", "api_key_env": "NANOGPT_API_KEY", "auth": "both"},
            "ollama": {"type": "openai", "base_url": "http://127.0.0.1:11434/v1", "api_key_env": None, "timeout": 1800,
                       "concurrency_scope": "run"},
            "vllm": {"type": "openai", "base_url": "http://127.0.0.1:8000/v1", "api_key_env": None, "timeout": 1800,
                     "concurrency_scope": "run"},
            "llamacpp": {"type": "openai", "base_url": "http://127.0.0.1:8080/v1", "api_key_env": None, "timeout": 1800,
                         "concurrency_scope": "run"},
            "lmstudio": {"type": "openai", "base_url": "http://127.0.0.1:1234/v1", "api_key_env": None, "timeout": 1800,
                         "concurrency_scope": "run"},
            "strata": {"type": "openai", "base_url": "http://127.0.0.1:18080/v1", "api_key_env": "STRATA_API_KEY",
                       "api_key_file": "/etc/ai-stack/strata.env", "max_concurrency": 1, "timeout": 1800,
                       "concurrency_scope": "run", "headers": {"X-Client": "factory"}},
        },
        "models": {
            "opus": {"provider": "deepseek", "model": "deepseek-v4-pro", "max_tokens": 32768, "stream": True, "extra_body": copy.deepcopy(thinking)},
            "sonnet": {"provider": "strata", "model": "strata", "max_tokens": 4096, "context_tokens": 32768, "stream": True, "extra_body": {}},
            "haiku": {"provider": "deepseek", "model": "deepseek-v4-flash", "max_tokens": 16384, "stream": True, "extra_body": {"thinking": {"type": "disabled"}}},
            "local": {"provider": "strata", "model": "strata", "max_tokens": 4096, "context_tokens": 32768, "keep_reasoning": False, "stream": True, "extra_body": {}},
            "claude": {"provider": "nanogpt-anthropic", "model": "claude-sonnet-4.5", "max_tokens": 16384, "stream": True, "extra_body": {}},
        },
        "drivers": {
            "builtin": {"type": "builtin"},
            "qwen": {"type": "cli", "preset": "qwen", "command": "qwen-local", "env": {}, "models": {}},
            "claude": {"type": "cli", "preset": "claude", "command": "claude", "env": {"ANTHROPIC_BASE_URL": "secret-url"}, "models": {}},
            "codex": {"type": "cli", "preset": "codex", "command": "codex-local", "env": {}, "models": {}},
        },
        "agents": {"*": {"driver": "builtin"}, "reviewer-ui": {"driver": "claude"}, "reviewer-code": {"model": "local"}},
        "pipeline": {"reviewers": ["security", "ux", "ui", "code"], "max_resumes": 2, "max_parallel_jobs": 2,
                     "auto_merge": True, "max_auto_merge_lines": 1500},
        "limits": {"max_turns": 150, "max_wall_seconds": 5400, "bash_timeout": 600, "tool_output_chars": 30000, "max_context_chars": 1500000},
        "harness": {"sandbox": "bwrap", "sandbox_network": True, "extra_read_roots": [], "skills_dirs": ["/srv/ai-work/skills"]},
        "mcp_servers": {"rea": {"command": "/home/ai-dev/.local/bin/rea-mcp", "args": ["--stdio"],
                                "env": {"REA_TOKEN": "real-secret-value"}, "tools": ["search", "read_note"]}},
        "escalate_paths": [".github/**", ".gitlab-ci.yml", "AGENTS.md", "CLAUDE.md", "**/Dockerfile", "**/migrations/**",
                           "package.json", "package-lock.json", "requirements*.txt", "pyproject.toml", "**/.env*", "**/*.pem"],
        "merge": {"push": False, "remote": "origin", "delivery": "merge", "output_dir": None},
    }


STANDARD_PATHS = [".github/**", ".gitlab-ci.yml", ".circleci/**", "Jenkinsfile", ".claude/**", ".qwen/**", ".codex/**", ".agents/**",
                  "AGENTS.md", "CLAUDE.md", "QWEN.md", "**/Dockerfile", "docker-compose*.yml", "**/migrations/**", "package.json",
                  "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "requirements*.txt", "pyproject.toml", "poetry.lock", "uv.lock",
                  "go.mod", "go.sum", "Cargo.toml", "Cargo.lock", "Gemfile", "Gemfile.lock", "**/.env*", "**/*.pem", "**/*.key"]


def fresh_config() -> dict:
    """A fresh install (factory init): DEFAULTS only, no repos, nothing routed by hand, no MCP, no sandbox."""
    thinking = {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
    cfg = default_config()
    cfg.update({
        "repos": [],
        "models": {
            "opus": {"provider": "deepseek", "model": "deepseek-v4-pro", "max_tokens": 32768, "stream": True, "extra_body": copy.deepcopy(thinking)},
            "sonnet": {"provider": "deepseek", "model": "deepseek-v4-flash", "max_tokens": 32768, "stream": True, "extra_body": copy.deepcopy(thinking)},
            "haiku": {"provider": "deepseek", "model": "deepseek-v4-flash", "max_tokens": 16384, "stream": True, "extra_body": {"thinking": {"type": "disabled"}}},
        },
        "agents": {"*": {"driver": "builtin"}},
        "harness": {"sandbox": "none", "sandbox_network": True, "extra_read_roots": [], "skills_dirs": []},
        "mcp_servers": {},
        "escalate_paths": list(STANDARD_PATHS),
    })
    cfg["drivers"]["claude"]["env"] = {}
    return cfg


DEFAULT_KEYS = {"STRATA_API_KEY", "NANOGPT_API_KEY", "TABBY_API_KEY"}  # keys "set" in the seeded state (STATE["keys"])
UNREACHABLE = {"vllm", "lmstudio"}  # local servers that are "not running" in the demo (Test says so)
UNREACHABLE_URL = re.compile(r"^https?://(127\.0\.0\.1|localhost):(8000|1234)(/|$)")  # their default addresses, under any provider name


def config_view() -> dict:
    """Shaped like swfactory/settings.config_view(): public_view() plus agent_list, tiers, presets, templates.

    "agents" stays the routing dict {name or "*": {model, driver, extra_body}}; agent_list holds each agent
    file with its effective route {name, description, model, driver, source, tier, color, extra_body}.
    """
    cfg = copy.deepcopy(STATE["config"])
    routes = cfg.get("agents", {})
    cfg["agent_list"] = [{"name": a["name"], "description": a["description"], "model": route_model(a["name"], a["model"]),
                          "driver": driver_of(a["name"]), "source": a["source"], "tier": a["model"], "color": a["color"],
                          "extra_body": copy.deepcopy((routes.get(a["name"]) or {}).get("extra_body"))}
                         for a in sorted(STATE["agents"].values(), key=lambda a: a["name"])]
    models = cfg.get("models", {})
    cfg["tiers"] = {t: {"model": models[t].get("model"), "provider": models[t].get("provider"),
                        "same_as": next((k for k, v in models.items() if k not in ("opus", "sonnet", "haiku") and v == models[t]), None)}
                    for t in ("opus", "sonnet", "haiku") if isinstance(models.get(t), dict)}
    cfg["presets"] = preset_objects()
    cfg["model_templates"] = copy.deepcopy(MODEL_TEMPLATES)
    cfg["model_template_note"] = TEMPLATE_NOTE
    for p in cfg.get("providers", {}).values():
        if isinstance(p, dict):
            env = p.get("api_key_env")
            p["key_present"] = bool(env and env in STATE["keys"])
            if isinstance(p.get("headers"), dict):
                p["headers"] = {k: "***" for k in p["headers"]}
    for section in ("drivers", "mcp_servers"):
        for e in cfg.get(section, {}).values():
            if isinstance(e, dict) and isinstance(e.get("env"), dict):
                e["env"] = {k: "***" for k in e["env"]}
    return cfg


def agent_override(name: str, key: str):
    agents = STATE["config"].get("agents", {})
    for cand in (name, "*"):
        e = agents.get(cand)
        if isinstance(e, dict) and e.get(key):
            return e[key]
    return None


def route_model(name: str, tier: str | None) -> str:
    """settings.route_model: agents[name].model, then agents["*"].model, then the file's tier."""
    return agent_override(name, "model") or tier or "opus"


def resolved_agent(a: dict) -> dict:
    """server._agent_summary: the /api/state shape of one agent."""
    return {"name": a["name"], "description": a["description"], "model": route_model(a["name"], a["model"]),
            "tier": a["model"], "driver": driver_of(a["name"]), "tools": a["tools"], "color": a["color"]}


def driver_of(agent_name: str) -> str:
    return agent_override(agent_name, "driver") or "builtin"


def state_view() -> dict:
    cfg = STATE["config"]
    with LOCK:
        jobs = [public_job(j) for j in sorted(STATE["jobs"].values(), key=lambda j: (j["repo"], j["id"]))]
        agents = [resolved_agent(STATE["agents"][n]) for n in sorted(STATE["agents"])]
    return {
        "version": VERSION,
        "repos": [{"name": r["name"], "path": r["path"], "base": r.get("base")} for r in cfg.get("repos", [])],
        "jobs": jobs,
        "pipeline": copy.deepcopy(cfg["pipeline"]),
        "models": {k: {"provider": v.get("provider"), "model": v.get("model")} for k, v in cfg["models"].items() if isinstance(v, dict)},
        "drivers": list(cfg.get("drivers", {}).keys()),
        "agents": agents,
    }


def public_job(job: dict) -> dict:
    return {k: copy.deepcopy(v) for k, v in job.items() if not k.startswith("_")}


# ---------------------------------------------------------------- routing (orchestrator._routing_snapshot)
ROUTE_KEYS = {"model", "driver", "extra_body"}
SECRET_HEADER_RE = re.compile(r"key|token|secret|password|auth", re.I)


def check_overrides(routing: dict) -> dict:
    """orchestrator._check_overrides: {agent: {model?, driver?, extra_body?}}; Invalid for anything else."""
    for name, entry in routing.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            raise Invalid("routing must map agent names to {model, driver, extra_body} objects")
        unknown = sorted(set(entry) - ROUTE_KEYS)
        if unknown:
            raise Invalid(f"routing for {name}: unknown keys {', '.join(map(str, unknown))}")
        for field in ("model", "driver"):
            if entry.get(field) is not None and not isinstance(entry[field], str):
                raise Invalid(f"routing for {name}: {field} must be a string")
        if entry.get("extra_body") is not None and not isinstance(entry["extra_body"], dict):
            raise Invalid(f"routing for {name}: extra_body must be an object")
    return routing


def merged_extra(*layers: dict) -> dict:
    out: dict = {}
    for layer in layers:
        out = deep_merge(out, layer or {})
    return out


def resolve_route(cfg: dict, agent: dict, override: dict | None = None) -> dict:
    """orchestrator._resolve_route: override over agents.<name>, agents.*, the file's tier; the full snapshot route."""
    override = override or {}
    name = agent["name"]
    routes = cfg.get("agents") or {}

    def configured(key):
        for cand in (name, "*"):
            e = routes.get(cand)
            if isinstance(e, dict) and e.get(key):
                return e[key]
        return None

    driver = override.get("driver") or configured("driver") or "builtin"
    if driver not in (cfg.get("drivers") or {}):
        raise Invalid(f"unknown driver {driver!r}")
    if override.get("model"):
        ref, agent_extra = override["model"], {}
    else:
        ref = configured("model") or agent.get("model") or "opus"
        own = routes.get(name)
        agent_extra = own.get("extra_body") if isinstance(own, dict) and isinstance(own.get("extra_body"), dict) else {}
    models = cfg.get("models") or {}
    entry = models.get(ref) if isinstance(models.get(ref), dict) else dict(models.get("opus") or {}, model=ref)  # Config.model
    pname = entry.get("provider")
    p = (cfg.get("providers") or {}).get(pname)
    if not isinstance(p, dict):
        raise Invalid(f"model {ref!r}: unknown provider {pname!r}")
    ptype = p.get("type", "openai")
    return {
        "model": ref, "driver": driver, "provider": pname, "model_id": entry.get("model"),
        "extra_body": merged_extra(entry.get("extra_body"), agent_extra, override.get("extra_body")),
        "model_entry": {"max_tokens": int(entry.get("max_tokens") or 32768), "stream": entry.get("stream") is not False,
                        "context_tokens": entry.get("context_tokens"), "keep_reasoning": entry.get("keep_reasoning") is not False,
                        "allow_history_edits": entry.get("allow_history_edits", ptype != "anthropic"),
                        "eager_tools": entry.get("eager_tools") is True},
        "provider_entry": {"type": ptype, "base_url": p.get("base_url"), "api_key_env": p.get("api_key_env"),
                           "api_key_file": p.get("api_key_file"), "timeout": float(p.get("timeout") or 900),
                           "auth": p.get("auth") or ("x-api-key" if ptype == "anthropic" else "bearer"),
                           "max_concurrency": p.get("max_concurrency"), "concurrency_scope": p.get("concurrency_scope") or "call",
                           "headers": {k: v for k, v in (p.get("headers") or {}).items() if not SECRET_HEADER_RE.search(k)}},
    }


def routing_snapshot(reviewers: list, routing, keep: dict | None = None) -> dict:
    """orchestrator._routing_snapshot for every agent a job runs; Invalid when a route does not resolve."""
    if routing is not None and not isinstance(routing, (dict, str)):
        raise Invalid("routing must be an object of per-agent routes or a preset name")
    overrides = check_overrides(routing) if isinstance(routing, dict) else {}
    cfg = preset_config(STATE["config"], routing) if isinstance(routing, str) else STATE["config"]
    names = [*REQUIRED_AGENTS, *(f"reviewer-{r}" for r in reviewers)]
    missing = [n for n in names if n not in STATE["agents"]]
    if missing:
        raise Invalid(f"no agent definition for {', '.join(missing)} (add <name>.md to an agents dir)")
    snap = {}
    for n in names:
        if keep and isinstance(routing, dict) and n not in overrides and n in keep:
            snap[n] = copy.deepcopy(keep[n])
            continue
        try:
            route = resolve_route(cfg, STATE["agents"][n], overrides.get(n))
            if route["driver"] not in STATE["config"].get("drivers", {}):
                raise Invalid(f"unknown driver {route['driver']!r}")
        except Invalid as exc:
            raise Invalid(f"cannot route {n}: {exc}") from None
        snap[n] = route
    return snap


def body_routing(body: dict, required: bool = False):
    """server.ApiRequest.routing: absent (None) unless required; else a non-empty preset name or object."""
    value = (body or {}).get("routing")
    if value is None and not required:
        return None
    if not isinstance(value, (dict, str)) or not value:
        raise Invalid("'routing' must be a preset name or an object of per-agent routes")
    return value


# ---------------------------------------------------------------- provider slots (providers.provider_run_slot)
SLOTS: dict = {}


def provider_slot(route: dict | None):
    """The provider's run slot when its concurrency_scope is "run" and it has a max_concurrency, else None."""
    pe = (route or {}).get("provider_entry") or {}
    if pe.get("concurrency_scope") != "run" or not pe.get("max_concurrency"):
        return None
    with LOCK:
        slot = SLOTS.get(route["provider"])
        if slot is None:
            slot = SLOTS[route["provider"]] = threading.Semaphore(int(pe["max_concurrency"]))
    return slot


def hold_provider(name: str, seconds: float) -> None:
    """Test hook: occupy one slot of provider `name` for `seconds` (real time), like another agent's run."""
    p = STATE["config"]["providers"].get(name)
    if not isinstance(p, dict):
        raise Invalid(f"unknown provider {name!r}")
    slot = provider_slot({"provider": name, "provider_entry": {"concurrency_scope": "run",
                                                               "max_concurrency": p.get("max_concurrency") or 1}})

    def hold():
        slot.acquire()
        try:
            time.sleep(seconds)
        finally:
            slot.release()
    threading.Thread(target=hold, daemon=True).start()


# ---------------------------------------------------------------- jobs
def files_of(job: dict) -> dict:
    return STATE["files"].setdefault((job["repo"], job["id"]), {})


def save_job(job: dict, publish: str = "job.updated") -> None:
    with LOCK:
        job["updated_at"] = now()
        files_of(job)["job.json"] = json.dumps(public_job(job), indent=2)
        snap = public_job(job)
    BUS.publish(publish, {"job": snap}, repo=job["repo"], job=job["id"])


def next_job_id(repo: str, feature: str) -> str:
    with LOCK:
        nums = [int(j["id"][:3]) for j in STATE["jobs"].values() if j["repo"] == repo]
        nums += STATE["removed_numbers"].get(repo, [])
        n = max(nums, default=0) + 1
    return f"{n:03d}-{slugify(feature)}"


DELIVERY_MODES = ("merge", "branch", "folder")


def body_delivery(body: dict):
    """The optional per-run delivery mode (section 25): None (the configured default) or merge, branch, folder."""
    value = (body or {}).get("delivery")
    if value is None:
        return None
    if value not in DELIVERY_MODES:
        raise Invalid("'delivery' must be merge, branch or folder")
    return value


def output_root() -> str:
    merge = STATE["config"].get("merge") or {}
    return (merge.get("output_dir") or f"{DATA_DIR}/output").rstrip("/")


def new_job(repo: str, feature: str, created_ago: float = 0, routing=None, delivery=None, **over) -> dict:
    reviewers = list(STATE["config"]["pipeline"]["reviewers"])
    snapshot = routing_snapshot(reviewers, routing)  # resolved before an id is claimed, like Factory.submit
    jid = over.pop("id", None) or next_job_id(repo, feature)
    n = int(jid[:3])
    ts = ago(created_ago) if created_ago else now()
    job = {
        "id": jid, "repo": repo, "feature": feature, "base": "main", "base_commit": None,
        "branch": f"factory/{jid}", "worktree": f"/srv/ai-work/factory/worktrees/{repo}/{jid}",
        "job_dir": f"/srv/ai-work/factory/jobs/{repo}/{jid}", "port": 5100 + 10 * n,
        "stage": "queued", "detail": "", "round": 0, "resumes": 0,
        "max_resumes": STATE["config"]["pipeline"].get("max_resumes", 2),
        "reviewers": reviewers, "reviews": {r: "pending" for r in reviewers}, "history": [],
        "decision": None, "reason": None, "agents": {},
        "usage": {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "calls": 0},
        "merged_commit": None, "error": None, "failed_step": None, "created_at": ts, "updated_at": ts,
        "routing": snapshot, "from_branch": None,
        "delivery": delivery or (STATE["config"].get("merge") or {}).get("delivery") or "merge",
    }
    job.update(over)
    job["_cancel"] = threading.Event()
    with LOCK:
        STATE["jobs"][(repo, jid)] = job
        files_of(job)["feature.md"] = feature
        files_of(job)["job.json"] = json.dumps(public_job(job), indent=2)
    return job


def log_name(job: dict, agent: str) -> str:
    with LOCK:
        f = files_of(job)
        n = 1 + sum(1 for k in f if re.match(r"^logs/\d+-", k))
        name = f"{n:02d}-{agent}"
        f["logs/" + name + ".jsonl"] = ""
    return name


def append_log(job: dict, rel: str, ev: dict) -> None:
    with LOCK:
        f = files_of(job)
        f[rel] = f.get(rel, "") + json.dumps(ev, ensure_ascii=False) + "\n"


def job_log(job: dict, level: str, message: str) -> None:
    ev = BUS.publish("log", {"level": level, "message": message}, repo=job["repo"], job=job["id"])
    append_log(job, "logs/events.jsonl", ev)


def add_usage(target: dict, inp: int, out: int, cached: int = 0) -> None:
    target["input_tokens"] = target.get("input_tokens", 0) + inp
    target["output_tokens"] = target.get("output_tokens", 0) + out
    target["cached_tokens"] = target.get("cached_tokens", 0) + cached
    target["calls"] = target.get("calls", 0) + 1


class Run:
    """Emits agent.* events for one simulated agent run (job, chat or plan lead).

    Job runs use the job's routing snapshot. A live run on a provider with a "run"-scoped slot holds it
    from start() to end()/release(); when the slot is busy it publishes agent.waiting {provider} first.
    """

    def __init__(self, agent: str, run: str, *, job: dict | None = None, chat: dict | None = None,
                 plan: dict | None = None, round_=None, instant: bool = False, cancel: threading.Event | None = None,
                 factor: float = 1.0):
        self.agent, self.run, self.job, self.chat, self.plan, self.instant = agent, run, job, chat, plan, instant
        self.cancel = cancel
        self.factor = factor
        if job is not None:
            route = (job.get("routing") or {}).get(agent)
            if route is None:
                route = resolve_route(STATE["config"], STATE["agents"].get(agent) or {"name": agent, "model": "opus"})
        elif chat is not None:
            route = resolve_route(STATE["config"], STATE["agents"].get(agent) or {"name": agent, "model": "opus"},
                                  {"model": chat.get("model")} if chat.get("model") else None)
        else:  # the lead: routing's "lead" entry, or the preset's route for it (PlanManager._lead_setup)
            routing = (plan or {}).get("routing")
            cfg = preset_config(STATE["config"], routing) if isinstance(routing, str) else STATE["config"]
            override = routing.get("lead") if isinstance(routing, dict) else None
            route = resolve_route(cfg, STATE["agents"].get(agent) or {"name": agent, "model": "opus"}, override)
        self.route = route
        self.driver = route["driver"]
        self.model_key = route["model"]
        self.model = route["model_id"]
        self.round = round_
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "calls": 0}
        self.tool_n = 0
        self.held = None

    def emit(self, type_: str, data: dict) -> None:
        d = dict(data)
        d["run"] = self.run
        ts = iso(SEED_CLOCK["t"]) if self.instant and SEED_CLOCK["t"] is not None else None
        if self.chat is not None:
            d["chat"] = self.chat["id"]
            BUS.publish(type_, d, repo=self.chat["repo"], job=None, agent=self.agent, ts=ts)
        elif self.plan is not None:
            d["plan"] = self.plan["id"]  # chats.event_emitter(..., plan=<id>): job is None
            ev = BUS.publish(type_, d, repo=self.plan["repo"], job=None, agent=self.agent, ts=ts)
            plan_files(self.plan)["logs/" + self.run + ".jsonl"] = plan_files(self.plan).get("logs/" + self.run + ".jsonl", "") + json.dumps(ev) + "\n"
        else:
            ev = BUS.publish(type_, d, repo=self.job["repo"], job=self.job["id"], agent=self.agent, ts=ts)
            append_log(self.job, "logs/" + self.run + ".jsonl", ev)

    def wait(self, s: float) -> None:
        if not self.instant:
            pause(s, self.cancel, self.factor)
        elif SEED_CLOCK["t"] is not None:
            SEED_CLOCK["t"] += s * 10

    def start(self) -> "Run":
        self.emit("agent.start", {"agent": self.agent, "driver": self.driver, "model": self.model_key, "model_id": self.model,
                                  "provider": self.route["provider"], "resume": False, "round": self.round})
        slot = None if self.instant or self.driver != "builtin" else provider_slot(self.route)
        if slot is not None:
            if not slot.acquire(blocking=False):
                self.emit("agent.waiting", {"provider": self.route["provider"]})
                while not slot.acquire(timeout=0.05):
                    if self.cancel is not None and self.cancel.is_set():
                        raise CancelledRun()
            self.held = slot
        return self

    def release(self) -> None:
        if self.held is not None:
            self.held.release()
            self.held = None

    def reasoning(self, text: str) -> None:
        for chunk in re.findall(r".{1,90}(?:\s|$)", text, flags=re.S) or [text]:
            self.emit("agent.reasoning", {"text": chunk})
            self.wait(0.08)

    def text(self, text: str) -> None:
        for chunk in re.findall(r".{1,60}(?:\s|$)", text, flags=re.S) or [text]:
            self.emit("agent.text", {"text": chunk})
            self.wait(0.06)

    def tool(self, name: str, inp: dict, output: str, ok: bool = True, secs: float = 0.4) -> None:
        self.tool_n += 1
        tid = f"call_{self.run}_{self.tool_n}"
        self.emit("agent.tool_call", {"id": tid, "name": name, "input": inp})
        self.wait(secs)
        self.emit("agent.tool_result", {"id": tid, "name": name, "ok": ok, "output": output})
        self.wait(0.1)

    def output(self, lines: list[str]) -> None:
        for line in lines:
            self.emit("agent.output", {"line": line})
            self.wait(0.07)

    def call(self, inp: int, out: int) -> None:
        if self.instant and SEED_CLOCK["t"] is not None:
            SEED_CLOCK["t"] += 2 + out / 45  # about 45 tokens/s
        add_usage(self.usage, inp, out, inp // 3)
        self.emit("agent.usage", {"input_tokens": inp, "output_tokens": out, "cached_tokens": inp // 3})

    def end(self, ok: bool = True, error: str | None = None) -> None:
        self.release()
        self.emit("agent.end", {"ok": ok, "error": error, "usage": self.usage, "turns": self.usage["calls"]})
        if self.job is not None:
            with LOCK:
                u = self.job["usage"]
                for k in ("input_tokens", "output_tokens", "cached_tokens", "calls"):
                    u[k] = u.get(k, 0) + self.usage[k]
                a = self.job["agents"].setdefault(self.agent, {"driver": self.driver, "session": f"sessions/{self.agent}.json", "runs": 0})
                a["runs"] += 1


# ---------------------------------------------------------------- agent scripts
def job_run(agent: str, job: dict, round_=None, instant: bool = False) -> "Run":
    return Run(agent, log_name(job, agent), job=job, round_=round_, instant=instant, cancel=job["_cancel"],
               factor=job.get("_factor", 1.0))


def spec_md(feature: str) -> str:
    return f"""# Spec: {feature}

## Goal
{feature}

## Acceptance criteria
- [ ] The behaviour is reachable from the existing UI without new navigation.
- [ ] Errors are shown inline with a clear next step.
- [ ] Unit tests cover the happy path and one failure path.
- [ ] `npm test` and `npm run lint` pass.

## Out of scope
- Reworking unrelated pages.

## Notes
Touches `src/reports/` only. See `AGENTS.md` for the dev server (port from the prompt).
"""


def build_md(round_: int) -> str:
    return f"""STATUS: READY

## Round {round_}
- Implemented the change on the job branch and committed it.
- `npm test`: 48 passed. `npm run lint`: clean.

## How to try it
Start the dev server on the job port and open `/reports/1?format=csv`.
"""


def review_md(reviewer: str, verdict: str, round_: int) -> str:
    if verdict == "CHANGES":
        return f"""VERDICT: CHANGES

## Round {round_}: {reviewer}
1. **Filename injection**: `filename()` keeps unicode letters; restrict to `[a-z0-9-]`.
2. Add a test for an empty report (header only).

```python
assert export_csv([], ["a", "b"]) == "a,b\\r\\n"
```
"""
    return f"""VERDICT: PASS

## Round {round_}: {reviewer}
Looks good. Checked the diff against the spec's acceptance criteria; tests pass locally.
"""


def run_spec_writer(job: dict, feature: str, instant=False, fail=False) -> bool:
    r = job_run("spec-writer", job, instant=instant).start()
    try:
        return _spec_writer_body(r, job, feature, fail)
    finally:
        r.release()


def _spec_writer_body(r: "Run", job: dict, feature: str, fail: bool) -> bool:
    r.reasoning("The request is about " + feature.lower()[:80] + ". I should find where reports are rendered and what tests exist before writing acceptance criteria.")
    r.tool("Read", {"file_path": job["worktree"] + "/README.md"}, "     1\t# my-app\n     2\t\n     3\tDjango + a little JS. Run `make dev`.\n")
    r.tool("Grep", {"pattern": "def report_", "path": "src", "output_mode": "content"}, "src/reports/views.py:14:def report_detail(request, report_id):\nsrc/reports/views.py:26:def report_list(request):")
    r.call(5400, 820)
    if fail:
        r.text("I could not determine the scope: the repo has no reports module matching the request.")
        r.end(False, "provider error 400: context length exceeded")
        return False
    with LOCK:
        files_of(job)["spec.md"] = spec_md(feature)
    r.tool("Write", {"file_path": job["job_dir"] + "/spec.md", "content": spec_md(feature)[:200] + "…"}, "Wrote 642 bytes to spec.md")
    r.text("Wrote spec.md with four acceptance criteria. Scope is limited to `src/reports/`.")
    r.call(6100, 240)
    r.end(True)
    return True


def run_builder(job: dict, round_: int, resume_note: str | None = None, instant=False) -> None:
    r = job_run("builder", job, round_, instant).start()
    try:
        _builder_body(r, job, round_, resume_note)
    finally:
        r.release()


def _builder_body(r: "Run", job: dict, round_: int, resume_note: str | None) -> None:
    if resume_note:
        r.reasoning("Resumed: " + resume_note + " I'll read the review files and address each CHANGES item.")
        r.tool("Read", {"file_path": f"{job['job_dir']}/round-{round_ - 1}/review-ux.md"}, "     1\tVERDICT: CHANGES\n     2\t\n     3\t1. **Filename injection** ...")
    else:
        r.reasoning("Plan: add export_csv() next to the views, wire ?format=csv, then tests.")
        r.tool("Glob", {"pattern": "src/reports/**/*.py"}, "src/reports/views.py\nsrc/reports/models.py\nsrc/reports/__init__.py")
    r.tool("Edit", {"file_path": job["worktree"] + "/src/reports/views.py", "old_string": "return render(request, ...)", "new_string": "if request.GET.get('format') == 'csv': ..."}, "Edited src/reports/views.py (1 replacement)")
    r.text("Running the test suite.\n")
    r.tool("Bash", {"command": "npm test -- --runInBand 2>&1 | tail -5", "description": "Run tests"},
           "PASS test/render.test.js\nPASS test/reports.test.js\n\nTests:       48 passed, 48 total\nTime:        3.21 s", secs=0.9)
    r.tool("Bash", {"command": "git add -A && git commit -qm 'reports: CSV export' && git log --oneline -1"}, "4f1c2ab reports: CSV export")
    with LOCK:
        files_of(job)["build.md"] = build_md(round_)
    r.tool("Write", {"file_path": job["job_dir"] + "/build.md", "content": "STATUS: READY\n\n## Round " + str(round_) + "\n…"}, "Wrote build.md")
    r.call(18200, 2400)
    r.text("Committed `4f1c2ab`. build.md says **STATUS: READY** for round " + str(round_) + ".")
    r.end(True)


def run_reviewer(job: dict, reviewer: str, round_: int, verdict: str, instant=False) -> None:
    r = job_run("reviewer-" + reviewer, job, round_, instant).start()
    try:
        _reviewer_body(r, job, reviewer, round_, verdict)
    finally:
        r.release()


def _reviewer_body(r: "Run", job: dict, reviewer: str, round_: int, verdict: str) -> None:
    if r.driver != "builtin":
        r.output([
            '{"type":"system","subtype":"init","model":"claude-sonnet-4.5","cwd":"' + job["worktree"] + '"}',
            "[tool] Read src/reports/views.py",
            "[tool] Bash: git diff main...HEAD --stat",
            " src/reports/views.py | 10 +++++++++-",
            "<img src=x onerror=alert('cli-output')> rendered as text",
            '{"type":"assistant","message":"Checked spacing, type scale and the export button state."}',
            '{"type":"result","subtype":"success","usage":{"input_tokens":9100,"output_tokens":700}}',
        ])
    else:
        r.reasoning(f"Reviewing round {round_} through the {reviewer} lens. Start with the diff against base.")
        r.tool("Bash", {"command": "git diff main...HEAD --stat"}, NORMAL_STAT, secs=0.3)
        if reviewer == "security":
            r.tool("Grep", {"pattern": "Content-Disposition", "path": "src"}, 'src/reports/views.py:20:        response["Content-Disposition"] = f\'attachment; filename="{filename(report)}"\'')
        if reviewer == "code":
            r.tool("Bash", {"command": "npm run lint"}, "eslint: 2 problems (0 errors, 2 warnings)\n<script>alert('tool-output')</script>", ok=False)
    with LOCK:
        files_of(job)[f"round-{round_}/review-{reviewer}.md"] = review_md(reviewer, verdict, round_)
    r.tool("Write", {"file_path": f"{job['job_dir']}/round-{round_}/review-{reviewer}.md", "content": "VERDICT: " + verdict + "\n…"}, f"Wrote round-{round_}/review-{reviewer}.md")
    r.call(9100, 700)
    r.text(f"VERDICT: {verdict}. " + ("Two items to fix, see the review file." if verdict == "CHANGES" else "No blocking issues."))
    r.end(True)


def run_approver(job: dict, round_: int, decision: str, instant=False) -> None:
    r = job_run("approver", job, round_, instant).start()
    try:
        _approver_body(r, job, round_, decision)
    finally:
        r.release()


def _approver_body(r: "Run", job: dict, round_: int, decision: str) -> None:
    r.reasoning("Every reviewer passed in the final round. Check the spec criteria and the size of the change before deciding.")
    r.tool("Bash", {"command": "git diff --numstat main...HEAD"}, "18\t0\tsrc/reports/export.py\n9\t1\tsrc/reports/views.py")
    text = "APPROVE\n\n## Why\nEvery review passes and every acceptance criterion is met.\n" if decision == "APPROVE" else HOSTILE_MD
    with LOCK:
        files_of(job)["decision.md"] = text
    r.tool("Write", {"file_path": job["job_dir"] + "/decision.md", "content": text[:120]}, "Wrote decision.md")
    r.call(7400, 310)
    r.text(decision + ": " + ("ready to merge." if decision == "APPROVE" else "this needs a human decision (scope of raw HTML)."))
    r.end(True)


# ---------------------------------------------------------------- the line (state machine, simulated)
class Pipeline(threading.Thread):
    """Drives one job like the orchestrator would, at --scenario-speed."""

    def __init__(self, job: dict, plan: dict):
        super().__init__(daemon=True)
        self.job, self.plan = job, plan

    def set(self, **kw) -> None:
        with LOCK:
            self.job.update(kw)
        save_job(self.job)

    def run(self) -> None:
        job, plan = self.job, self.plan
        try:
            start = plan.get("start", "spec")
            pause(0.8, job["_cancel"], job.get("_factor", 1.0))
            order = ["spec", "build", "review", "approve", "merge"]
            idx = order.index(start) if start in order else 0
            if idx <= 0:
                self.set(stage="spec", detail="spec-writer running", base_commit="3f2c9e1a7b")
                if not run_spec_writer(job, job["feature"], fail=plan.get("fail_spec", False)):
                    pause(0.2, None, job.get("_factor", 1.0))
                    self.set(stage="failed", detail="", failed_step="spec", reason="spec-writer wrote no spec.md (after a retry)",
                             error="provider error 400: context length exceeded")
                    job_log(job, "error", "spec-writer failed twice; job failed at spec")
                    return
            if plan.get("handoff"):
                with LOCK:
                    files_of(job)["build.md"] = "STATUS: READY\n\nBuilt interactively in chat " + plan["handoff"] + ".\n"
                job_log(job, "info", "build.md written by the orchestrator: built interactively in chat " + plan["handoff"])
            elif idx <= 1:
                self.set(stage="build", round=max(job["round"], 1) if job["round"] else 1, detail="builder running (round 1)")
                run_builder(job, job["round"] or 1, resume_note=plan.get("rework_note"))
            rnd = max(job["round"], 1)
            rounds = plan.get("rounds") or [{}]
            k = 0
            if idx <= 2:
                while True:
                    verdicts = rounds[min(k, len(rounds) - 1)]
                    self.review_round(rnd, verdicts)
                    changes = [r for r in job["reviewers"] if job["reviews"].get(r) == "CHANGES"]
                    errors = [r for r in job["reviewers"] if job["reviews"].get(r) == "ERROR"]
                    if errors:
                        self.set(stage="needs-human", detail="", reason=f"reviewer {errors[0]} failed")
                        return
                    if not changes:
                        break
                    if job["resumes"] < job["max_resumes"]:
                        self.set(resumes=job["resumes"] + 1, stage="build", detail=f"resuming builder: round {rnd} CHANGES from " + ", ".join(changes))
                        run_builder(job, rnd + 1, resume_note=f"Round {rnd} reviews are in round-{rnd}/.")
                        rnd += 1
                        k += 1
                        continue
                    with LOCK:
                        files_of(job)["decision.md"] = f"ESCALATE\n\n## Why\nReview rounds exhausted. Outstanding CHANGES in round-{rnd}/ from: {', '.join(changes)}.\n"
                    self.set(stage="needs-human", detail="", decision="ESCALATE", reason="review rounds exhausted: " + ", ".join(changes) + " still request changes")
                    return
            if idx <= 3:
                self.set(stage="approve", detail="approver running", round=rnd)
                decision = plan.get("decision", "APPROVE")
                run_approver(job, rnd, decision)
                if decision != "APPROVE":
                    self.set(stage="needs-human", detail="", decision="ESCALATE", reason="approver escalated: raw HTML in user notes needs a product decision")
                    return
                self.set(decision="APPROVE")
            self.merge()
        except CancelledRun:
            job_log(job, "warn", "cancelled by a human; running agents stopped")
        except Exception as exc:  # keep the mock alive
            print("pipeline error:", repr(exc), file=sys.stderr)

    def review_round(self, rnd: int, verdicts: dict) -> None:
        job = self.job
        self.set(stage="review", round=rnd, reviews={r: "pending" for r in job["reviewers"]}, detail=f"round {rnd}: reviewers starting")
        threads = []
        for i, reviewer in enumerate(job["reviewers"]):
            def go(reviewer=reviewer, i=i):
                try:
                    pause(0.3 + 0.5 * i, job["_cancel"], job.get("_factor", 1.0))
                    with LOCK:
                        job["reviews"][reviewer] = "running"
                    save_job(job)
                    verdict = verdicts.get(reviewer, "PASS")
                    run_reviewer(job, reviewer, rnd, verdict)
                    pause(0.4 * (len(job["reviewers"]) - i), job["_cancel"], job.get("_factor", 1.0))
                    with LOCK:
                        job["reviews"][reviewer] = verdict
                        waiting = [r for r in job["reviewers"] if job["reviews"][r] in ("pending", "running")]
                        job["detail"] = f"round {rnd}: waiting for " + ", ".join(waiting) if waiting else f"round {rnd}: all reviews in"
                    save_job(job)
                except CancelledRun:
                    pass
            t = threading.Thread(target=go, daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        if job["_cancel"].is_set():
            raise CancelledRun()
        if rnd > 1 or any(v == "CHANGES" for v in job["reviews"].values()):
            if job["reviews"].get("ui") == "CHANGES":
                job_log(job, "warn", f"discarded reviewer edits round {rnd} (git stash)")
        with LOCK:
            job["history"] = [h for h in job["history"] if h.get("round") != rnd] + [{"round": rnd, "reviews": dict(job["reviews"])}]
        save_job(job)

    def merge(self) -> None:
        mode = self.job.get("delivery") or "merge"
        if mode != "merge" and not self.plan.get("force_merge"):
            return self.deliver(mode)
        self.set(stage="merging", detail="merging into main")
        pause(0.8, self.job["_cancel"], self.job.get("_factor", 1.0))
        self.set(stage="merged", detail="", merged_commit="9d8c7b6a5f4e3d2c1b0a", reason=None)
        job_log(self.job, "info", "merged into main as 9d8c7b6")

    def deliver(self, mode: str) -> None:
        """Section 25: branch keeps the branch; folder exports changes.patch, files/ and REPORT.md. Stage delivered."""
        job = self.job
        self.set(stage="merging", detail="keeping the branch" if mode == "branch" else "exporting to the output folder")
        pause(0.6, job["_cancel"], job.get("_factor", 1.0))
        record = delivered_record(job, mode, now())
        where = f"to {record['path']}" if mode == "folder" else f"on branch {job['branch']}"
        self.set(stage="delivered", reason=None, delivered=record,
                 detail=f"delivered {where} at {record['commit']}; approve to merge it into {job['base']}")
        job_log(job, "info", f"delivered {where} (changes.patch, files/, REPORT.md)" if mode == "folder" else f"delivered {where}; kept for you to merge")


def start_pipeline(job: dict, plan: dict) -> Pipeline:
    t = Pipeline(job, plan)
    with LOCK:
        STATE["pipelines"][(job["repo"], job["id"])] = t
    t.start()
    return t


# ---------------------------------------------------------------- plans (plans.PlanManager, simulated)
PLAN_ID_RE = re.compile(r"^P\d{3}-[a-z0-9]+(-[a-z0-9]+)*$")
EDITABLE_PLAN_STAGES = ("ready", "needs-human", "blocked")
FINISHED_PLAN_STAGES = ("done", "cancelled")
BLOCKING_JOB_STAGES = ("needs-human", "failed", "rejected", "cancelled")
DEAD_JOB_STAGES = ("rejected", "cancelled")
ACTIVE_JOB_STAGES = ("queued", "spec", "build", "review", "approve", "merging")
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)[\s#]*$")
_ITEM_RE = re.compile(r"^ ?(\d+)[.)]\s+(.*\S)\s*$")
_BULLET_RE = re.compile(r"^ ?(?:\d+[.)]|[-*+])\s+(.*\S)\s*$")
_AFTER_RE = re.compile(r"\s*\(\s*after\s*:?\s*([^()]*)\)\s*[.;]?$", re.IGNORECASE)


def plan_status(text: str):
    for line in text.lstrip("﻿").splitlines():
        stripped = line.strip()
        if stripped:
            while True:
                bare = stripped.lstrip("#").strip().strip("*_`").strip()
                if bare == stripped:
                    return {"PLAN: READY": "READY", "PLAN: QUESTIONS": "QUESTIONS"}.get(bare)
                stripped = bare
    return None


def _md_section(text: str, title: str):
    lines, level = None, 0
    for line in text.splitlines():
        heading = _HEADING_RE.match(line)
        if heading and lines is not None and len(heading.group(1)) <= level:
            break
        if heading and lines is None:
            if heading.group(2).strip().lower() == title:
                lines, level = [], len(heading.group(1))
        elif lines is not None and not heading:
            lines.append(line)
    return lines


def parse_plan(text: str) -> dict:
    """plans.parse_plan: {status, jobs: [{n, feature, after}], questions}; Invalid naming the problem."""
    status = plan_status(text)
    if status is None:
        raise Invalid("plan.md must start with PLAN: READY or PLAN: QUESTIONS")
    if status == "QUESTIONS":
        return {"status": status, "jobs": [], "questions": [m.group(1) for m in map(_BULLET_RE.match, _md_section(text, "questions") or []) if m]}
    lines = _md_section(text, "jobs")
    if lines is None:
        raise Invalid("plan.md has no '## Jobs' section")
    jobs = []
    for line in lines:
        item = _ITEM_RE.match(line)
        if not item:
            continue
        n, feature = int(item.group(1)), item.group(2)
        if n != len(jobs) + 1:
            raise Invalid(f"jobs must be numbered 1, 2, 3, … in order; found {n} after {len(jobs)}")
        after = []
        suffix = _AFTER_RE.search(feature)
        if suffix:
            feature = feature[:suffix.start()].strip()
            for ref in filter(None, (part.strip() for part in suffix.group(1).split(","))):
                if not ref.lstrip("#").isdigit():
                    raise Invalid(f"job {n}: invalid reference {ref!r} in (after: …)")
                after.append(int(ref.lstrip("#")))
        if not feature:
            raise Invalid(f"job {n} has no feature text")
        jobs.append({"n": n, "feature": feature, "after": sorted(set(after))})
    if not jobs:
        raise Invalid("the '## Jobs' section lists no numbered jobs")
    for job in jobs:
        unknown = [ref for ref in job["after"] if not 1 <= ref <= len(jobs)]
        if unknown:
            raise Invalid(f"job {job['n']} comes after unknown job {unknown[0]}")
    waiting = {job["n"]: set(job["after"]) for job in jobs}
    while waiting:
        free = {n for n, deps in waiting.items() if not deps}
        if not free:
            raise Invalid(f"dependency cycle: jobs {', '.join(map(str, sorted(waiting)))} can never start")
        for n in free:
            del waiting[n]
        for deps in waiting.values():
            deps -= free
    return {"status": status, "jobs": jobs, "questions": []}


def merge_plan_jobs(old: list, items: list) -> tuple:
    """plans._merge_jobs: parsed items keep the job of an unchanged earlier entry (unless it is dead)."""
    dead = lambda e: bool(e.get("job_id")) and e.get("stage") in DEAD_JOB_STAGES
    submitted = [e for e in old if e.get("job_id") and "review" not in e and not dead(e)]
    dropped = [e for e in old if dead(e)]
    entries = []
    for item in items:
        entry = {**item, "job_id": None, "stage": None}
        match = next((e for e in submitted if e["feature"] == item["feature"]), None)
        if match is not None:
            submitted.remove(match)
            entry.update({k: match[k] for k in ("job_id", "stage", "merged_commit") if k in match})
        entries.append(entry)
    for n, entry in enumerate([e for e in old if "review" in e and not dead(e)], start=len(entries) + 1):
        entries.append({**entry, "n": n})
    return entries, dropped + submitted


def plan_files(plan: dict) -> dict:
    return STATE["plan_files"].setdefault((plan["repo"], plan["id"]), {})


def save_plan(plan: dict) -> dict:
    with LOCK:
        plan["updated_at"] = now()
        plan_files(plan)["plan.json"] = json.dumps(public_job(plan), indent=2)
        snap = public_job(plan)
    BUS.publish("plan.updated", {"plan": snap}, repo=plan["repo"])
    return snap


def plan_or_404(repo: str, plan_id: str) -> dict:
    with LOCK:
        plan = STATE["plans"].get((repo, plan_id))
    if plan is None:
        raise KeyError(f"unknown plan {repo}/{plan_id}")
    return plan


def new_plan(repo: str, request: str, routing=None, created_ago: float = 0, delivery=None, **over) -> dict:
    with LOCK:
        nums = [int(p["id"][1:4]) for p in STATE["plans"].values() if p["repo"] == repo]
        plan_id = over.pop("id", None) or f"P{max(nums, default=0) + 1:03d}-{slugify(request)}"
        ts = ago(created_ago) if created_ago else now()
        plan = {"id": plan_id, "repo": repo, "request": request, "stage": "planning", "reason": None, "failed_step": None,
                "base": "main", "base_commit": "3f2c9e1a7b6d5c4e", "plan_dir": f"{DATA_DIR}/plans/{repo}/{plan_id}",
                "jobs": [], "reviews": [], "routing": routing, "delivery": delivery,
                "usage": {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "calls": 0},
                "created_at": ts, "updated_at": ts}
        plan.update(over)
        plan["_cancel"] = threading.Event()
        STATE["plans"][(repo, plan_id)] = plan
        plan_files(plan)["request.md"] = request
        plan_files(plan)["plan.json"] = json.dumps(public_job(plan), indent=2)
    return plan


def lead_plan_md(request: str, answered: bool = False) -> str:
    first = request.strip().splitlines()[0].rstrip(".") if request.strip() else "the request"
    if "?" in first and not answered:
        return ("PLAN: QUESTIONS\n\n## Questions\n1. Should the change keep the old behaviour behind a flag while it rolls out?\n"
                "2. Which repos besides this one consume the API? (`grep` found none.)\n")
    return (f"PLAN: READY\n\n## Context\n{first}. The work splits into a data layer, the API on top and the UI, so each "
            "job stays small enough for one builder run.\n\n## Jobs\n"
            f"1. Add the data model and migration for {first.lower()[:60]}\n"
            "2. Expose the new data through the REST API with tests (after: 1)\n"
            "3. Show it in the dashboard with loading and empty states (after: 1)\n\n"
            "## Risks\n- The migration touches **migrations/**, which always needs a human (escalate paths).\n")


def run_lead(plan: dict, step: str) -> None:
    """The lead's Plan or Final review step on a thread (cancel-aware, like PlanManager._lead_step)."""
    cancel = plan["_cancel"]
    factor = plan.get("_factor", 1.0)
    try:
        with LOCK:
            n = 1 + sum(1 for k in plan_files(plan) if k.startswith("logs/"))
        r = Run("lead", f"{n:02d}-lead", plan=plan, cancel=cancel, factor=factor).start()
        try:
            r.reasoning("Read the request and the repo layout; the factory runs one small job per builder run, so split the work.")
            r.tool("Glob", {"pattern": "src/**/*.py"}, "src/reports/views.py\nsrc/reports/models.py\nsrc/auth/session.py")
            r.call(14000, 900)
            if step == "plan":
                with LOCK:
                    answered = any(k.startswith("questions-") for k in plan_files(plan))
                text = lead_plan_md(plan["request"], answered)
                with LOCK:
                    plan_files(plan)["plan.md"] = text
                r.tool("Write", {"file_path": plan["plan_dir"] + "/plan.md", "content": text[:120]}, "Wrote plan.md")
                r.text(text.splitlines()[0])
            else:
                k = len(plan["reviews"]) + 1
                review = (f"VERDICT: PASS\n\n## Final review {k}\nThe combined change does what the request asks: every job "
                          "merged cleanly and the parts fit together.\n\n## Findings\n- None.\n")
                with LOCK:
                    plan_files(plan)[f"review-{k}.md"] = review
                r.tool("Write", {"file_path": f"{plan['plan_dir']}/review-{k}.md", "content": review[:80]}, f"Wrote review-{k}.md")
                r.text("VERDICT: PASS")
            r.wait(0.6)
            r.end(True)
        finally:
            r.release()
        with LOCK:
            add_usage(plan["usage"], 14000, 900, 4000)
            if plan["stage"] in FINISHED_PLAN_STAGES or cancel.is_set():
                return
            if step == "plan":
                parsed = parse_plan(plan_files(plan)["plan.md"])
                if parsed["status"] == "QUESTIONS":
                    plan.update(stage="needs-human", reason="the lead has questions (see plan.md)", failed_step="plan")
                else:
                    plan["jobs"] = merge_plan_jobs(plan["jobs"], parsed["jobs"])[0]
                    plan.update(stage="ready", reason=None, failed_step=None)
            else:
                k = len(plan["reviews"]) + 1
                plan["reviews"].append({"k": k, "verdict": "PASS"})
                plan.update(stage="done", reason=None)
        save_plan(plan)
    except CancelledRun:
        pass


def start_lead(plan: dict, step: str) -> None:
    def go():
        pause(0.5, plan["_cancel"], plan.get("_factor", 1.0)) if not plan["_cancel"].is_set() else None
        run_lead(plan, step)
    threading.Thread(target=lambda: _safe(go), daemon=True).start()


def _safe(fn) -> None:
    try:
        fn()
    except CancelledRun:
        pass
    except Exception as exc:  # keep the mock alive
        print("plan error:", repr(exc), file=sys.stderr)


def plan_job_feature(plan: dict, entry: dict) -> str:
    text = entry["feature"]
    if entry.get("why"):
        text += f"\n\nFrom final review {entry['review']} of the plan: {entry['why']}"
    return f"{text}\n\nPlan context: {plan['id']} — see {plan['plan_dir']}/plan.md"


def advance_plan(plan: dict) -> bool:
    """PlanManager._advance: sync job stages, blocked/running, submit unblocked jobs, start the final review."""
    changed = False
    final_review = False
    with LOCK:
        for entry in plan["jobs"]:
            job = STATE["jobs"].get((plan["repo"], entry.get("job_id"))) if entry.get("job_id") else None
            if job is not None and (entry.get("stage"), entry.get("merged_commit")) != (job["stage"], job.get("merged_commit") or entry.get("merged_commit")):
                entry["stage"] = job["stage"]
                if job.get("merged_commit"):
                    entry["merged_commit"] = job["merged_commit"]
                changed = True
        if plan["stage"] not in ("running", "blocked"):
            return changed
        blocking = next((e for e in plan["jobs"] if e.get("job_id") and e.get("stage") in BLOCKING_JOB_STAGES), None)
        if blocking is not None:
            reason = f"job {blocking['job_id']} is {blocking['stage']}"
            changed |= (plan["stage"], plan["reason"]) != ("blocked", reason)
            plan.update(stage="blocked", reason=reason)
            return changed
        changed |= plan["stage"] == "blocked"
        plan.update(stage="running", reason=None)
        stages = {e["n"]: e.get("stage") for e in plan["jobs"]}
        ready = [e for e in plan["jobs"] if not e.get("job_id") and all(stages.get(d) == "merged" for d in e["after"])]
    for entry in ready:
        job = new_job(plan["repo"], plan_job_feature(plan, entry), routing=plan.get("routing"), delivery=plan.get("delivery"))
        job["_factor"] = plan.get("_factor", 1.0) * 0.4
        job["_plan"] = plan["id"]
        save_job(job, "job.created")
        start_pipeline(job, {"rounds": [{}]})
        with LOCK:
            entry.update(job_id=job["id"], stage=job["stage"])
        changed = True
    with LOCK:
        if plan["jobs"] and all(e.get("stage") == "merged" for e in plan["jobs"]):
            plan["stage"] = "final-review"
            final_review = changed = True
    if final_review:
        start_lead(plan, "final-review")
    return changed


def plan_follower(stop: threading.Event) -> None:
    """PlanManager follows job.updated; the mock polls the job stages instead."""
    while not stop.is_set():
        time.sleep(0.15)
        with LOCK:
            plans = [p for p in STATE["plans"].values() if p["stage"] not in FINISHED_PLAN_STAGES]
        for plan in plans:
            try:
                if advance_plan(plan):
                    save_plan(plan)
            except Exception as exc:  # keep following the others
                print("plan follower error:", repr(exc), file=sys.stderr)


# ---------------------------------------------------------------- seed data
def seed_plans() -> None:
    """P001 done (a 4-job graph, all merged, final review PASS), P002 waiting on the lead's questions,
    P003 ready (a 5-job graph in three layers, not started)."""
    feats = [("Add export_csv() with tests", []), ("Serve ?format=csv on the report view", [1]),
             ("Add an Export button to the report page", [2]), ("Document CSV export in the user guide", [1])]
    p = new_plan("my-app", "Let users export any report as CSV, from the report page and the API.", created_ago=86400,
                 id="P001-csv-export-for-reports", stage="done", base_commit="1a2b3c4d5e6f")
    text = "PLAN: READY\n\n## Context\nCSV export touches the report model, the view and the template.\n\n## Jobs\n"
    for n, (feature, after) in enumerate(feats, start=1):
        text += f"{n}. {feature}" + (f" (after: {', '.join(map(str, after))})" if after else "") + "\n"
        job = new_job("my-app", plan_job_feature(p, {"feature": feature}), created_ago=86000 - n * 600, id=f"{4 + n:03d}-{slugify(feature)}")
        allp = {r: "PASS" for r in job["reviewers"]}
        job.update(stage="merged", round=1, reviews=dict(allp), history=[{"round": 1, "reviews": dict(allp)}], decision="APPROVE",
                   merged_commit=f"{n}e{n}d{n}c{n}b{n}a{n}f{n}0912", base_commit="1a2b3c4d")
        job["updated_at"] = ago(85000 - n * 600)
        job["_seed"] = True
        if n in (1, 2):   # merge.push (DESIGN §40): git's own line for the ref; the second one refused by the remote
            line = f"1a2b3c4..{job['merged_commit'][:7]}" if n == 1 else "[rejected] (fetch first)"
            job["push"] = {"remote": "origin", "ref": "refs/heads/main", "commit": job["merged_commit"] + "aa" * 10,
                           "outcome": "succeeded" if n == 1 else "failed", "at": ago(85000 - n * 600 + 5),
                           "response": {"status": 0 if n == 1 else 1, "ids": [line], "body_sha256": "ab" * 32},
                           "detail": f"the remote answered {line}" if n == 1 else f"the remote refused it: {line}",
                           "done_at": ago(85000 - n * 600 + 2)}
        p["jobs"].append({"n": n, "feature": feature, "after": after, "job_id": job["id"], "stage": "merged",
                          "merged_commit": job["merged_commit"]})
    plan_files(p)["plan.md"] = text + "\n## Risks\n- None beyond the usual: keep the CSV filename ASCII.\n"
    plan_files(p)["review-1.md"] = ("VERDICT: PASS\n\n## Final review 1\nThe four jobs together deliver CSV export from "
                                    "the page and the API. <img src=x onerror=alert('plan-review')> stays text.\n\n"
                                    "## Findings\n- None.\n")
    p["reviews"] = [{"k": 1, "verdict": "PASS"}]
    p["updated_at"] = ago(84000)
    q = new_plan("docs-site", "Move the docs to a versioned layout? Old links must keep working.", created_ago=7000,
                 id="P001-move-the-docs-to-a-versioned", stage="needs-human", failed_step="plan",
                 reason="the lead has questions (see plan.md)")
    plan_files(q)["plan.md"] = ("PLAN: QUESTIONS\n\n## Questions\n1. Should `/latest/` redirect to the newest version, or "
                                "serve it directly?\n2. Which versions must stay online: all, or the last **two**?\n"
                                "3. Is a 301 from the old paths enough, or do you need a sitemap too? <script>alert('q')</script>\n")
    q["updated_at"] = ago(6600)
    r = new_plan("my-app", "Audit log v2: searchable, exportable, with retention.", created_ago=1800, id="P002-audit-log-v2",
                 stage="ready")
    plan_files(r)["plan.md"] = ("PLAN: READY\n\n## Context\nThe audit log grows without bound and cannot be searched.\n\n## Jobs\n"
                                "1. Add an indexed audit_events table with a migration\n"
                                "2. Write events through one AuditLog.record() helper (after: 1)\n"
                                "3. Add a nightly retention job that keeps 180 days (after: 1)\n"
                                "4. Search and filter API for audit events (after: 2)\n"
                                "5. Audit log page with search and CSV export (after: 3, 4)\n\n"
                                "## Risks\n- Job 1 touches `**/migrations/**`: it always goes to a human.\n")
    r["jobs"] = merge_plan_jobs([], parse_plan(plan_files(r)["plan.md"])["jobs"])[0]
    r["updated_at"] = ago(1500)
    for plan in (p, q, r):
        plan_files(plan)["plan.json"] = json.dumps(public_job(plan), indent=2)


def seed(fresh: bool = False) -> None:
    """The core seed, then each area's (tests/gui_mock/<area>.py) on a fresh STATE["area"]."""
    seed_core(fresh)
    STATE["area"] = {name: {} for name in gui_mock.AREAS}
    for area in AREAS:
        if hasattr(area, "seed"):
            area.seed(CORE, fresh)


def seed_core(fresh: bool = False) -> None:
    agents = load_agents()
    STATE.update({"config": fresh_config() if fresh else default_config(), "agents": agents, "builtin_agents": copy.deepcopy(agents), "jobs": {},
                  "files": {}, "diffs": {}, "backlog": {}, "chats": {}, "messages": {}, "scout": {}, "pipelines": {},
                  "removed_numbers": {}, "chat_cancel": {}, "requests": [], "plans": {}, "plan_files": {},
                  "keys": set() if fresh else set(DEFAULT_KEYS), "look": {"last": None, "started": [], "finds": copy.deepcopy(LOOK_FINDS), "error": None, "gap": LOOK_GAP}})
    SLOTS.clear()
    if fresh:
        return
    allpass = {"security": "PASS", "ux": "PASS", "ui": "PASS", "code": "PASS"}
    # 001: merged, full history, logs
    j = new_job("my-app", "Add a dark mode toggle to the settings page", created_ago=86400 * 2, id="001-add-dark-mode-toggle")
    seed_clock(86400 * 2 - 30)
    run_spec_writer(j, j["feature"], instant=True)
    run_builder(j, 1, instant=True)
    for r in j["reviewers"]:
        run_reviewer(j, r, 1, "PASS", instant=True)
    run_approver(j, 1, "APPROVE", instant=True)
    j.update(stage="merged", round=1, reviews=dict(allpass), history=[{"round": 1, "reviews": dict(allpass)}],
             decision="APPROVE", merged_commit="a1b2c3d4e5f60718293a", base_commit="1a2b3c4d")
    # merge.push (DESIGN §40): no answer for the ref, and the remote has main elsewhere when looked at again, so the
    # outcome is unknown, with its tracker item (orchestrator._push, _push_done)
    j["push"] = {"remote": "origin", "ref": "refs/heads/main", "commit": "a1b2c3d4e5f60718293a0b1c2d3e4f5a6b7c8d9e", "outcome": "unknown",
                 "at": ago(86400 - 5), "response": {"status": 128, "ids": [], "body_sha256": None},
                 "observed": {"remote_commit": "0f9e8d7c6b5a49382716a5b4c3d2e1f0a9b8c7d6", "at": ago(86400 - 40)},
                 "detail": "fatal: the remote end hung up unexpectedly <b>(raw)</b>; looked again: origin has main at 0f9e8d7c6b5a",
                 "done_at": ago(86400 - 40), "item": "system:S-10"}
    j["updated_at"] = ago(86400)
    files_of(j)["job.json"] = json.dumps(public_job(j), indent=2)
    STATE["diffs"][("my-app", j["id"])] = {"stat": NORMAL_STAT, "diff": NORMAL_DIFF, "truncated": False}
    # 002: needs-human after exhausting rounds
    j = new_job("my-app", "Rate limit the signup form to 5 attempts per IP per minute", created_ago=7200, id="002-rate-limit-signup-form")
    seed_clock(7200 - 20)
    run_spec_writer(j, j["feature"], instant=True)
    run_builder(j, 1, instant=True)
    hist = []
    for rnd, ver in ((1, {"security": "CHANGES", "ux": "CHANGES", "ui": "PASS", "code": "PASS"}),
                     (2, {"security": "CHANGES", "ux": "PASS", "ui": "PASS", "code": "PASS"}),
                     (3, {"security": "CHANGES", "ux": "PASS", "ui": "PASS", "code": "PASS"})):
        for r in j["reviewers"]:
            run_reviewer(j, r, rnd, ver[r], instant=True)
        hist.append({"round": rnd, "reviews": dict(ver)})
        if rnd < 3:
            run_builder(j, rnd + 1, resume_note=f"Round {rnd} reviews are in round-{rnd}/.", instant=True)
    files_of(j)["decision.md"] = "ESCALATE\n\n## Why\nReview rounds exhausted. Outstanding CHANGES in round-3/ from: security.\n"
    j.update(stage="needs-human", round=3, resumes=2, reviews=dict(hist[-1]["reviews"]), history=hist, decision="ESCALATE",
             reason="review rounds exhausted: security still requests changes", base_commit="1a2b3c4d")
    j["updated_at"] = ago(1800)
    STATE["diffs"][("my-app", j["id"])] = {"stat": NORMAL_STAT, "diff": NORMAL_DIFF, "truncated": False}
    # 003: failed (interrupted)
    j = new_job("my-app", "Fix the flaky checkout test", created_ago=5400, id="003-fix-the-flaky-checkout-test")
    seed_clock(5400 - 15)
    run_spec_writer(j, j["feature"], instant=True)
    r = Run("builder", log_name(j, "builder"), job=j, round_=1, instant=True).start()
    r.reasoning("The checkout test depends on wall-clock time; freeze it.")
    r.tool("Bash", {"command": "npm test -- checkout"}, "FAIL test/checkout.test.js\n  ✕ applies coupon (31 ms)", ok=False)
    j.update(stage="failed", round=1, failed_step="build", reason="interrupted (server restarted)", detail="")
    j["updated_at"] = ago(3600)
    # 004: hostile content everywhere, needs-human
    j = new_job("my-app", "Render user notes as <b>markdown</b> <img src=x onerror=alert('feature')> safely", created_ago=3000,
                id="004-render-user-notes")
    seed_clock(3000 - 25)
    run_spec_writer(j, "render user notes", instant=True)
    files_of(j)["spec.md"] = HOSTILE_MD.replace("ESCALATE", "# Spec <script>alert('spec')</script>", 1)
    run_builder(j, 1, instant=True)
    for rv in j["reviewers"]:
        run_reviewer(j, rv, 1, "PASS", instant=True)
    files_of(j)["round-1/review-security.md"] = "VERDICT: PASS\n\n" + HOSTILE_MD.split("\n", 1)[1]
    files_of(j)["round-1/review-code.md"] = NESTED_MD
    hr = Run("approver", log_name(j, "approver"), job=j, round_=1, instant=True).start()
    hr.text("ESCALATE <img src=x onerror=alert('agent-text')> [bad](javascript:alert(1)) **bold** `<code>`")
    hr.tool("Write", {"file_path": "<script>alert('tool-input')</script>"}, "<img src=x onerror=alert('tool-result')>")
    hr.end(True)
    files_of(j)["decision.md"] = HOSTILE_MD
    allp = dict(allpass)
    j.update(stage="needs-human", round=1, reviews=allp, history=[{"round": 1, "reviews": dict(allp)}], decision="ESCALATE",
             reason="approver escalated: <img src=x onerror=alert('reason')> needs a product call",
             detail="<script>alert('detail')</script>")
    j["updated_at"] = ago(900)
    job_log(j, "warn", "touches protected path <img src=x onerror=alert('log')>")
    STATE["diffs"][("my-app", j["id"])] = {"stat": HOSTILE_STAT, "diff": HOSTILE_DIFF, "truncated": True}
    # docs-site: rejected + cancelled
    j = new_job("docs-site", "Add a search page", created_ago=86400 * 3, id="001-add-a-search-page")
    j.update(stage="rejected", reason="superseded by a hosted search widget", round=1, reviews=dict(allpass), history=[{"round": 1, "reviews": dict(allpass)}])
    j["updated_at"] = ago(86400 * 2)
    j = new_job("docs-site", "Migrate the docs to Astro", created_ago=86400, id="002-migrate-the-docs-to-astro")
    j.update(stage="cancelled", detail="", round=0, base_commit="5e6f7a8b")
    j["updated_at"] = ago(80000)
    # The real server answers 200 with an "error" field when git cannot diff (here: the branch is gone).
    STATE["diffs"][("docs-site", j["id"])] = {"stat": "", "diff": "", "truncated": False,
                                              "error": "fatal: ambiguous argument 'main...factory/002-migrate-the-docs-to-astro': unknown revision"}
    seed_plans()
    seed_delivered()
    SEED_CLOCK["t"] = None
    for jj in STATE["jobs"].values():
        files_of(jj)["job.json"] = json.dumps(public_job(jj), indent=2)
    STATE["backlog"] = {
        "my-app": [{"text": "Add a dark mode toggle to the settings page", "done": True, "job": "001-add-dark-mode-toggle"},
                   {"text": "Paginate the audit log (50 per page)", "done": False, "job": None},
                   {"text": "Email digest of failed jobs <b>weekly</b>", "done": False, "job": None}],
        "docs-site": [],
    }
    t0 = ago(4000)
    STATE["chats"]["c-1a2b3c4d5e"] = {
        "id": "c-1a2b3c4d5e", "title": "How is auth wired in this repo?", "repo": "my-app", "model": "opus", "agent": "coder",
        "mode": "worktree", "branch": "chat/c-1a2b3c4d5e", "worktree": "/srv/ai-work/factory/worktrees/my-app/c-1a2b3c4d5e",
        "cwd": "/srv/ai-work/factory/worktrees/my-app/c-1a2b3c4d5e", "status": "idle", "created_at": t0, "updated_at": ago(3500),
        "usage": {"input_tokens": 41200, "output_tokens": 3100, "cached_tokens": 22000, "calls": 6}, "job": None, "turns": 1}
    STATE["messages"]["c-1a2b3c4d5e"] = [
        {"role": "user", "text": "How is auth wired in this repo? <img src=x onerror=alert('chat-user')>"},
        {"role": "assistant", "text": "Auth lives in `src/auth/`:\n\n1. **Middleware** `session.py` loads the user from the signed cookie.\n2. Views use `@login_required` (see **`auth/decorators.py`**).\n\n```python\n@login_required\ndef report_detail(request, report_id): ...\n```\n\nRaw HTML stays text: <script>alert('chat')</script> and [no](javascript:alert(1)). See [the docs [v2]](https://example.com/auth)."},
    ]
    STATE["chats"]["c-9f8e7d6c5b"] = {
        "id": "c-9f8e7d6c5b", "title": "Fix broken links in the changelog", "repo": "docs-site", "model": "local", "agent": "coder",
        "mode": "repo", "branch": "main", "worktree": None, "cwd": "/home/me/code/docs-site", "status": "idle",
        "created_at": ago(90000), "updated_at": ago(86000), "usage": {"input_tokens": 9000, "output_tokens": 800, "cached_tokens": 0, "calls": 2}, "job": None, "turns": 1}
    STATE["messages"]["c-9f8e7d6c5b"] = [{"role": "user", "text": "Fix the broken links in CHANGELOG.md"},
                                         {"role": "assistant", "text": "Fixed 3 links; see the diff with `git diff`."}]
    STATE["scout"]["docs-site"] = {"running": False, "draft": "# AGENTS.md (draft)\n\n## Stack\nAstro 4, pnpm.\n\n## Commands\n- `pnpm dev` (port from prompt)\n- `pnpm test`\n\n<img src=x onerror=alert('scout')>\n"}


def chat_messages(chat_id: str) -> list[dict]:
    """chats.ChatManager._messages: [{role, text, turn}], the person's k-th message opening turn k and its reply
    carrying it (so a sweep suggestion of turn k shows under that reply)."""
    out, turn = [], 0
    for entry in copy.deepcopy(STATE["messages"].get(chat_id, [])):
        turn += entry.get("role") == "user"
        entry["turn"] = turn or None
        out.append(entry)
    return out


def delivered_record(job: dict, mode: str, at: str) -> dict:
    """orchestrator._deliver: {mode, branch, base, commit, at}, plus export_folder's {path, files, skipped,
    patch_bytes} for a folder (<output_dir>/<repo>/<job-id>: changes.patch, files/, REPORT.md)."""
    record = {"mode": mode, "branch": job["branch"], "base": job["base"], "commit": "7c1e9a2b44d0", "at": at}
    if mode == "folder":
        record.update(path=f"{output_root()}/{job['repo']}/{job['id']}", files=3, skipped=[], patch_bytes=4182)
    return record


def seed_delivered() -> None:
    """Section 25: an approved job exported to the output folder, and one kept on its branch."""
    allp = {"security": "PASS", "ux": "PASS", "ui": "PASS", "code": "PASS"}
    for repo, jid, feature, mode, age in (("my-app", "009-export-the-audit-log-as-csv", "Export the audit log as CSV for the compliance team", "folder", 2400),
                                          ("docs-site", "003-add-a-changelog-page", "Add a changelog page generated from the release notes", "branch", 5000)):
        j = new_job(repo, feature, created_ago=age + 1500, id=jid, delivery=mode)
        j.update(stage="delivered", round=1, reviews=dict(allp), history=[{"round": 1, "reviews": dict(allp)}], decision="APPROVE",
                 base_commit="1a2b3c4d")
        j.update(delivered=delivered_record(j, mode, ago(age)))
        files_of(j)["spec.md"] = spec_md(feature)
        files_of(j)["build.md"] = build_md(1)
        files_of(j)["decision.md"] = "APPROVE\n\n## Why\nEvery review passes and every acceptance criterion is met.\n"
        j["updated_at"] = ago(age)
        j["_seed"] = True
        STATE["diffs"][(repo, jid)] = {"stat": NORMAL_STAT, "diff": NORMAL_DIFF, "truncated": False}


# ---------------------------------------------------------------- section 25: setup status, secrets, procedures
SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


def _loopback(url) -> bool:
    return bool(re.match(r"^[a-z]+://(127\.\d+\.\d+\.\d+|localhost|\[::1\])(:|/|$)", str(url or "")))


def setup_status() -> dict:
    """GET /api/setup: {configured, steps: {providers, models, routing, repos, tools, output}: {ok, detail}, host}."""
    cfg = STATE["config"]
    models, provs = cfg.get("models") or {}, cfg.get("providers") or {}
    reviewers = (cfg.get("pipeline") or {}).get("reviewers") or []
    names = [n for n in ("spec-writer", "builder", *(f"reviewer-{r}" for r in reviewers), "approver") if n in STATE["agents"]]
    used: dict = {}
    unresolved = []
    local = api = 0
    for n in names:
        key = route_model(n, STATE["agents"][n]["model"])
        entry = models.get(key)
        if not isinstance(entry, dict) or entry.get("provider") not in provs:
            unresolved.append(f"{n} → {key}")
            continue
        used.setdefault(entry["provider"], []).append(n)
        if _loopback(provs[entry["provider"]].get("base_url")):
            local += 1
        else:
            api += 1
    cli = ((cfg.get("agents") or {}).get("*") or {}).get("driver") not in (None, "builtin")
    missing = []
    for prov, agents in sorted(used.items()):
        env = (provs.get(prov) or {}).get("api_key_env")
        if env and env not in STATE["keys"] and not cli:
            missing.append(f"{env} (used by {', '.join(agents[:2])}{' and more' if len(agents) > 2 else ''})")
    repos = [r["name"] for r in cfg.get("repos") or []]
    harness = cfg.get("harness") or {}
    mcp = sorted((cfg.get("mcp_servers") or {}).keys())
    merge = cfg.get("merge") or {}
    mode = merge.get("delivery") or "merge"
    steps = {
        "providers": {"ok": not missing, "detail": ("Missing " + "; ".join(missing)) if missing else
                      (f"Keys set for {', '.join(sorted(used))}" if used else "No provider in use")},
        "models": {"ok": not unresolved, "detail": ("Not configured: " + ", ".join(unresolved)) if unresolved else
                   f"{len([m for m in models.values() if isinstance(m, dict)])} model entries; the line uses {', '.join(sorted({route_model(n, STATE['agents'][n]['model']) for n in names}))}"},
        "routing": {"ok": not unresolved, "detail": ("Every step through the claude CLI" if cli else f"{local} local · {api} API steps")},
        "repos": {"ok": bool(repos), "detail": ", ".join(repos) if repos else "No repository yet"},
        "tools": {"ok": True, "detail": (f"{len(harness.get('skills_dirs') or [])} skill folder(s)" + (" · MCP: " + ", ".join(mcp) if mcp else " · no MCP servers"))},
        "output": {"ok": True, "detail": {"merge": "Merge into the base branch", "branch": "Keep approved work on its branch",
                                          "folder": "Export to " + output_root()}.get(mode, mode)},
    }
    # doctor.REQUIRED_STEPS: tools and output are optional.
    # doctor.setup_host: this demo machine has no /etc/ai-stack, and two more servers listen on ports no preset names
    # (5000 and 5005 answer /v1/models like a model server; 18090 is something else). Which of them is a model server
    # only the owner's look tells (POST /api/setup/look-around): its last answer, with when it was taken.
    host = {"key_dirs": {"/etc/ai-stack": False}, "listening": [5000, 5005, 18090]}
    last = STATE["look"]["last"]
    if last is not None and last.get("error"):   # a look that broke found nothing: its error, not "none answered"
        host.update(model_servers_error=last["error"], model_servers_at=last["at"])
    elif last is not None:
        host.update(model_servers=copy.deepcopy(last["model_servers"]), model_servers_at=last["at"])
    return {"configured": all(steps[k]["ok"] for k in ("providers", "models", "routing", "repos")), "steps": steps,
            "host": host}


# doctor.look_for_model_servers on the demo machine (POST /__mock/look-around {finds} changes it)
LOOK_FINDS = [{"url": "http://127.0.0.1:5000/v1", "port": 5000, "status": 200, "ids": ["qwen3.8-flash-next"]},
              {"url": "http://127.0.0.1:5005/v1", "port": 5005, "status": 401, "ids": []}]
LOOK_GAP, LOOK_PER_HOUR = 30.0, 10   # doctor.LOOK_AROUND_GAP and LOOK_AROUND_PER_HOUR
LOOK_SECONDS = 0.6   # how long the demo look takes, so the page's busy state shows


def look_around() -> dict | tuple:
    """POST /api/setup/look-around (doctor.LookAround.ask): {at, model_servers, note}, {at, error} for a look that
    broke, or (429, message, retry) when asked again within the gap (LOOK_GAP seconds) or past LOOK_PER_HOUR an
    hour. The look itself takes LOOK_SECONDS."""
    t = time.monotonic()
    with LOCK:
        look = STATE["look"]
        look["started"] = [x for x in look["started"] if t - x < 3600.0]
        if look["started"] and t - look["started"][-1] < look["gap"]:
            since, wait = t - look["started"][-1], look["gap"] - (t - look["started"][-1])
            return 429, f"You looked {'just now' if since < 2 else f'{int(since)} seconds ago'}. You can look again in {int(wait + 0.999)} seconds.", wait
        if len(look["started"]) >= LOOK_PER_HOUR:
            wait = 3600.0 - (t - look["started"][0])
            return 429, f"You looked {LOOK_PER_HOUR} times in the last hour. You can look again in {int(wait / 60 + 0.999)} minutes.", wait
        look["started"].append(t)
        finds, error = copy.deepcopy(look["finds"]), look["error"]
    time.sleep(LOOK_SECONDS)
    result = {"at": now(), "error": error} if error else {
        "at": now(), "model_servers": finds,
        "note": "Sent one GET /v1/models, without a key, to each program listening on this machine (ports from 1024 up, at most 40, "
                "1.5 s each; the harness's own port skipped). Nothing was changed."}
    with LOCK:
        STATE["look"]["last"] = copy.deepcopy(result)
    return result


def _key_users() -> dict:
    users: dict = {}
    for name, p in sorted((STATE["config"].get("providers") or {}).items()):
        if isinstance(p, dict) and p.get("api_key_env"):
            users.setdefault(p["api_key_env"], []).append(name)
    return users


def secrets_view() -> dict:
    """GET /api/secrets (envfile.secret_status): {env_file, secrets: [{name, set, in_env_file, providers}]}, names only."""
    users = _key_users()
    names = sorted(set(users) | set(STATE["keys"]))
    return {"env_file": "~/.config/swfactory/env",
            "secrets": [{"name": n, "set": n in STATE["keys"], "in_env_file": n in STATE["keys"], "providers": users.get(n, [])} for n in names]}


def save_secret(body: dict) -> dict:
    """POST /api/secrets {name, value} (envfile.check_secret + set_secret): the real server writes NAME=value into
    <config dir>/env (0600) and its environment; "" removes it. The reply is {secret, env_file, secrets}, never the value."""
    name, value = body.get("name"), body.get("value")
    if not isinstance(name, str) or not isinstance(value, str):
        raise Invalid("'name' and 'value' must be strings")
    if not SECRET_NAME_RE.fullmatch(name):
        raise Invalid("name must match ^[A-Z][A-Z0-9_]{1,63}$ (for example DEEPSEEK_API_KEY)")
    if name not in _key_users() and not re.search(r"(_API_KEY|_KEY|_TOKEN|_SECRET|_PASSWORD)$", name):
        raise Invalid(f"{name} is not an api_key_env of a configured provider and does not look like a credential "
                      "(…_API_KEY, …_TOKEN, …_SECRET): only secrets are stored here")
    if len(value) > 4096:
        raise Invalid("value is longer than 4096 characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise Invalid("value must be a single line without control characters")
    if value != value.strip():
        raise Invalid("value must not start or end with spaces")
    if "'" in value:
        raise Invalid("value cannot contain a single quote (')")
    with LOCK:
        if value:
            STATE["keys"].add(name)
        else:
            STATE["keys"].discard(name)
    view = secrets_view()
    entry = next(x for x in view["secrets"] if x["name"] == name) if value or name in _key_users() else \
        {"name": name, "set": False, "in_env_file": False, "providers": []}
    return {"secret": entry, **view}


def _sfdocs():
    """swfactory.docs (DESIGN section 25) when importable: the mock then serves its exact shapes."""
    try:
        sys.path.insert(0, str(ROOT))
        from swfactory import docs  # noqa: PLC0415
        return docs
    except Exception:  # an older checkout: the GUI falls back to its built-in procedures
        return None


def _line_rank(name: str) -> int:
    reviewers = [f"reviewer-{r}" for r in (STATE["config"].get("pipeline") or {}).get("reviewers") or []]
    order = ["spec-writer", "builder", *reviewers, "approver", "lead", "coder", "repo-scout"]
    return order.index(name) if name in order else len(order)


def docs_procedures() -> dict:
    """GET /api/docs/procedures (docs.procedures): {generated_at, agents: [{name, description, model, tier, driver,
    tools, color, source, intro, procedure: [{title, steps: [{n, lead, text}], text}], markdown}], maps}."""
    d = _sfdocs()
    if d is None:
        raise KeyError("procedures")
    rows = []
    for a in sorted(STATE["agents"].values(), key=lambda a: (_line_rank(a["name"]), a["name"])):
        parsed = d.parse_procedure(a["body"])
        rows.append({"name": a["name"], "description": a["description"], "model": route_model(a["name"], a["model"]),
                     "tier": a["model"], "driver": driver_of(a["name"]), "tools": list(a["tools"]), "color": a["color"],
                     "source": a["source"], "intro": parsed["intro"], "procedure": parsed["sections"], "markdown": a["body"]})
    return {"generated_at": now(), "agents": rows, "maps": copy.deepcopy(d.MAPS)}


def docs_html() -> str:
    """GET /api/docs/procedures.html (docs.procedures_html): one self-contained page, no scripts."""
    d = _sfdocs()
    if d is None:
        raise KeyError("procedures")
    return d.procedures_html(docs_procedures())


# ---------------------------------------------------------------- scenario
SCENARIO_FEATURES = [
    ("my-app", "Add CSV export to the reports page"),
    ("my-app", "Show a friendly 404 page with search"),
    ("my-app", "Cache the dashboard query for 60 seconds"),
]


def scenario(stop: threading.Event) -> None:
    cycle = 0
    pause(1.0)
    while not stop.is_set():
        repo, feature = SCENARIO_FEATURES[cycle % len(SCENARIO_FEATURES)]
        cycle += 1
        a = new_job(repo, feature)
        save_job(a, "job.created")
        pa = start_pipeline(a, {"rounds": [{"security": "PASS", "ux": "CHANGES", "ui": "PASS", "code": "PASS"}, {}]})
        pause(2.5)
        b = new_job("docs-site", ["Let admins impersonate users for support", "Render user notes as markdown", "Add an audit log export"][cycle % 3])
        save_job(b, "job.created")
        pb = start_pipeline(b, {"rounds": [{}], "decision": "ESCALATE"})
        pause(2.0)
        c = new_job("my-app", "Migrate sessions to UUID primary keys")
        save_job(c, "job.created")
        pc = start_pipeline(c, {"fail_spec": True})
        for t in (pa, pb, pc):
            while t.is_alive() and not stop.is_set():
                t.join(0.2)
        pause(5.0)
        trim_scenario_jobs()


def trim_scenario_jobs() -> None:
    """Keep the board readable: drop the oldest scenario jobs beyond a few per stage."""
    with LOCK:
        candidates = sorted((j for j in STATE["jobs"].values()
                             if not j.get("_seed") and not j.get("_plan") and time.time() - j.get("_touched", 0) > 120
                             and (int(j["id"][:3]) >= 5 or (j["repo"] == "docs-site" and int(j["id"][:3]) >= 3))),
                            key=lambda j: j["created_at"])
        by_stage: dict = {}
        for j in candidates:
            by_stage.setdefault(j["stage"], []).append(j)
        doomed = []
        for stage, lst in by_stage.items():
            keep = 3 if stage == "merged" else 2
            if stage in ("merged", "failed", "needs-human", "rejected", "cancelled") and len(lst) > keep:
                doomed.extend(lst[: len(lst) - keep])
        for j in doomed:
            STATE["jobs"].pop((j["repo"], j["id"]), None)
            STATE["files"].pop((j["repo"], j["id"]), None)
            STATE["removed_numbers"].setdefault(j["repo"], []).append(int(j["id"][:3]))
    for j in doomed:
        BUS.publish("job.removed", {}, repo=j["repo"], job=j["id"])


# ---------------------------------------------------------------- chat simulation
CHAT_AGENTS = ("coder", "analyst")
def chat_turn(chat_id: str, text: str) -> None:
    cancel = threading.Event()
    with LOCK:
        STATE["chat_cancel"][chat_id] = cancel
        chat = STATE["chats"][chat_id]
        n = 1 + sum(1 for m in STATE["messages"].get(chat_id, []) if m["role"] == "user")
    r = Run(chat.get("agent") or "coder", f"{n:02d}-turn", chat=chat, cancel=cancel)
    r.model = STATE["config"]["models"].get(chat.get("model") or "opus", {}).get("model", "deepseek-v4-pro")
    reply = ""
    ok, error = True, None
    try:
        r.start()
        pause(0.4, cancel)
        r.reasoning("The user asked: " + text[:120] + ". Look at the relevant files before answering.")
        intro = "Let me look at the code first.\n"
        r.text(intro)
        reply += intro
        r.tool("Read", {"file_path": chat["cwd"] + "/src/auth/session.py"}, "     1\tfrom django.contrib.auth import get_user\n     2\t\n     3\tclass SessionMiddleware:\n     4\t    ...")
        r.tool("Bash", {"command": "git log --oneline -3", "description": "Recent commits"}, "4f1c2ab reports: CSV export\n1a2b3c4 initial\n0f0f0f0 <b>not bold</b>")
        r.call(12000, 900)
        if "fail" in text.lower():
            raise RuntimeError("provider error 529: overloaded (after 6 attempts)")
        answer = ("Here is what I found:\n\n- The session middleware loads the user from a **signed cookie**.\n"
                  "- Your message was: `" + text.replace("`", "'")[:200] + "`\n\n```bash\ngit diff main...HEAD --stat\n```\n\n"
                  "I committed nothing yet. Say *go* and I'll make the change.")
        r.text(answer)
        reply += answer
        r.call(13500, 640)
    except CancelledRun:
        ok, error = False, "cancelled"
    except RuntimeError as exc:
        ok, error = False, str(exc)
    r.end(ok, error)
    with LOCK:
        STATE["messages"].setdefault(chat_id, []).append({"role": "user", "text": text})
        if reply:
            STATE["messages"][chat_id].append({"role": "assistant", "text": reply})
        chat = STATE["chats"].get(chat_id)
        if chat is None:
            return
        chat["status"] = "idle" if ok or error == "cancelled" else "error"
        chat["turns"] = int(chat.get("turns") or 0) + 1
        chat["updated_at"] = now()
        u = chat.setdefault("usage", {})
        for k in ("input_tokens", "output_tokens", "cached_tokens", "calls"):
            u[k] = u.get(k, 0) + r.usage[k]
        snap = copy.deepcopy(chat)
        STATE["chat_cancel"].pop(chat_id, None)
    BUS.publish("chat.updated", {"chat": snap}, repo=snap["repo"])


# ---------------------------------------------------------------- config updates
CONFIG_SECTIONS = ("providers", "models", "agents", "pipeline", "limits", "harness", "mcp_servers", "escalate_paths", "repos", "merge")


def deep_merge(base, patch):
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Invalid(ValueError):
    pass


class Conflict(Exception):
    """ActionError: the server answers 409."""


def validate_config(cfg: dict) -> None:
    for name, p in cfg.get("providers", {}).items():
        if not isinstance(p, dict):
            raise Invalid(f"providers.{name}: must be an object")
        if p.get("type") not in ("openai", "anthropic"):
            raise Invalid(f"providers.{name}.type: must be openai or anthropic")
        if not isinstance(p.get("base_url"), str) or not p["base_url"].strip():
            raise Invalid(f"providers.{name}.base_url: required")
        if p.get("auth") not in (None, "bearer", "x-api-key", "both"):
            raise Invalid(f"providers.{name}.auth: must be bearer, x-api-key or both")
        mc = p.get("max_concurrency")
        if mc is not None and (not isinstance(mc, int) or mc < 1):
            raise Invalid(f"providers.{name}.max_concurrency: must be an integer >= 1")
        if p.get("headers") and any(v == "***" for v in p["headers"].values()):
            raise Invalid(f"providers.{name}.headers: masked value sent back")
    for key, m in cfg.get("models", {}).items():
        if not isinstance(m, dict):
            raise Invalid(f"models.{key}: must be an object")
        if m.get("provider") not in cfg.get("providers", {}):
            raise Invalid(f"models.{key}.provider: unknown provider {m.get('provider')!r}")
        if not m.get("model"):
            raise Invalid(f"models.{key}.model: required")
        if not isinstance(m.get("extra_body", {}), dict):
            raise Invalid(f"models.{key}.extra_body: must be an object")
        if not isinstance(m.get("max_tokens", 1), int) or m.get("max_tokens", 1) < 1:
            raise Invalid(f"models.{key}.max_tokens: must be an integer >= 1")
    pipe = cfg.get("pipeline", {})
    if not isinstance(pipe.get("reviewers"), list) or not pipe["reviewers"]:
        raise Invalid("pipeline.reviewers: at least one reviewer")
    if not isinstance(pipe.get("max_parallel_jobs"), int) or pipe["max_parallel_jobs"] < 1:
        raise Invalid("pipeline.max_parallel_jobs: must be an integer >= 1")
    if not isinstance(pipe.get("max_resumes"), int) or pipe["max_resumes"] < 0:
        raise Invalid("pipeline.max_resumes: must be an integer >= 0")
    for k, v in cfg.get("limits", {}).items():
        if not isinstance(v, (int, float)) or v <= 0:
            raise Invalid(f"limits.{k}: must be a positive number")
    if cfg.get("harness", {}).get("sandbox") not in ("none", "bwrap"):
        raise Invalid("harness.sandbox: must be none or bwrap")
    for name, a in cfg.get("agents", {}).items():
        if a is None:
            continue
        if not isinstance(a, dict):
            raise Invalid(f'agents.{name} must be an object like {{"model": ..., "driver": ...}}')
        if a.get("model") not in (None, "") and a["model"] not in cfg.get("models", {}):
            raise Invalid(f"agents.{name}.model {a['model']!r} is not a key of models")
        if a.get("driver") and a["driver"] not in cfg.get("drivers", {}):
            raise Invalid(f"agents.{name}.driver {a['driver']!r} is not a configured driver")
        if a.get("extra_body") is not None and not isinstance(a["extra_body"], dict):
            raise Invalid(f"agents.{name}.extra_body must be an object (got {json.dumps(a['extra_body'])[:60]})")
    reviewers = pipe.get("reviewers") or []
    if len(set(reviewers)) != len(reviewers):
        raise Invalid("pipeline.reviewers lists a reviewer twice")
    for r in reviewers:
        if not isinstance(r, str) or not REVIEWER_RE.fullmatch(r):
            raise Invalid(f"pipeline.reviewers: invalid name {r!r} (use a-z, 0-9 and '-')")
        if f"reviewer-{r}" not in STATE["agents"]:
            raise Invalid(f"pipeline.reviewers: there is no agent reviewer-{r} (add {USER_AGENTS_DIR}/reviewer-{r}.md)")
    for name, s in cfg.get("mcp_servers", {}).items():
        if not isinstance(s, dict) or not s.get("command"):
            raise Invalid(f"mcp_servers.{name}.command: required")
        if any(v == "***" for v in (s.get("env") or {}).values()):
            raise Invalid(f"mcp_servers.{name}.env: masked value sent back")
    if not isinstance(cfg.get("escalate_paths"), list):
        raise Invalid("escalate_paths: must be a list of globs")
    for r in cfg.get("repos", []):
        if not isinstance(r, dict) or not r.get("name") or not r.get("path"):
            raise Invalid("repos: every entry needs a name and a path")
    merge = cfg.get("merge") or {}
    if merge.get("delivery", "merge") not in DELIVERY_MODES:
        raise Invalid("merge.delivery: must be merge, branch or folder")
    if merge.get("output_dir") is not None and not (isinstance(merge["output_dir"], str) and merge["output_dir"].strip()[:1] in ("/", "~")):
        raise Invalid("merge.output_dir: must be an absolute path (or null for <data_dir>/output)")


def update_config(patch) -> dict:
    if not isinstance(patch, dict):
        raise Invalid("body must be a JSON object")
    unknown = [k for k in patch if k not in CONFIG_SECTIONS]
    if unknown:
        raise Invalid(f"{unknown[0]}: not editable through the API")
    with LOCK:
        cfg = STATE["config"]
        new = copy.deepcopy(cfg)
        for k, v in patch.items():  # settings.merge_patch: dicts merge, None removes (a whole section: defaults)
            if v is None:
                new[k] = copy.deepcopy(default_config()[k])
            elif isinstance(v, dict) and isinstance(cfg.get(k), dict):
                new[k] = deep_merge(cfg[k], v)
            else:
                new[k] = copy.deepcopy(v)
        validate_config(new)
        repos_changed = new.get("repos") != cfg.get("repos")
        STATE["config"] = new
    if repos_changed:
        BUS.publish("repos.updated", {})
    return config_view()


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "MockFactory/" + VERSION
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):  # quiet
        if self.server.verbose:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers
    def send_json(self, obj, status=200, headers=None):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text, status=200, ctype="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def error(self, status, msg):
        self.send_json({"error": msg}, status)

    def static(self, name):
        path = WEB_DIR / name
        if path.is_symlink() or not path.is_file():
            return self.error(404, f"{name} not found")
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}[path.suffix]
        self.send_text(path.read_text(encoding="utf-8"), 200, ctype)

    def authed(self) -> bool:
        if self.headers.get("Authorization", "") == "Bearer " + TOKEN:
            return True
        c = cookies.SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return False
        return "sf_token" in c and c["sf_token"].value == TOKEN

    def host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        return name in ("127.0.0.1", "localhost", "[::1]")

    def cookie_header(self):
        return {"Set-Cookie": f"sf_token={TOKEN}; HttpOnly; SameSite=Strict; Path=/"}

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    # -- dispatch
    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def dispatch(self, method):
        if not self.host_ok():
            return self.error(403, "bad Host header")
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            if method == "GET" and path == "/login":
                if q.get("token") == TOKEN:
                    self.send_response(302)
                    self.send_header("Location", "/")
                    self.send_header("Set-Cookie", self.cookie_header()["Set-Cookie"])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                return self.static("login.html")
            if method == "POST":
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype != "application/json":
                    return self.error(400, "Content-Type must be application/json")
                origin = self.headers.get("Origin")
                if origin and origin != "http://" + (self.headers.get("Host") or ""):
                    return self.error(403, "cross-origin request refused")
                body = self.body_json()
                logged = dict(body, value="***") if path == "/api/secrets" and isinstance(body, dict) and "value" in body else body
                with LOCK:
                    STATE["requests"].append({"method": method, "path": path, "body": logged, "ts": now()})
            else:
                body = None
            if method == "POST" and path == "/api/login":
                if isinstance(body, dict) and body.get("token") == TOKEN:
                    return self.send_json({"ok": True}, 200, self.cookie_header())
                return self.error(401, "invalid token")
            if not self.authed():
                if method == "GET" and path in ("/", "/index.html"):
                    return self.static("login.html")
                return self.error(401, "authentication required")
            if method == "GET" and path in ("/", "/index.html"):
                return self.static("index.html")
            if method == "GET" and path in ("/app.js", "/style.css", "/flows.js"):
                return self.static(path[1:])
            if method == "GET" and re.fullmatch(r"/views/[a-z0-9][a-z0-9_-]*\.js", path):   # the view modules
                return self.static(path[1:])
            if path.startswith("/api/"):
                return self.api(method, [urllib.parse.unquote(p) for p in path[5:].split("/")], q, body)
            if path.startswith("/__mock/"):
                return self.mock(method, path[8:], body)
            return self.error(404, "not found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Invalid as exc:
            self.error(400, str(exc))
        except json.JSONDecodeError:
            self.error(400, "invalid JSON body")
        except Conflict as exc:
            self.error(409, str(exc))
        except KeyError as exc:
            self.error(404, f"not found: {exc.args[0] if exc.args else ''}")

    def mock(self, method, name, body):
        area_name, _, rest = name.partition("/")
        if rest and area_name in gui_mock.AREAS:   # /__mock/<area>/<name>: the area's own test hooks
            area = AREAS[gui_mock.AREAS.index(area_name)]
            answer = area.mock(CORE, self, method, rest, body) if hasattr(area, "mock") else NotImplemented
            return self.error(404, "unknown mock endpoint") if answer is NotImplemented else answer
        if name == "drop-sse" and method == "POST":
            return self.send_json({"dropped": BUS.drop_all()})
        if name == "publish" and method == "POST":   # any event on the stream: {type, data?, repo?, job?, agent?}
            b = body or {}
            if not isinstance(b.get("type"), str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", b["type"]):
                raise Invalid("'type' must be an event type such as items.updated")
            ev = BUS.publish(b["type"], b.get("data") if isinstance(b.get("data"), dict) else {}, repo=b.get("repo"), job=b.get("job"), agent=b.get("agent"))
            return self.send_json({"event": ev})
        if name == "requests":
            with LOCK:
                return self.send_json({"requests": STATE["requests"][-200:]})
        if name == "look-around" and method == "POST":   # what the next look finds ({finds} or {error}), a fresh budget
            b = body or {}
            with LOCK:
                if isinstance(b.get("finds"), list):
                    STATE["look"]["finds"] = copy.deepcopy(b["finds"])
                STATE["look"]["error"] = str(b["error"]) if b.get("error") else None
                STATE["look"]["gap"] = float(b.get("gap") or LOOK_GAP)
                STATE["look"]["started"] = []
                if b.get("forget"):
                    STATE["look"]["last"] = None
            return self.send_json({"look": {"finds": STATE["look"]["finds"], "last": STATE["look"]["last"]}})
        if name == "hold" and method == "POST":
            hold_provider(str((body or {}).get("provider") or ""), float((body or {}).get("seconds") or 5))
            return self.send_json({"held": True})
        if name == "stopped-job" and method == "POST":
            # A job that stopped at once (no simulated run), with the routing it was submitted with.
            b = body or {}
            stage = b.get("stage") if b.get("stage") in ("needs-human", "failed") else "failed"
            delivery = b.get("delivery") if b.get("delivery") in ("merge", "branch", "folder") else None
            job = new_job(str(b.get("repo") or "my-app"), str(b.get("feature") or "A stopped job"), routing=b.get("routing"), delivery=delivery,
                          stage=stage, reason=str(b.get("reason") or "stopped by the test"),
                          failed_step="build" if stage == "failed" else None, error=None)
            save_job(job, "job.created")
            return self.send_json({"job": public_job(job)}, 201)
        return self.error(404, "unknown mock endpoint")

    # -- API
    def api(self, method, parts, q, body):
        p = parts
        for area in AREAS:   # each area first: its own routes, or a core route it extends
            if hasattr(area, "api"):
                answer = area.api(CORE, self, method, p, q, body)
                if answer is not NotImplemented:
                    return answer
        if method == "GET" and p == ["state"]:
            return self.send_json(state_view())
        if method == "GET" and p == ["events"]:
            return self.events(int(q.get("since") or 0))
        if method == "GET" and p == ["doctor"]:
            return self.send_json({"checks": doctor_checks()})
        if method == "GET" and p == ["setup"]:
            return self.send_json(setup_status())
        if method == "POST" and p == ["setup", "look-around"]:
            answer = look_around()
            if isinstance(answer, tuple):
                return self.send_json({"error": answer[1]}, answer[0], {"Retry-After": str(max(1, int(answer[2] + 0.999)))})
            return self.send_json(answer)
        if p == ["secrets"]:
            if method == "GET":
                return self.send_json(secrets_view())
            return self.send_json(save_secret(body or {}))
        if method == "GET" and p == ["docs", "procedures"]:
            return self.send_json(docs_procedures())
        if method == "GET" and p == ["docs", "procedures.html"]:
            body_html = docs_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            if (q.get("download") or "") not in ("", "0"):  # server.api_procedures_html
                self.send_header("Content-Disposition", 'attachment; filename="software-factory-procedures.html"')
            self.send_header("Content-Length", str(len(body_html)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body_html)
            return None
        if p[0] == "jobs":
            return self.api_jobs(method, p[1:], q, body)
        if p[0] == "repos":
            return self.api_repos(method, p[1:], body)
        if p[0] == "chats":
            return self.api_chats(method, p[1:], body)
        if p[0] == "agents":
            return self.api_agents(method, p[1:], body)
        if p[0] == "plans":
            return self.api_plans(method, p[1:], q, body)
        if p == ["config"]:
            if method == "GET":
                return self.send_json(config_view())
            return self.send_json(update_config(body))
        if method == "POST" and p == ["config", "preset"]:
            name = (body or {}).get("name")
            if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", name):
                raise Invalid("invalid preset name")
            with LOCK:
                merged = preset_config(STATE["config"], name)
                validate_config(merged)
                STATE["config"] = merged
            return self.send_json(config_view())
        if method == "POST" and len(p) == 3 and p[0] == "models" and p[2] == "test":
            return self.model_test(p[1])
        return self.error(404, "unknown endpoint")

    def api_agents(self, method, p, body):
        """GET/POST /api/agents/<name>, POST /api/agents {name, from?}, POST /api/agents/<name>/reset (DESIGN 22)."""
        if method == "POST" and not p:
            name = (body or {}).get("name")
            if not isinstance(name, str) or not name.strip():
                raise Invalid("'name' must be a non-empty string")
            if not WRITABLE_AGENT_RE.fullmatch(name):
                raise Invalid("invalid agent name (use a-z, 0-9 and '-', max 41 chars)")
            source = (body or {}).get("from") or "reviewer-code"
            return self.send_json({"agent": new_agent(name, source)}, 201)
        if len(p) == 1 and method == "GET":
            return self.send_json(agent_detail(p[0]))
        if len(p) == 1 and method == "POST":
            if not WRITABLE_AGENT_RE.fullmatch(p[0]):
                raise Invalid("invalid agent name (use a-z, 0-9 and '-', max 41 chars)")
            text = (body or {}).get("text")
            if not isinstance(text, str) or not text.strip():
                raise Invalid("'text' must be a non-empty string")
            return self.send_json({"agent": save_agent(p[0], text)})
        if len(p) == 2 and p[1] == "reset" and method == "POST":
            return self.send_json({"agent": reset_agent(p[0])})
        return self.error(404, "no such API route")

    def api_plans(self, method, p, q, body):
        """DESIGN 24: GET/POST /api/plans, GET /api/plans/<repo>/<id>[/file], POST .../plan {text}, POST .../<action>."""
        if not p:
            if method == "GET":
                with LOCK:
                    plans = [public_job(x) for x in sorted(STATE["plans"].values(), key=lambda x: (x["repo"], x["id"]))]
                return self.send_json({"plans": plans})
            repo = (body or {}).get("repo")
            request = (body or {}).get("request")
            if repo not in [r["name"] for r in STATE["config"]["repos"]]:
                raise KeyError(f"unknown repo {repo!r}")
            if not isinstance(request, str) or not request.strip():
                raise Invalid("'request' must be a non-empty string")
            routing = body_routing(body)
            routing_snapshot(STATE["config"]["pipeline"]["reviewers"], routing)  # _check_job_routing: fail now
            plan = new_plan(repo, request, routing, delivery=body_delivery(body))
            plan["_factor"] = 0.5
            snap = save_plan(plan)
            start_lead(plan, "plan")
            return self.send_json({"plan": snap}, 201)
        if len(p) < 2:
            return self.error(404, "no such API route")
        if not PLAN_ID_RE.fullmatch(p[1]):
            raise Invalid("invalid plan id")
        plan = plan_or_404(p[0], p[1])
        if method == "GET" and len(p) == 2:
            with LOCK:
                files = sorted(k for k in plan_files(plan) if not k.startswith("sessions/"))
                return self.send_json({"plan": public_job(plan), "files": files})
        if method == "GET" and p[2:] == ["file"]:
            rel = q.get("path") or ""
            if not rel or rel.startswith("/") or ".." in rel.split("/"):
                raise Invalid("path must be relative and stay inside the folder")
            with LOCK:
                text = plan_files(plan).get(rel)
            if text is None:
                raise KeyError("")
            return self.send_text(text)
        if method == "GET" and p[2:] == ["log"]:  # section 25: the lead's run logs, paged like a job's
            rel = q.get("file") or ""
            if not re.match(r"^logs/[\w.-]+\.jsonl$", rel):
                raise Invalid("file must be logs/<name>.jsonl")
            with LOCK:
                text = plan_files(plan).get(rel)
            if text is None:
                return self.error(404, f"no log {rel}")
            lines = [json.loads(x) for x in text.splitlines() if x.strip()]
            offset = int(q.get("offset") or 0)
            page = lines[offset: offset + 400]
            return self.send_json({"lines": page, "offset": offset + len(page), "more": offset + len(page) < len(lines)})
        if method == "POST" and p[2:] == ["plan"]:
            text = (body or {}).get("text")
            if not isinstance(text, str) or not text.strip():
                raise Invalid("'text' must be a non-empty string")
            return self.send_json({"plan": self.plan_text(plan, text)})
        if method == "POST" and len(p) == 3 and p[2] in ("run", "cancel", "retry"):
            return self.send_json({"plan": getattr(self, "plan_" + p[2])(plan)})
        return self.error(404, "no such API route")

    @staticmethod
    def plan_text(plan, text):
        parsed = parse_plan(text)
        with LOCK:
            if plan["stage"] not in EDITABLE_PLAN_STAGES:
                raise Conflict(f"plan {plan['id']} is {plan['stage']}; it can be edited while {', '.join(EDITABLE_PLAN_STAGES)}")
            if parsed["status"] != "READY":
                if (plan["stage"], plan.get("failed_step")) != ("needs-human", "plan"):
                    raise Invalid("an edited plan must start with PLAN: READY and list its jobs (a PLAN: QUESTIONS text is "
                                  "taken only as answers to the lead's questions)")
                plan_files(plan)["plan.md"] = text
                plan["reason"] = "the human answered the lead's questions in plan.md; Retry re-plans with them"
            else:
                jobs, dropped = merge_plan_jobs(plan["jobs"], parsed["jobs"])
                active = [e for e in dropped if e.get("stage") in ACTIVE_JOB_STAGES]
                if active:
                    names = "; ".join(f"{e['n']}. {e['feature']!r} ({e['job_id']} is {e['stage']})" for e in active)
                    raise Conflict(f"plan {plan['id']}: the edit drops or rewords jobs still in progress: {names}. "
                                   "Keep their lines as they are, or cancel those jobs first")
                plan_files(plan)["plan.md"] = text
                plan["jobs"] = jobs
                plan.update(stage="ready", reason=None, failed_step=None)
        return save_plan(plan)

    @staticmethod
    def plan_run(plan):
        with LOCK:
            if plan["stage"] != "ready":
                raise Conflict(f"plan {plan['id']} is {plan['stage']}; only a ready plan can run")
            plan.update(stage="running", reason=None)
            plan["_factor"] = plan.get("_factor", 0.5)
        advance_plan(plan)
        return save_plan(plan)

    @staticmethod
    def plan_cancel(plan):
        with LOCK:
            if plan["stage"] in FINISHED_PLAN_STAGES:
                raise Conflict(f"plan {plan['id']} is already {plan['stage']}")
            plan.update(stage="cancelled", reason="cancelled", failed_step=None)
            plan["_cancel"].set()
            active = [STATE["jobs"].get((plan["repo"], e["job_id"])) for e in plan["jobs"]
                      if e.get("job_id") and e.get("stage") in ACTIVE_JOB_STAGES]
        for job in filter(None, active):
            job["_cancel"].set()
            with LOCK:
                job.update(stage="cancelled", detail="cancelled", reason="cancelled by a human")
            save_job(job)
        return save_plan(plan)

    @staticmethod
    def plan_retry(plan):
        with LOCK:
            step = plan.get("failed_step")
            if plan["stage"] not in ("failed", "needs-human") or not step:
                raise Conflict(f"plan {plan['id']} is {plan['stage']}; nothing to retry")
            plan.update(reason=None, failed_step=None)
            plan["_cancel"] = threading.Event()
            if step == "plan":
                files = plan_files(plan)
                if plan_status(files.get("plan.md", "")) == "QUESTIONS":
                    k = 1 + sum(1 for f in files if f.startswith("questions-"))
                    files[f"questions-{k}.md"] = files.pop("plan.md")
                plan["stage"] = "planning"
            elif step == "final-review":
                plan["stage"] = "final-review"
            else:
                plan["stage"] = "running"
        snap = save_plan(plan)
        if step in ("plan", "final-review"):
            start_lead(plan, step)
        return snap

    def model_test(self, key):
        # settings.test_model never raises: an unknown key is a 200 result with ok false and the error.
        base = {"ok": False, "latency_ms": None, "text": "", "error": None, "tool_call": False, "tokens_per_second": None}
        m = STATE["config"]["models"].get(key)
        if not isinstance(m, dict):
            return self.send_json(dict(base, error=f"unknown model {key!r}"))
        prov = STATE["config"]["providers"].get(m.get("provider"), {})
        time.sleep(0.25)
        env = prov.get("api_key_env")
        if env and env not in STATE["keys"]:  # settings.missing_key_message
            msg = f"{env} is not set" + (f" and {prov['api_key_file']} does not define it" if prov.get("api_key_file") else "")
            return self.send_json(dict(base, error=msg))
        if m.get("provider") in UNREACHABLE or UNREACHABLE_URL.match(str(prov.get("base_url") or "")):
            return self.send_json(dict(base, error=f"cannot reach {prov['base_url'].rstrip('/')}/models: [Errno 111] Connection refused (start your local server)"))
        local = "127.0.0.1" in prov.get("base_url", "")
        tools = not str(m.get("model", "")).startswith("gemma")
        return self.send_json(dict(base, ok=True, latency_ms=88 if local else 312, text="OK", tool_call=tools,
                                   tokens_per_second=91.6 if local else 58.3))

    def job_or_404(self, repo, jid):
        with LOCK:
            job = STATE["jobs"].get((repo, jid))
        if job is None:
            raise KeyError(f"{repo}/{jid}")
        return job

    def api_jobs(self, method, p, q, body):
        if method == "POST" and not p:
            repo, feature = (body or {}).get("repo"), ((body or {}).get("feature") or "").strip()
            if repo not in [r["name"] for r in STATE["config"]["repos"]]:
                raise Invalid(f"unknown repo {repo!r}")
            if not feature:
                raise Invalid("'feature' must be a non-empty string")
            job = new_job(repo, feature, routing=body_routing(body), delivery=body_delivery(body))
            save_job(job, "job.created")
            start_pipeline(job, {"rounds": [{}]})
            return self.send_json({"job": public_job(job)}, 201)
        if len(p) < 2:
            return self.error(404, "unknown endpoint")
        job = self.job_or_404(p[0], p[1])
        job["_touched"] = time.time()  # a job someone has open is never trimmed from the demo board
        if method == "GET" and len(p) == 2:
            with LOCK:
                files = sorted(files_of(job).keys())
            return self.send_json({"job": public_job(job), "files": files})
        if method == "GET" and p[2:] == ["file"]:
            rel = q.get("path") or ""
            if not rel or rel.startswith("/") or ".." in rel.split("/"):
                raise Invalid("path must be relative and inside the job folder")
            with LOCK:
                text = files_of(job).get(rel)
            if text is None:
                return self.error(404, f"no file {rel}")
            return self.send_text(text)
        if method == "GET" and p[2:] == ["diff"]:
            if job["stage"] in ("queued",) or (job["stage"] == "spec" and not job.get("base_commit")):
                return self.error(404, "worktree does not exist yet")
            d = STATE["diffs"].get((job["repo"], job["id"])) or {"stat": NORMAL_STAT, "diff": NORMAL_DIFF, "truncated": False}
            return self.send_json(d)
        if method == "GET" and p[2:] == ["log"]:
            rel = q.get("file") or ""
            if not re.match(r"^logs/[\w.-]+\.jsonl$", rel):
                raise Invalid("file must be logs/<name>.jsonl")
            with LOCK:
                text = files_of(job).get(rel)
            if text is None:
                return self.error(404, f"no log {rel}")
            lines = [json.loads(x) for x in text.splitlines() if x.strip()]
            offset = int(q.get("offset") or 0)
            page = lines[offset: offset + 400]
            return self.send_json({"lines": page, "offset": offset + len(page), "more": offset + len(page) < len(lines)})
        if method == "POST" and len(p) == 3:
            return self.job_action(job, p[2], body or {})
        return self.error(404, "unknown endpoint")

    def job_action(self, job, action, body):
        stage = job["stage"]
        active = stage in ("queued", "spec", "build", "review", "approve", "merging")
        if action == "approve":
            if not (stage in ("needs-human", "delivered") or (stage == "failed" and job.get("failed_step") == "merge")):
                return self.error(409, f"cannot approve a job in stage {stage}")
            job["_cancel"] = threading.Event()
            with LOCK:
                job.update(reason=None, decision="APPROVE")
            start_pipeline(job, {"start": "merge", "force_merge": stage == "delivered"})
        elif action == "rework":
            note = (body.get("note") or "").strip()
            if stage != "needs-human":
                return self.error(409, f"cannot rework a job in stage {stage}")
            if not note:
                raise Invalid("note must be a non-empty string")
            with LOCK:
                k = 1 + sum(1 for f in files_of(job) if re.match(r"^rework-\d+\.md$", f))
                files_of(job)[f"rework-{k}.md"] = note
                job.update(resumes=0, stage="build", reason=None, decision=None, detail="builder resuming with rework-%d.md" % k, round=job["round"] + 1)
                job["_cancel"] = threading.Event()
            save_job(job)
            start_pipeline(job, {"start": "build", "rework_note": f"Rework: read rework-{k}.md and address it."})
            return self.send_json({"job": public_job(job)})
        elif action == "reject":
            if active or stage in ("merged", "rejected"):
                return self.error(409, f"cannot reject a job in stage {stage}")
            with LOCK:
                job.update(stage="rejected", reason=(body.get("reason") or "").strip() or "rejected by a human", detail="")
        elif action == "retry":
            if stage != "failed":
                return self.error(409, f"cannot retry a job in stage {stage}")
            step = job.get("failed_step") or "spec"
            job["_cancel"] = threading.Event()
            with LOCK:
                job.update(reason=None, error=None, detail=f"retrying from {step}")
            start_pipeline(job, {"start": {"setup": "spec"}.get(step, step), "rounds": [{}]})
        elif action == "cancel":
            if not active:
                return self.error(409, f"cannot cancel a job in stage {stage}")
            job["_cancel"].set()
            with LOCK:
                job.update(stage="cancelled", detail="", reason="cancelled by a human")
        elif action == "routing":  # Factory.set_routing: stopped jobs only; a dict keeps the agents it does not name
            routing = body_routing(body, required=True)
            if stage not in ("needs-human", "failed"):
                return self.error(409, f"cannot change the routing of {job['repo']}/{job['id']} while it is {stage}")
            snapshot = routing_snapshot(job["reviewers"], routing, keep=job.get("routing") or {})
            with LOCK:
                job["routing"] = snapshot
        else:
            return self.error(404, f"unknown action {action!r}")
        save_job(job)
        return self.send_json({"job": public_job(job)})

    def api_repos(self, method, p, body):
        if method == "POST" and not p:
            path = ((body or {}).get("path") or "").strip()
            name = ((body or {}).get("name") or "").strip() or re.sub(r"[^a-z0-9._-]", "-", path.rstrip("/").rsplit("/", 1)[-1].lower())
            if not path.startswith("/"):
                raise Invalid(f"{path or '(empty)'} is not an absolute path")
            if "notgit" in path:
                raise Invalid(f"{path} is not a git repository: fatal: not a git repository")
            if not re.match(r"^[a-z0-9][a-z0-9._-]*$", name):
                raise Invalid(f"invalid repo name {name!r}: use [a-z0-9][a-z0-9._-]*")
            with LOCK:
                if any(r["name"] == name for r in STATE["config"]["repos"]):
                    raise Invalid(f"a repo named {name!r} already exists")
                entry = {"name": name, "path": path, "base": None, "setup": None, "escalate_paths": []}
                STATE["config"]["repos"].append(entry)
                STATE["backlog"].setdefault(name, [])
            BUS.publish("repos.updated", {})
            return self.send_json({"repo": {"name": name, "path": path, "base": None}})
        if not p or p[0] not in [r["name"] for r in STATE["config"]["repos"]]:
            return self.error(404, f"unknown repo {p[0] if p else ''!r}")
        repo = p[0]
        if p[1:] == ["backlog"]:
            if method == "GET":
                return self.send_json({"items": copy.deepcopy(STATE["backlog"].get(repo, []))})
            text = ((body or {}).get("text") or "").strip()
            if not text or "\n" in text:
                raise Invalid("backlog item must be a single non-empty line")
            with LOCK:
                STATE["backlog"].setdefault(repo, []).append({"text": text, "done": False, "job": None})
                items = copy.deepcopy(STATE["backlog"][repo])
            BUS.publish("backlog.updated", {"repo": repo}, repo=repo)
            return self.send_json({"items": items})
        if p[1:] == ["next"] and method == "POST":
            with LOCK:
                item = next((i for i in STATE["backlog"].get(repo, []) if not i["done"]), None)
                if item is None:
                    return self.error(409, "backlog is empty")
                job = new_job(repo, item["text"])
                item.update(done=True, job=job["id"])
            save_job(job, "job.created")
            BUS.publish("backlog.updated", {"repo": repo}, repo=repo)
            start_pipeline(job, {"rounds": [{}]})
            return self.send_json({"job": public_job(job)})
        if p[1:] == ["scout"]:
            if method == "GET":
                st = STATE["scout"].get(repo) or {"running": False, "draft": None}
                return self.send_json({"draft": st.get("draft"), "running": st.get("running", False)})
            with LOCK:
                if (STATE["scout"].get(repo) or {}).get("running"):
                    return self.error(409, "repo-scout is already running")
                STATE["scout"][repo] = {"running": True, "draft": (STATE["scout"].get(repo) or {}).get("draft")}

            def scout_done():
                pause(3.0)
                with LOCK:
                    STATE["scout"][repo] = {"running": False, "draft": f"# AGENTS.md for {repo} (draft)\n\n## Stack\n- Python 3.12, Django 5\n\n## Commands\n- `make dev` (dev server on the prompt's port)\n- `make test`\n\n## Hot spots\n- `src/auth/` <script>alert('scout')</script>\n"}
            threading.Thread(target=scout_done, daemon=True).start()
            return self.send_json({"started": True})
        return self.error(404, "unknown endpoint")

    def api_chats(self, method, p, body):
        if not p:
            if method == "GET":
                with LOCK:
                    chats = sorted((copy.deepcopy(c) for c in STATE["chats"].values()), key=lambda c: c["updated_at"], reverse=True)
                return self.send_json({"chats": chats})
            repo = (body or {}).get("repo")
            mode = (body or {}).get("mode") or "worktree"
            model = (body or {}).get("model") or None
            if repo not in [r["name"] for r in STATE["config"]["repos"]]:
                raise Invalid(f"unknown repo {repo!r}")
            if mode not in ("worktree", "repo"):
                raise Invalid("mode must be worktree or repo")
            if model and model not in STATE["config"]["models"]:
                raise Invalid(f"unknown model {model!r}")
            agent = (body or {}).get("agent") or "coder"
            if agent not in CHAT_AGENTS or agent not in STATE["agents"]:
                raise Invalid(f"agent {agent!r} cannot chat (chat agents: {', '.join(a for a in CHAT_AGENTS if a in STATE['agents'])})")
            cid = "c-" + "%010x" % (int(time.time() * 1000) & 0xFFFFFFFFFF)
            repo_path = next(r["path"] for r in STATE["config"]["repos"] if r["name"] == repo)
            wt = f"/srv/ai-work/factory/worktrees/{repo}/{cid}"
            chat = {"id": cid, "title": "", "repo": repo, "model": model or "opus", "agent": agent, "mode": mode,
                    "branch": f"chat/{cid}" if mode == "worktree" else "main", "worktree": wt if mode == "worktree" else None,
                    "cwd": wt if mode == "worktree" else repo_path, "status": "idle", "created_at": now(), "updated_at": now(),
                    "usage": {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "calls": 0}, "job": None, "turns": 0}
            with LOCK:
                STATE["chats"][cid] = chat
                STATE["messages"][cid] = []
            BUS.publish("chat.updated", {"chat": copy.deepcopy(chat)}, repo=repo)
            return self.send_json({"chat": copy.deepcopy(chat)}, 201)
        cid = p[0]
        with LOCK:
            chat = STATE["chats"].get(cid)
        if chat is None:
            return self.error(404, f"unknown chat {cid!r}")
        if method == "GET" and len(p) == 1:
            with LOCK:
                return self.send_json({"chat": copy.deepcopy(chat), "messages": chat_messages(cid)})
        action = p[1] if len(p) > 1 else ""
        if method != "POST":
            return self.error(404, "unknown endpoint")
        if action == "messages":
            text = ((body or {}).get("text") or "").strip()
            if not text:
                raise Invalid("text must be a non-empty string")
            with LOCK:
                if chat["status"] == "running":
                    return self.error(409, "a turn is already running in this chat")
                chat["status"] = "running"
                if not chat["title"]:
                    chat["title"] = text[:60]
                chat["updated_at"] = now()
                snap = copy.deepcopy(chat)
            BUS.publish("chat.updated", {"chat": snap}, repo=chat["repo"])
            threading.Thread(target=chat_turn, args=(cid, text), daemon=True).start()
            return self.send_json({"chat": snap}, 202)
        if action == "cancel":
            ev = STATE["chat_cancel"].get(cid)
            if ev:
                ev.set()
            return self.send_json({"chat": copy.deepcopy(chat)})
        if action == "handoff":
            feature = ((body or {}).get("feature") or "").strip()
            if chat["mode"] != "worktree":
                return self.error(409, "hand-off needs a worktree-mode chat")
            if chat["status"] == "running":
                return self.error(409, "wait for the current turn to finish")
            if not feature:
                raise Invalid("feature must be a non-empty string")
            job = new_job(chat["repo"], feature, from_branch=chat["branch"], delivery=body_delivery(body))
            save_job(job, "job.created")
            start_pipeline(job, {"handoff": cid, "rounds": [{}]})
            with LOCK:
                chat["job"] = job["id"]
                chat["updated_at"] = now()
                snap = copy.deepcopy(chat)
            BUS.publish("chat.updated", {"chat": snap}, repo=chat["repo"])
            return self.send_json({"job": public_job(job)})
        if action == "delete":
            with LOCK:
                STATE["chats"].pop(cid, None)
                STATE["messages"].pop(cid, None)
            BUS.publish("chat.removed", {"chat": cid}, repo=chat["repo"])  # data.chat is the id (chats.py)
            return self.send_json({"ok": True})
        return self.error(404, "unknown endpoint")

    # -- SSE
    def events(self, since):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = BUS.subscribe()
        try:
            last = since
            for ev in BUS.since(since):
                self.write_event(ev)
                last = ev["seq"]
            self.wfile.flush()
            while not self.server.stopping.is_set():
                try:
                    ev = q.get(timeout=PING_INTERVAL)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if ev is CANCEL_TOKEN:
                    break
                if ev["seq"] <= last:
                    continue
                self.write_event(ev)
                last = ev["seq"]
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            BUS.unsubscribe(q)

    def write_event(self, ev):
        data = json.dumps(ev, ensure_ascii=False)
        self.wfile.write(f"id: {ev['seq']}\nevent: {ev['type']}\ndata: {data}\n\n".encode("utf-8"))


def doctor_checks() -> list:
    return [
        {"name": "python", "ok": True, "detail": "3.12.3 (>= 3.10)", "level": "info"},
        {"name": "git", "ok": True, "detail": "git version 2.43.0", "level": "info"},
        {"name": "data dir writable", "ok": True, "detail": "/srv/ai-work/factory", "level": "error"},
        {"name": "provider deepseek: key", "ok": False, "detail": "DEEPSEEK_API_KEY is not set; used by tier opus (spec-writer, builder, approver, reviewer-security)", "level": "error"},
        {"name": "provider strata: reachable", "ok": True, "detail": "GET http://127.0.0.1:18080/v1/models -> 200 in 41 ms; unauthenticated request rejected (401)", "level": "error"},
        {"name": "provider strata: context", "ok": False, "detail": "32768-token context and 1 concurrent generation: four parallel reviewers will queue (max_concurrency 1)", "level": "warn"},
        {"name": "bwrap", "ok": True, "detail": "bubblewrap 0.9.0", "level": "warn"},
        {"name": "driver claude", "ok": True, "detail": "claude 2.1.89 on PATH", "level": "info"},
        {"name": "driver codex", "ok": False, "detail": "codex-local not found on PATH <img src=x onerror=alert('doctor')>", "level": "info"},
    ]


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, verbose=False):
        super().__init__(addr, handler)
        self.verbose = verbose
        self.stopping = threading.Event()


def main(argv=None) -> int:
    global SPEED, PING_INTERVAL
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--scenario-speed", type=float, default=1.0, help="speed multiplier for the simulated activity")
    ap.add_argument("--ping-interval", type=float, default=15.0, help="seconds between SSE ': ping' comments")
    ap.add_argument("--no-scenario", action="store_true", help="seed data only, no background activity")
    ap.add_argument("--fresh", action="store_true", help="a fresh install: no repos, no keys, no jobs (implies --no-scenario)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    SPEED = max(args.scenario_speed, 0.01)
    PING_INTERVAL = max(args.ping_interval, 0.2)
    seed(fresh=args.fresh)
    srv = Server((args.host, args.port), Handler, verbose=args.verbose)
    stop = threading.Event()
    if not args.no_scenario and not args.fresh:
        threading.Thread(target=scenario, args=(stop,), daemon=True).start()
    threading.Thread(target=plan_follower, args=(stop,), daemon=True).start()
    print(f"READY http://{args.host}:{srv.server_address[1]}", flush=True)
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.stopping.set()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
