"""Delivery modes: what happens to a job once the approver has approved it (DESIGN.md section 25).

``merge`` (the default) runs the merge gate and merges the branch into base. ``branch``
keeps the branch (its worktree removed) for the human to merge or review elsewhere.
``folder`` also writes the work to ``<output_dir>/<repo>/<job-id>/``:

    changes.patch   git format-patch --stdout <base>..<branch> (git am applies it)
    files/          every file the branch changed, as it is at the branch tip
                    (symlinks and submodules are skipped, deleted files are absent)
    REPORT.md       feature, spec and build summaries, every round's verdicts, the decision

Both stop at stage ``delivered`` with the branch kept; a human approve merges it later.
Everything here reads git through gitops (hardened, no shell) and the job folder; it
never runs repository-controlled code.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

from . import gitops
from .agentdefs import read_small_text
from .config import DELIVERY_MODES

__all__ = ["DELIVERY_MODES", "check_delivery", "export_folder", "report_markdown"]

SKIPPED_MODES = {"120000": "symlink", "160000": "submodule"}
REGULAR_MODES = {"100644": 0o644, "100755": 0o755, "100664": 0o644}
SECTION_CHARS = 4000       # each summary in REPORT.md is cut to this many characters
FEATURE_CHARS = 20000
READ_BYTES = 1 << 20


def check_delivery(delivery: Any, default: str = "merge") -> str:
    """The delivery mode to use: delivery, or default when it is None or ""; ValueError for anything else."""
    if delivery is None or delivery == "":
        delivery = default
    if not isinstance(delivery, str) or delivery not in DELIVERY_MODES:
        raise ValueError(f"delivery must be one of {', '.join(DELIVERY_MODES)} (got {delivery!r})")
    return delivery


def folder_for(output_dir: Path, job: dict) -> Path:
    """<output_dir>/<repo>/<job-id>: the job's names are validated job-store names (no '/' or '..')."""
    return Path(output_dir) / str(job["repo"]) / str(job["id"])


def export_folder(job: dict, repo_path: Path, output_dir: Path, tip: str) -> dict:
    """Write the job's folder delivery and return {path, files, skipped, patch_bytes}.

    The folder is built next to its final place and swapped in whole, so a retry replaces an
    earlier export and a reader never sees half of one. tip is the branch commit exported
    (files/ and REPORT.md describe it; changes.patch is base..branch at that moment).
    """
    target = folder_for(output_dir, job)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=target.parent, prefix=f".{job['id']}.", suffix=".tmp"))
    try:
        patch = gitops.format_patch(repo_path, job["base"], tip)
        (staging / "changes.patch").write_bytes(patch)
        written, skipped, deleted = _write_files(repo_path, job["base"], tip, staging / "files")
        report = report_markdown(job, tip=tip, written=written, skipped=skipped, deleted=deleted,
                                 patch_bytes=len(patch))
        (staging / "REPORT.md").write_text(report, encoding="utf-8")
        _replace_dir(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"path": str(target), "files": len(written), "skipped": [f"{kind} {path}" for path, kind in skipped],
            "patch_bytes": len(patch)}


def _write_files(repo_path: Path, base: str, tip: str, root: Path) -> tuple[list[str], list[tuple[str, str]],
                                                                         list[str]]:
    """files/: the changed paths' blobs at tip. Returns (written, [(path, kind)] skipped, deleted)."""
    root.mkdir()
    changed = gitops.branch_files(repo_path, base, tip)
    entries = {path: (mode, kind, oid) for mode, kind, oid, path in gitops.tree_entries(repo_path, tip, changed)}
    written: list[str] = []
    skipped: list[tuple[str, str]] = []
    deleted: list[str] = []
    for path in changed:
        entry = entries.get(path)
        if entry is None or entry[1] == "tree":   # gone, or a file replaced by a directory
            deleted.append(path)
            continue
        mode, kind, oid = entry
        if mode in SKIPPED_MODES or kind != "blob" or mode not in REGULAR_MODES:
            skipped.append((path, SKIPPED_MODES.get(mode, f"mode {mode}")))
            continue
        destination = _safe_destination(root, path)
        if destination is None:
            skipped.append((path, "unsafe path"))
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(destination, flags, REGULAR_MODES[mode])
        with os.fdopen(fd, "wb") as handle:
            handle.write(gitops.blob(repo_path, oid))
        os.chmod(destination, REGULAR_MODES[mode])
        written.append(path)
    return written, skipped, deleted


def _safe_destination(root: Path, path: str) -> Path | None:
    """root/path, or None for a path that is absolute, has an empty, '.', '..' or '.git' part, or would
    pass through something that is not a directory this export created."""
    parts = path.split("/")
    if not path or any(p in ("", ".", "..") or p.lower() == ".git" for p in parts):
        return None
    current = root
    for part in parts[:-1]:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            return None
    return root.joinpath(*parts)


def _replace_dir(staging: Path, target: Path) -> None:
    """Move staging to target, replacing an earlier export there (a real directory, never a symlink)."""
    if os.path.lexists(target):
        if target.is_symlink() or not target.is_dir():
            raise OSError(f"{target} exists and is not a directory of an earlier export")
        old = target.with_name(f".{target.name}.old-{os.getpid()}")
        os.rename(target, old)
        try:
            os.rename(staging, target)
        except BaseException:
            os.rename(old, target)
            raise
        shutil.rmtree(old, ignore_errors=True)
    else:
        os.rename(staging, target)


# ---------------------------------------------------------------- REPORT.md


def report_markdown(job: dict, *, tip: str, written: list[str], skipped: list[tuple[str, str]],
                    deleted: list[str], patch_bytes: int) -> str:
    """REPORT.md of a folder delivery: what was asked, specified, built, reviewed and decided."""
    job_dir = Path(job["job_dir"])
    reviewers = [str(r) for r in job.get("reviewers") or []]
    lines = [f"# Delivery report: {job['repo']}/{job['id']}", "",
             f"- Branch: `{job.get('branch')}` at `{tip[:12]}` (kept in the repository)",
             f"- Base: `{job.get('base')}`" + (f" at `{str(job.get('base_commit'))[:12]}` when the job started"
                                               if job.get("base_commit") else ""),
             f"- Rounds: {job.get('round') or 0}, builder resumes {job.get('resumes') or 0}/"
             f"{job.get('max_resumes') or 0}",
             "- Approve the job in the factory to merge the branch into the base branch.", "",
             "## Feature", "", _clip(str(job.get("feature") or "").strip(), FEATURE_CHARS), "",
             "## Spec summary", "", _summary(job_dir / "spec.md", ("summary", "requirements")), "",
             "## Build summary", "", _summary(job_dir / "build.md", ("summary",)), "",
             "## Reviews", ""]
    history = [entry for entry in job.get("history") or [] if isinstance(entry, dict)]
    if history and reviewers:
        lines.append("| round | " + " | ".join(reviewers) + " |")
        lines.append("| ----- | " + " | ".join("-" * max(3, len(r)) for r in reviewers) + " |")
        for entry in history:
            verdicts = entry.get("reviews") if isinstance(entry.get("reviews"), dict) else {}
            lines.append(f"| {entry.get('round')} | " + " | ".join(str(verdicts.get(r, "-")) for r in reviewers)
                         + " |")
    else:
        lines.append("No review rounds recorded.")
    lines += _independence_lines(job)
    lines += ["", "## Decision", ""]
    if job.get("approved_by") == "human":
        lines += ["Approved for delivery by a human in the factory, after the line stopped for a human. "
                  "The approver's decision file says:", ""]
    lines += [_decision(job_dir / "decision.md", job.get("decision")), "",
              "## What is in this folder", "",
              f"- `changes.patch`: the branch's commits since `{job.get('base')}` ({patch_bytes} bytes). Apply "
              f"them to a checkout with `git am changes.patch` (or `git apply changes.patch` for the changes "
              f"without the commits).",
              f"- `files/`: the {len(written)} changed file(s) as they are at the branch tip."]
    if skipped:
        lines.append("- Not in `files/`: " + ", ".join(f"`{path}` ({kind})" for path, kind in skipped) + ".")
    if deleted:
        lines.append("- Deleted on the branch: " + ", ".join(f"`{path}`" for path in deleted) + ".")
    lines.append("")
    return "\n".join(lines)


def _independence_lines(job: dict) -> list[str]:
    """The independence guard's record for REPORT.md (DESIGN §23): each judge run on a model family the
    builder ran on (job.shared_models: model keys and families only), or that the code came from a chat
    whose models the job does not record. [] when every judge was independent."""
    status = job.get("independence")
    if status not in ("reduced", "accepted", "unknown"):
        return []
    lines = [""]
    if status == "reduced":
        lines.append("Independence reduced: a judge ran on a model family the builder ran on, with no way out.")
    elif status == "unknown":
        lines.append("Independence unknown: the code it judged was written in a chat whose models the job does "
                     "not record.")
    else:
        lines.append("Independence: a judge shared the builder's model family, as the routing chose "
                     "(independent_alternate: false).")
    for entry in job.get("shared_models") or []:
        if isinstance(entry, dict):
            accepted = " (accepted by the routing)" if entry.get("accepted") else ""
            lines.append(f"- round {entry.get('round')}: {entry.get('agent')} on `{entry.get('model')}` "
                         f"({entry.get('family')} family), like the builder's `{entry.get('builder_model')}`"
                         f"{accepted}")
    return lines


def _read(path: Path) -> str:
    try:
        return read_small_text(path, READ_BYTES)
    except (OSError, UnicodeDecodeError):
        return ""


def _summary(path: Path, headings: tuple[str, ...]) -> str:
    """The file's first heading and the first of headings' sections it has, else its opening lines."""
    text = _read(path)
    if not text.strip():
        return f"({path.name} is missing)"
    lines = text.splitlines()
    title = next((line.strip().lstrip("#").strip() for line in lines if line.lstrip().startswith("# ")), "")
    for heading in headings:
        body = _section(lines, heading)
        if body:
            return _clip((f"**{title}**\n\n" if title else "") + body, SECTION_CHARS)
    opening = "\n".join(line for line in lines if not line.startswith("STATUS:")).strip()
    return _clip(opening, SECTION_CHARS)


def _section(lines: list[str], heading: str) -> str:
    """The text under the first markdown heading named heading (any level, any case), up to the next
    heading of the same or a higher level."""
    collected: list[str] | None = None
    level = 0
    for line in lines:
        stripped = line.strip()
        marks = len(stripped) - len(stripped.lstrip("#"))
        is_heading = 0 < marks <= 6 and stripped[marks:marks + 1] == " "
        if collected is None:
            if is_heading and stripped[marks:].strip().lower() == heading:
                collected, level = [], marks
            continue
        if is_heading and marks <= level:
            break
        collected.append(line)
    return "\n".join(collected or []).strip()


def _decision(path: Path, decision: Any) -> str:
    text = _read(path)
    first = next((line.strip() for line in text.splitlines() if line.strip()), "") or str(decision or "none")
    why = _section(text.splitlines(), "why")
    return _clip(f"**{first}**" + (f": {why}" if why else ""), SECTION_CHARS)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + " …"
