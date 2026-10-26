#!/usr/bin/env python3
"""Build the release zip, and refuse to build when a file could leak private data.

Usage:
    python3 scripts/package.py --denylist FILE [--out DIR] [--repo DIR] [--allow-dirty]
    python3 scripts/package.py --denylist FILE --scan-dir DIR

Build mode puts the files git tracks (`git ls-files`) into
`<out>/seraphina-os-harness-<VERSION>.zip`, under one top folder of the same
name. Untracked files never go in. These tracked files are left out:

- anything in a folder named `private`;
- `docs/server-factory/`, the owner's server catalog, unless a sanitized
  version exists: the folder is included only when it holds `SANITIZED.md`,
  which says the owner has cleaned it (it is then scanned like any file);
- `docs/seraphina/research/real-server-lessons.md`, the owner's server notes;
- `__pycache__` folders and `.pyc` files;
- test screenshots: images under `tests/`, and any `screenshots/` or `shots/`
  folder.

Before anything is written, every included file is checked. Any finding
refuses the build. A finding prints the file path, the line number and the
kind of finding. It never prints the matched text.

1. Denylist. Each line of the --denylist file is one literal. Blank lines and
   lines starting with `#` are skipped. A file matches when a line holds a
   literal, ignoring case. File names are checked too; a file name that
   matches is shown by its position in `git ls-files`, not by its name.
2. Secrets. A private key block with a key body in it (a marker alone, as in a
   redaction test, is not a key); an `sk-` style API key; a Bearer token of 32
   characters or more; a GitHub, AWS or Hugging Face token. Test values built
   from a counting sequence such as `0123456789abcdef` are not keys.
3. Absolute paths under /home/ or /srv/. Only these are allowed:
   - a path with no user or folder name: `/home/`, `/srv/`;
   - a placeholder segment: `<user>`, `$USER`, `${USER}`, `%i`, `*`, `...`;
   - the example paths in GLOBAL_ALLOWED_PATHS below;
   - in files under `tests/` only, the made-up fixture paths in
     TEST_ALLOWED_NAMES below.
   Write any other example as `/home/<user>/...` or `/home/me/...`.

Build mode also refuses when tracked files have uncommitted changes (unless
--allow-dirty), when VERSION differs from `swfactory.__version__`, and when a
tracked path is a symbolic link or a submodule.

The zip holds MANIFEST.json (path, bytes and sha256 of each file, the version
and the commit) and SHA256SUMS (`sha256sum -c` format, run from the top
folder). The zip is reproducible: the same commit gives the same bytes.

Scan mode (--scan-dir) runs the same checks on every file under a folder, for
example an unzipped release, and checks the files against SHA256SUMS when the
folder has one. It writes nothing.

Exit status: 0 built or clean, 1 refused (findings), 2 a usage or setup error.
Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

PRODUCT = "Seraphina OS Harness"
ZIP_STEM = "seraphina-os-harness"

SERVER_FACTORY = "docs/server-factory/"
SERVER_FACTORY_SANITIZED = SERVER_FACTORY + "SANITIZED.md"
HELD_BACK_FILES = {"docs/seraphina/research/real-server-lessons.md"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
SCREENSHOT_DIRS = {"screenshots", "shots"}

# Example paths the public docs and code may name. Each is a placeholder or a
# documented example, not a path on the owner's server.
GLOBAL_ALLOWED_PATHS = {
    "/home/me": "example home in DESIGN and the tests",
    "/home/you": "placeholder in the GUI's repository field",
    "/home/owner": "placeholder owner in examples and tests",
    "/home/ai-dev": "the README's example service user (not an account on the owner's server)",
    "/srv/ai-work": "the README's example data folder (not found on the owner's server)",
    "/srv/models": "the Model Factory's example registry root",
    "/srv/seraphina": "example folder in the v2 design's restore drill",
}

# Made-up fixture paths that only files under tests/ may name: the folder
# right under /home/ or /srv/. Written as names, so this file names no path.
TEST_ALLOWED_NAMES = {
    "home": {"u", "operator"},
    "srv": {".snapraid", "snapraid.parity", "api", "app", "chats", "data", "docker", "early", "f",
            "factory-output", "m", "m.db", "media", "memory", "notes", "operator", "out", "real", "sera",
            "shop", "staged", "x"},
}

KEY_BEGIN = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")
KEY_END = re.compile(r"-----END ")
KEY_TEXT_STOP = re.compile(r"[^A-Za-z0-9+/=:,\s\\.-]")  # where a key body cannot go on
KEY_BODY_MIN = 64  # base64 characters; a real key body has hundreds
SK_KEY = re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}")
BEARER = re.compile(r"(?i)(?<![A-Za-z0-9_])bearer\s+([A-Za-z0-9._~+/=-]{32,})")
TOKENS = (
    ("a GitHub token", re.compile(r"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{36,}")),
    ("a GitHub token", re.compile(r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{50,}")),
    ("an AWS access key", re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![A-Za-z0-9])")),
    ("a Hugging Face token", re.compile(r"(?<![A-Za-z0-9_])hf_[A-Za-z0-9]{30,}")),
)
ABS_PATH = re.compile(r"(?<![A-Za-z0-9_.~$@-])/(home|srv)(?![A-Za-z0-9_.-])([^\s'\"`()\[\],;|\\=&?#:]*)")
PLACEHOLDER_START = ("<", "$", "%", "{", "*", ".", "…")
COUNTING = "0123456789abcdefghijklmnopqrstuvwxyz"


class Finding:
    def __init__(self, path: str, line: int | None, kind: str) -> None:
        self.path, self.line, self.kind = path, line, kind

    def __str__(self) -> str:
        where = self.path if self.line is None else f"{self.path}:{self.line}"
        return f"{where}: {self.kind}"


class SetupError(Exception):
    """A usage or setup problem: exit 2."""


def load_denylist(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        raise SetupError(f"cannot read the denylist file: {err.strerror}") from None
    terms = []
    for raw in text.splitlines():
        term = raw.strip()
        if term and not term.startswith("#"):
            terms.append(term.casefold())
    if not terms:
        raise SetupError("the denylist file has no terms; refusing to build without one")
    return terms


def counting(value: str) -> bool:
    """True for a test value built from a counting sequence (0123456789abcdef...)."""
    chars = [c for c in value.lower() if c in COUNTING]
    if len(chars) < 2:
        return False
    steps = sum(1 for a, b in zip(chars, chars[1:]) if COUNTING.index(b) == COUNTING.index(a) + 1)
    return steps * 2 >= len(chars) - 1


def real_sk_key(value: str) -> bool:
    body = value[3:]
    for run in re.findall(r"[A-Za-z0-9]+", body):
        if len(run) >= 24 and re.search(r"[A-Za-z]", run) and re.search(r"[0-9]", run) and not counting(run):
            return True
    mixed = re.search(r"[A-Z]", body) and re.search(r"[a-z]", body) and re.search(r"[0-9]", body)
    return bool(len(body) >= 40 and mixed and not counting(body))


def secret_findings(rel: str, lines: list[str], text: str) -> list[Finding]:
    found: list[Finding] = []
    for match in KEY_BEGIN.finditer(text):
        rest = text[match.end():match.end() + 8000]
        end = KEY_END.search(rest)
        other = KEY_TEXT_STOP.search(rest)
        body = rest[:min(end.start() if end else len(rest), other.start() if other else len(rest))]
        if len(re.findall(r"[A-Za-z0-9+/=]", body)) >= KEY_BODY_MIN:
            found.append(Finding(rel, text.count("\n", 0, match.start()) + 1, "a private key block"))
    for number, line in enumerate(lines, 1):
        if any(real_sk_key(m.group(0)) for m in SK_KEY.finditer(line)):
            found.append(Finding(rel, number, "an sk- style API key"))
        if any(not counting(m.group(1)) for m in BEARER.finditer(line)):
            found.append(Finding(rel, number, "a Bearer token with a long value"))
        for kind, pattern in TOKENS:
            if any(not counting(m.group(0).split("_", 1)[-1]) for m in pattern.finditer(line)):
                found.append(Finding(rel, number, kind))
    return found


def path_allowed(rel: str, root: str, rest: str) -> bool:
    rest = rest.rstrip(".,:;!?)")
    first = rest.lstrip("/").split("/", 1)[0] if rest.startswith("/") else ""
    if not first or first.startswith(PLACEHOLDER_START):
        return True
    candidate = f"/{root}/{first}"
    if candidate in GLOBAL_ALLOWED_PATHS:
        return True
    return rel.startswith("tests/") and first in TEST_ALLOWED_NAMES.get(root, ())


def path_findings(rel: str, lines: list[str]) -> list[Finding]:
    found = []
    for number, line in enumerate(lines, 1):
        if "/home" not in line and "/srv" not in line:
            continue
        if any(not path_allowed(rel, m.group(1), m.group(2)) for m in ABS_PATH.finditer(line)):
            found.append(Finding(rel, number, "an absolute /home or /srv path outside the allow-list"))
    return found


def scan_file(rel: str, data: bytes, terms: list[str]) -> list[Finding]:
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    found = []
    for number, line in enumerate(lines, 1):
        folded = line.casefold()
        if any(term in folded for term in terms):
            found.append(Finding(rel, number, "a denylist term"))
    found.extend(secret_findings(rel, lines, text))
    found.extend(path_findings(rel, lines))
    return found


def scan_names(names: list[str], terms: list[str]) -> list[Finding]:
    found = []
    for index, name in enumerate(names, 1):
        if any(term in name.casefold() for term in terms):
            found.append(Finding(f"file #{index} in the list", None, "a denylist term in the file name"))
    return found


def exclusion(rel: str, tracked: set[str]) -> str | None:
    parts = rel.split("/")
    folders = parts[:-1]
    if "private" in folders:
        return "private folder"
    if rel.startswith(SERVER_FACTORY) and SERVER_FACTORY_SANITIZED not in tracked:
        return "server-factory catalog (no sanitized version)"
    if rel in HELD_BACK_FILES:
        return "owner-specific research note"
    if "__pycache__" in folders or rel.endswith((".pyc", ".pyo")):
        return "Python cache"
    if (rel.startswith("tests/") and Path(rel).suffix.lower() in IMAGE_SUFFIXES) or SCREENSHOT_DIRS & set(folders):
        return "test screenshot"
    return None


def git(repo: Path, *args: str) -> str:
    try:
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    except OSError as err:
        raise SetupError(f"cannot run git: {err.strerror}") from None
    if done.returncode != 0:
        raise SetupError(f"git {args[0]} failed in {repo}")
    return done.stdout.decode("utf-8", errors="surrogateescape")


def read_version(repo: Path) -> str:
    try:
        version = (repo / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        raise SetupError("no VERSION file at the repository root") from None
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", version):
        raise SetupError("VERSION does not hold a version such as 0.2.0")
    init = repo / "swfactory" / "__init__.py"
    if init.is_file():
        match = re.search(r'^__version__\s*=\s*"([^"]+)"', init.read_text(encoding="utf-8"), re.M)
        if not match or match.group(1) != version:
            raise SetupError("VERSION and swfactory.__version__ differ")
    return version


def tracked_files(repo: Path) -> list[tuple[str, str]]:
    """(mode, path) for each tracked file, from the index."""
    entries = []
    for record in git(repo, "ls-files", "-s", "-z").split("\0"):
        if not record:
            continue
        meta, rel = record.split("\t", 1)
        entries.append((meta.split()[0], rel))
    return sorted(set(entries), key=lambda e: e[1])


def report(findings: list[Finding], files: int) -> None:
    print(f"REFUSED: {len(findings)} finding(s) in {files} file(s) checked. Matched text is never shown.")
    for finding in findings:
        print(f"  {finding}")


def build(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve() if args.repo else Path(__file__).resolve().parent.parent
    terms = load_denylist(Path(args.denylist))
    version = read_version(repo)
    if not args.allow_dirty and git(repo, "status", "--porcelain", "--untracked-files=no").strip():
        raise SetupError("tracked files have uncommitted changes; commit them first (or pass --allow-dirty)")
    entries = tracked_files(repo)
    tracked = {rel for _, rel in entries}
    included, excluded = [], {}
    for mode, rel in entries:
        reason = exclusion(rel, tracked)
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        if mode == "120000":
            raise SetupError(f"{rel} is a symbolic link; the release zip holds regular files only")
        if mode == "160000":
            raise SetupError(f"{rel} is a submodule; the release zip holds regular files only")
        included.append((mode, rel))

    findings = scan_names([rel for _, rel in included], terms)
    contents = {}
    for _, rel in included:
        try:
            data = (repo / rel).read_bytes()
        except OSError as err:
            raise SetupError(f"cannot read {rel}: {err.strerror}") from None
        contents[rel] = data
        findings.extend(scan_file(rel, data, terms))
    if findings:
        report(findings, len(included))
        return 1

    commit = git(repo, "rev-parse", "HEAD").strip()
    stamp = int(git(repo, "log", "-1", "--format=%ct").strip())
    when = max(stamp, 315532800)  # the zip format starts in 1980
    date_time = time.gmtime(when)[:6]
    top = f"{ZIP_STEM}-{version}"
    manifest = {
        "name": PRODUCT, "version": version, "commit": commit,
        "files": [{"path": rel, "bytes": len(contents[rel]), "sha256": hashlib.sha256(contents[rel]).hexdigest()}
                  for _, rel in included],
    }
    manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    sums = [(f["sha256"], f["path"]) for f in manifest["files"]]
    sums.append((hashlib.sha256(manifest_bytes).hexdigest(), "MANIFEST.json"))
    sums_bytes = "".join(f"{digest}  {rel}\n" for digest, rel in sums).encode("utf-8")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        def add(rel: str, data: bytes, mode: int) -> None:
            info = zipfile.ZipInfo(f"{top}/{rel}", date_time=date_time)
            info.create_system = 3
            info.external_attr = (0o100000 | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
        for mode, rel in included:
            add(rel, contents[rel], 0o755 if mode == "100755" else 0o644)
        add("MANIFEST.json", manifest_bytes, 0o644)
        add("SHA256SUMS", sums_bytes, 0o644)
    out_dir = Path(args.out).resolve() if args.out else repo / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{top}.zip"
    partial = target.with_name(target.name + ".partial")
    partial.write_bytes(buffer.getvalue())
    os.replace(partial, target)
    held = ", ".join(f"{reason}: {count}" for reason, count in sorted(excluded.items())) or "none"
    print(f"built {target}")
    print(f"  version {version}, commit {commit[:12]}, {len(included)} files; left out ({held})")
    print(f"  sha256 {hashlib.sha256(buffer.getvalue()).hexdigest()}")
    return 0


def scan_dir(args: argparse.Namespace) -> int:
    root = Path(args.scan_dir).resolve()
    if not root.is_dir():
        raise SetupError("--scan-dir is not a folder")
    terms = load_denylist(Path(args.denylist))
    names = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and not p.is_symlink())
    findings = scan_names(names, terms)
    for rel in names:
        findings.extend(scan_file(rel, (root / rel).read_bytes(), terms))
    sums = root / "SHA256SUMS"
    checked = 0
    if sums.is_file():
        listed = set()
        for line in sums.read_text(encoding="utf-8").splitlines():
            digest, _, rel = line.partition("  ")
            listed.add(rel)
            target = root / rel
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                findings.append(Finding(rel, None, "does not match SHA256SUMS"))
            checked += 1
        for rel in names:
            if rel not in listed and rel != "SHA256SUMS":
                findings.append(Finding(rel, None, "is not listed in SHA256SUMS"))
    if findings:
        report(findings, len(names))
        return 1
    note = f"; {checked} checksums match" if sums.is_file() else ""
    print(f"clean: {len(names)} files scanned, no findings{note}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the release zip, refusing any file that could leak private data.")
    parser.add_argument("--denylist", required=True, help="file of private literals, one per line (never printed)")
    parser.add_argument("--out", help="folder for the zip (default: dist/ in the repository)")
    parser.add_argument("--repo", help="repository root (default: the folder above scripts/)")
    parser.add_argument("--allow-dirty", action="store_true", help="build although tracked files have uncommitted changes")
    parser.add_argument("--scan-dir", help="only scan every file under this folder (for example an unzipped release)")
    args = parser.parse_args(argv)
    try:
        return scan_dir(args) if args.scan_dir else build(args)
    except SetupError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
