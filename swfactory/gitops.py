"""Git helpers: base detection, worktrees, diffs, conflict checks and the merge.

Every command runs as ``git -C <path> ...`` through subprocess with an argument
list (never a shell), ``LC_ALL=C`` so messages are parseable, and
``GIT_TERMINAL_PROMPT=0`` so nothing ever waits for a password. Nothing here
force-updates a ref, and the only push is push(): one branch, never forced
(merge.push), whose answer per ref is git's porcelain line (push_line,
push_refused; push_never_left for a push that never reached the remote), and
remote_head() looks at the remote again, read only (DESIGN.md section 40).

Host-side git must never run code a repository or an agent controls: every
command (the final merge commit included) runs with ``-c core.hooksPath=/dev/null
-c core.fsmonitor=false`` (HARDENING), which beats any config file and is passed
on to the git processes git itself starts. Filter and merge drivers from the
repository's config still run, so every command also starts with no new
privileges (``setpriv --no-new-privs``, sandbox.without_new_privileges): the
harness keeps sudo for its root helpers (DESIGN §17), and a driver an agent
slipped into ``.git/config`` must never reach it. Git itself never needs sudo.
Error messages show the command without those flags. Status and stash on a worktree an agent could write
(is_clean, stash_all, and the checkout a merge lands in) first check with
verify_worktree that its ``.git`` file and git dir still lead back to the repo.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
from collections.abc import Sequence
from contextlib import suppress
from functools import lru_cache
from pathlib import Path
from typing import IO

from .harness.sandbox import without_new_privileges

DEFAULT_TIMEOUT = 120
SLOW_TIMEOUT = 600  # checkouts and merges of big repos
MERGE_TREE_MIN_VERSION = (2, 38, 0)
PATCH_MAX_BYTES = 256 << 20   # format_patch refuses a larger mbox

# Inherited repository overrides would beat `-C` (e.g. when launched from a git hook).
_SCRUBBED_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                 "GIT_OBJECT_DIRECTORY", "GIT_PREFIX")
_BUSY_MARKERS = ("already checked out", "already used by worktree")
_IN_PROGRESS_REFS = {"MERGE_HEAD": "merge", "CHERRY_PICK_HEAD": "cherry-pick", "REVERT_HEAD": "revert"}
_DIFF_FLAGS = ("--no-color", "--no-ext-diff", "--no-textconv")
_MESSAGE_LIMIT = 2000
_READ_CHUNK = 1 << 16
_LS_TREE_BATCH = 200

# Repository-controlled code git would otherwise run: hooks (any of .git/hooks, or a core.hooksPath
# set in a config file) and the core.fsmonitor command. "false" disables fsmonitor on git >= 2.36;
# older git (2.34) treats the value as a command and runs `false`, which only fails the query.
HARDENING = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")
_POINTER_BYTES = 4096   # a .git file, commondir or gitdir is one short line

_merge_locks: dict[str, threading.Lock] = {}
_merge_locks_guard = threading.Lock()


class GitError(Exception):
    """A git command failed, or a git precondition did not hold.

    ``cmd`` is the argv, ``stderr`` git's error output (its stdout when stderr was
    empty, as for merge conflicts) and ``returncode`` the exit status (None when the
    command never ran to completion or no command failed). ``timed_out``: git was
    killed at its timeout, so what it did is unknown (a push may have landed).
    """

    timed_out = False

    def __init__(self, cmd: Sequence[str], stderr: str, returncode: int | None = None):
        self.cmd = [str(part) for part in cmd]
        self.stderr = stderr
        self.returncode = returncode
        detail = stderr.strip() or "no output"
        if len(detail) > _MESSAGE_LIMIT:
            detail = detail[:_MESSAGE_LIMIT] + " …"
        status = f" failed (exit {returncode})" if returncode is not None else ""
        super().__init__(f"{' '.join(self.cmd)}{status}: {detail}")


class MergeBusy(GitError):
    """The base branch is checked out somewhere that cannot take a merge right now."""


def git(repo: Path, *args: object, check: bool = True, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Run ``git -C repo *args`` and return stdout; raise GitError on failure when check."""
    proc = _run(repo, args, timeout)
    if check and proc.returncode != 0:
        raise _error(proc)
    return proc.stdout


@lru_cache(maxsize=1)
def git_version() -> tuple[int, int, int]:
    """The installed git's version, e.g. (2, 43, 0)."""
    proc = _run(None, ["--version"], DEFAULT_TIMEOUT)
    if proc.returncode != 0:
        raise _error(proc)
    return _parse_version(proc.stdout)


def detect_base(repo: Path, configured: str | None) -> str:
    """The branch jobs start from and merge into.

    The configured branch if given (it must exist locally); else origin/HEAD's
    branch if it exists locally; else ``main``; else ``master``; else the branch
    currently checked out in repo.
    """
    if configured:
        if _branch_exists(repo, configured):
            return configured
        raise GitError(_cmd(repo, ["show-ref", "--verify", f"refs/heads/{configured}"]),
                       f"configured base branch {configured!r} does not exist")
    candidates = []
    origin_head = _symbolic_ref(repo, "refs/remotes/origin/HEAD")
    if origin_head and origin_head.startswith("refs/remotes/origin/"):
        candidates.append(origin_head[len("refs/remotes/origin/"):])
    candidates += ["main", "master"]
    current = _symbolic_ref(repo, "HEAD")
    if current and current.startswith("refs/heads/"):
        candidates.append(current[len("refs/heads/"):])
    for name in candidates:
        if _branch_exists(repo, name):
            return name
    raise GitError(_cmd(repo, ["show-ref", "--heads"]),
                   "cannot detect the base branch: no origin/HEAD, main, master or checked-out branch")


def rev_parse(repo: Path, ref: str) -> str:
    """The full sha of the commit ref points at."""
    return git(repo, "rev-parse", "--verify", f"{_ref(ref)}^{{commit}}").strip()


def add_worktree(repo: Path, worktree: Path, branch: str, base: str) -> None:
    """Create branch at base and check it out in a new worktree (parents are created)."""
    path = Path(worktree).absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-b", _ref(branch), "--", path, _ref(base), timeout=SLOW_TIMEOUT)


def checkout_worktree(repo: Path, worktree: Path, branch: str) -> None:
    """Check an existing branch out in a new worktree (parents are created), e.g. to rework a job
    whose worktree was removed when it was delivered."""
    path = Path(worktree).absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "--", path, _ref(branch), timeout=SLOW_TIMEOUT)


def remove_worktree(repo: Path, worktree: Path) -> None:
    """Remove a worktree (discarding its changes) and prune stale worktree records.

    Missing or already-unregistered worktrees are fine. A directory left behind is
    deleted only if it is empty or still a checkout of one of repo's worktrees (its
    ``.git`` file points into repo's git dir), never another repository's checkout.
    """
    path = Path(worktree).absolute()
    proc = _run(repo, ["worktree", "remove", "--force", path], SLOW_TIMEOUT)
    if proc.returncode != 0 and "is not a working tree" not in proc.stderr and path.exists():
        raise _error(proc)
    git(repo, "worktree", "prune")
    if not path.exists():
        return
    if _is_worktree_of(repo, path):
        shutil.rmtree(path)
    elif path.is_dir() and not any(path.iterdir()):
        path.rmdir()
    else:
        raise GitError(proc.args, f"{path} is not a worktree of {repo}; refusing to delete it")


def branch_exists(repo: Path, branch: str) -> bool:
    """Whether the local branch refs/heads/<branch> exists."""
    return _branch_exists(repo, _ref(branch))


def delete_branch(repo: Path, branch: str, force: bool = False) -> None:
    """Delete a local branch (``-D`` when force). A branch that does not exist is a no-op."""
    if _branch_exists(repo, branch):
        git(repo, "branch", "-D" if force else "-d", "--", branch)


def commits_ahead(repo: Path, base: str, branch: str) -> int:
    """Number of commits on branch that are not on base."""
    return int(git(repo, "rev-list", "--count", f"{_ref(base)}..{_ref(branch)}", "--").strip())


def is_clean(worktree: Path, repo: Path | None = None) -> bool:
    """True when the worktree has no staged, modified or untracked files.

    With repo, verify_worktree(repo, worktree) runs first (GitError if it fails).
    """
    if repo is not None:
        verify_worktree(repo, worktree)
    return _status(worktree) == ""


def verify_worktree(repo: Path, worktree: Path) -> None:
    """Raise GitError unless worktree is repo's main checkout or a linked worktree of repo.

    For a linked worktree, ``<worktree>/.git`` must be a regular file whose ``gitdir:``
    resolves to ``<repo common dir>/worktrees/<name>``, that git dir's ``commondir`` must
    resolve to the same common dir, and its ``gitdir`` file must point back at
    ``<worktree>/.git``. An agent that may write the worktree (or, under bwrap, its git
    dir) could otherwise point host-side git at a repository and config of its own.
    """
    path = Path(worktree).absolute()
    common = _common_dir(repo)
    cmd = ["git", "-C", str(path)]
    if path.resolve() == Path(repo).absolute().resolve():
        return   # the repo's own checkout: as trusted as the repo
    dotgit = path / ".git"
    gitdir = _pointer(dotgit, "gitdir:")
    if gitdir is None:
        raise GitError(cmd, f"refusing to run git in {path}: {dotgit} is not a gitdir pointer file")
    gitdir = (path / gitdir).resolve()   # an absolute gitdir replaces path
    if gitdir.parent != common / "worktrees":
        raise GitError(cmd, f"refusing to run git in {path}: its .git points to {gitdir}, not into "
                            f"{common / 'worktrees'}")
    shared = _pointer(gitdir / "commondir", "")
    if shared is None or (gitdir / shared).resolve() != common:
        raise GitError(cmd, f"refusing to run git in {path}: {gitdir / 'commondir'} does not lead to {common}")
    back = _pointer(gitdir / "gitdir", "")
    if back is None or (gitdir / back).resolve() != dotgit.resolve():
        raise GitError(cmd, f"refusing to run git in {path}: {gitdir / 'gitdir'} does not point back at {dotgit}")


def changed_files(repo: Path, base: str, branch: str, tmp_dir: Path | None = None) -> list[str]:
    """Sorted paths the merge gate must check (a rename lists both old and new path).

    These are the paths changed on branch since it left base, plus every path that
    merging branch would change on base. The merge can touch paths the branch diff
    does not show: a rename on base carries a branch edit to the new path, and a
    history with several merge bases can revert base's own changes. tmp_dir is as
    for conflicts().
    """
    tree, conflicted = _merge_result(repo, base, branch, tmp_dir)
    names = set(_names(repo, _range(base, branch)))
    names.update(_names(repo, _ref(base), tree))
    names.update(conflicted)
    return sorted(names)


def changed_lines(repo: Path, base: str, branch: str, tmp_dir: Path | None = None) -> int:
    """Added plus deleted lines (binary files count 0).

    The larger of branch's diff since it left base and the change that merging it
    would make to base (see changed_files()).
    """
    tree, _ = _merge_result(repo, base, branch, tmp_dir)
    return max(_numstat_total(repo, _range(base, branch)), _numstat_total(repo, _ref(base), tree))


def diff(repo: Path, base: str, branch: str, max_bytes: int = 2_000_000) -> str:
    """Unified diff of branch against its merge base with base, at most max_bytes."""
    return diff_truncated(repo, base, branch, max_bytes)[0]


def diff_truncated(repo: Path, base: str, branch: str, max_bytes: int = 2_000_000) -> tuple[str, bool]:
    """Like diff(), plus whether it was cut (on a line boundary) to fit max_bytes.

    Git is stopped once its output passes max_bytes, so a huge diff costs no more memory.
    """
    limit = max(max_bytes, 0)
    data, more = _run_capped(repo, ["diff", *_DIFF_FLAGS, _range(base, branch), "--"], limit,
                             DEFAULT_TIMEOUT)
    if not more:
        return data.decode("utf-8", "replace"), False
    cut = data[:limit]
    newline = cut.rfind(b"\n")
    if newline >= 0:
        cut = cut[:newline + 1]
    return cut.decode("utf-8", "replace"), True


def diffstat(repo: Path, base: str, branch: str) -> str:
    """``git diff --stat`` of branch against its merge base with base."""
    return git(repo, "diff", *_DIFF_FLAGS, "--stat", _range(base, branch), "--")


def branch_files(repo: Path, base: str, branch: str) -> list[str]:
    """Sorted paths branch changed since it left base (``base...branch``; a rename gives both paths)."""
    return sorted(set(_names(repo, _range(base, branch))))


def format_patch(repo: Path, base: str, branch: str, max_bytes: int = PATCH_MAX_BYTES) -> bytes:
    """``git format-patch --stdout base..branch``: every commit of branch not on base as an mbox that
    ``git am`` applies (binary changes included). GitError when it is larger than max_bytes."""
    data, more = _run_capped(repo, ["format-patch", "--stdout", "--binary", "--no-ext-diff", "--no-textconv",
                                    "--no-color", f"{_ref(base)}..{_ref(branch)}", "--"], max_bytes, SLOW_TIMEOUT)
    if more:
        raise GitError(_cmd(repo, ["format-patch", "--stdout", f"{base}..{branch}"]),
                       f"the patch is larger than {max_bytes} bytes")
    return data


def tree_entries(repo: Path, rev: str, paths: list[str]) -> list[tuple[str, str, str, str]]:
    """(mode, type, object id, path) of each of paths that exists in rev's tree (``git ls-tree -z``).

    Paths go to git in batches as literal pathspecs, so a name with glob characters
    matches only itself.
    """
    found: list[tuple[str, str, str, str]] = []
    for start in range(0, len(paths), _LS_TREE_BATCH):
        batch = paths[start:start + _LS_TREE_BATCH]
        out = git(repo, "--literal-pathspecs", "ls-tree", "-z", "--full-tree", f"{_ref(rev)}^{{tree}}", "--",
                  *batch)
        for record in out.split("\0"):
            meta, tab, path = record.partition("\t")
            fields = meta.split()
            if tab and len(fields) == 3:
                found.append((fields[0], fields[1], fields[2], path))
    return found


def blob(repo: Path, oid: str) -> bytes:
    """The raw content of a blob (``git cat-file blob``: no filters, no textconv)."""
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", oid):
        raise GitError(["git", "cat-file", "blob", oid], f"not an object id: {oid!r}")
    proc = _run(repo, ["cat-file", "blob", oid], SLOW_TIMEOUT, text=False)
    if proc.returncode != 0:
        raise _error(proc)
    return proc.stdout


def conflicts(repo: Path, base: str, branch: str, tmp_dir: Path | None = None) -> list[str]:
    """Sorted paths that would conflict if branch were merged into base; [] when clean.

    Uses ``git merge-tree --write-tree`` (git >= 2.38), else a trial merge in a
    temporary detached worktree under tmp_dir (default: the system temp dir).
    """
    return _merge_result(repo, base, branch, tmp_dir)[1]


def checked_out_at(repo: Path, branch: str) -> Path | None:
    """The worktree (main checkout included) that has branch checked out, if any."""
    target = f"branch refs/heads/{branch}"
    current = None
    for line in git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            current = line[len("worktree "):]
        elif line == target and current is not None:
            return Path(current)
    return None


def merge(repo: Path, base: str, branch: str, message: str, tmp_dir: Path) -> str:
    """Merge branch into base with a merge commit and return base's new head sha.

    If base is checked out somewhere, the merge happens there, and that checkout
    must be on base with no tracked changes and no operation in progress, else
    MergeBusy. Otherwise base is checked out in a temporary worktree under tmp_dir
    for the merge. A failed merge is aborted and raised as GitError.
    """
    base, branch = _ref(base), _ref(branch)
    with _merge_lock(repo):
        git(repo, "worktree", "prune")  # a deleted checkout must not keep base "checked out"
        checkout = checked_out_at(repo, base)
        if checkout is not None:
            verify_worktree(repo, checkout)
            _ensure_mergeable(checkout, base, branch, message)
            _merge_in(checkout, branch, message)
        else:
            _merge_in_temp_worktree(repo, base, branch, message, Path(tmp_dir))
        return rev_parse(repo, f"refs/heads/{base}")


def push(repo: Path, remote: str, branch: str, timeout: float = SLOW_TIMEOUT) -> subprocess.CompletedProcess:
    """git push --porcelain <remote> refs/heads/<branch>:refs/heads/<branch>: one branch, never forced. Returns the
    finished process whatever its exit status: its stdout holds the remote's answer for the ref (push_line). GitError
    when git could not run (nothing left this machine) or when it was killed at its timeout (timed_out: the remote may
    have the commit, so it is looked at again, never pushed again by itself)."""
    if not remote or remote.startswith("-"):
        raise GitError(["git", "push"], f"refusing remote name {remote!r}")
    ref = f"refs/heads/{_ref(branch)}"
    return _run(repo, ["push", "--porcelain", "--", remote, f"{ref}:{ref}"], timeout)


def push_line(output: str, ref: str) -> tuple[str, str] | None:
    """(flag, summary) of ref's line in git push --porcelain output (``<flag>\\t<from>:<to>\\t<summary>``), or None
    when there is none. flag: ' ' fast-forward, '+' forced, '-' deleted, '*' new, '=' up to date, '!' not done or not
    confirmed: a refusal ("[rejected]", "[remote rejected]", push_refused) or "[remote failure] (remote failed to
    report status)", where the pack was sent and the ref may have moved."""
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and len(parts[0]) == 1 and parts[1].rpartition(":")[2] == ref:
            return parts[0], parts[2].strip()
    return None


PUSH_DONE_FLAGS = " +-*="   # push_line flags for a ref the remote updated, or already had


def push_refused(line: tuple[str, str] | None) -> bool:
    """Whether ref's porcelain line (push_line) is a refusal: flag '!' and "[rejected]" (refused here, before the
    pack) or "[remote rejected]" (the remote refused the update). Any other '!' is not a refusal: "[remote failure]"
    means the remote never reported, so the outcome is unknown (DESIGN.md section 40; from git's transport.c,
    checked with git 2.43)."""
    return line is not None and line[0] == "!" and line[1].startswith(("[rejected]", "[remote rejected]"))


# git's own words for a push that stopped before it reached a remote that could take it: the remote name or
# path is no repository, the host cannot be resolved or connected to, or the remote refused the login before it
# listed its refs (from git's connect.c and remote-curl.c, and ssh; with LC_ALL=C). Checked with git 2.43 for a
# missing remote, a missing path, an unresolvable ssh host and a refused HTTP connection.
_NEVER_REACHED = ("does not appear to be a git repository", "Could not resolve host", "ssh: connect to host",
                  "Host key verification failed", "Permission denied (publickey", "unable to access '",
                  "Authentication failed for", "could not read Username", "could not read Password")


def push_never_left(proc: subprocess.CompletedProcess, ref: str) -> bool:
    """Whether a finished git push provably sent nothing: it failed, printed no line for ref, and its error is one
    of _NEVER_REACHED. Anything else that failed with no line (a hang-up after the pack, a signal) may have
    landed."""
    return proc.returncode != 0 and push_line(proc.stdout, ref) is None and \
        any(marker in (proc.stderr or "") for marker in _NEVER_REACHED)


def complaint(stderr: str) -> str:
    """git's first "fatal:" or "error:" line (its own reason; later lines are hints), else its first non-empty
    line, else ""."""
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    return next((line for line in lines if line.startswith(("fatal:", "error:"))), lines[0] if lines else "")


def remote_head(repo: Path, remote: str, branch: str, timeout: float = DEFAULT_TIMEOUT) -> str | None:
    """The sha the remote's branch points at now (git ls-remote: read only), or None when the remote has no such
    branch. GitError when the remote cannot be asked."""
    if not remote or remote.startswith("-"):
        raise GitError(["git", "ls-remote"], f"refusing remote name {remote!r}")
    ref = f"refs/heads/{_ref(branch)}"
    proc = _run(repo, ["ls-remote", "--exit-code", "--", remote, ref], timeout)
    if proc.returncode == 2:
        return None
    if proc.returncode != 0:
        raise _error(proc)
    for line in proc.stdout.splitlines():
        sha, _, name = line.partition("\t")
        if name.strip() == ref:
            return sha.strip()
    return None


def is_ancestor(repo: Path, commit: str, ref: str) -> bool:
    """Whether commit is ref or one of its ancestors (git merge-base --is-ancestor; read only)."""
    proc = _run(repo, ["merge-base", "--is-ancestor", _ref(commit), _ref(ref)], DEFAULT_TIMEOUT)
    if proc.returncode not in (0, 1):
        raise _error(proc)
    return proc.returncode == 0


def stash_all(worktree: Path, message: str, repo: Path | None = None) -> bool:
    """Stash every change, untracked files included. True if anything was stashed.

    Raises GitError when changes remain that git cannot stash (an untracked nested
    repository, edits inside a submodule), so a dirty worktree is never reported clean.
    With repo, verify_worktree(repo, worktree) runs first.
    """
    if is_clean(worktree, repo):
        return False
    args = ["stash", "push", "--include-untracked", "-m", message]
    git(worktree, *args, timeout=SLOW_TIMEOUT)
    left = _status(worktree)
    if left:
        raise GitError(_cmd(worktree, args), f"changes git could not stash remain:\n{left}")
    return True


def matches_any(path: str, globs: list[str]) -> str | None:
    """The first glob that matches path (gitignore-like), or None.

    A glob without '/' matches a file or directory name at any depth; one with '/'
    (a leading '/' or './' included) is anchored at the repo root, the leading
    part itself is dropped. ``*`` and ``?`` never cross '/', ``**`` spans
    zero or more directories. A glob that matches a path matches everything
    below it, and unlike gitignore, ``dir/**`` and ``dir/`` also match ``dir``
    itself: a file or symlink there stands in for the protected directory.
    """
    normalized = _strip_leading(path)
    for glob in globs:
        regex = _glob_regex(glob)
        if regex is not None and regex.fullmatch(normalized):
            return glob
    return None


# --- internals -----------------------------------------------------------------

def _cmd(repo: Path | None, args: Sequence[object]) -> list[str]:
    """The command as error messages show it (without HARDENING)."""
    prefix = ["git"] if repo is None else ["git", "-C", str(repo)]
    return prefix + [str(arg) for arg in args]


def _argv(cmd: list[str]) -> list[str]:
    """The command as it runs: HARDENING right after "git", the whole started with no new privileges."""
    return without_new_privileges([cmd[0], *HARDENING, *cmd[1:]])


def _git_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in _SCRUBBED_ENV}
    # GIT_OPTIONAL_LOCKS=0: our status calls never compete with an agent for index.lock.
    env.update(GIT_TERMINAL_PROMPT="0", LC_ALL="C", GIT_OPTIONAL_LOCKS="0")
    return env


def _run(repo: Path | None, args: Sequence[object], timeout: float,
         text: bool = True) -> subprocess.CompletedProcess:
    cmd = _cmd(repo, args)
    decoding = {"encoding": "utf-8", "errors": "replace"} if text else {}
    try:
        proc = subprocess.run(_argv(cmd), capture_output=True, stdin=subprocess.DEVNULL, check=False,
                              timeout=timeout, env=_git_env(), **decoding)
        proc.args = cmd   # errors show the command without HARDENING
        return proc
    except subprocess.TimeoutExpired as exc:
        error = GitError(cmd, f"timed out after {timeout}s")
        error.timed_out = True
        raise error from exc
    except OSError as exc:
        raise GitError(cmd, str(exc)) from exc


def _run_capped(repo: Path, args: Sequence[object], limit: int, timeout: float) -> tuple[bytes, bool]:
    """Run git and return its stdout, at most limit + 1 bytes, and whether it passed limit.

    Git is killed as soon as its output passes limit, so memory stays bounded.
    """
    cmd = _cmd(repo, args)
    with tempfile.TemporaryFile() as errors:
        try:
            proc = subprocess.Popen(_argv(cmd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=errors, env=_git_env())
        except OSError as exc:
            raise GitError(cmd, str(exc)) from exc
        expired = threading.Event()

        def expire() -> None:
            expired.set()
            proc.kill()

        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
        try:
            with proc.stdout:
                data = _read_at_most(proc.stdout, limit + 1)
            more = len(data) > limit
            if more:
                proc.kill()
            returncode = proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if more or returncode == 0:
            return data, more
        if expired.is_set():
            raise GitError(cmd, f"timed out after {timeout}s")
        errors.seek(0)
        raise _error(subprocess.CompletedProcess(cmd, returncode, data, errors.read()))


def _read_at_most(stream: IO[bytes], size: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total < size:
        chunk = stream.read(min(_READ_CHUNK, size - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _error(proc: subprocess.CompletedProcess) -> GitError:
    stderr, stdout = (_as_text(stream) for stream in (proc.stderr, proc.stdout))
    return GitError(proc.args, stderr if stderr.strip() else stdout, proc.returncode)


def _as_text(stream: str | bytes | None) -> str:
    if isinstance(stream, bytes):
        return stream.decode("utf-8", "replace")
    return stream or ""


def _quietly(repo: Path, *args: object) -> None:
    """Run a best-effort cleanup command, ignoring every failure."""
    with suppress(GitError):
        _run(repo, args, DEFAULT_TIMEOUT)


def _parse_version(text: str) -> tuple[int, int, int]:
    found = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if not found:
        raise GitError(["git", "--version"], f"cannot parse git version from {text!r}")
    major, minor, patch = found.groups()
    return int(major), int(minor), int(patch or 0)


def _ref(name: str) -> str:
    """Return name, rejecting ref names git could mistake for options."""
    if not name or name.startswith("-"):
        raise GitError(["git"], f"refusing ref name {name!r}")
    return name


def _range(base: str, branch: str) -> str:
    return f"{_ref(base)}...{_ref(branch)}"


def _branch_exists(repo: Path, name: str) -> bool:
    proc = _run(repo, ["show-ref", "--verify", "--quiet", f"refs/heads/{name}"], DEFAULT_TIMEOUT)
    if proc.returncode not in (0, 1):
        raise _error(proc)
    return proc.returncode == 0


def _symbolic_ref(repo: Path, name: str) -> str | None:
    proc = _run(repo, ["symbolic-ref", "--quiet", name], DEFAULT_TIMEOUT)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _status(worktree: Path) -> str:
    return git(worktree, "status", "--porcelain", "--untracked-files=normal").strip()


def _is_worktree_of(repo: Path, path: Path) -> bool:
    """True if path is a linked-worktree checkout of repo: its .git file points into
    repo's ``worktrees/`` (not into another repository or a submodule's git dir)."""
    gitdir = _pointer(path / ".git", "gitdir:")
    if gitdir is None:
        return False
    return (path / gitdir).resolve().parent == _common_dir(repo) / "worktrees"  # an absolute gitdir replaces path


def _common_dir(repo: Path) -> Path:
    """repo's git common dir (its .git, shared by all worktrees), resolved."""
    return (Path(repo) / git(repo, "rev-parse", "--git-common-dir").strip()).resolve()


def _pointer(path: Path, prefix: str) -> str | None:
    """The path a one-line git pointer file holds (after prefix), or None if path is not a small regular
    file of that form (a symlink, a directory, a FIFO, missing, unreadable or empty)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        with open(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None
            data = handle.read(_POINTER_BYTES + 1)
    except OSError:
        return None
    if len(data) > _POINTER_BYTES:
        return None
    try:
        text = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not text.startswith(prefix):
        return None
    value = text[len(prefix):].strip()
    return value if value and "\n" not in value else None


def _names(repo: Path, *revs: str) -> list[str]:
    """Paths that differ between revs (``base...branch``, or two tree-ishes), renames split."""
    out = git(repo, "diff", "--name-only", "--no-renames", "-z", *revs, "--")
    return [path for path in out.split("\0") if path]


def _numstat_total(repo: Path, *revs: str) -> int:
    out = git(repo, "diff", *_DIFF_FLAGS, "--numstat", "-M", *revs, "--")
    total = 0
    for line in out.splitlines():
        fields = line.split("\t", 2)
        if len(fields) == 3:
            total += sum(int(n) for n in fields[:2] if n.isdigit())
    return total


def _nul_list(text: str) -> list[str]:
    """Names from a NUL-terminated list, up to the first empty entry."""
    names = []
    for name in text.split("\0"):
        if not name:
            break
        names.append(name)
    return names


def _merge_result(repo: Path, base: str, branch: str, tmp_dir: Path | None) -> tuple[str, list[str]]:
    """The tree that merging branch into base would produce, and its sorted conflicted paths.

    Conflicted files are in the tree with their conflict markers, as merge-tree
    writes them. git >= 2.38 uses merge-tree, older git a trial merge (see conflicts()).
    """
    base, branch = _ref(base), _ref(branch)
    if git_version() >= MERGE_TREE_MIN_VERSION:
        return _merge_tree(repo, base, branch)
    return _trial_merge(repo, base, branch, tmp_dir)


def _merge_tree(repo: Path, base: str, branch: str) -> tuple[str, list[str]]:
    proc = _run(repo, ["merge-tree", "--write-tree", "--name-only", "--no-messages", "-z",
                       base, branch], SLOW_TIMEOUT)
    # Exit 0: the tree oid. Exit 1 (conflicts): the tree oid, then the conflicted paths.
    fields = _nul_list(proc.stdout) if proc.returncode in (0, 1) else []
    conflicted = sorted(set(fields[1:]))
    if not fields or (proc.returncode == 1 and not conflicted):
        raise _error(proc)
    return fields[0], conflicted


def _trial_merge(repo: Path, base: str, branch: str, tmp_dir: Path | None) -> tuple[str, list[str]]:
    root = Path(tmp_dir if tmp_dir is not None else tempfile.gettempdir()).absolute()
    root.mkdir(parents=True, exist_ok=True)
    trial = root / f"trial-merge-{secrets.token_hex(6)}"
    try:
        git(repo, "worktree", "add", "--detach", "--", trial, base, timeout=SLOW_TIMEOUT)
        proc = _run(trial, ["merge", "--no-commit", "--no-ff", branch], SLOW_TIMEOUT)
        conflicted: list[str] = []
        if proc.returncode != 0:
            conflicted = sorted(set(_nul_list(git(trial, "diff", "--name-only", "--diff-filter=U", "-z"))))
            if not conflicted:
                raise _error(proc)
            git(trial, "add", "-A", timeout=SLOW_TIMEOUT)  # conflicted files, markers included
        return git(trial, "write-tree").strip(), conflicted
    finally:
        _quietly(trial, "merge", "--abort")
        _discard_worktree(repo, trial)


def _discard_worktree(repo: Path, worktree: Path) -> None:
    with suppress(GitError, OSError):
        remove_worktree(repo, worktree)


def _merge_lock(repo: Path) -> threading.Lock:
    """One merge at a time per repository (two jobs approved at once must queue)."""
    key = os.path.realpath(repo)
    with _merge_locks_guard:
        return _merge_locks.setdefault(key, threading.Lock())


def _merge_args(branch: str, message: str) -> list[str]:
    return ["merge", "--no-ff", "--no-edit", "-m", message, branch]


def _ensure_mergeable(checkout: Path, base: str, branch: str, message: str) -> None:
    """Raise MergeBusy unless checkout is on base, has no tracked changes and nothing in progress."""
    cmd = _cmd(checkout, _merge_args(branch, message))
    if _symbolic_ref(checkout, "HEAD") != f"refs/heads/{base}":
        raise MergeBusy(cmd, f"{checkout} is not on {base}")
    if git(checkout, "status", "--porcelain", "--untracked-files=no").strip():
        raise MergeBusy(cmd, f"{base} is checked out at {checkout} with uncommitted changes")
    for ref, operation in _IN_PROGRESS_REFS.items():
        if _run(checkout, ["rev-parse", "--quiet", "--verify", ref], DEFAULT_TIMEOUT).returncode == 0:
            raise MergeBusy(cmd, f"{base} is checked out at {checkout} with a {operation} in progress")


def _merge_in(checkout: Path, branch: str, message: str) -> None:
    """Merge branch into checkout's branch; abort and re-raise on failure."""
    try:
        git(checkout, *_merge_args(branch, message), timeout=SLOW_TIMEOUT)
    except GitError:
        _quietly(checkout, "merge", "--abort")
        raise


def _merge_in_temp_worktree(repo: Path, base: str, branch: str, message: str, tmp_dir: Path) -> None:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    temp = tmp_dir.absolute() / f"merge-{secrets.token_hex(6)}"
    try:
        try:
            git(repo, "worktree", "add", "--", temp, base, timeout=SLOW_TIMEOUT)
        except GitError as exc:
            if any(marker in exc.stderr for marker in _BUSY_MARKERS):
                raise MergeBusy(exc.cmd, exc.stderr, exc.returncode) from exc
            raise
        _merge_in(temp, branch, message)
    finally:
        _discard_worktree(repo, temp)


def _strip_leading(text: str) -> str:
    """Drop leading './' and '/' so paths and globs are repo-relative."""
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


@lru_cache(maxsize=512)
def _glob_regex(glob: str) -> re.Pattern[str] | None:
    """Compile a gitignore-like glob to a regex over repo-relative paths (None if blank)."""
    text = glob.strip()
    pattern = _strip_leading(text)
    rooted = pattern != text  # a leading '/' or './' anchors, like any other '/'
    pattern = pattern.rstrip("/")
    if not pattern:
        return None
    parts = pattern.split("/")
    if len(parts) == 1 and not rooted:
        parts.insert(0, "**")
    # The final suffix already covers everything below a match, so dropping a trailing
    # '**' (or '/') loses nothing and lets "dir/**" match "dir" itself.
    while len(parts) > 1 and parts[-1] == "**":
        parts.pop()
    out = []
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if part == "**":
            out.append(".*" if last else "(?:.*/)?")
        else:
            out.append(_segment_regex(part) + ("" if last else "/"))
    # A match on a directory covers everything inside it.
    return re.compile("".join(out) + "(?:/.*)?", re.DOTALL)


def _segment_regex(segment: str) -> str:
    """Translate one path segment of a glob (no '/') to a regex."""
    out = []
    i, n = 0, len(segment)
    while i < n:
        char = segment[i]
        if char == "*":
            while i + 1 < n and segment[i + 1] == "*":
                i += 1
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "\\" and i + 1 < n:
            i += 1
            out.append(re.escape(segment[i]))
        elif char == "[":
            end = _bracket_end(segment, i)
            if end < 0:
                out.append(re.escape(char))
            else:
                out.append(_bracket_regex(segment[i + 1:end]))
                i = end
        else:
            out.append(re.escape(char))
        i += 1
    return "".join(out)


def _bracket_end(segment: str, start: int) -> int:
    """Index of the ']' closing the bracket expression at start, or -1."""
    i = start + 1
    if i < len(segment) and segment[i] in "!^":
        i += 1
    if i < len(segment) and segment[i] == "]":
        i += 1
    return segment.find("]", i)


def _bracket_regex(body: str) -> str:
    negate = body[:1] in ("!", "^")
    if negate:
        body = body[1:]
    escaped = "".join(char if char == "-" else re.escape(char) for char in body)
    return f"(?!/)[{'^' if negate else ''}{escaped}]"
