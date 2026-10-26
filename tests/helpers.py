"""Shared test fixtures. Tests never touch the network or real API keys."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


def run_git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def make_repo(tmp: Path, name: str = "repo", files: dict[str, str] | None = None) -> Path:
    """Create a git repo on `main` with one commit and local user config. Returns its path."""
    repo = Path(tmp) / name
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    run_git(repo, "config", "user.name", "Factory Test")
    run_git(repo, "config", "user.email", "factory-test@example.invalid")
    run_git(repo, "config", "commit.gpgsign", "false")
    for rel, text in (files or {"README.md": "# test repo\n"}).items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "initial")
    return repo


def commit_file(repo: Path, rel: str, text: str, message: str = "change") -> str:
    path = Path(repo) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    run_git(repo, "add", rel)
    run_git(repo, "commit", "-q", "-m", message)
    return run_git(repo, "rev-parse", "HEAD").strip()


def clean_env(**extra: str) -> dict[str, str]:
    """os.environ without provider keys, plus extra."""
    env = {k: v for k, v in os.environ.items()
           if not k.endswith(("_API_KEY", "_TOKEN")) and not k.startswith(("ANTHROPIC_", "OPENAI_", "DEEPSEEK_"))}
    env.update(extra)
    return env


def outside_tmp(program: Path, case: unittest.TestCase) -> Path:
    """program itself, or a copy of it outside /tmp when it sits below /tmp (a checkout under /tmp): bwrap gives
    every sandboxed command a private /tmp, so a program kept there cannot be started inside the sandbox. The copy
    lives in a folder under /var/tmp or ~/.cache that case removes afterwards; case is skipped when neither works."""
    program = Path(program).resolve()
    if Path("/tmp") not in program.parents:
        return program
    for base in (Path("/var/tmp"), Path.home() / ".cache"):
        try:
            base.mkdir(parents=True, exist_ok=True)
            folder = Path(tempfile.mkdtemp(prefix="sf-outside-tmp-", dir=base)).resolve()
        except OSError:
            continue
        case.addCleanup(shutil.rmtree, folder, True)
        return Path(shutil.copy2(program, folder / program.name))
    case.skipTest("the checkout is under /tmp and no folder outside /tmp is writable for a copy of the test program")
    raise AssertionError("unreachable")   # skipTest raises
