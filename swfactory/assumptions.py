"""The spec's product assumptions and a person's confirmations of them (DESIGN.md section 7).

The rules are the Claude Code kit's gate.sh rules, so both harnesses read a spec the same way:
a line of spec.md is marked when it holds "(product)" in any case, except the verbatim
"> Feature:" line and a mention that says "not (product)" or "never (product)". Lines compare
after normalize(): heading marks, list markers, a "[x]" box, ** bold and runs of spacing do not
count. confirmed.md is CONFIRMED_HEADER and then one confirmed line per line.

In the web harness only the orchestrator writes confirmed.md, from the job's `confirmed` record,
when a person confirms (Factory.confirm); see Factory._restore_confirmed for the rest. The
Housekeeping Tracker (DESIGN.md section 32) makes each marked Assumptions item a decision item
that only a person answers; answers() says which confirmed line answers which item, so a
confirmation and the item's proof stay one answer.
"""

from __future__ import annotations

import re
import shutil
import stat
from pathlib import Path

CONFIRMED_FILE = "confirmed.md"
CONFIRMED_HEADER = "# Confirmed by a human"
MARK = "(product)"
SPEC_MAX_BYTES = 1 << 20
_FEATURE_LINE_RE = re.compile(r"^\s*>\s*Feature:", re.ASCII)
_NEGATED_RE = re.compile(r"(^|[^a-z0-9_])(?:not|never)\s+\(product\)", re.ASCII)
_HEADING_RE = re.compile(r"^\s*#+\s+", re.ASCII)
_LIST_RE = re.compile(r"^\s*(?:[-*+]|[0-9]+[.)])\s+", re.ASCII)
_BOX_RE = re.compile(r"^\[[xX ]\]\s+", re.ASCII)
_SPACE_RE = re.compile(r"\s+", re.ASCII)


def normalize(line: str) -> str:
    """One spelling of line: no heading mark, list marker, checkbox or bold, single spaces."""
    text = _BOX_RE.sub("", _LIST_RE.sub("", _HEADING_RE.sub("", line.rstrip("\r\n"))))
    return _SPACE_RE.sub(" ", text.replace("**", "")).strip()


def marked(spec_text: str) -> list[str]:
    """The normalized lines of spec_text that carry the mark (the module docstring), once each, in order."""
    lines: dict[str, None] = {}
    for line in spec_text.splitlines():
        if _FEATURE_LINE_RE.match(line):
            continue
        if MARK in _NEGATED_RE.sub(r"\1", line.lower().replace("**", "")):
            text = normalize(line)
            if text:
                lines[text] = None
    return list(lines)


def answers(line: str, quote: str) -> bool:
    """Whether the marked line (normalized) is the mark of the spec item whose text is quote (a decision
    item's source quote): that text, normalized, is the line or holds it (an item whose mark sits on a
    continuation line)."""
    text = normalize(quote)
    return bool(line) and (text == line or line in text)


def view(spec_text: str | None, confirmed: list[str]) -> dict:
    """{marked, confirmed, unconfirmed}: the spec's marked lines, the confirmed ones on record, and the
    marked lines no confirmation matches yet (all normalized)."""
    lines = marked(spec_text or "")
    have = {normalize(line) for line in confirmed}
    return {"marked": lines, "confirmed": list(confirmed), "unconfirmed": [line for line in lines if line not in have]}


def render(lines: list[str]) -> str:
    """confirmed.md for lines."""
    return CONFIRMED_HEADER + "\n" + "".join(f"{line}\n" for line in lines)


def read_spec(job_dir: Path) -> str | None:
    """The job's spec.md (None when there is none, or it is not a regular file), cut at SPEC_MAX_BYTES."""
    path = Path(job_dir) / "spec.md"
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return None
        with path.open("rb") as handle:
            return handle.read(SPEC_MAX_BYTES).decode("utf-8", "replace")
    except OSError:
        return None


def on_record(path: Path, lines: list[str]) -> bool:
    """Whether path is exactly what the record gives: render(lines) in a regular file, or nothing at all
    when no line is confirmed. A link, a folder or anything else never is."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return not lines
    except OSError:
        return False
    if not lines or not stat.S_ISREG(info.st_mode):
        return False
    try:
        return path.read_bytes() == render(lines).encode("utf-8")
    except OSError:
        return False


def clear(path: Path) -> None:
    """Remove whatever is at path (a file, a link, a folder), so the record can be written in its place."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)
