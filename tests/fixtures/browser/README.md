# The browser adapter's fixtures

What the browser adapter's tests run against (DESIGN.md §43,
docs/seraphina/BROWSER_AND_CODEX.md §4.6).

## `site/`: the disposable fixture site

The harness's own small site. `swfactory/browser/fixture.py` serves it on
`127.0.0.1:18937` inside the browser's namespace, which has no network, and adds
the routes the pages post to (`/submit`, `/order`, `/effect`), a download whose
suggested name carries an injection string (`/report.csv`), a redirect off the
origin set (`/away`) and a private page behind a seeded cookie
(`/private.html`). Every request it acts on is one JSON line in the session
folder's `fixture.jsonl`, which is what the live tests count.

| Page | What it is for |
| ---- | -------------- |
| `index.html` | T1 to T6: the order number, the unnamed form with Name and Submit, the result element, Re-render (T8) and Rename the button (T11) |
| `effects.html` | T11 and K15: a link with an `onclick` that posts, a `div role="link"` that posts, a Save button, and a link that leaves the origin set (T18) |
| `popup.html` | T18: a `target=_blank` link and a `window.open` button |
| `shop.html` | T11: the slow shop. The order is taken at once and the answer stays open for `shop_delay` seconds |
| `injection.html` | T12: off-screen, white-on-white, `aria-hidden`, alt-text and WebMCP texts, an injected title, a dialog and a download name, chat-template control tokens and format characters |
| `decoy.html` | `expect`'s scope: the expected text only outside the result element; a form for Enter (T11) |
| `login.html`, `private.html`, `elsewhere.html` | T9's login page, its private page, and where the redirect lands |

## `playwright-0.0.83/`: responses recorded from the real Playwright MCP

One file per scenario of `tests/browser_rig.py` (`basic`, `stale`, `enter`,
`effects`, `shop`, `popups`, `injection`, `login`, `urls`): the tool list the
server offered and every call the shim made with the answer it got, in order.
`tests/fake_playwright.py` replays them for the unit tests
(`tests/test_browser_shim.py`, `tests/test_browser_harness.py`); the live
contract test (`tests/test_browser_live.py`, `ContractLiveTests`) runs each
scenario against the real server and fails when the calls or the answers differ.
Local paths and times are placeholders (`<session>`, `<downloads>`, `<time>`)
and every screenshot is a 1x1 PNG, so nothing of the machine that recorded them
is in the files.

**What they were recorded with, honestly.** Playwright MCP **0.0.83**, the
design's provisional pin (npm integrity
`sha512-oNcl+Ae2/IAjhfPeP46BfIkSakfmprY+aOtkv5MjrQ4lPav4/yNtPhL0iq8SlIM90oApWgBDUxaNKvktazUKOg==`,
checked in the install's lock file; its bundled playwright-core is
1.64.0-alpha-1790635538000), Node 22.22.0, and **chromium-headless-shell
141.0.7390.37** (Playwright build 1194), which is what the recording machine had.
That is **not** the Chrome the design pins: 0.0.83's `browsers.json` names
chrome-headless-shell **155.0.8059.12** (build 1247), and no 155 build was
installed there. The shim passes `--executable-path`, so Playwright MCP drove the
older Chrome; the doctor's `browser pins` line reports exactly this difference
(Chrome 141 against the pin 155) on that machine. Before the owner's server
relies on these recordings, record them again with the pinned 155 build:

    SF_BROWSER_RUNTIME=<the harness's own runtime directory> \
    SF_BROWSER_RECORDED_WITH="Playwright MCP 0.0.83 with chrome-headless-shell 155.0.8059.12" \
    python3 -m tests.browser_rig record

(`SF_BROWSER_RECORD=1` on the contract test does the same.) Things the
recordings pin about 0.0.83, which the design had as UNVERIFIED: `--caps
testing` is accepted although `--help` lists only vision, pdf and devtools, and
it offers `browser_verify_text_visible`; `browser_network_requests` reports each
request's method and the status the site answered (`2. [POST] … => [200] OK`),
but not whether it had a body, and it numbers requests per page load;
`browser_click` and `browser_type` take the ref as `target`; the current tab
stays on the page that opened a popup; a `javascript:` navigation runs its
script (so the shim's URL rules refuse it first); a redirect off
`--allowed-origins` is followed; an action's answer carries a link to a snapshot
file, never the tree.

The pre-installed Chrome on the recording machine was a shell wrapper around
`headless_shell.real` that adds font flags; the runtime used `headless_shell.real`
itself, because the wrapper names a path outside the namespace's binds.
