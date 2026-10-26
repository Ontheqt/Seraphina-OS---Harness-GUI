"""Static checks for the web GUI (no browser): files exist, no external URLs, no unsafe
HTML sinks, valid JavaScript syntax, the Seraphina OS Harness branding, the view modules
(swfactory/web/views/<area>.js) and the window.SF namespace they use, and the mock server
used by the smoke test serves the documented shapes (each area's through its own module in
tests/gui_mock/). The browser smoke test is tests/gui/smoke.mjs (Playwright), with one suite
file per area in tests/gui/."""

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
WEB = ROOT / "swfactory" / "web"
FILES = ("index.html", "app.js", "flows.js", "style.css", "login.html")
URL_RE = re.compile(r"https?://", re.I)
PRODUCT = "Seraphina OS Harness"
SVG_NS = "http://www.w3.org/2000/svg"   # the favicon's XML namespace: a name, never fetched
AREAS = ("routing", "tracker", "personas", "messages", "operator", "backups")   # one view module, mock module and suite each
VIEWS = WEB / "views"
# window.SF, the one namespace the view modules use (app.js, "view modules"): area builders rely on these names.
SF_API = ("version", "product", "registerView", "registerJobTab", "extend", "refreshJobs", "h", "add", "clear", "icon", "btn",
          "field", "selectEl", "callout", "emptyState", "skeleton", "setBusy", "timeEl", "viewHead", "procedureList", "rebuild",
          "placeholder", "unavailable", "trackFades", "text", "md", "safeHref", "relTime", "fmtDur", "fmtNum", "plural",
          "firstLine", "enc", "isObj", "api", "ApiError", "gone", "toast", "toastError", "copyText", "openModal",
          "confirmModal", "on", "onReconnect", "conn", "drawer", "route", "go", "jobs", "job", "repos", "agents",
          "filterRepo", "isNarrow", "storage", "announce", "sentence", "words", "itemWord", "custodyAnswer", "custodyReason",
          "custodyPlain", "itemHref", "itemLink", "beforeImport")


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
            hits = [line.strip() for line in read(name).replace(SVG_NS, "").splitlines() if URL_RE.search(line)]
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
        for view in ("view-home", "view-flow", "view-grid", "view-procedures"):
            self.assertIn(f'id="{view}"', html)
        self.assertIn('href="#/home"', html)
        self.assertIn('href="#/grid"', html)
        self.assertIn('href="#/procedures"', html)

    def test_branding_is_seraphina_os_harness(self):
        """The product is Seraphina OS Harness: the page title, the header, the login page, the favicon's letter and
        Welcome say so; Software Factory is the name of one section (its card on Home, the board, the booklet)."""
        html, login, app = read("index.html"), read("login.html"), read("app.js")
        self.assertIn(f"<title>{PRODUCT}</title>", html)
        self.assertIn(f'<span class="brand-name">{PRODUCT}</span>', html)
        self.assertRegex(html, r'<a class="brand" href="#/home" aria-label="Seraphina OS Harness: Home">')
        self.assertIn(f"<title>Sign in · {PRODUCT}</title>", login)
        self.assertIn(f"<h1>{PRODUCT}</h1>", login)
        for page in (html, login):
            icon = re.search(r'<link rel="icon" href="([^"]+)"', page).group(1)
            self.assertTrue(icon.startswith("data:image/svg+xml,"), icon)
            self.assertIn("%3ES%3C/text%3E", icon, "the favicon shows the letter S")
        self.assertNotIn("Software Factory", re.sub(r"<!--.*?-->", "", html, flags=re.S).split("<body>")[0])
        self.assertIn(f"const PRODUCT = '{PRODUCT}';", app)
        self.assertIn("'Welcome to ' + PRODUCT", app)
        self.assertIn("document.title = (human ? '(' + human + ') ' : '') + view + ' · ' + PRODUCT;", app)
        self.assertIn("title: 'Software Factory'", app, "Software Factory is Home's first section")
        self.assertIn("const v = parts[0] || 'home';", app, "Home is the default route")

    def test_view_modules_are_loaded_after_app_and_use_only_window_sf(self):
        """Each area lives in swfactory/web/views/<area>.js, loaded by index.html after app.js (deferred, so app.js
        boots after every module registered), and reaches the page only through window.SF."""
        html = read("index.html")
        scripts = re.findall(r'<script src="(/[^"]+)" defer></script>', html)
        self.assertEqual(scripts[:2], ["/flows.js", "/app.js"])
        on_disk = sorted(f"/views/{path.name}" for path in VIEWS.glob("*.js"))
        self.assertEqual(sorted(scripts[2:]), on_disk, "index.html loads every view module, and only those")
        self.assertEqual(sorted(path.stem for path in VIEWS.glob("*.js")), sorted(AREAS))
        for path in VIEWS.glob("*.js"):
            js = path.read_text(encoding="utf-8")
            code = strip_js_comments(js)
            with self.subTest(view=path.name):
                self.assertGreater(len(js), 200)
                self.assertTrue(js.startswith("/* Seraphina OS Harness: "), "a header naming the area and its DESIGN notes")
                self.assertIn("'use strict';\n(function (SF) {", js)
                self.assertTrue(js.rstrip().endswith("})(window.SF);"), "one IIFE that gets window.SF")
                self.assertIn("SF.registerView({", code)
                self.assertNotRegex(js, URL_RE, "no URLs")
                for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function(",
                            "createContextualFragment", "srcdoc", "fetch(", "XMLHttpRequest", "EventSource", "localStorage",
                            "sessionStorage", "import(", "importScripts", "window.SFFlows"):
                    self.assertNotIn(bad, code, f"{path.name}: {bad} (use SF.h, SF.api, SF.on, SF.storage)")
                self.assertNotRegex(code, r"setAttribute\(\s*['\"]on", "inline event handler attributes")
                self.assertEqual(set(re.findall(r"\bwindow\.(\w+)", code)), {"SF"}, "window.SF is the only global it reads")

    def test_window_sf_is_the_documented_namespace(self):
        """window.SF (app.js) is frozen and carries exactly the helpers the view modules are written against."""
        app = read("app.js")
        body = app[app.index("  const SF = Object.freeze({"):]
        body = body[: body.index("\n  });\n")]
        keys = []
        for line in body.splitlines()[1:]:
            if not line.startswith("    ") or line.startswith("     ") or line.strip().startswith(("//", "/*", "*")):
                continue   # only the object's own lines (4 spaces), not comments or continuations
            named = re.match(r"\s+(\w+):", line)
            keys += [named.group(1)] if named else [k.strip() for k in line.split(",") if re.fullmatch(r"\w+", k.strip())]
        self.assertEqual(sorted(keys), sorted(SF_API))
        self.assertIn("Object.defineProperty(window, 'SF', { value: SF, writable: false, configurable: false, enumerable: true });", app)
        for name in ("registerView", "registerJobTab", "extend", "openPanel", "closePanel", "onEvent", "onReconnect", "rebuild"):
            self.assertRegex(app, rf"function {name}\(", name)

    def test_one_vocabulary_for_every_section(self):
        """SF.words (app.js) holds the words of items.WORDS, driftsources.STATES and custody.LEASE_STATES, and no view
        module keeps a copy of a table: an item reads "Waiting for a check" on every page, a drift check "Cannot reach
        it", a lease "Given back"."""
        from swfactory import custody, driftsources, items

        app = read("app.js")
        block = app[app.index("  const WORDS = Object.freeze({"):]
        block = block[: block.index("\n  });\n")]

        def table(name):
            found = re.search(name + r": Object\.freeze\(\{(.*?)\}\)", block)
            self.assertIsNotNone(found, name)
            return dict(re.findall(r"(\w+): '([^']*)'", found.group(1)))

        self.assertEqual(table("items"), items.WORDS)
        self.assertEqual(set(table("drift")), set(driftsources.STATES))
        self.assertEqual(set(table("driftTone")), set(driftsources.STATES))
        self.assertEqual(set(table("lease")), set(custody.LEASE_STATES))
        for path in sorted(VIEWS.glob("*.js")):
            code = strip_js_comments(path.read_text(encoding="utf-8"))
            for word in ("Waiting for a check", "Working on it", "Observation too old", "Not given back"):
                self.assertNotIn("'" + word + "'", code, f"{path.name} keeps its own copy of SF.words ({word})")

    def test_outcome_words_are_the_rule_s_and_the_receipts(self):
        """DESIGN §40: SF.words.outcome names outcomes.STATES in words ("Outcome unknown: it may or may not have
        happened"), SF.words.unknownTitle is outcomes.UNKNOWN_TITLE, and SF.words.receiptTone covers receipts.OUTCOMES
        and is never green; a view reads them from SF.words (no copy of the tables)."""
        from swfactory import outcomes, receipts

        app = read("app.js")
        block = app[app.index("  const WORDS = Object.freeze({"):]
        block = block[: block.index("\n  });\n")]

        def table(name):
            found = re.search(r"\b" + name + r": Object\.freeze\(\{(.*?)\}\)", block)
            self.assertIsNotNone(found, name)
            return dict(re.findall(r"(\w+): '([^']*)'", found.group(1)))

        self.assertEqual(set(table("outcome")), set(outcomes.STATES))
        self.assertEqual(table("outcome")["unknown"], "Outcome unknown: it may or may not have happened")
        self.assertEqual(table("outcomeTone"), {"succeeded": "ok", "failed": "err", "unknown": "warn"})
        self.assertIn(f"unknownTitle: '{outcomes.UNKNOWN_TITLE}'", block)
        tones = table("receiptTone")
        self.assertEqual(set(tones), set(receipts.OUTCOMES))
        self.assertNotIn("ok", tones.values(), "a receipt is never green")
        self.assertEqual((tones["outcome_unknown"], tones["reported_failed"]), ("warn", "err"))
        for path in sorted(VIEWS.glob("*.js")):
            code = strip_js_comments(path.read_text(encoding="utf-8"))
            self.assertNotIn("'Outcome unknown: it may or may not have happened'", code, f"{path.name} keeps its own copy")

    def test_messages_rule_words_name_every_rule(self):
        """The rulebook's quiet-hours list (views/messages.js RULE_WORDS) names every notify.RULE_IDS kind, the disk
        protection ones (§41) too, so none of them is missing from "may break through"."""
        from swfactory import notify

        js = (VIEWS / "messages.js").read_text(encoding="utf-8")
        block = js[js.index("  const RULE_WORDS = {"):]
        block = block[: block.index("\n  };\n")]
        self.assertEqual(sorted(re.findall(r"'([a-z_]+\.[a-z_]+)':", block)), sorted(notify.RULE_IDS))

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_custody_answers_in_plain_words(self):
        """SF.custodyAnswer (app.js) words every custody answer (DESIGN §37): a refused claim with the register's words
        (the line's own, else reason_codes), a HELD wait and how it ended, "not sent" to a register that restarted, and
        a request with no answer recorded (§40); SF.custodyPlain is a grant, a give-back or an ended wait only."""
        from swfactory import custody

        app = read("app.js")
        code = app[app.index("  function custodyReason(code, codes) {"):app.index("  /** Text from the server as a sentence of the page")]
        script = ("const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);\n" + code +
                  "\nconst codes = JSON.parse(process.argv[1]);\nconst lines = JSON.parse(process.argv[2]);\n"
                  "console.log(JSON.stringify(lines.map((x) => [custodyAnswer(x, codes), custodyPlain(x)])));")
        lines = [
            {"op": "claim", "outcome": "denied", "reason": "FULL", "words": "the card cannot fit this model now"},
            {"op": "claim", "outcome": "denied", "reason": "LEASED"},
            {"op": "claim", "outcome": "granted"},
            {"op": "wait", "outcome": "waiting", "reason": "HELD", "seconds": 120},
            {"op": "wait", "outcome": "granted", "reason": "HELD", "seconds": 60.2},
            {"op": "wait", "outcome": "gave_up", "reason": "HELD", "seconds": 120},
            {"op": "wait", "outcome": "ended", "reason": "FULL"},
            {"op": "release", "outcome": "lost", "reason": "not sent: the register restarted (epoch a became b)"},
            {"op": "renew", "outcome": "lost", "reason": "not sent: the register restarted (epoch a became b)"},
            {"op": "release", "outcome": "released"},
            {"op": "claim", "outcome": "requested"},
        ]
        proc = subprocess.run(["node", "-e", script, json.dumps(custody.REFUSAL_WORDS), json.dumps(lines)],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        got = json.loads(proc.stdout)
        self.assertEqual(got, [
            ["Lease denied: the card cannot fit this model now", False],
            ["Lease denied: " + custody.REFUSAL_WORDS["LEASED"], False],
            ["Lease granted", True],
            ["Waits up to 120s: the card is held by someone else", False],
            ["The hold ended after 60.2s: lease granted", True],
            ["Gave up after 120s: the card is still held", False],
            ["Stopped waiting: " + custody.REFUSAL_WORDS["FULL"], False],
            ["Not released: the register restarted, so it no longer knows this lease", False],
            ["Not renewed: the register restarted, so it no longer knows this lease", False],
            ["Lease given back", True],
            ["Asked, with no answer recorded: outcome unknown", False],
        ])
        fallback = dict(re.findall(r"(\w+): '([^']*)'", app[app.index("const CUSTODY_FALLBACK"):].split("\n", 1)[0]))
        self.assertEqual(fallback, {k: custody.REFUSAL_WORDS[k] for k in fallback}, "the words without reason_codes are custody's")

    def test_custody_editor_offers_every_token_source(self):
        """Settings › Providers (views/routing.js CUSTODY_FIELDS) edits every custody.SETTINGS_KEYS field (models and the
        timeout have their own fields), token_credential among them (one of three token sources, DESIGN §37)."""
        from swfactory import custody

        js = (VIEWS / "routing.js").read_text(encoding="utf-8")
        block = js[js.index("  const CUSTODY_FIELDS = ["):]
        block = block[: block.index("];\n")]
        keys = set(re.findall(r"\['([a-z_]+)', '", block))
        self.assertEqual(keys | {"models", "timeout"}, set(custody.SETTINGS_KEYS))
        self.assertIn("const TOKEN_KEYS = ['token_file', 'token_env', 'token_credential'];", js)

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
        self.assertEqual(sorted(table), routing, "every routing preset has a row, and only those")
        for name in routing:
            data = json.loads((examples / f"{name}.json").read_text(encoding="utf-8"))
            row = table[name]
            with self.subTest(preset=name):
                self.assertEqual(row["providers"], {k: v.get("api_key_env") for k, v in data["providers"].items()})
                self.assertEqual(row["models"], {k: f"{v['provider']}/{v['model']}" for k, v in data["models"].items()})
                self.assertEqual(row["agents"], {k: v["model"] for k, v in data["agents"].items() if "model" in v})
                self.assertTrue(row["title"] and row["does"].endswith(".") and row["when"].endswith("."), row)

    def test_mock_server_serves_the_view_modules_and_loads_every_area(self):
        """tests/gui_mock_server.py serves swfactory/web/views/*.js and loads tests/gui_mock/<area>.py for every area;
        each area has its smoke suite tests/gui/<area>.mjs, which smoke.mjs imports."""
        from tests import gui_mock
        self.assertEqual(gui_mock.AREAS, AREAS)
        for module, name in zip(gui_mock.load(), AREAS):
            with self.subTest(area=name):
                self.assertEqual(module.AREA, name)
                self.assertTrue(module.__doc__ and "DESIGN" in module.__doc__)
        smoke = (ROOT / "tests" / "gui" / "smoke.mjs").read_text(encoding="utf-8")
        for name in ("home",) + AREAS:
            with self.subTest(suite=name):
                self.assertTrue((ROOT / "tests" / "gui" / f"{name}.mjs").is_file())
                self.assertIn(f"import {{ run as {name}Suite }} from './{name}.mjs';", smoke)
                self.assertIn(f"['{name}', {name}Suite]", smoke)

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

    def test_the_look_for_model_servers_runs_only_from_its_button(self):
        """The look-around (POST /api/setup/look-around) is sent by lookAround() alone, and lookAround is wired to the
        button's click only: nothing on page load, on a route change or on a timer asks for it, and no other file
        names the route."""
        js = strip_js_comments(read("app.js"))
        self.assertEqual(js.count("/api/setup/look-around"), 1)
        self.assertRegex(js, r"async function lookAround\(\) \{[^}]*api\('/api/setup/look-around', \{ method: 'POST'")
        uses = [m.start() for m in re.finditer(r"\blookAround\b(?!Widget)", js)]
        self.assertEqual(len(uses), 2, "its definition and the button's click listener")
        self.assertIn("b.addEventListener('click', lookAround);", js)
        # the one timer there (a refusal's Retry-After) only redraws: it never looks again
        body = js[js.index("async function lookAround() {"):js.index("function lookAroundWidget() {")]
        self.assertEqual(body.count("setTimeout("), 1)
        self.assertRegex(body, r"setTimeout\(\(\) => \{ if \(L\.until === until\) \{ lookRefusalOver\(\); invalidate\('flow'\); \} \}")
        self.assertIn("it sends no key and changes nothing", js)
        for name in ["flows.js", *(f"views/{v.name}" for v in sorted((WEB / "views").glob("*.js")))]:
            self.assertNotIn("/api/setup/look-around", strip_js_comments(read(name)), name)

    def test_reviewers_are_not_hardcoded(self):
        """DESIGN §22: the strip, the chips and Routing follow pipeline.reviewers; no four-name tables."""
        js = strip_js_comments(read("app.js"))
        self.assertNotIn("REVIEWER_SHORT", js)
        self.assertNotIn("REVIEWER_NAME", js)
        self.assertNotRegex(js, r"\[\s*'security'\s*,\s*'ux'\s*,\s*'ui'\s*,\s*'code'\s*\]")
        self.assertIn("function reviewerLabels", js)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_a_saved_setting_runs_the_doctor_again(self):
        """Found by tests/gui/e2e_real.mjs against the real server: the header's Doctor badge kept the checks it read at
        page load, so after the setup wizard's Apply a fresh install's to-do list ("9 things to set up") became
        "9 issues" in red while the server found 3. app.js's api() now runs Doctor again, once, after any saved
        setting (config, a preset, a repository, a key) settles; never after a read, a refused save or other POSTs."""
        proc = subprocess.run(["node", "-e", DOCTOR_HARNESS, str(WEB / "app.js")], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DOCTOR-OK", proc.stdout)

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
// This machine (GET /api/setup's host): the preset's key folder is not here, and servers listen on other ports.
const sd2 = F.emptyDraft();
sd2.choices.where = 'local';
sd2.choices['local-servers'] = ['strata', 'tabby'];
const there = Object.assign({}, ctx, { setup: { configured: false, host: { key_dirs: { '/etc/ai-stack': false }, listening: [5000, 5005, 18090] } } });
const cards2 = F.optionsOf(F.stepOf(setup, 'local-servers'), sd2, there);
eq(cards2.find((o) => o.id === 'tabby').sub, '127.0.0.1:5000 · something listens there', 'a listener on the usual port is said');
eq(cards2.find((o) => o.id === 'strata').sub, '127.0.0.1:18080 · nothing listens there now', 'and its absence');
eq(F.optionsOf(F.stepOf(setup, 'local-servers'), sd2, ctx).find((o) => o.id === 'strata').sub, '127.0.0.1:18080', 'not asked: nothing said');
const groups2 = F.groupsOf(F.stepOf(setup, 'local-details'), sd2, there);
const strata = groups2.find((g) => g.id === 'strata');
ok(!/ai-stack/.test(strata.tip + JSON.stringify(strata.fields)), 'no tip or hint names a key folder this machine does not have: ' + strata.tip);
eq(strata.fields.find((f) => f.id === 'strata.url').hint, 'Nothing listens on 127.0.0.1:18080 now. Ports in use here: 5000, 5005, 18090. Press “Look for model servers” above to see which of them is a model server.', 'the ports that do listen, and how to tell which is a model server');
eq(groups2[0], { id: 'look-around', title: 'Model servers on this machine', widget: 'look-around', fields: [] }, 'the look-around button comes first, above the servers');
eq(F.groupsOf(F.stepOf(setup, 'local-details'), F.emptyDraft(), there).map((g) => g.id), ['look-around'], 'it is there with no server picked yet');
const lookedNone = Object.assign({}, ctx, { setup: { configured: false, host: Object.assign({}, there.setup.host, { model_servers: [], model_servers_at: '2026-10-26T09:00:00Z' }) } });
eq(F.groupsOf(F.stepOf(setup, 'local-details'), sd2, lookedNone).find((g) => g.id === 'strata').fields.find((f) => f.id === 'strata.url').hint,
  'Nothing listens on 127.0.0.1:18080 now. Ports in use here: 5000, 5005, 18090. None of them answered like a model server when you looked.', 'a look that found none says so');
const lookedBroke = Object.assign({}, ctx, { setup: { configured: false, host: Object.assign({}, there.setup.host, { model_servers_error: 'PermissionError: denied', model_servers_at: '2026-10-26T09:00:00Z' }) } });
eq(F.groupsOf(F.stepOf(setup, 'local-details'), sd2, lookedBroke).find((g) => g.id === 'strata').fields.find((f) => f.id === 'strata.url').hint,
  'Nothing listens on 127.0.0.1:18080 now. Ports in use here: 5000, 5005, 18090. The last look for model servers did not work: press \u201cLook for model servers\u201d above to try again.', 'a look that broke is not one that found none');
const longId = Object.assign({}, ctx, { setup: { configured: false, host: Object.assign({}, there.setup.host, { model_servers_at: '2026-10-26T09:00:00Z', model_servers: [{ url: 'http://127.0.0.1:5000/v1', port: 5000, status: 200, ids: ['a'.repeat(120)] }] }) } });
eq(F.groupsOf(F.stepOf(setup, 'local-details'), sd2, longId).find((g) => g.id === 'strata').fields.find((f) => f.id === 'strata.url').hint,
  'Nothing listens on 127.0.0.1:18080 now. Model servers answer here: http://127.0.0.1:5000/v1 (lists ' + 'a'.repeat(59) + '\u2026). If one of them is Strata, use its address.', 'a listener\u2019s long model id is cut at 60 characters');
eq(F.idText('b'.repeat(60)), 'b'.repeat(60), 'an id of 60 characters is shown whole');
// B.5.4: the model servers the owner's look found are offered by their address (the presets' ports are only the usual ones)
const servers = Object.assign({}, ctx, { setup: { configured: false, host: { key_dirs: { '/etc/ai-stack': false }, listening: [5000, 5005, 18090], model_servers_at: '2026-10-26T09:00:00Z',
  model_servers: [{ url: 'http://127.0.0.1:5000/v1', port: 5000, status: 200, ids: ['a', 'b', 'c', 'd'] }, { url: 'http://127.0.0.1:5005/v1', port: 5005, status: 401, ids: [] }] } } });
eq(F.groupsOf(F.stepOf(setup, 'local-details'), sd2, servers).find((g) => g.id === 'strata').fields.find((f) => f.id === 'strata.url').hint,
  'Nothing listens on 127.0.0.1:18080 now. Model servers answer here: http://127.0.0.1:5000/v1 (lists a, b, c, …); http://127.0.0.1:5005/v1 (wants a key). If one of them is Strata, use its address.', 'the model servers here, offered by address');
ok(F.groupsOf(F.stepOf(setup, 'local-details'), sd2, servers).find((g) => g.id === 'tabby').fields.find((f) => f.id === 'tabby.url').hint === undefined, 'a server on its own port: no hint');
ok(groups2.find((g) => g.id === 'tabby').fields.find((f) => f.id === 'tabby.url').hint === undefined, 'no hint where a server listens');
ok(/ai-stack/.test(F.groupsOf(F.stepOf(setup, 'local-details'), sd2, ctx).find((g) => g.id === 'strata').tip), 'not asked: the preset\u2019s tip stays');
F.setValue(sd2, 'local-details', 'strata.model', 'strata');
F.setValue(sd2, 'local-details', 'tabby.model', 'qwen3.8-flash');
const noFile = F.setupPlan(sd2, there, []).items.find((i) => i.id === 'models').requests[0].body;
ok(!('api_key_file' in noFile.providers.strata), 'no key file a folder of this machine cannot hold: ' + JSON.stringify(noFile.providers.strata));
ok('api_key_file' in F.setupPlan(sd2, ctx, []).items.find((i) => i.id === 'models').requests[0].body.providers.strata, 'not asked: as before');
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


DOCTOR_HARNESS = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const i = src.indexOf('  async function api(path, opts) {');
const j = src.indexOf('  const jobPath = ', i);
if (i < 0 || j < 0) throw new Error('api() not found in app.js');
let timers = [];
const setTimeout = (fn) => { timers.push(fn); return timers.length; };
const clearTimeout = (id) => { if (id) timers[id - 1] = null; };
const flush = () => { const now = timers; timers = []; for (const fn of now) if (fn) fn(); };
let runs = 0;
function loadDoctor(manual) { runs++; if (manual !== false) throw new Error('a quiet run: loadDoctor(false)'); }
class ApiError extends Error { constructor(status, msg, data) { super(msg); this.status = status; this.data = data; } }
function onUnauthorized() {}
const sleep = () => Promise.resolve();
let status = 200;
const fetch = async () => ({ status, ok: status < 400, statusText: '', headers: { get: () => 'application/json' }, json: async () => (status < 400 ? {} : { error: 'refused' }), text: async () => '{}' });
const call = eval(src.slice(i, j) + '\n;api');
const expect = (want, what) => { flush(); if (runs !== want) throw new Error(what + ': ' + runs + ' Doctor runs, wanted ' + want); runs = 0; };
(async () => {
  for (const path of ['/api/config', '/api/config/preset', '/api/repos', '/api/secrets']) { await call(path, { body: {} }); expect(1, 'POST ' + path); }
  await call('/api/config'); expect(0, 'a read');
  for (const path of ['/api/jobs', '/api/models/local/test', '/api/repos/app/scout', '/api/config/x']) { await call(path, { body: {} }); expect(0, 'POST ' + path); }
  await call('/api/config', { body: { a: 1 } }); await call('/api/config', { body: { a: null } }); expect(1, 'two saves back to back (a Test entry added and removed)');
  status = 400;
  try { await call('/api/config', { body: {} }); } catch (e) { /* refused */ }
  expect(0, 'a refused save');
  console.log('DOCTOR-OK');
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
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
        base = {"name", "description", "model", "driver", "source", "tier", "color", "extra_body"}
        for agent in cfg["agent_list"]:  # settings._agent_summary (a route's placement fields when it has them, §23)
            self.assertTrue(base <= set(agent) <= base | {"effort", "escalate", "reason", "independent_alternate"}, sorted(agent))
            self.assertIn(agent["source"], ("builtin", "user", "repo"))

    def test_jobs_carry_the_orchestrator_routing_snapshot(self):
        from swfactory import orchestrator
        _, _, raw = self.request("GET", "/api/state")
        job = next(j for j in json.loads(raw)["jobs"] if j["id"] == "002-rate-limit-signup-form")
        self.assertEqual(set(job["routing"]), {"spec-writer", "builder", "approver", *("reviewer-" + r for r in job["reviewers"])})
        base = {"model", "driver", "provider", "model_id", "family", "extra_body", "model_entry", "provider_entry"}
        placement = {"reason", "escalate", "independent_alternate", "builds", "overflow", "cli_model"}   # Factory._resolve_route
        for route in job["routing"].values():
            self.assertTrue(base <= set(route) <= base | placement, sorted(route))
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
                "reviews", "routing", "delivery", "usage", "created_at", "updated_at", "profile", "cost"}
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

    def test_look_for_model_servers_only_when_asked(self):
        """POST /api/setup/look-around as doctor.LookAround: GET /api/setup carries no model_servers until the owner
        looks, then the last look with when it was taken; a second look too soon is 429 with Retry-After."""
        self.request("POST", "/__mock/look-around", {"forget": True})
        self.addCleanup(self.request, "POST", "/__mock/look-around", {"forget": True, "finds": self.mock.LOOK_FINDS})
        host = json.loads(self.request("GET", "/api/setup")[2])["host"]
        self.assertEqual(set(host), {"key_dirs", "listening"})
        status, _, raw = self.request("POST", "/api/setup/look-around", {})
        self.assertEqual(status, 200)
        looked = json.loads(raw)
        self.assertEqual(set(looked), {"at", "model_servers", "note"})
        self.assertEqual([m["url"] for m in looked["model_servers"]], ["http://127.0.0.1:5000/v1", "http://127.0.0.1:5005/v1"])
        self.assertIn("without a key", looked["note"])
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/setup/look-around", body=b"{}", headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"})
        again = conn.getresponse()
        body = json.loads(again.read())
        conn.close()
        self.assertEqual(again.status, 429)
        self.assertIn(again.getheader("Retry-After"), ("28", "29", "30"))
        self.assertRegex(body["error"], r"You can look again in (28|29|30) seconds")
        host = json.loads(self.request("GET", "/api/setup")[2])["host"]
        self.assertEqual((host["model_servers"], host["model_servers_at"]), (looked["model_servers"], looked["at"]))
        self.request("POST", "/__mock/look-around", {"finds": []})
        self.assertEqual(json.loads(self.request("POST", "/api/setup/look-around", {})[2])["model_servers"], [])
        # a look that broke (doctor.look_for_model_servers): its error alone, and the setup status says so
        self.request("POST", "/__mock/look-around", {"error": "PermissionError: [Errno 13] Permission denied: '/proc/net/tcp'"})
        broke = json.loads(self.request("POST", "/api/setup/look-around", {})[2])
        self.assertEqual(set(broke), {"at", "error"})
        host = json.loads(self.request("GET", "/api/setup")[2])["host"]
        self.assertNotIn("model_servers", host)
        self.assertEqual((host["model_servers_error"], host["model_servers_at"]), (broke["error"], broke["at"]))

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

    def test_view_modules_are_served(self):
        for name in AREAS:
            status, ctype, raw = self.request("GET", f"/views/{name}.js")
            self.assertEqual((status, ctype.split(";")[0]), (200, "text/javascript"))
            self.assertEqual(raw, (VIEWS / f"{name}.js").read_bytes())
        for bad in ("/views/../app.js", "/views/x.md", "/views/sub/x.js"):
            self.assertEqual(self.request("GET", bad)[0], 404, bad)

    def test_area_contracts(self):
        """Each area's mock module checks its own shapes against swfactory/ (tests/gui_mock/<area>.py contract)."""
        from tests import gui_mock
        for module in gui_mock.load():
            if hasattr(module, "contract"):
                with self.subTest(area=module.AREA):
                    module.contract(self)

    def test_every_seeded_id_is_one_the_server_routes_accept(self):
        """The areas seed chats, jobs and plans into one mock: each id must match the server's route patterns
        (server.CHAT_ID_RE and friends), or a page asks the mock for what the real server would answer 404."""
        from swfactory import server as real
        rules = real._PARAM_RULES
        with self.mock.LOCK:
            jobs = list(self.mock.STATE["jobs"].values())
            chats = list(self.mock.STATE["chats"].values())
            plans = list(self.mock.STATE["plans"].values())
        self.assertTrue(jobs and chats and plans)
        for kind, rows in (("job", jobs), ("chat", chats), ("plan", plans)):
            for row in rows:
                with self.subTest(kind=kind, id=row["id"]):
                    self.assertTrue(rules[kind][0].fullmatch(row["id"]), f"a {kind} id the server's routes accept")
                    if row.get("repo"):
                        self.assertTrue(rules["repo"][0].fullmatch(row["repo"]))

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
