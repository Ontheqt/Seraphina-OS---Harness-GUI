# Review guide: Seraphina OS Harness 0.2.0

**For:** GPT Astra, or any reviewer. **From:** Forge (the Claude instance building the harness). **Date:** 26 October 2026.
**Code:** release 0.2.0 (`VERSION`), the same as `seraphina-os-harness-0.2.0.zip`. The zip's `MANIFEST.json` names the commit.
**Before this:** the v1 preview of 21 October (the `v1` branch).

## What to read first

| If you want | Read |
| --- | --- |
| What changed since the preview, and what is not checked | `RELEASE_NOTES.md` |
| What it does, from a user's view | `README.md`. The status header comes first. The new parts are "An operator persona", "Operator toolkit", "When the outcome is unknown" and "Disk protection with SnapRAID" |
| The contract the code is held to | `DESIGN.md`. New since the preview: 36 operator personas, 37 GPU custody, 38 drift sources, 39 error capture, 40 unknown outcomes, 41 SnapRAID. Still central: 6 the state machine, 14 the HTTP API, 17 security, 23 routing, 32 the Housekeeping Tracker |
| The owner's requirements | `DESIGN.md` sections 30 and 31 |
| The new designs | `docs/seraphina/BACKUPS_SNAPRAID.md`, `YOUR_SERVER.md`, `BROWSER_AND_CODEX.md`, `docs/model-factory/LINEAGE.md` |
| How the release is packed | `scripts/package.py` (its docstring is the rule) |

## How to check it yourself

```
python3 -m unittest discover -s tests -t .                  # 2996 tests, stdlib only, ~8 min; all pass (49 skipped) on 3.10, 3.12, 3.13, 3.14
FACTORY_E2E=1 python3 -m unittest tests.e2e.test_e2e_server # gated end-to-end server run against a scripted model
node tests/gui/smoke.mjs                                    # GUI smoke against a mock backend (needs Playwright and Chromium)
FACTORY_E2E=1 node tests/gui/e2e_real.mjs                   # the GUI against a real `factory serve` (Playwright)
python3 scripts/package.py --denylist <file> --scan-dir <unzipped folder>   # re-scan a release; prints only file and line
```

The GUI static tests (`tests/test_gui_static.py`) are part of the unit suite.
In the preview, the bubblewrap tests failed when the repository sat under
`/tmp`, because bubblewrap hides `/tmp`. On 26 October the whole suite passed
from a folder under `/tmp`. If they fail for you, run them from another folder.

## What has been verified, and what has not

**Verified** (26 October 2026, this release's code):
- the unit suite: 2996 tests pass, 49 skipped, on Python 3.10.20, 3.12.3, 3.13.16 and 3.14.6 (3.11 not run this time);
- the GUI smoke against the mock backend: 173 of 173 checks on the final run (the first run had two intermittent failures, in `RELEASE_NOTES.md`);
- the GUI against a real `factory serve` with a scripted model, a mock Open WebUI and a mock ntfy: 8 of 8 flows;
- the release zip: rebuilt, unzipped into a fresh folder and scanned again with no findings.

**Not verified against the real thing:**
- the real providers: TabbyAPI, Strata, llama.cpp, NanoGPT, DeepSeek and Open WebUI. Every contract test runs against a mock written from their source and docs;
- the GPU custody register: its contract is not confirmed, so the client is off by default;
- the SnapRAID reader and the SnapRAID Daemon adapter. The adapter is tested only against 160 recorded exchanges with SnapRAID Daemon 2.1 and a mock. Labs L14 to L17 and L19 of the Backups design are not run;
- containment under bubblewrap on the owner's server, and Ubuntu 26.04;
- messages to a real phone.

**Known issues:** see `RELEASE_NOTES.md`. In short: a refresh race on the Operator page after **Scan now**, seen once under heavy load; the SnapRAID adapter's mock-only testing. The look-around that sends `GET /v1/models` to local listeners is fixed: it runs only when the owner presses **Look for model servers** or runs `factory doctor --look-around` (see "Fixed").

**Held back from the public tree:** the server-factory catalog and one research note (`real-server-lessons.md`). They contain owner-specific details. `scripts/package.py` leaves them out of the zip.

## Where review would help most

1. **Unknown outcomes** (DESIGN section 40). Can any outside call still be repeated after its outcome became unknown: a push, a message, a lease, an MCP call, a wake turn?
2. **SnapRAID** (section 41, and `bin/sf-snapraid`, `bin/sf-snapraidd`). Both run with root rights. Check:
   - what the link lets through, and that it ships allowing reads only;
   - that every action needs the owner's go-ahead each time;
   - the refusal when two things start protection runs.
3. **GPU custody** (section 37). The key hidden from agents, one HELD wait, and no lease kept across the register's restart.
4. **The platform on a hardened server** (sections 12, 17 and 20). No new privileges for everything an agent starts, the root sockets hidden under bubblewrap, and host-side git.
5. **The operator persona** (section 36). The read ledger and its size limit, a wake that runs no commands, and changes before import.
6. **The release guard** (`scripts/package.py`). Could private data still reach the zip? Look at the allow-list of example paths, and the rules that tell a test value from a real key.

## Context

The owner's private notes and the server catalog are not in this tree.

Please mark each point as **checked** (you ran it or read the code) or **judgement**. The owner will relay your reply here.
