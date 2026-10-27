# Work in progress: the road to 1.0.0

**Snapshot of 27 October 2026, saved by the owner's request while the build is paused. This branch is not a release.** The released version is 0.2.0 on the `v1` branch. Use that one to install.

## What this snapshot holds

Everything built since 0.2.0. Each feature is off by default:

- **Your server (DESIGN §42).**
  - Phase P1a: the Apps launcher, the host strip and the read-only helper `sf-system`.
  - Phase P1b: the Check buttons.
  - Phase P1c: who can act as root, view only.
  - It only looks. There are no buttons that change the server, by the owner's decision D24.
- **SnapRAID P4 (DESIGN §41):** the checklist for replacing a failed data disk.
- **The browser adapter, BC1a (DESIGN §43):** sandboxed, no network, the harness's own test pages and a job's preview only.
- **The Codex worker, BC1b (DESIGN §44):** a factory job's builder on Codex 0.163.0, local models only. It runs as its own user, through a launcher, under a firewall rule.
- **The operator persona's round-4 answers:**
  - the read ledger's limits as settings, and an erase that really deletes;
  - shared receipts that keep their actor;
  - GPU custody epochs on renew and release, and a held card that sends the run to a free one;
  - the lineage decisions L1 to L19, in the design document.

## How far the checking got

1. **Final review of everything since 0.2.0:** 13 reviewers and an independent verifier for each finding.
   - 90 findings were confirmed: 1 critical, 20 high, 50 medium and 19 low.
   - 6 more were disproved.
2. **Rounds 1 to 3 of fixes:** seven groups, each fix checked by an independent checker, with up to two rounds.
   - 83 findings fixed and confirmed, plus follow-ups.
   - Merged; the full suite passed on the merged fixes before round 3: 3,511 tests, 89 skipped.
3. **A cross-review of the merged fixes:** 24 more problems confirmed, 1 high, 11 medium and 12 low, mostly in the Codex worker.
4. **Round 4 (fixing those 24) was paused part-way. It is in this snapshot but NOT checked:**
   - the browser's fixes are complete but unchecked;
   - the Codex and Your server fixes are partial, committed as "WIP round 4";
   - the SnapRAID leftover is not started.

**The tests were not run on this exact snapshot.** Expect some failures until round 4 is finished.

## What is still needed for 1.0.0

1. **Finish round 4 and check it.** The remaining items:
   - **Codex:** a traverse entry on a home folder that is too broad; the note telling Codex about ignored files; the first and last setup blocks as printed.
   - **Your server:** the GUI and server answers on "No, it's another one"; the keep hold after a restart; disk health when only some disks were read; Beszel's Check; the record wording; a keep loop for images under the owner's own tag prefixes; a mount covered by the service's own sandbox.
   - **SnapRAID:** a way on when the rebuild units belong to an earlier checklist.
2. **Run every suite:** the full unit suite on Python 3.10, 3.11, 3.13 and 3.14, and the GUI smoke for every area.
3. **A last review of round 4's fixes**, verified the same way.
4. **The documentation findings:**
   - DESIGN §2, §3, §14 and §16 must list the new modules, folders, routes and commands;
   - README's status box, the browser section and START_HERE must match what is built;
   - BROWSER_AND_CODEX's status paragraph;
   - DESIGN §42 must not claim the installer creates the `seraphina` user;
   - the browser's "Turning it on" steps need a `models.kate-browse` entry.
5. **The release:** set the version to 1.0.0, write the release notes, build the zip with `scripts/package.py` (the privacy guard), and scan the unzipped copy.
6. **The owner's install kit.** It is kept privately with the owner, not in this repository.

## Not in 1.0.0, by design

- The Maintain, Fix and root-change buttons (they wait for a tested restore);
- real websites, logins and downloads for the browser;
- Codex driving the browser;
- the app-server driver;
- the lineage line itself, which is designed but not built.
