"""The Codex worker, phase BC1b (docs/seraphina/BROWSER_AND_CODEX.md §3; DESIGN.md §44): a factory job whose builder
runs on Codex, hardened. Codex edits, the harness commits; the harness re-runs the repository's tests itself; every
Codex run gets a code-written receipt outside every directory Codex can write.

Modules: ``settings`` (the ``codex`` section and the repository settings verify, preview and personal_data),
``launch`` (``bin/sf-codex-launch``, self-contained so root can install it: argument checks, the per-job ``CODEX_HOME``
with its config.toml template, permissions profile and check before every spawn, the environment, exec of the
pinned binary, and ``verify`` under bwrap as the Codex user),
``work`` (what the orchestrator does around a Codex run: the routing check, the commit, ``verify`` under bwrap,
reconcile, receipts and the model check record), ``checks`` (the doctor's codex lines) and ``setup`` (the blocks the
owner pastes: the Codex user, the worktree group and its ACLs, the sudoers rule and the nftables rule).

Codex 0.163.0 is the pin (tag ``rust-v0.163.0``, commit 83086188ae0614bd882818ff1aa23f3194962012). Off by default
(``codex.enabled`` false). Nothing here changes the host: users, groups, sudoers, nftables and files under /etc are
the owner's own changes, which the doctor only checks and ``factory codex setup`` only prints.
"""

from __future__ import annotations

CODEX_PIN = "0.163.0"
CODEX_COMMIT = "83086188ae0614bd882818ff1aa23f3194962012"
PRESET = "codex-worker"
# K9: exec answers no approval (approval policy never); out-of-sandbox actions fail. The launcher is self-contained
# (it is installed by root on the server), so the names it writes live there.
from .launch import APPROVAL_POLICY, PROFILE, PROVIDER_ID, TEMPLATE_PIN, TEMPLATE_SHA256  # noqa: E402,F401
SURFACE = "exec"
# The typed outcomes of a run (§3.6, DESIGN §40): completed and declined end a turn; cancelled and timed_out say how
# the harness ended it; unknown means it may have acted and nothing says how far it got.
OUTCOMES = ("completed", "failed", "declined", "cancelled", "timed_out", "unknown")
UNCLEAN = frozenset({"failed", "declined", "cancelled", "timed_out", "unknown"})
# Paths a harness commit refuses unless the job's spec names them (§3.3): attributes and submodules change what git
# runs, .codex and .agents what any later Codex loads, workflows what CI runs.
PROTECTED_PATHS = (".gitattributes", ".gitmodules", ".codex/**", ".agents/**", ".github/workflows/**")
NO_NETWORK_NOTE = ("No network in this run: do not start servers or browsers, and do not try agent-browser or curl; "
                   "the harness runs the repository's verify command and the browser check itself. You cannot run "
                   "git commit here (.git is read-only): leave your changes in the worktree, and the harness commits "
                   "them after your run. Write build.md in {out_dir} (the job folder is read-only to you); the "
                   "harness copies it into the job folder.")
