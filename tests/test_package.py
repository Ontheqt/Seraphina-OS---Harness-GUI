"""Tests for scripts/package.py: the release zip and its privacy guard.

The planted values are built at run time, so this file itself passes the guard.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests.helpers import make_repo, run_git

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "package.py"

SPEC = importlib.util.spec_from_file_location("package_script", SCRIPT)
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)

TERM = "Quokka-Lantern-7731"           # a synthetic private term
OTHER = "zebra.example.internal"
DENYLIST = f"# one literal per line\n\n{TERM.lower()}\n{OTHER}\n"


def fake_hex(seed: str, n: int) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:n]


def real_looking_key_block() -> str:
    body = base64.b64encode(hashlib.sha512(b"one").digest() * 6).decode()
    lines = [body[i:i + 70] for i in range(0, len(body), 70)]
    kind = "OPENSSH " + "PRIVATE KEY"
    return "\n".join([f"-----BEGIN {kind}-----", *lines, f"-----END {kind}-----"]) + "\n"


class Repo:
    def __init__(self, tmp: Path, files: dict[str, str]) -> None:
        base = {"VERSION": "0.2.0\n", "swfactory/__init__.py": '__version__ = "0.2.0"\n',
                "README.md": "# Example\n\nRun it from /home/me/code or /home/<user>/x.\n"}
        base.update(files)
        self.path = make_repo(tmp, "repo", base)
        self.out = tmp / "dist"
        self.denylist = tmp / "denylist.txt"
        self.denylist.write_text(DENYLIST)

    def commit(self, rel: str, text: str | bytes, mode: int | None = None) -> None:
        path = self.path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if isinstance(text, bytes) else text.encode())
        if mode is not None:
            path.chmod(mode)
        run_git(self.path, "add", "-f", rel)
        run_git(self.path, "commit", "-q", "-m", f"add {rel}")

    def build(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-I", str(SCRIPT), "--repo", str(self.path), "--out", str(self.out),
                               "--denylist", str(self.denylist), *extra], capture_output=True, text=True, check=False)

    def zip_path(self) -> Path:
        return self.out / "seraphina-os-harness-0.2.0.zip"


class PackageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def repo(self, files: dict[str, str] | None = None) -> Repo:
        return Repo(self.tmp, files or {})

    def assert_refused(self, result: subprocess.CompletedProcess, *where: str) -> None:
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("REFUSED", result.stdout)
        for item in where:
            self.assertIn(item, result.stdout)

    def assert_silent_about(self, result: subprocess.CompletedProcess, *values: str) -> None:
        both = (result.stdout + result.stderr).casefold()
        for value in values:
            self.assertNotIn(value.casefold(), both)


class DenylistTest(PackageTestCase):
    def test_planted_term_refuses_and_is_never_echoed(self) -> None:
        repo = self.repo({"docs/notes.md": "line one\nline two\nour host is " + TERM.upper() + " today\n"})
        result = repo.build()
        self.assert_refused(result, "docs/notes.md:3: a denylist term")
        self.assert_silent_about(result, TERM, "our host is")
        self.assertFalse(repo.zip_path().exists())

    def test_every_term_is_checked_ignoring_case(self) -> None:
        repo = self.repo({"a.txt": "x\n", "b.json": '{"host": "ZEBRA.Example.INTERNAL"}\n'})
        result = repo.build()
        self.assert_refused(result, "b.json:1: a denylist term")
        self.assertNotIn("a.txt", result.stdout)
        self.assert_silent_about(result, OTHER)

    def test_a_term_in_a_file_name_is_shown_by_position_only(self) -> None:
        repo = self.repo({f"docs/{TERM}.md": "nothing here\n"})
        result = repo.build()
        self.assert_refused(result, "a denylist term in the file name")
        self.assert_silent_about(result, TERM)

    def test_comments_and_blank_lines_are_not_terms(self) -> None:
        repo = self.repo({"notes.md": "# one literal per line\nplain words\n"})
        self.assertEqual(repo.build().returncode, 0)

    def test_a_denylist_with_no_terms_is_a_setup_error(self) -> None:
        repo = self.repo()
        repo.denylist.write_text("# only a comment\n\n")
        result = repo.build()
        self.assertEqual(result.returncode, 2)
        self.assertFalse(repo.zip_path().exists())

    def test_held_back_files_are_not_scanned_or_shipped(self) -> None:
        repo = self.repo()
        repo.commit("private/owner.md", TERM)
        repo.commit("docs/server-factory/CATALOG.md", TERM)
        repo.commit("docs/seraphina/research/real-server-lessons.md", TERM)
        result = repo.build()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        names = zipfile.ZipFile(repo.zip_path()).namelist()
        self.assertFalse([n for n in names if "private/" in n or "server-factory" in n or "real-server" in n])

    def test_a_sanitized_server_factory_folder_is_shipped_and_scanned(self) -> None:
        repo = self.repo()
        repo.commit("docs/server-factory/SANITIZED.md", "Cleaned by the owner.\n")
        repo.commit("docs/server-factory/CATALOG.md", "# Catalog\n")
        self.assertEqual(repo.build().returncode, 0)
        names = zipfile.ZipFile(repo.zip_path()).namelist()
        self.assertIn("seraphina-os-harness-0.2.0/docs/server-factory/CATALOG.md", names)
        repo.commit("docs/server-factory/CATALOG.md", "# Catalog\n" + TERM + "\n")
        self.assert_refused(repo.build(), "docs/server-factory/CATALOG.md:2")


class SecretTest(PackageTestCase):
    def test_planted_private_key_refuses(self) -> None:
        block = real_looking_key_block()
        repo = self.repo({"config/deploy_key": "# deploy\n" + block})
        result = repo.build()
        self.assert_refused(result, "config/deploy_key:2: a private key block")
        self.assert_silent_about(result, block.splitlines()[1][:20])

    def test_planted_api_keys_refuse_and_are_never_echoed(self) -> None:
        deepseek = "sk-" + fake_hex("deepseek", 32)
        anthropic = "sk-ant-api03-" + base64.urlsafe_b64encode(hashlib.sha512(b"a").digest()).decode().rstrip("=")
        bearer = "Bearer " + base64.b64encode(hashlib.sha256(b"b").digest()).decode()
        repo = self.repo({"a.env": f"DEEPSEEK_API_KEY={deepseek}\n", "b.py": f"KEY = '{anthropic}'\n",
                          "c.sh": f"curl -H 'Authorization: {bearer}' https://example.invalid\n"})
        result = repo.build()
        self.assert_refused(result, "a.env:1: an sk- style API key", "b.py:1: an sk- style API key",
                            "c.sh:1: a Bearer token with a long value")
        self.assert_silent_about(result, deepseek, anthropic, bearer.split()[1])

    def test_well_known_token_formats_refuse(self) -> None:
        github = "ghp_" + base64.b32encode(hashlib.sha256(b"g").digest()).decode()[:36]
        repo = self.repo({"t.txt": f"token: {github}\n"})
        self.assert_refused(repo.build(), "t.txt:1: a GitHub token")

    def test_test_fixtures_are_not_keys(self) -> None:
        marker = "-----BEGIN OPENSSH " + "PRIVATE KEY-----"
        fixtures = "\n".join([
            "API = 'sk-test-secret-0123456789abcdef'",
            "OWNER = 'sk-owner-0123456789abcdef0123456789abcdef'",
            "HEADER = 'Bearer register-token-0123456789'",
            f"SHORT = '{marker}\\nb3BlbnNzaC1rZXktdjEAAAAA0012\\n-----END OPENSSH PRIVATE KEY-----'",
            f"REDACTED = f'{marker} {{MARK}}'",
            "TELL = 'Authorization: Bearer $TOKEN'",
        ])
        repo = self.repo({"tests/test_redact.py": fixtures + "\n"})
        result = repo.build()
        self.assertEqual(result.returncode, 0, result.stdout)


class PathTest(PackageTestCase):
    def test_an_owner_path_refuses_and_placeholders_pass(self) -> None:
        owner = "/home/" + "jsmith/notes/plan.md"
        served = "/srv/" + "family-photos/2026"
        repo = self.repo({
            "docs/a.md": "Placeholders: /home/<user>/.config, /home/me/code, $HOME/srv/x, /home/%i/bin,\n"
                         "/srv/ai-work/factory, `/home/` and /srv/ alone, https://example.com/home/jsmith.\n",
            "docs/b.md": f"one\nthe notes are in {owner}\n",
            "docs/c.md": f"data at {served}\n",
        })
        result = repo.build()
        self.assert_refused(result, "docs/b.md:2: an absolute /home or /srv path", "docs/c.md:1:")
        self.assertNotIn("docs/a.md", result.stdout)
        self.assert_silent_about(result, "jsmith", "family-photos")

    def test_fixture_paths_pass_only_under_tests(self) -> None:
        fixture = "/srv/" + "shop/app.db"
        repo = self.repo({"tests/test_x.py": f"PATH = '{fixture}'\n", "docs/x.md": f"see {fixture}\n"})
        result = repo.build()
        self.assert_refused(result, "docs/x.md:1:")
        self.assertNotIn("tests/test_x.py", result.stdout)


class BuildTest(PackageTestCase):
    def test_zip_has_tracked_files_a_manifest_and_checksums(self) -> None:
        repo = self.repo({"bin/tool": "#!/bin/sh\necho hi\n"})
        (repo.path / "bin" / "tool").chmod(0o755)
        run_git(repo.path, "add", "bin/tool")
        run_git(repo.path, "commit", "-q", "-m", "exec")
        repo.commit("swfactory/__pycache__/x.cpython-313.pyc", b"\0\1")
        repo.commit("tests/gui/shots/home.png", b"\x89PNG")
        (repo.path / "untracked.md").write_text("not in git\n")
        result = repo.build()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        archive = zipfile.ZipFile(repo.zip_path())
        top = "seraphina-os-harness-0.2.0/"
        names = sorted(n[len(top):] for n in archive.namelist())
        self.assertEqual(names, ["MANIFEST.json", "README.md", "SHA256SUMS", "VERSION", "bin/tool",
                                 "swfactory/__init__.py"])
        manifest = json.loads(archive.read(top + "MANIFEST.json"))
        self.assertEqual(manifest["version"], "0.2.0")
        self.assertEqual(manifest["commit"], run_git(repo.path, "rev-parse", "HEAD").strip())
        for entry in manifest["files"]:
            data = archive.read(top + entry["path"])
            self.assertEqual((entry["bytes"], entry["sha256"]), (len(data), hashlib.sha256(data).hexdigest()))
        sums = dict(reversed(line.split("  ", 1)) for line in archive.read(top + "SHA256SUMS").decode().splitlines())
        self.assertEqual(set(sums), {e["path"] for e in manifest["files"]} | {"MANIFEST.json"})
        self.assertEqual(sums["MANIFEST.json"], hashlib.sha256(archive.read(top + "MANIFEST.json")).hexdigest())
        self.assertEqual((archive.getinfo(top + "bin/tool").external_attr >> 16) & 0o777, 0o755)
        self.assertEqual((archive.getinfo(top + "README.md").external_attr >> 16) & 0o777, 0o644)

    def test_the_same_commit_gives_the_same_zip(self) -> None:
        repo = self.repo()
        self.assertEqual(repo.build().returncode, 0)
        first = repo.zip_path().read_bytes()
        self.assertEqual(repo.build().returncode, 0)
        self.assertEqual(repo.zip_path().read_bytes(), first)

    def test_uncommitted_changes_and_a_version_mismatch_refuse(self) -> None:
        repo = self.repo()
        (repo.path / "README.md").write_text("changed\n")
        self.assertEqual(repo.build().returncode, 2)
        self.assertEqual(repo.build("--allow-dirty").returncode, 0)
        run_git(repo.path, "checkout", "--", "README.md")
        repo.commit("VERSION", "0.3.0\n")
        result = repo.build()
        self.assertEqual(result.returncode, 2)
        self.assertIn("VERSION and swfactory.__version__ differ", result.stderr)

    def test_scan_dir_checks_an_unzipped_release(self) -> None:
        repo = self.repo()
        self.assertEqual(repo.build().returncode, 0)
        unpacked = self.tmp / "unpacked"
        zipfile.ZipFile(repo.zip_path()).extractall(unpacked)
        folder = unpacked / "seraphina-os-harness-0.2.0"
        scan = [sys.executable, "-I", str(SCRIPT), "--denylist", str(repo.denylist), "--scan-dir", str(folder)]
        clean = subprocess.run(scan, capture_output=True, text=True, check=False)
        self.assertEqual(clean.returncode, 0, clean.stdout)
        self.assertIn("checksums match", clean.stdout)
        (folder / "README.md").write_text("now with " + TERM + "\n")
        dirty = subprocess.run(scan, capture_output=True, text=True, check=False)
        self.assertEqual(dirty.returncode, 1)
        self.assertIn("README.md:1: a denylist term", dirty.stdout)
        self.assertIn("README.md: does not match SHA256SUMS", dirty.stdout)
        self.assert_silent_about(dirty, TERM)


class HeuristicsTest(unittest.TestCase):
    def test_counting_values_are_fixtures(self) -> None:
        self.assertTrue(package.counting("0123456789abcdef0123456789abcdef"))
        self.assertFalse(package.counting(fake_hex("x", 32)))

    def test_this_repository_matches_its_version_and_passes_its_own_guard(self) -> None:
        from swfactory import __version__
        self.assertEqual((ROOT / "VERSION").read_text().strip(), __version__)
        for path in (SCRIPT, Path(__file__)):
            rel = path.relative_to(ROOT).as_posix()
            self.assertEqual([str(f) for f in package.scan_file(rel, path.read_bytes(), ["no-such-" + "term-anywhere"])], [])


if __name__ == "__main__":
    unittest.main()
