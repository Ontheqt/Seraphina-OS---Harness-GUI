"""Static checks for the web GUI (no browser): files exist, no external URLs, no unsafe
HTML sinks, valid JavaScript syntax, and the mock server used by the smoke test serves
the documented shapes. The browser smoke test is tests/gui/smoke.mjs (Playwright)."""

from __future__ import annotations

import http.client
import json
import re
import shutil
import subprocess
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUI_PENDING_PRESETS = ("local-first", "nanogpt-value")   # no flows.js PRESETS row yet (GUI phase)
WEB = ROOT / "swfactory" / "web"
FILES = ("index.html", "app.js", "flows.js", "style.css", "login.html")
URL_RE = re.compile(r"https?://", re.I)


def read(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


def strip_js_comments(src: str) -> str:
    """Drop // and /* */ comments (good enough for a grep heuristic; keeps strings)."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(re.sub(r"(^|[^:\\'\"])//(?![/*]).*$", r"\1", line) for line in src.splitlines())


class WebFilesTest(unittest.TestCase):
    def test_files_exist_and_are_not_empty(self):
        for name in FILES:
            path = WEB / name
            self.assertTrue(path.is_file(), f"missing {path}")
            self.assertGreater(path.stat().st_size, 200, f"{name} looks empty")

    def test_no_external_urls_in_html_and_css(self):
        for name in ("index.html", "style.css", "login.html"):
            hits = [line.strip() for line in read(name).splitlines() if URL_RE.search(line)]
            self.assertEqual(hits, [], f"{name} must not reference external URLs")

    def test_app_js_urls_only_in_link_scheme_check(self):
        """http(s):// appears only in the safeHref link-scheme check, or as a loopback default (a base URL
        offered for a new local model server: data for the config, never fetched by the page)."""
        lines = read("app.js").splitlines()
        loopback = re.compile(r"""['"]https?://127\.0\.0\.1:['"]""")
        offending = [(i + 1, l.strip()) for i, l in enumerate(lines)
                     if URL_RE.search(l) and "safeHref" not in l and not loopback.search(l)]
        self.assertEqual(offending, [], "http(s):// may only appear in the safeHref link-scheme check")
        self.assertIn("function safeHref", read("app.js"))
        for i, l in enumerate(lines):
            for m in re.finditer(r"""['"](https?://[^'"]*)['"]""", l):
                self.assertTrue(m.group(1).startswith(("http://127.0.0.1:", "https://127.0.0.1:")) or "safeHref" in l,
                                f"app.js:{i + 1}: external URL {m.group(1)!r}")

    def test_no_external_resources_loaded(self):
        html = read("index.html") + read("login.html")
        for src in re.findall(r"""(?:src|href)\s*=\s*["']([^"']+)["']""", html):
            self.assertFalse(src.startswith("//"), src)
            self.assertTrue(src.startswith(("/", "#", "data:")), f"unexpected resource reference {src!r}")
        self.assertNotRegex(read("style.css"), r"@import|url\(\s*['\"]?(?!data:)[a-z]+:")
        self.assertNotRegex(read("app.js"), r"\bimport\s*\(|importScripts|new\s+Worker\s*\(")

    def test_flows_js_is_data_with_loopback_urls_only(self):
        """flows.js (DESIGN §25) holds the flows as plain data: no DOM sinks, no network, only loopback defaults."""
        js = read("flows.js")
        for i, line in enumerate(js.splitlines()):
            for m in re.finditer(r"""https?://[^'"`\s)]*""", line):
                self.assertTrue(m.group(0).startswith("http://127.0.0.1:"), f"flows.js:{i + 1}: external URL {m.group(0)!r}")
        code = strip_js_comments(js)
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.", "fetch(", "XMLHttpRequest", "localStorage", "eval(", "new Function("):
            self.assertNotIn(bad, code, f"flows.js must stay pure data and helpers: {bad}")
        self.assertIn("root.SFFlows = Object.freeze(", js)

    def test_index_loads_flows_before_app(self):
        html = read("index.html")
        self.assertLess(html.index('src="/flows.js"'), html.index('src="/app.js"'))
        for view in ("view-flow", "view-grid", "view-procedures"):
            self.assertIn(f'id="{view}"', html)
        self.assertIn('href="#/grid"', html)
        self.assertIn('href="#/procedures"', html)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_flows_js_syntax_and_helpers(self):
        """The §25 flows under node: paths and branches, dropped answers, the setup plan (keys never in it), the start
        request, Mermaid maps, mcp: lines in agent files, and owner-style procedures."""
        proc = subprocess.run(["node", "--check", str(WEB / "flows.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = subprocess.run(["node", "-e", FLOWS_HARNESS, str(WEB / "flows.js")], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("FLOWS-OK", proc.stdout)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_flows_preset_table_matches_examples(self):
        """flows.js PRESETS (what each preset routes, used for the cards and the per-step review) is the examples/ files."""
        dump = "global.window = {}; require(process.argv[1]); console.log(JSON.stringify(window.SFFlows.PRESETS));"
        proc = subprocess.run(["node", "-e", dump, str(WEB / "flows.js")], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        table = json.loads(proc.stdout)
        examples = ROOT / "examples"
        routing = sorted(p.stem for p in examples.glob("*.json") if json.loads(p.read_text(encoding="utf-8")).get("agents"))
        # The routing-engine presets get their rows with the GUI phase; until then the cards show the server's
        # own description (flows.js: "A preset the table does not know is shown from the server's own description").
        routing = [name for name in routing if name not in GUI_PENDING_PRESETS]
        self.assertEqual(sorted(table), routing, "every routing preset has a row, and only those")
        for name in routing:
            data = json.loads((examples / f"{name}.json").read_text(encoding="utf-8"))
            row = table[name]
            with self.subTest(preset=name):
                self.assertEqual(row["providers"], {k: v.get("api_key_env") for k, v in data["providers"].items()})
                self.assertEqual(row["models"], {k: f"{v['provider']}/{v['model']}" for k, v in data["models"].items()})
                self.assertEqual(row["agents"], {k: v["model"] for k, v in data["agents"].items() if "model" in v})
                self.assertTrue(row["title"] and row["does"].endswith(".") and row["when"].endswith("."), row)

    def test_no_unsafe_html_sinks_in_app_js(self):
        js = strip_js_comments(read("app.js"))
        sinks = []
        for m in re.finditer(r"\.(innerHTML|outerHTML)\s*\+?=\s*([^;\n]*)", js):
            value = m.group(2).strip()
            if not re.fullmatch(r"(''|\"\"|``)", value):
                sinks.append(m.group(0))
        # Any non-literal assignment must go through one audited helper that escapes first.
        helpers = re.findall(r"function\s+(\w*[Ee]scape\w*Html\w*)\s*\(", js)
        self.assertLessEqual(len(helpers), 1, "at most one audited escape helper")
        if sinks:
            self.assertEqual(len(helpers), 1, f"innerHTML assignments without an escape helper: {sinks}")
        for bad in ("insertAdjacentHTML", "document.write", "eval(", "new Function(", "createContextualFragment", "srcdoc"):
            self.assertNotIn(bad, js, f"unsafe API {bad}")
        self.assertNotRegex(js, r"setAttribute\(\s*['\"]on", "inline event handler attributes")

    def test_markdown_links_are_scheme_checked(self):
        js = read("app.js")
        body = js[js.index("function safeHref"):]
        body = body[: body.index("\n  }\n")]
        self.assertIn("https?:", body)
        self.assertIn("^", body, "the scheme check must be anchored")
        self.assertIn("rel: 'noopener noreferrer'", js)
        self.assertIn("target: '_blank'", js)

    def test_theme_tokens_and_dark_mode(self):
        css = read("style.css")
        self.assertIn(":root {", css)
        self.assertIn("@media (prefers-color-scheme: dark)", css)
        self.assertIn(':root:not([data-theme="light"])', css)
        self.assertIn(':root[data-theme="dark"]', css)
        self.assertIn("prefers-reduced-motion", css)
        self.assertRegex(css, r"body\s*\{[^}]*background:")
        self.assertIn("ui-sans-serif", css)

    def test_login_page_posts_token_and_explains_factory_url(self):
        html = read("login.html")
        self.assertIn("/api/login", html)
        self.assertIn("factory url", html)
        self.assertIn("application/json", html)
        self.assertNotIn("innerHTML", html)

    def test_index_loads_only_local_assets(self):
        html = read("index.html")
        self.assertIn('src="/app.js"', html)
        self.assertIn('href="/style.css"', html)
        self.assertNotIn("<script>", html, "no inline scripts in index.html")

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_app_js_syntax(self):
        proc = subprocess.run(["node", "--check", str(WEB / "app.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_reviewers_are_not_hardcoded(self):
        """DESIGN §22: the strip, the chips and Routing follow pipeline.reviewers; no four-name tables."""
        js = strip_js_comments(read("app.js"))
        self.assertNotIn("REVIEWER_SHORT", js)
        self.assertNotIn("REVIEWER_NAME", js)
        self.assertNotRegex(js, r"\[\s*'security'\s*,\s*'ux'\s*,\s*'ui'\s*,\s*'code'\s*\]")
        self.assertIn("function reviewerLabels", js)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_reviewer_labels_and_config_merge_patch(self):
        """Chip labels (first letter, deduplicated with a second) and the POST /api/config merge patch."""
        proc = subprocess.run(["node", "-e", LOGIC_HARNESS, str(WEB / "app.js")], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("LOGIC-OK", proc.stdout)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_markdown_renderer_terminates_and_stays_safe(self):
        """Runs app.js's real MD renderer under node with a minimal DOM: nested emphasis and brackets once
        made inline() loop forever (shared sticky regexes); every case must finish and stay escaped."""
        proc = subprocess.run(["node", "-e", MD_HARNESS, str(WEB / "app.js")], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("MD-OK", proc.stdout)


# Loads safeHref, h() and the MD module out of app.js and renders hang-prone and hostile inputs.
MD_HARNESS = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
function grab(start, end) {
  const i = src.indexOf(start); const j = src.indexOf(end, i);
  if (i < 0 || j < 0) throw new Error('marker not found: ' + start);
  return src.slice(i, j + end.length);
}
class Node {}
class Text extends Node { constructor(s) { super(); this.nodeType = 3; this.data = String(s); this.parentNode = null; } get textContent() { return this.data; } }
class El extends Node {
  constructor(tag) { super(); this.nodeType = 1; this.tagName = String(tag).toUpperCase(); this.childNodes = []; this.attrs = {}; this.parentNode = null;
    this.style = { setProperty() {} }; this.dataset = {}; this.className = ''; }
  appendChild(c) { if (c.parentNode) c.parentNode.removeChild(c); this.childNodes.push(c); c.parentNode = this; return c; }
  insertBefore(c, ref) { if (c.parentNode) c.parentNode.removeChild(c); const k = ref ? this.childNodes.indexOf(ref) : -1;
    if (k < 0) this.childNodes.push(c); else this.childNodes.splice(k, 0, c); c.parentNode = this; return c; }
  removeChild(c) { const k = this.childNodes.indexOf(c); if (k >= 0) this.childNodes.splice(k, 1); c.parentNode = null; return c; }
  get firstChild() { return this.childNodes[0] || null; }
  get children() { return this.childNodes.filter((c) => c.nodeType === 1); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener() {}
  set textContent(v) { this.childNodes = []; this.appendChild(new Text(v)); }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(''); }
}
global.document = { createElement: (t) => new El(t), createTextNode: (s) => new Text(s) };
const PROP_KEYS = new Set(['value', 'checked', 'selected', 'disabled', 'hidden', 'open', 'readOnly', 'required', 'multiple']);
const icon = () => new El('svg');
const copyText = () => {};
eval(grab('function safeHref', '\n  }\n'));
eval(grab('function h(tag, props) {', '\n  }\n'));
eval(grab('function add(el, kid) {', '\n  }\n'));
const MD = eval(grab('const MD = (function () {', 'return { render };\n  })();').replace('const MD = ', ''));
function walk(n, f) { f(n); for (const c of n.childNodes || []) walk(c, f); }
const cases = [
  'Use *args and **kwargs in *the wrapper*.', '[see [1]](https://example.com)', 'Per [RFC 6749 [section 4.1]](https://example.com/a)',
  'Returns [dict[str, int]](https://example.com)', '*Note: *all* reviewers must pass first.*', '**Warning: **do not** merge this.**',
  '~~a ~~b~~ c~~', '[a [b] c](javascript:1)', '- [ ] update [the [config] docs](https://example.com)',
  '<img src=x onerror=alert(1)> [x](javascript:alert(1)) [y](JaVaScRiPt:alert(1)) [z](data:text/html,1) ![i](https://evil.example/p.png)',
  '| a | b |\n| - | - |\n| `|` | **c** |', '> quote *em **strong** em*\n\n```js\n<script>x</script>\n```',
];
let seed = 7;
const rnd = (n) => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed % n; };
const toks = ['*', '**', '_', '__', '`', '[', ']', '(', ')', '](', '![', '~~', '# ', '> ', '|', '- ', '1. ', '\n', ' ', 'a', 'https://x.io', '<', '\\', '[ ] ', 'javascript:'];
for (let k = 0; k < 400; k++) { let o = ''; for (let i = 0, n = 1 + rnd(30); i < n; i++) o += toks[rnd(toks.length)]; cases.push(o); }
const started = Date.now();
for (const c of cases) {
  const t0 = Date.now();
  const root = MD.render(c);
  if (Date.now() - t0 > 500) throw new Error('slow render (' + (Date.now() - t0) + ' ms): ' + JSON.stringify(c));
  walk(root, (n) => {
    if (n.nodeType !== 1) return;
    if (['IMG', 'SCRIPT', 'IFRAME', 'OBJECT', 'EMBED'].includes(n.tagName)) throw new Error('element ' + n.tagName + ' from ' + JSON.stringify(c));
    if (n.tagName === 'A' && !/^https?:\/\//i.test(n.getAttribute('href') || '')) throw new Error('bad href ' + n.getAttribute('href') + ' from ' + JSON.stringify(c));
    for (const k of Object.keys(n.attrs)) if (/^on/i.test(k)) throw new Error('handler attribute ' + k);
  });
}
const nested = MD.render('[see [1]](https://example.com)');
let label = null;
walk(nested, (n) => { if (n.tagName === 'A') label = n.textContent; });
if (label !== 'see [1]') throw new Error('nested link label: ' + label);
console.log('MD-OK ' + cases.length + ' cases in ' + (Date.now() - started) + ' ms');
"""


# The §25 flows (flows.js) under node: no DOM needed, it is data and pure helpers.
FLOWS_HARNESS = r"""
global.window = {};
require(process.argv[1]);
const F = window.SFFlows;
const eq = (got, want, what) => { if (F.stable(got) !== F.stable(want)) throw new Error(what + ': ' + JSON.stringify(got) + ' != ' + JSON.stringify(want)); };
const ok = (c, what) => { if (!c) throw new Error(what); };
const ctx = { cfg: { presets: [{ name: 'hybrid', description: 'h' }, 'anthropic', { name: 'rea-static', description: 'REA', sections: ['mcp_servers'] }],
  providers: { anthropic: { api_key_env: 'ANTHROPIC_API_KEY' }, llamacpp: { base_url: 'http://127.0.0.1:8080/v1' } }, models: {},
  harness: { sandbox: 'none', skills_dirs: [] }, pipeline: { auto_merge: true, max_auto_merge_lines: 1500 }, escalate_paths: F.STANDARD_PATHS, merge: {} },
  state: { repos: [{ name: 'app', path: '/srv/app', base: 'main' }], agents: [{ name: 'analyst' }, { name: 'coder' }], pipeline: { reviewers: ['security', 'code'] } },
  setup: { configured: false }, secrets: { ANTHROPIC_API_KEY: false }, agentMcp: { analyst: [] } };
const setup = F.flowOf('setup');
const ids = (d) => F.pathOf(setup, d, ctx).map((p) => p.id).join('>');
const d = F.emptyDraft();
eq(ids(d), 'where>local-servers>local-details>api-providers>api-keys>roles>tools>sandbox>protected>repo>output>merge-policy>review', 'hybrid is the projected default');
d.choices.output = 'branch';
eq(ids(d).split('>').slice(-2), ['output', 'review'], 'the merge policy is asked only when approved work merges');
delete d.choices.output;
d.choices.where = 'local';
eq(ids(d).split('>').slice(0, 4), ['where', 'local-servers', 'local-details', 'roles'], 'local skips the API steps');
d.choices.where = 'kit';
eq(ids(d).split('>').slice(0, 2), ['where', 'tools'], 'the Claude Code kit skips models and routing');
d.choices.where = 'hybrid';
d.choices['local-servers'] = ['llamacpp'];
d.choices['api-providers'] = ['anthropic'];
d.done.where = d.done['local-servers'] = true;
d.choices.where = 'api';
eq(F.pruneDraft(setup, d, ctx), ['Which local servers do you run?'], 'answers off the new path are dropped');
eq(F.choiceOf(d, 'api-providers'), ['anthropic'], 'answers still on the path stay');
d.choices.where = 'hybrid';
d.choices['local-servers'] = ['llamacpp'];
ok(!F.validateStep(setup, 'local-details', d, ctx).ok, 'a local server needs its model id');
F.setValue(d, 'local-details', 'llamacpp.model', 'qwen3.8-27b');
ok(F.validateStep(setup, 'local-details', d, ctx).ok, 'then it validates');
d.choices.roles = 'preset:hybrid';
d.choices.repo = 'new';
F.setValue(d, 'repo', 'path', 'relative/path');
ok(F.validateStep(setup, 'repo', d, ctx).fields.path, 'a repo path must be absolute');
F.setValue(d, 'repo', 'path', '/srv/shop');
F.setValue(d, 'tools', 'rea', 'form');
F.setValue(d, 'tools', 'rea_command', '/opt/rea-mcp');
d.choices.output = 'folder';
d.choices.sandbox = 'bwrap';
const plan = F.setupPlan(d, ctx, ['ANTHROPIC_API_KEY']);
eq(plan.items.map((i) => i.id), ['keys', 'preset', 'models', 'tools', 'agents', 'safety', 'repo', 'output'], 'apply order');
ok(!JSON.stringify(plan).includes('sk-'), 'no key value in the plan');
eq(plan.items[0].requests[0], { method: 'POST', path: '/api/secrets', body: { name: 'ANTHROPIC_API_KEY' }, secret: 'ANTHROPIC_API_KEY', label: 'ANTHROPIC_API_KEY' }, 'the key request carries the name only');
eq(plan.items[1].requests[0].body, { name: 'hybrid' }, 'the preset');
const models = plan.items[2].requests[0].body;
eq([models.providers.llamacpp.base_url, models.models.local.provider, models.models.local.model], ['http://127.0.0.1:8080/v1', 'llamacpp', 'qwen3.8-27b'], 'the local server becomes provider + model local');
eq(plan.items[3].requests[0].body.mcp_servers.rea.command, '/opt/rea-mcp', 'REA from the form');
eq(plan.items[4].requests[0].agentMcp, { agent: 'analyst', server: 'rea' }, 'the analyst gets mcp: rea');
eq(plan.items[6].requests[0].body, { path: '/srv/shop' }, 'POST /api/repos');
eq(plan.items[7].requests[0].body, { merge: { delivery: 'folder', output_dir: null } }, 'output');
ok(!JSON.stringify(plan.items[5].requests[0].body).includes('auto_merge'), 'no merge policy with a folder output');
eq(models.models.opus.provider, 'llamacpp', 'the local server also takes the tier that was the preset’s local model');
const pre = plan.items[1].changes;
ok(pre.some((c) => c.label === 'builder' && /qwen3\.8-27b · llama\.cpp/.test(c.after)), 'the preset lists each step before → after: ' + JSON.stringify(pre));
ok(!JSON.stringify(pre).includes('TABBY_API_KEY'), 'a key the choices cover is not asked for');
d.choices.output = 'merge';
d.choices['merge-policy'] = 'ask';
const withPolicy = F.setupPlan(d, ctx, []).items.find((i) => i.id === 'output');
eq(withPolicy.requests[0].body, { pipeline: { auto_merge: false } }, 'merge output (already the default): the merge policy goes with it');
d.choices.output = 'folder';
delete d.choices['merge-policy'];
const roles = F.stepOf(setup, 'roles');
const fresh = F.emptyDraft();
fresh.choices.where = 'hybrid';
fresh.done.where = true;
eq(F.defaultChoice(roles, fresh, ctx), 'preset:hybrid', 'a fresh setup that chose Hybrid starts from the Hybrid preset');
eq(F.defaultChoice(roles, fresh, Object.assign({}, ctx, { used: true })), 'keep', 'a factory in use keeps its routing unless a preset is picked');
eq(F.defaultChoice(roles, F.emptyDraft(), ctx), 'keep', 'a visit that skipped "Where do your models run?" keeps the routing');
const cards = F.optionsOf(roles, fresh, ctx);
eq(cards[0].title, 'Hybrid', 'preset cards have titles, not file names');
ok(cards.every((o) => !/…$/.test(o.does) || !/\w…$/.test(o.does)), 'no description is cut mid-word');
eq(F.firstSentence('Recommended for the owner server: local Flash-Next on TabbyAPI for spec-writer. More.'), 'Recommended for the owner server: local Flash-Next on TabbyAPI for spec-writer.', 'first sentence');
ok(/ …$|\w…$/.test(F.firstSentence('word '.repeat(60), 50)) && !/wor…$/.test(F.firstSentence('word '.repeat(60), 50)), 'cut at a word');
eq(F.routingPresets(ctx).map((p) => p.name), ['hybrid', 'anthropic'], 'an MCP example is not a routing preset');
const start = F.flowOf('start');
const sd = F.emptyDraft();
sd.choices.what = 'chat';
eq(F.pathOf(start, sd, ctx).map((p) => p.id), ['what', 'repo', 'describe', 'review'], 'a chat skips routing and output');
sd.choices.repo = 'app';
F.setValue(sd, 'describe', 'text', 'hello');
eq(F.startRequest(sd, ctx).requests[0].body, { repo: 'app', mode: 'worktree', agent: 'coder' }, 'chat body');
sd.choices.what = 'analyse';
F.setValue(sd, 'describe', 'binary', 'bin/x');
eq([F.startRequest(sd, ctx).requests[0].body.agent, F.startRequest(sd, ctx).message], ['analyst', 'Analyse bin/x.\n\nhello'], 'analyse is a chat with the analyst');
sd.choices.what = 'feature';
sd.choices.routing = 'preset:hybrid';
sd.choices.output = 'branch';
eq(F.startRequest(sd, ctx).requests[0], { method: 'POST', path: '/api/jobs', body: { repo: 'app', feature: 'hello', routing: 'hybrid', delivery: 'branch' } }, 'feature body');
sd.choices.output = 'merge';
eq(F.startRequest(sd, ctx).requests[0].body.delivery, 'merge', 'the output the review shows is always sent');
const g = F.parseMermaid('flowchart TD\n  A([Start]) --> B{Ready?}\n  B -->|Yes| C[Go<br>now]\n  B -- No --> A');
eq(g.nodes.map((n) => n.shape), ['pill', 'diamond', 'box'], 'mermaid shapes');
eq(g.edges.map((e) => e.label), ['', 'Yes', 'No'], 'mermaid labels');
eq(F.addMcp('---\nname: a\nmcp: x\n---\nbody', 'rea'), '---\nname: a\nmcp: x, rea\n---\nbody', 'mcp appended');
eq(F.addMcp('---\nname: a\n---\nbody', 'rea'), '---\nname: a\nmcp: rea\n---\nbody', 'mcp line added');
eq(F.parseProcedure('1. **Open.** Now.\n2. **Close.** Later\n   still.'), [['Open.', 'Now.'], ['Close.', 'Later still.']], 'owner-style steps');
const fm = F.flowMap(setup, ctx);
ok(fm.nodes.find((n) => n.id === 'where').shape === 'diamond' && fm.edges.filter((e) => e.to === 'end').length === 1, 'flow map: diamonds where steps branch, one end');
ok(fm.nodes.find((n) => n.id === 'local-details').shape === 'box' && fm.nodes.find((n) => n.id === 'if:local-details').cond, 'a form is a box; the earlier answer it follows is a check after it');
ok(fm.edges.some((e) => e.from === 'if:local-details' && e.to === 'api-providers' && e.label === 'Hybrid'), 'the check carries the labels');
const sm = F.flowMap(start, ctx);
ok(sm.nodes.find((n) => n.id === 'describe').shape === 'box' && sm.nodes.find((n) => n.id === 'if:describe').shape === 'diamond', 'Start: no fork drawn on the text box');
for (const flow of Object.values(F.FLOWS)) for (const s of flow.steps) ok(s.procedure.length >= 2 && s.procedure.every((p) => p[0].endsWith('.') && p[1]), flow.id + '/' + s.id + ': a numbered procedure');
const nd = F.normalizeDocs({ generated_at: 't', agents: [{ name: 'builder', description: 'Builds.', model: 'local', tier: 'opus', driver: 'builtin', tools: ['Read', 'Bash'], intro: 'You build.',
  procedure: [{ title: 'Read first', steps: [{ n: 1, lead: 'The spec', text: 'Read spec.md.' }], text: '1. **The spec.** Read spec.md.\n\nThen build.' }, { title: 'Rules', steps: [], text: '- No extras.' }], markdown: '' }],
  maps: [{ id: 'factory-line', title: 'Line', nodes: [{ id: 'a', label: 'A', kind: 'terminal', col: 0, row: 0 }, { id: 'b', label: 'B?', kind: 'decision', col: 0, row: 1 }, { id: 'h', label: 'needs-human', kind: 'human', col: 1, row: 1 }],
    edges: [{ from: 'a', to: 'b', label: '' }, { from: 'b', to: 'h', label: 'no', side: 'right' }] }] });
eq(nd.sections[0].parts, [{ title: 'Read first', steps: [['The spec', 'Read spec.md.']], text: 'Then build.' }, { title: 'Rules', steps: [], text: '- No extras.' }], 'docs.procedures parts');
eq([nd.sections[0].meta, nd.sections[0].intro], ['model local (tier opus) · driver builtin · tools Read, Bash', 'You build.'], 'agent meta and intro');
eq([nd.maps[0].module, nd.maps[0].graph.nodes.map((n) => n.shape), nd.maps[0].graph.nodes[2].tone], ['factory-line', ['pill', 'diamond', 'pill'], 'human'], 'docs.MAPS nodes');
eq(F.reviewersOf({ state: { agents: [{ name: 'reviewer-perf' }, { name: 'builder' }] } }), ['perf'], 'reviewers come from the agents, not a table');
console.log('FLOWS-OK');
"""


# reviewerLabels and mergeDiff out of app.js, checked against the rules they implement.
LOGIC_HARNESS = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
function grab(start, end) {
  const i = src.indexOf(start); const j = src.indexOf(end, i);
  if (i < 0 || j < 0) throw new Error('marker not found: ' + start);
  return src.slice(i, j + end.length);
}
eval(grab('const clone = ', ';\n').replace(/^const /, 'var '));
eval(grab('const isObj = ', ';\n').replace(/^const /, 'var '));
eval(grab('function stable(v) {', '\n  }\n'));
eval(grab('function mergeDiff(a, b) {', '\n  }\n'));
eval(grab('function reviewerLabels(list) {', '\n  }\n'));
const eq = (got, want, what) => { if (stable(got) !== stable(want)) throw new Error(what + ': ' + JSON.stringify(got) + ' != ' + JSON.stringify(want)); };
eq(reviewerLabels(['security', 'ux', 'ui', 'code']), { security: 'S', ux: 'U', ui: 'Ui', code: 'C' }, 'default lenses');
eq(reviewerLabels(['security', 'style']), { security: 'S', style: 'St' }, 'DESIGN example');
eq(reviewerLabels(['security', 'ux', 'ui', 'code', 'perf']), { security: 'S', ux: 'U', ui: 'Ui', code: 'C', perf: 'P' }, 'five lenses');
eq(reviewerLabels(['aa', 'ab', 'ac', 'a']), { aa: 'A', ab: 'Ab', ac: 'Ac', a: 'A2' }, 'collisions');
eq(reviewerLabels(['stats', 'style', 'sec', 'st']), { stats: 'S', style: 'St', sec: 'Se', st: 'S2' }, 'second letters taken');
eq(mergeDiff({ a: 1, b: { c: 1, d: 2 }, e: [1] }, { a: 1, b: { c: 2 }, e: [1, 2], f: 3 }), { b: { c: 2, d: null }, e: [1, 2], f: 3 }, 'nested diff');
eq(mergeDiff({ x: { model: 'a', driver: 'q' } }, {}), { x: null }, 'removed entry');
eq(mergeDiff({ x: { extra_body: { t: { type: 'enabled' } } } }, { x: { extra_body: {} } }), { x: { extra_body: { t: null } } }, 'removed nested key');
eq(mergeDiff({ k: '***', n: 1 }, { k: '***', n: 2 }), { n: 2 }, 'an unchanged masked value is not sent');
eq(mergeDiff({}, { y: { model: 'm' } }), { y: { model: 'm' } }, 'new entry');
console.log('LOGIC-OK');
"""


class MockServerContractTest(unittest.TestCase):
    """The smoke test's mock backend speaks the DESIGN.md API (auth, shapes)."""

    @classmethod
    def setUpClass(cls):
        from tests import gui_mock_server as mock
        cls.mock = mock
        mock.seed()
        cls.srv = mock.Server(("127.0.0.1", 0), mock.Handler)
        cls.thread = threading.Thread(target=cls.srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        cls.thread.start()
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.stopping.set()
        cls.srv.shutdown()
        cls.srv.server_close()

    def request(self, method, path, body=None, token=True, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})
        if token:
            hdrs["Authorization"] = "Bearer test-token"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, resp.getheader("Content-Type") or "", raw

    def test_unauthenticated_root_serves_login_and_api_is_401(self):
        status, ctype, raw = self.request("GET", "/", token=False)
        self.assertEqual(status, 200)
        self.assertIn(b"/api/login", raw)
        status, _, raw = self.request("GET", "/api/state", token=False)
        self.assertEqual(status, 401)
        self.assertIn("error", json.loads(raw))

    def test_login_sets_cookie(self):
        status, _, _ = self.request("POST", "/api/login", {"token": "nope"}, token=False)
        self.assertEqual(status, 401)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/login", body=b'{"token": "test-token"}', headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 200)
        self.assertIn("sf_token=test-token", resp.getheader("Set-Cookie"))
        self.assertIn("HttpOnly", resp.getheader("Set-Cookie"))
        conn.close()

    def test_state_shape(self):
        status, _, raw = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        st = json.loads(raw)
        for key in ("version", "repos", "jobs", "pipeline", "models", "drivers", "agents"):
            self.assertIn(key, st)
        self.assertTrue(all({"name", "path", "base"} <= set(r) for r in st["repos"]))
        self.assertTrue(all({"provider", "model"} <= set(m) for m in st["models"].values()))
        self.assertTrue(all({"name", "description", "model", "driver", "tools", "color"} <= set(a) for a in st["agents"]))
        job = next(j for j in st["jobs"] if j["id"] == "002-rate-limit-signup-form")
        for key in ("stage", "round", "resumes", "max_resumes", "reviews", "history", "reason", "usage", "branch", "worktree"):
            self.assertIn(key, job)

    def test_job_detail_files_log_and_diff(self):
        status, _, raw = self.request("GET", "/api/jobs/my-app/002-rate-limit-signup-form")
        detail = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertIn("decision.md", detail["files"])
        logs = [f for f in detail["files"] if f.startswith("logs/")]
        self.assertTrue(logs)
        status, ctype, raw = self.request("GET", "/api/jobs/my-app/002-rate-limit-signup-form/file?path=decision.md")
        self.assertEqual(status, 200)
        self.assertTrue(ctype.startswith("text/plain"))
        self.assertTrue(raw.startswith(b"ESCALATE"))
        status, _, raw = self.request("GET", "/api/jobs/my-app/002-rate-limit-signup-form/log?file=" + logs[0] + "&offset=0")
        page = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertIn("run", page["lines"][0]["data"])
        status, _, raw = self.request("GET", "/api/jobs/my-app/002-rate-limit-signup-form/file?path=../x")
        self.assertEqual(status, 400)
        status, _, raw = self.request("GET", "/api/jobs/my-app/004-render-user-notes/diff")
        diff = json.loads(raw)
        self.assertEqual(set(diff), {"stat", "diff", "truncated"})
        # A git failure is a 200 with an "error" field, like server.api_job_diff.
        status, _, raw = self.request("GET", "/api/jobs/docs-site/002-migrate-the-docs-to-astro/diff")
        self.assertEqual(status, 200)
        self.assertIn("error", json.loads(raw))
        status, _, raw = self.request("GET", "/api/jobs/my-app/002-rate-limit-signup-form/log?file=" + logs[0] + "&offset=0")
        self.assertIn("more", json.loads(raw))

    def test_post_requires_json_content_type(self):
        status, _, raw = self.request("POST", "/api/jobs/my-app/003-fix-the-flaky-checkout-test/retry", {}, headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 400)
        status, _, raw = self.request("POST", "/api/jobs/my-app/001-add-dark-mode-toggle/cancel", {})
        self.assertEqual(status, 409)
        self.assertIn("error", json.loads(raw))

    def test_config_hides_secrets(self):
        status, _, raw = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertNotIn(b"real-secret-value", raw)
        cfg = json.loads(raw)
        self.assertNotIn("token", cfg.get("server", {}))
        self.assertIn("key_present", cfg["providers"]["deepseek"])
        # Shaped like settings.config_view(): "agents" stays the override dict; the resolved list is agent_list.
        self.assertIsInstance(cfg["agents"], dict)
        self.assertIsInstance(cfg["agent_list"], list)
        self.assertIn("tiers", cfg)

    def wait_for(self, predicate, timeout=6.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            found = predicate()
            if found:
                return found
            time.sleep(0.05)
        self.fail("condition not met in time")

    def test_state_and_config_shapes_match_server_and_settings(self):
        from swfactory import settings
        _, _, raw = self.request("GET", "/api/state")
        for agent in json.loads(raw)["agents"]:  # server._agent_summary
            self.assertEqual(set(agent), {"name", "description", "model", "tier", "driver", "tools", "color"})
        _, _, raw = self.request("GET", "/api/config")
        cfg = json.loads(raw)
        # settings.config_view: presets are [{name, description, replaces, merges}] (DESIGN §25), and
        # preset_names the plain list; the mock may leave out the parts the GUI does not read.
        self.assertEqual([preset["name"] for preset in cfg["presets"]], settings.list_presets())
        real_keys = set(settings.preset_summaries()[0])
        for preset in cfg["presets"]:
            self.assertTrue({"name", "description"} <= set(preset) <= real_keys, sorted(preset))
            self.assertIsInstance(preset["description"], str)
        if "preset_names" in cfg:
            self.assertEqual(cfg["preset_names"], settings.list_presets())
        self.assertEqual(cfg["model_templates"], settings.MODEL_TEMPLATES)
        self.assertEqual(cfg["model_template_note"], settings.TEMPLATE_NOTE)
        for agent in cfg["agent_list"]:  # settings._agent_summary
            self.assertEqual(set(agent), {"name", "description", "model", "driver", "source", "tier", "color", "extra_body"})
            self.assertIn(agent["source"], ("builtin", "user", "repo"))

    def test_jobs_carry_the_orchestrator_routing_snapshot(self):
        from swfactory import orchestrator
        _, _, raw = self.request("GET", "/api/state")
        job = next(j for j in json.loads(raw)["jobs"] if j["id"] == "002-rate-limit-signup-form")
        self.assertEqual(set(job["routing"]), {"spec-writer", "builder", "approver", *("reviewer-" + r for r in job["reviewers"])})
        for route in job["routing"].values():
            self.assertEqual(set(route), {"model", "driver", "provider", "model_id", "extra_body", "model_entry", "provider_entry"})
            self.assertEqual(set(route["model_entry"]), set(orchestrator.MODEL_FIELDS))
            self.assertEqual(set(route["provider_entry"]), set(orchestrator.PROVIDER_FIELDS) | {"headers"})

    def test_submit_routing_and_change_routing(self):
        status, _, raw = self.request("POST", "/api/jobs", {"repo": "docs-site", "feature": "Preset run", "routing": "hybrid"})
        self.assertEqual(status, 201)
        job = json.loads(raw)["job"]
        self.assertEqual((job["routing"]["builder"]["provider"], job["routing"]["approver"]["model"]), ("tabby", "claude-opus"))
        status, _, raw = self.request("POST", "/api/jobs", {"repo": "docs-site", "feature": "Custom run",
                                                            "routing": {"builder": {"model": "local"}}})
        self.assertEqual(status, 201)
        custom = json.loads(raw)["job"]
        self.assertEqual(custom["routing"]["builder"]["model"], "local")
        self.assertEqual(custom["routing"]["spec-writer"]["model"], "opus")
        for bad in ({"builder": {"bogus": 1}}, "", "no-such-preset", {"builder": {"driver": "nope"}}):
            status, _, _ = self.request("POST", "/api/jobs", {"repo": "docs-site", "feature": "x", "routing": bad})
            self.assertEqual(status, 400, bad)
        status, _, raw = self.request("POST", "/api/jobs/my-app/002-rate-limit-signup-form/routing", {"routing": {"builder": {"model": "local"}}})
        self.assertEqual(status, 200)
        routed = json.loads(raw)["job"]["routing"]
        self.assertEqual((routed["builder"]["model"], routed["spec-writer"]["model"]), ("local", "opus"))
        status, _, _ = self.request("POST", "/api/jobs/my-app/002-rate-limit-signup-form/routing", {})
        self.assertEqual(status, 400)
        status, _, _ = self.request("POST", "/api/jobs/docs-site/" + custom["id"] + "/routing", {"routing": "hybrid"})
        self.assertEqual(status, 409, "an active job keeps its routing")

    def test_agent_detail_edit_new_and_reset(self):
        status, _, raw = self.request("GET", "/api/agents/reviewer-ux")
        detail = json.loads(raw)
        self.assertEqual(set(detail), {"name", "description", "tools", "model", "color", "skills", "mcp", "body", "text", "source", "path"})
        self.assertEqual(detail["source"], "builtin")
        status, _, raw = self.request("POST", "/api/agents/reviewer-ux", {"text": detail["text"].replace("name: reviewer-ux", "name: other")})
        self.assertEqual(status, 400)
        self.assertIn("frontmatter name", json.loads(raw)["error"])
        status, _, raw = self.request("POST", "/api/agents", {"name": "reviewer-tmp", "from": "reviewer-code"})
        self.assertEqual(status, 201)
        agent = json.loads(raw)["agent"]
        self.assertEqual((agent["name"], agent["source"]), ("reviewer-tmp", "user"))
        self.assertIn("review-tmp.md", agent["text"])
        status, _, raw = self.request("POST", "/api/agents/reviewer-tmp/reset", {})
        self.assertEqual((status, json.loads(raw)), (200, {"agent": None}))
        status, _, _ = self.request("POST", "/api/agents/reviewer-ux/reset", {})
        self.assertEqual(status, 404, "no user override to reset")

    def test_plans_shapes_edits_and_actions(self):
        _, _, raw = self.request("GET", "/api/plans")
        plans = json.loads(raw)["plans"]
        keys = {"id", "repo", "request", "stage", "reason", "failed_step", "base", "base_commit", "plan_dir", "jobs",
                "reviews", "routing", "delivery", "usage", "created_at", "updated_at"}
        self.assertTrue(plans and all(set(p) == keys for p in plans), [sorted(set(p) ^ keys) for p in plans])
        status, _, raw = self.request("GET", "/api/plans/my-app/P002-audit-log-v2")
        detail = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertIn("plan.md", detail["files"])
        self.assertEqual([j["after"] for j in detail["plan"]["jobs"]], [[], [1], [1], [2], [3, 4]])
        status, ctype, raw = self.request("GET", "/api/plans/my-app/P002-audit-log-v2/file?path=plan.md")
        self.assertTrue(ctype.startswith("text/plain") and raw.startswith(b"PLAN: READY"))
        status, _, raw = self.request("POST", "/api/plans/my-app/P002-audit-log-v2/plan", {"text": "PLAN: READY\n\n## Jobs\n1. a (after: 2)\n2. b (after: 1)\n"})
        self.assertEqual(status, 400)
        self.assertIn("cycle", json.loads(raw)["error"])
        status, _, _ = self.request("POST", "/api/plans/my-app/P001-csv-export-for-reports/plan", {"text": "PLAN: READY\n\n## Jobs\n1. a\n"})
        self.assertEqual(status, 409, "a done plan cannot be edited")
        status, _, _ = self.request("POST", "/api/plans/my-app/P001-csv-export-for-reports/run", {})
        self.assertEqual(status, 409)
        status, _, raw = self.request("POST", "/api/plans", {"repo": "docs-site", "request": "Rework the navigation"})
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(raw)["plan"]["stage"], "planning")

    def test_model_probe_reports_both_probes(self):
        status, _, raw = self.request("POST", "/api/models/local/test", {})
        result = json.loads(raw)
        self.assertEqual(set(result), {"ok", "latency_ms", "text", "error", "tool_call", "tokens_per_second"})
        self.assertTrue(result["ok"] and result["tool_call"] and result["tokens_per_second"])

    def test_busy_provider_slot_publishes_agent_waiting(self):
        status, _, _ = self.request("POST", "/__mock/hold", {"provider": "strata", "seconds": 1.5})
        self.assertEqual(status, 200)
        status, _, raw = self.request("POST", "/api/jobs", {"repo": "my-app", "feature": "Queue on strata",
                                                            "routing": {"spec-writer": {"model": "local"}}})
        job_id = json.loads(raw)["job"]["id"]
        waiting = self.wait_for(lambda: [e for e in self.mock.BUS.since(0) if e["type"] == "agent.waiting" and e["job"] == job_id])
        self.assertEqual((waiting[0]["agent"], waiting[0]["data"]["provider"]), ("spec-writer", "strata"))
        self.assertIn("run", waiting[0]["data"])

    def test_setup_status_secrets_and_procedures(self):
        """DESIGN §25: GET /api/setup, names-only secrets (a value is never echoed or logged), the procedures data
        and its self-contained HTML booklet."""
        status, _, raw = self.request("GET", "/api/setup")
        st = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertIsInstance(st["configured"], bool)
        self.assertEqual(set(st["steps"]), {"providers", "models", "routing", "repos", "tools", "output"})
        self.assertTrue(all(set(v) == {"ok", "detail"} for v in st["steps"].values()))
        status, _, raw = self.request("GET", "/api/secrets")
        names = {x["name"]: x["set"] for x in json.loads(raw)["secrets"]}
        self.assertIn("ANTHROPIC_API_KEY", names)
        self.assertTrue(all(set(x) == {"name", "set", "in_env_file", "providers"} for x in json.loads(raw)["secrets"]))
        status, _, raw = self.request("POST", "/api/secrets", {"name": "OPENAI_API_KEY", "value": "sk-never-echoed"})
        self.assertEqual(status, 200)
        self.assertNotIn(b"sk-never-echoed", raw)
        self.assertEqual(json.loads(raw)["secret"], {"name": "OPENAI_API_KEY", "set": True, "in_env_file": True, "providers": ["openai"]})
        _, _, raw = self.request("GET", "/api/secrets")
        self.assertTrue({x["name"]: x["set"] for x in json.loads(raw)["secrets"]}["OPENAI_API_KEY"])
        _, _, raw = self.request("GET", "/__mock/requests")
        self.assertNotIn(b"sk-never-echoed", raw)
        for bad in ({"name": "lower", "value": "x"}, {"name": "OK_API_KEY", "value": "two\nlines"}, {"name": "OK_API_KEY", "value": "x" * 4097},
                    {"name": "OK_API_KEY", "value": " padded"}, {"name": "HOME_DIR", "value": "x"}):
            status, _, _ = self.request("POST", "/api/secrets", bad)
            self.assertEqual(status, 400, bad)
        status, _, raw = self.request("GET", "/api/docs/procedures")
        docs = json.loads(raw)
        # docs.procedures: one entry per agent (its file's procedure, parsed), and the maps as nodes and edges.
        self.assertEqual(set(docs), {"generated_at", "agents", "maps"})
        keys = {"name", "description", "model", "tier", "driver", "tools", "color", "source", "intro", "procedure", "markdown"}
        self.assertTrue(docs["agents"] and all(set(a) == keys for a in docs["agents"]))
        self.assertEqual(docs["agents"][0]["name"], "spec-writer", "the line's agents come first")
        self.assertTrue(all({"title", "steps", "text"} == set(s) for a in docs["agents"] for s in a["procedure"]))
        self.assertEqual([m["id"] for m in docs["maps"]], ["factory-line", "plan-flow"])
        status, ctype, raw = self.request("GET", "/api/docs/procedures.html")
        self.assertEqual(status, 200)
        self.assertTrue(ctype.startswith("text/html"))
        self.assertIn(b"<h1>", raw)
        self.assertNotIn(b"<script", raw.lower())

    def test_delivery_modes(self):
        """DESIGN §25: merge.delivery in the config, per-job delivery (validated), delivered jobs and Merge it now."""
        _, _, raw = self.request("GET", "/api/config")
        self.assertEqual(json.loads(raw)["merge"]["delivery"], "merge")
        _, _, raw = self.request("GET", "/api/state")
        delivered = {j["id"]: j for j in json.loads(raw)["jobs"] if j["stage"] == "delivered"}
        folder = delivered["009-export-the-audit-log-as-csv"]
        self.assertEqual((folder["delivery"], folder["delivered"]["mode"]), ("folder", "folder"))
        self.assertTrue(folder["delivered"]["path"].endswith("/my-app/009-export-the-audit-log-as-csv"))
        self.assertTrue({"mode", "branch", "base", "commit", "at", "path", "files", "skipped", "patch_bytes"} <= set(folder["delivered"]))
        self.assertEqual(delivered["003-add-a-changelog-page"]["delivery"], "branch")
        status, _, _ = self.request("POST", "/api/jobs", {"repo": "my-app", "feature": "x", "delivery": "elsewhere"})
        self.assertEqual(status, 400)
        status, _, raw = self.request("POST", "/api/jobs", {"repo": "docs-site", "feature": "Export the glossary", "delivery": "folder"})
        self.assertEqual((status, json.loads(raw)["job"]["delivery"]), (201, "folder"))
        status, _, _ = self.request("POST", "/api/config", {"merge": {"delivery": "nowhere"}})
        self.assertEqual(status, 400)
        status, _, raw = self.request("POST", "/api/jobs/docs-site/003-add-a-changelog-page/approve", {})
        self.assertEqual(status, 200, "approve on a delivered job merges it")

    def test_chat_agent_and_agent_updated(self):
        status, _, raw = self.request("POST", "/api/chats", {"repo": "my-app", "mode": "worktree", "agent": "analyst"})
        self.assertEqual((status, json.loads(raw)["chat"]["agent"]), (201, "analyst"))
        status, _, _ = self.request("POST", "/api/chats", {"repo": "my-app", "agent": "builder"})
        self.assertEqual(status, 400, "only chat agents can chat")
        _, _, raw = self.request("GET", "/api/agents/reviewer-code")
        status, _, _ = self.request("POST", "/api/agents/reviewer-code", {"text": json.loads(raw)["text"] + "\nOne more line.\n"})
        self.assertEqual(status, 200)
        self.assertTrue(any(e["type"] == "agent.updated" and e["data"]["name"] == "reviewer-code" and e["data"]["agent"]["name"] == "reviewer-code"
                            for e in self.mock.BUS.since(0)))
        self.request("POST", "/api/agents/reviewer-code/reset", {})

    def test_chat_delete_publishes_chat_removed_with_the_id(self):
        status, _, raw = self.request("POST", "/api/chats", {"repo": "docs-site", "mode": "repo"})
        self.assertEqual(status, 201)
        chat = json.loads(raw)["chat"]
        self.assertEqual(chat["turns"], 0)
        status, _, _ = self.request("POST", "/api/chats/" + chat["id"] + "/delete", {})
        self.assertEqual(status, 200)
        removed = [e for e in self.mock.BUS.since(0) if e["type"] == "chat.removed"]
        self.assertEqual(removed[-1]["data"], {"chat": chat["id"]})


if __name__ == "__main__":
    unittest.main()
