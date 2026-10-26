# Release notes: Seraphina OS Harness 0.2.0

**Date:** 26 October 2026. **Before this:** the v1 preview of 21 October (the
`v1` branch). **Status:** work in progress. Nothing new in this release has
run on a real server yet.

## What is new since the 21 October preview

### The GUI is complete for every area

The preview had the Grid Card GUI and its wizards, and it still said
"Software Factory". Now the GUI is branded Seraphina OS Harness and has a
screen for every area:

- **Home:** what needs you, from every area, on one page.
- **Routing:** which model does each step, why, and what it costs.
- **Housekeeping Tracker:** the promises, findings and checks the agents
  still owe you.
- **Personas:** your Open WebUI profiles, and the personas that wake
  themselves. Each persona and agent page has **Activity**: what it did in
  the last 7 days.
- **Messages & Wakes:** messages to your phone, and when each persona wakes.
- **Operator:** the operator persona's change receipts, what she did not
  check, her GPU custody requests and her drift checks.
- **Backups:** disk protection with SnapRAID (see below).

Every screen was checked at desktop width (1440 px) and phone width
(390 px), in light and dark.

### The operator persona

- **A wake that only reads.** A persona's wake can be set to run no
  commands at all: she reads files and the harness's status, and nothing
  else. The operator's own wake may read her change receipts.
- **A read ledger** (off by default). The harness never writes her memory,
  so it records what it reads instead: each read, and a copy of each new
  version of a file. It keeps 32 MiB at most, and the oldest copies go first.
- **Changes before import.** The first import of her Open WebUI profile is
  the baseline. What changed before it is shown only from the receipts that
  you or she attach by id, labelled "reported by the operator".
- **GPU custody client.** Before a local model call, the harness can ask the
  operator's GPU custody register for a lease. It knows two states, FULL and
  HELD. When the register restarts, every earlier lease counts as lost. The
  register's key is hidden from the agents. It stays off until the
  register's contract is confirmed (DESIGN §37).

### An unknown outcome is never repeated (DESIGN §40)

Some calls change something outside the harness: a merge or a push, a
message to your phone, a GPU lease, a tool call. If no answer confirms such a
call, its outcome is "unknown", and the harness never repeats it by itself.
It looks again, read only, when it can. Otherwise it puts "Outcome unknown:
check before retrying" on the tracker for you. The screens say how each call
ended in words, not only as a colour.

### Disk protection with SnapRAID (DESIGN §41)

- **The reader** (`bin/sf-snapraid`): a small root helper that reads
  SnapRAID's state and your disks' health. It protects nothing, and the
  Backups page says so.
- **The SnapRAID Daemon adapter** (`bin/sf-snapraidd`): for SnapRAID Daemon
  2.1 with SnapRAID 15. Backups reads the daemon's nightly run, its stops
  before mass deletions and your disks' health. After your go-ahead each
  time, it asks the daemon to protect now, skip a night, stop safely, bring
  files back or repair damage. It ships allowing reads only; actions stay off
  until you widen its settings.
- Seraphina never runs or schedules SnapRAID herself. If two things start
  protection runs, she says so and offers no run button.
- Both are off until you install them and turn them on.

### The platform

- Python 3.10, 3.11, 3.12, 3.13 and 3.14. The doctor says when a Python is
  newer than 3.14 and untested.
- Hardened servers. Everything an agent starts runs with no new privileges,
  so the system unit no longer needs NoNewPrivileges and the root helpers
  work with sudo-rs and classic sudo. The doctor names a server where a
  sandbox cannot run inside another. Host-side git also starts with no new
  privileges, so a planted git filter cannot use the harness's sudo.
- The setup wizard offers the model servers that answer on this machine
  when a preset's port is silent.
- The test suite never reaches the outside network and never probes this
  machine's own listeners.

### New designs (documents only, nothing built)

- `docs/seraphina/BACKUPS_SNAPRAID.md`: disk protection through the
  SnapRAID Daemon, with the reader as a read-only fallback.
- `docs/seraphina/YOUR_SERVER.md`: an Apps launcher without Cosmos, a system
  helper, and root access.
- `docs/seraphina/BROWSER_AND_CODEX.md`: a browser adapter and a Codex
  worker, with the operator's decisions of 22 October.
- `docs/model-factory/LINEAGE.md`: the persona lineage line, for the
  persona's own decision.

### The release itself

- `VERSION` is 0.2.0.
- `scripts/package.py` builds the release zip from the files git tracks. It
  refuses to build if any file holds a term from the owner's private list, a
  key or token, or a home folder path that is not a placeholder. It never
  prints what it found, only the file and the line. The zip holds
  `MANIFEST.json` and `SHA256SUMS`.

## What is verified

Checked here, on 26 October 2026, on this release's code:

- **Unit suite:** 3010 tests, all pass, 49 skipped, on each of Python
  3.10.20, 3.11.17, 3.13.16 and 3.14.6 (about 10 minutes each), after the
  look-around fix. The skipped tests need a real server, a systemd-booted
  host or a lab.
- **GUI static tests** (`tests/test_gui_static.py`, part of the suite):
  pass.
- **GUI smoke** (`tests/gui/smoke.mjs`, headless Chromium against a mock
  backend, every area): 173 of 173 passed on 26 October before the
  look-around fix; after it, full runs gave 170 and 171 of 173, with the
  failing checks passing when their areas run alone (see Known issues).
- **Real-backend GUI test** (`FACTORY_E2E=1 node tests/gui/e2e_real.mjs`: the
  GUI against a real `factory serve`, a scripted model, a mock Open WebUI and
  a mock ntfy, one flow per area): 8 of 8 flows passed.
- **The release zip:** built with `scripts/package.py`, unzipped into a fresh
  folder and scanned again: no findings, and every checksum matches.

## What is not verified on a real server

Every test runs against mocks: mock model servers, a mock Open WebUI, a mock
ntfy, a mock custody register, a mock SnapRAID Daemon. None of these was
checked on the owner's server or any real server:

- the real providers: TabbyAPI, Strata, llama.cpp, NanoGPT, DeepSeek and Open
  WebUI;
- the GPU custody register (its contract is not confirmed yet);
- the SnapRAID reader and the daemon adapter (labs L14 to L17 and L19 of the
  Backups design are not run; the card and the doctor list them as not
  checked);
- the sandbox (bubblewrap) on the owner's server, and anything on Ubuntu
  26.04;
- messages to a real phone over ntfy or Telegram.

## Fixed

- **The look-around for model servers now runs only when you ask.** It
  asks each program listening on this machine whether it is a model server:
  one `GET /v1/models`, with no key, to at most 40 local ports, 1.5 s each.
  Before this fix, the doctor did it whenever a configured local server was
  down, and the GUI's setup status did it each time it was read (at most once
  a minute), so while the GUI was open every local service, a model server
  under a quality battery too, got an uninvited request about once a minute.
  Now:
  - the setup page's **Server details** step has a **Look for model
    servers** button, with one sentence on what it does. It shows what
    answered, what was sent and when, or says what to do when nothing
    answered. A look that broke shows only its error and what to do next,
    also after a reload. A press too soon says so until you can look again.
    Nothing looks when a page loads or on a timer;
  - `factory doctor --look-around` looks once from the command line;
  - the look skips the harness's own port, also when `factory serve --port`
    moved it: before, the harness could send the request to itself;
  - `GET /api/setup` and the doctor (`factory doctor`, and `GET /api/doctor`
    behind the GUI's Doctor badge) never look. The setup status shows the
    last look you asked for, with when it was taken. When a local server is
    down, the doctor says how to look, and the GUI's Doctor links to the
    button;
  - the look (`POST /api/setup/look-around`) needs the master token (an
    outside agent's token is refused), runs one at a time, and runs at most
    once in 30 seconds and 10 times an hour, so a double click or a stuck
    script cannot turn it into a scan loop.

## Known issues

- **A refresh race on the Operator page.** Once, under heavy load, the
  Operator page did not show the new state after **Scan now** until it was
  refreshed again. It was not seen again in two reruns. The cause is not
  found yet.
- **Intermittent GUI smoke failures when every area runs in one session.**
  The full smoke run (`node tests/gui/smoke.mjs`) sometimes fails 1 to 3 of
  its 173 checks, in the Tracker, Operator, Messages or Personas areas: a
  keyboard focus check, a live-refresh check, the Operator step "Choose the
  operator" (a persona list read lands earlier than the test expects), and a
  Messages step that once timed out. Each of these checks passes when its
  area runs alone (`GUI_ONLY=tracker,personas`: 46 of 46; `GUI_ONLY=operator`:
  19 of 19), and the same failures appear on the commit before the
  look-around fix, so they depend on the order of the full run, not on a
  change in this release. A full run passed 173 of 173 earlier on 26
  October. The cause is not found yet.
- **The SnapRAID daemon adapter is tested only against recorded fixtures and
  a mock.** The fixtures are 160 recorded exchanges with SnapRAID Daemon 2.1
  and SnapRAID 15.1, with placeholder paths. It has not met a real daemon.
- **Held back from this release:** the server-factory catalog
  (`docs/server-factory/`) and one research note (`real-server-lessons.md`).
  They hold owner-specific details. The designs still name them, so those
  links do not resolve in the zip.
