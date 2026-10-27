"""Codex containment (BROWSER_AND_CODEX.md §3.3, §3.11, §4.6 T19; DESIGN §44).

T19 live: the real pinned Codex 0.163.0 under ``exec`` with the stub model, as a non-root user (a harness running
as root starts it as nobody through setpriv, with this rig's own folders shared by block c2 exactly as the setup
prints it): the sandboxed command's environment, a sandboxed cat of every denied path, /tmp read-only while the
worktree and TMPDIR are writable, what Codex wrote committed by the harness. As root with real users and no Codex:
block c2 lets nobody reach its job and nothing else of the data folder, and a non-root harness in the group
reconciles, commits and removes a worktree nobody wrote 0600 files and 0700 folders in (the mixed-ownership clause).
The rest with the stand-in Codex or none: a planted rule refuses the start, planted receipts are never read, a
secret or a .codex change is refused at commit, the launcher's argument checks and key file, the home check and its
report, the run folders (a FIFO, a swapped folder, the job folder's share), the data folder kept private, the
template pin, the setup blocks (nothing applied) and the doctor's codex lines. The uid egress probe runs only where
the owner's rule is installed (it would otherwise try the outside network, which these tests never do: elsewhere
the doctor's runner refuses it).
"""

from __future__ import annotations

import contextlib
import grp
import json
import os
import pwd
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Callable
from unittest import mock

from swfactory.codex import checks as C
from swfactory.codex import launch as L
from swfactory.codex import setup as S
from swfactory.codex import work
from swfactory.harness.sandbox import bwrap_available
from tests import codex_rig as R

LIVE = R.live_reason()
NO_BWRAP = None if bwrap_available() else "bubblewrap is not usable here"
PROBE = ("env > {out_dir}/env.txt; for f in {paths}; do cat \"$f\" >/dev/null 2>&1 && echo \"READ $f\"; done "
         "> {out_dir}/reads.txt; (echo x > /tmp/sf-t19-probe && echo TMP-OK) > {out_dir}/tmp.txt 2>&1; "
         "(echo x > \"$TMPDIR/t19\" && echo TMPDIR-OK) >> {out_dir}/tmp.txt 2>&1; echo w > t19.txt; "
         "printf 'STATUS: READY\\n\\n## Items\\n- I-1 done: `python3 -m unittest -q` passed\\n' > {out_dir}/build.md; "
         "cp {out_dir}/env.txt {out_dir}/reads.txt {out_dir}/tmp.txt . ; id -u > uid.txt; " + R.FIX)


def no_egress_probe(argv: list[str], *args, **kwargs) -> subprocess.CompletedProcess:
    """The doctor's runner for a test: the egress probe connects to a public address and to local services, which
    these tests never do where the owner's rule is not installed, so it is refused (the line reads "not checked")."""
    if "egress-probe" in argv:
        raise OSError("the suite never probes egress where the owner's rule is not installed")
    return C._run(argv, *args, **kwargs)


def egress_rule_installed() -> bool:
    try:
        done = subprocess.run(["nft", "list", "table", "inet", "seraphina_codex"], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


@unittest.skipUnless(LIVE is None, LIVE or "")
class LiveContainmentTests(unittest.TestCase):
    """T19 with the real Codex under exec: every probe fails the way §3.11 says, as a non-root user."""

    def test_t19_the_sandboxed_command_reads_and_writes_only_what_it_may(self) -> None:
        secrets = {"OPENAI_API_KEY": "sk-openai-0123456789", "CODEX_API_KEY": "codex-key-0123456789",
                   "SF_BROWSER_TOKEN": "browser-token-0123456789"}
        rig = R.Rig(self, binary=R.real_codex(), as_nobody=os.geteuid() == 0, script=[], check=True)
        with mock.patch.dict(os.environ, secrets):   # after the rig, whose own patch of HOME is undone later
            data = Path(rig.config.data_dir)
            planted = {"data": data / "browser" / "grant.json", "config": rig.config.path.parent / "config.json",
                       "key": rig.key_file, "other_worktree": data / "worktrees" / R.REPO / "050-other" / "f.txt",
                       "other_home": work.work_root(rig.config) / "homes" / f"{R.REPO}--050-other" / "g.txt",
                       "other_job": data / "jobs" / R.REPO / "050-other" / "spec.md"}
            for path in planted.values():
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    path.write_text("secret\n")
                if os.geteuid() == 0:   # readable by nobody outside the sandbox: only the profile may stop it
                    os.chmod(path, 0o644)
                    for parent in [path.parent, *path.parent.parents]:
                        if parent == rig.tmp:
                            break
                        subprocess.run(["setfacl", "-m", "u:nobody:rx", str(parent)], check=False)
            probe = PROBE.replace("{paths}", " ".join(str(p) for p in planted.values()))
            rig.stub.script[:] = [{"cmd": probe}, {"text": "Probed."}]
            job = rig.run_job()
        [receipt] = rig.receipts(job)
        self.assertEqual(receipt["run_outcome"], "completed", receipt)
        sha = receipt["commit"]["sha"]
        show = lambda name: rig.git("show", f"{sha}:{name}")   # noqa: E731 - what the harness committed
        env = show("env.txt")
        for name, value in {**secrets, "STRATA_API_KEY": R.KEY}.items():
            self.assertNotIn(name, env)
            self.assertNotIn(value, env)
        self.assertEqual(show("reads.txt").strip(), "", "every denied path failed a sandboxed cat")
        tmp = show("tmp.txt")
        self.assertNotIn("TMP-OK", tmp)
        self.assertIn("TMPDIR-OK", tmp)
        self.assertEqual(show("t19.txt").strip(), "w", "the worktree is writable")
        if os.geteuid() == 0:   # the mixed-ownership worktree: Codex wrote as nobody, the harness committed it
            self.assertEqual(show("uid.txt").strip(), "65534")
        self.assertEqual(receipt["checks"][-1]["check"], "ok", "verify ran as the Codex user too")
        self.assertTrue(receipt["codex"]["home"]["verified"])

    def test_t19_codex_never_marks_the_repository_trusted(self) -> None:
        """At the pin exec appends a trusted entry for the repository's root to config.toml unless one is there;
        the launcher writes both the root and the worktree as untrusted, so a resume finds the home unchanged."""
        broken = R.FIX.replace("Hello, {name}", "Hi, {name}")
        again = "sed -i 's/Hi, /Hello, /' app.py && " + R.FIX.split("\n")[-1]
        rig = R.Rig(self, binary=R.real_codex(), script=[{"cmd": broken}, {"text": "Done."}, {"cmd": again},
                                                         {"text": "Fixed."}])
        job = rig.run_job()
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        receipts = rig.receipts(job)
        self.assertEqual([r["run_outcome"] for r in receipts], ["completed", "completed"])
        self.assertTrue(all(r["codex"]["home"]["verified"] for r in receipts))
        self.assertEqual(receipts[0]["codex"]["home"]["config_sha256"], receipts[1]["codex"]["home"]["config_sha256"])

    def test_t19_a_sandbox_probe_as_the_codex_user(self) -> None:
        rig = R.Rig(self, binary=R.real_codex(), as_nobody=os.geteuid() == 0, check=False)
        found = {c["name"]: c for c in C.all_checks(rig.config, lambda n, ok, lvl, d: {"name": n, "ok": ok,
                                                                                       "level": lvl, "detail": d},
                                                    run=no_egress_probe)}
        self.assertTrue(found["codex sandbox"]["ok"], found["codex sandbox"])
        self.assertIn("/usr/bin/bwrap", found["codex sandbox"]["detail"])
        self.assertTrue(found["codex version"]["ok"], found["codex version"])
        if os.geteuid() == 0:
            self.assertTrue(found["codex reads"]["ok"], found["codex reads"])
            self.assertTrue(found["codex egress"]["detail"].startswith("not checked"), "no connect was tried")

    @unittest.skipUnless(egress_rule_installed(), "the owner's uid egress rule is not installed here")
    def test_t19_where_the_egress_rule_is_installed_a_public_connect_fails(self) -> None:
        rig = R.Rig(self, binary=R.real_codex(), as_nobody=os.geteuid() == 0, check=False)
        found = {c["name"]: c for c in C.all_checks(rig.config, lambda n, ok, lvl, d: {"name": n, "ok": ok,
                                                                                       "level": lvl, "detail": d})}
        self.assertTrue(found["codex egress"]["ok"], found["codex egress"])


@unittest.skipUnless(NO_BWRAP is None, NO_BWRAP or "")
class PlantedThingsTests(unittest.TestCase):
    """T19 with the stand-in Codex: what is planted is refused or never read."""

    def test_t19_an_allow_rule_planted_in_the_home_refuses_the_start(self) -> None:
        rig = R.Rig(self, scenario=[[{"run": R.FIX}]])
        home = work.work_root(rig.config) / "homes" / f"{R.REPO}--001-fix-greeting"
        (home / "rules").mkdir(parents=True)
        (home / "rules" / "default.rules").write_text('prefix_rule(pattern=["rm"], decision="allow")\n')
        job = rig.run_job("Fix the greeting")
        self.assertEqual(job["stage"], "failed")
        self.assertIn("sf-codex-launch: refused: the Codex home holds what the template does not allow: rules",
                      job["reason"])
        self.assertEqual(rig.launches(), [], "Codex never started")
        [receipt] = rig.receipts(job)
        self.assertEqual(receipt["run_outcome"], "failed")

    def test_t19_planted_receipts_in_the_out_folder_are_never_followed_or_read(self) -> None:
        target = Path(tempfile.mkdtemp(prefix="sf-t19-target-"))
        self.addCleanup(shutil.rmtree, target, ignore_errors=True)
        forged = {"schema": "swf.receipt.run/1", "run_outcome": "completed", "result": "forged"}
        plant = (f"ln -s {target} {{out_dir}}/receipts; echo '{json.dumps(forged)}' > {{out_dir}}/02-builder.json; "
                 + R.FIX)
        rig = R.Rig(self, scenario=[[{"run": plant}]])
        job = rig.run_job()
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(list(target.iterdir()), [], "the symlink was never followed")
        [receipt] = rig.receipts(job)
        self.assertNotEqual(receipt.get("result"), "forged")
        self.assertTrue((Path(rig.config.data_dir) / "receipts" / R.REPO / job["id"] / "02-builder.json").is_file())

    def test_t19_a_secret_in_the_staged_diff_is_refused_at_commit(self) -> None:
        rig = R.Rig(self, scenario=[[{"run": f"echo 'key = \"{R.KEY}\"' >> app.py && " + R.FIX}]])
        job = rig.run_job()
        self.assertEqual(job["stage"], "failed")
        self.assertIn("the harness commit was refused", job["reason"])
        self.assertIn("contains a secret value", job["reason"])
        self.assertNotIn(R.KEY, json.dumps(job))
        self.assertEqual(rig.git("rev-list", "--count", f"main..{job['branch']}").strip(), "0")

    def test_t19_a_change_to_codex_config_is_refused_at_commit(self) -> None:
        rig = R.Rig(self, scenario=[[{"run": "mkdir -p .codex && echo 'model = \"x\"' > .codex/config.toml && "
                                              + R.FIX}]])
        job = rig.run_job()
        self.assertEqual(job["stage"], "failed")
        self.assertIn("does not commit .codex/config.toml: protected", job["reason"])

    def test_the_spec_can_name_a_protected_path(self) -> None:
        self.assertIn(".gitattributes", work.spec_paths(Path(self.spec_dir("Add `.gitattributes` for line ends."))))

    def spec_dir(self, text: str) -> Path:
        folder = Path(tempfile.mkdtemp(prefix="sf-spec-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        (folder / "spec.md").write_text(text)
        return folder


class LauncherTests(unittest.TestCase):
    """bin/sf-codex-launch's fixed patterns and the per-job home's check (no Codex needed)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-launch-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.worktree = self.tmp / "worktrees" / "r" / "001-x"
        self.worktree.mkdir(parents=True)
        for name in ("out", "tmp", "shell"):
            (self.tmp / "work" / "runs" / "r--001-x" / "02-builder" / name).mkdir(parents=True)
        self.settings = {"v": 1, "binary": "/bin/true", "worktrees": str(self.tmp / "worktrees"),
                         "work_root": str(self.tmp / "work"),
                         "providers": [{"base_url": "http://127.0.0.1:18080/v1", "env_key": "STRATA_API_KEY",
                                        "key_file": None}]}
        self.out = self.tmp / "work" / "runs" / "r--001-x" / "02-builder" / "out"

    def args(self, *extra: str, tail: list[str] | None = None) -> list[str]:
        return ["exec", "--json", "--strict-config", "--ignore-rules", "-m", "m", "-C", str(self.worktree),
                "--add-dir", str(self.out), *extra, *(tail or ["-"])]

    def test_only_the_presets_shape_is_accepted(self) -> None:
        good = self.args()
        self.assertEqual(L.check_codex_args(good, model="m", worktree=self.worktree, out=self.out), good)
        resume = self.args("resume", "0199c000-0000-7000-8000-00000000000c")
        L.check_codex_args(resume, model="m", worktree=self.worktree, out=self.out)
        for bad in (self.args("-s", "danger-full-access"), self.args("-c", 'sandbox_mode="workspace-write"'),
                    self.args("--dangerously-bypass-approvals-and-sandbox"), self.args("--profile", "x"),
                    self.args(tail=["prompt as an argument"]), ["exec", "--json", "-m", "m", "-"],
                    self.args("--enable", "hooks")):
            with self.subTest(bad=bad):
                with self.assertRaises(L.Refused):
                    L.check_codex_args(bad, model="m", worktree=self.worktree, out=self.out)
        with self.assertRaisesRegex(L.Refused, "-C must be"):
            L.check_codex_args(self.args()[:6] + ["-C", "/etc"] + self.args()[8:], model="m", worktree=self.worktree,
                               out=self.out)

    def test_the_worktree_and_the_run_are_fixed_by_the_job(self) -> None:
        self.assertEqual(L.worktree_of(self.settings, "r/001-x", str(self.worktree)), self.worktree.resolve())
        with self.assertRaises(L.Refused):
            L.worktree_of(self.settings, "r/001-x", str(self.tmp / "worktrees" / "r" / "002-y"))
        with self.assertRaises(L.Refused):
            L.job_parts("../etc")
        with self.assertRaises(L.Refused):
            L.run_dir_of(self.settings, "r/001-x", "../../x")
        os.rmdir(self.out)
        os.symlink(self.tmp, self.out)
        with self.assertRaisesRegex(L.Refused, "symlink|not a directory"):
            L.run_folders(self.settings, "r/001-x", "02-builder")

    def test_under_sudo_only_the_root_owned_settings_are_read(self) -> None:
        with mock.patch.dict(os.environ, {"SUDO_UID": "1000", "STRATA_API_KEY": "from-the-caller"}):
            with self.assertRaisesRegex(L.Refused, "under sudo the launcher reads only"):
                L.load_settings(str(self.tmp / "settings.json"))
            with self.assertRaisesRegex(L.Refused, "has no key file, and under sudo the launcher never takes a key "
                                                   "from the caller's environment"):
                L.provider_key(self.settings, "http://127.0.0.1:18080/v1")
        with self.assertRaisesRegex(L.Refused, "not one the launcher's settings allow"):
            L.provider_key(self.settings, "https://api.example.invalid/v1")

    def test_the_key_comes_from_its_file_or_the_run_is_refused(self) -> None:
        key_file = self.tmp / "codex-key.env"
        settings = {**self.settings, "providers": [{**self.settings["providers"][0], "key_file": str(key_file)}]}
        url = "http://127.0.0.1:18080/v1"
        with self.assertRaisesRegex(L.Refused, f"the key file {key_file} cannot be read by the Codex user"):
            L.provider_key(settings, url)   # missing, or unreadable to the Codex user: never "unused"
        key_file.write_text("OTHER_KEY=x\nSTRATA_API_KEY=\n")
        with self.assertRaisesRegex(L.Refused, f"the key file {key_file} does not define STRATA_API_KEY"):
            L.provider_key(settings, url)
        key_file.write_text("export STRATA_API_KEY='k-0123456789'\n")
        self.assertEqual(L.provider_key(settings, url), ("STRATA_API_KEY", "k-0123456789"))
        plain = {name: value for name, value in os.environ.items() if name != "SUDO_UID"}
        with mock.patch.dict(os.environ, {**plain, "STRATA_API_KEY": ""}, clear=True):
            self.assertEqual(L.provider_key(self.settings, url), ("STRATA_API_KEY", "unused"),
                             "no key file and no Codex user: a keyless local server")
        home = self.tmp / "home"
        self.assertIn(str(key_file), L.deny_list(settings, [], home), "the commands never read the route's key")

    def test_the_provider_must_be_loopback(self) -> None:
        path = self.tmp / "s.json"
        path.write_text(json.dumps({**self.settings, "providers": [{"base_url": "http://10.0.0.5:8080/v1",
                                                                   "env_key": "K"}]}))
        with self.assertRaisesRegex(L.Refused, "loopback"):
            L.load_settings(str(path))

    def test_the_environment_and_path(self) -> None:
        folders = L.run_folders(self.settings, "r/001-x", "02-builder")
        env = L.codex_env(self.settings, folders, self.tmp / "home", "STRATA_API_KEY", "k")
        self.assertEqual(sorted(env), ["CODEX_HOME", "HOME", "LANG", "PATH", "STRATA_API_KEY", "TMPDIR"])
        self.assertTrue(env["PATH"].startswith("/usr/bin"), "Codex uses the first bwrap on its PATH")
        with self.assertRaisesRegex(L.Refused, "must start with /usr/bin"):
            L.codex_env({**self.settings, "path": "/opt/codex/resources:/usr/bin"}, folders, self.tmp, "K", "k")

    def test_the_home_refuses_anything_but_codexs_own_state(self) -> None:
        home = self.tmp / "home"
        text = L.config_text(model="m", base_url="http://127.0.0.1:18080/v1", env_key="K", deny=["/x"])
        L.prepare(home, text)
        for name in ("sessions/2026/10/26", "skills/.system/x", "shell_snapshots", "thread-writer-locks", "tmp"):
            (home / name).mkdir(parents=True, exist_ok=True)
        for name in ("state_5.sqlite", "state_5.sqlite-wal", "installation_id",
                     "sessions/2026/10/26/rollout-1.jsonl"):
            (home / name).write_text("x")
        L.prepare(home, text)
        self.assertFalse((home / "skills" / ".system").exists(), "Codex writes its system skills again itself")
        for planted, kind in (("rules/default.rules", "file"), ("AGENTS.md", "file"), ("skills/evil/SKILL.md", "file"),
                              ("hooks.json", "file"), ("auth.json", "file"), ("work.config.toml", "file"),
                              ("odd", "file")):
            with self.subTest(planted):
                path = home / planted
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x")
                with self.assertRaises(L.HomeError):
                    L.prepare(home, text)
                path.unlink()
                if path.parent != home and path.parent.name != ".system" and not any(path.parent.iterdir()):
                    path.parent.rmdir()
        (home / "config.toml").write_text(text + '\nsandbox_mode = "danger-full-access"\n')
        with self.assertRaisesRegex(L.HomeError, "changed since the launcher wrote it"):
            L.prepare(home, text)

    def test_the_report_names_a_config_toml_changed_during_the_run(self) -> None:
        home = self.tmp / "home"
        L.prepare(home, L.config_text(model="m", base_url="http://127.0.0.1:18080/v1", env_key="K", deny=["/x"]))
        clean = L.report(home)
        self.assertTrue(clean["verified"], clean)
        self.assertEqual(clean["config_sha256_now"], clean["config_sha256"])
        with open(home / "config.toml", "a") as handle:   # what exec did at the pin to a trust root it did not know
            handle.write('\n[projects."/srv/real"]\ntrust_level = "trusted"\n')
        changed = L.report(home)
        self.assertFalse(changed["verified"], "the receipt never calls a changed home verified")
        self.assertIn("config.toml (changed since the launcher wrote it)", changed["problems"])
        self.assertNotEqual(changed["config_sha256_now"], changed["config_sha256"])

    def test_a_fifo_is_refused_at_once_never_waited_on(self) -> None:
        fifo = self.tmp / "build.md"
        os.mkfifo(fifo, 0o600)
        done: list = []

        def read() -> None:
            try:
                L._read_regular(fifo)
            except L.HomeError as exc:
                done.append(str(exc))

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(5)
        self.assertFalse(reader.is_alive(), "opening a FIFO with no writer never blocks the reader")
        self.assertEqual(done, ["build.md is not a regular file"])

    def test_the_template_is_pinned_and_has_no_sandbox_mode(self) -> None:
        self.assertEqual(L.TEMPLATE_SHA256, L.TEMPLATE_PIN, "a template change is a reviewed change (T19 again)")
        text = L.config_text(model="m", base_url="http://127.0.0.1:18080/v1", env_key="K",
                             deny=['/a "quoted" path', "/b"])
        self.assertNotIn("sandbox_mode", text)
        self.assertNotIn("sandbox_workspace_write", text)
        self.assertIn('default_permissions = "factory_worker"', text)
        self.assertIn('extends = ":workspace"', text)
        self.assertIn('":slash_tmp" = "read"', text)
        self.assertIn('"/a \\"quoted\\" path" = "deny"', text)
        self.assertIn('inherit = "core"', text)
        untrusted = L.config_text(model="m", base_url="http://127.0.0.1:18080/v1", env_key="K", deny=[],
                                  untrusted=["/repo", "/worktrees/r/001-x"])
        self.assertIn('[projects."/repo"]\ntrust_level = "untrusted"', untrusted)
        self.assertNotIn('"trusted"', untrusted)
        self.assertIn('include_only = ["PATH", "HOME", "LANG", "TMPDIR"]', text)
        for setting in ('web_search = "disabled"', "check_for_update_on_startup = false", 'persistence = "none"',
                        "memories = false", 'wire_api = "responses"', 'approval_policy = "never"'):
            self.assertIn(setting, text)

    def test_the_deny_list_names_every_other_job_one_by_one(self) -> None:
        rig_tmp = Path(tempfile.mkdtemp(prefix="sf-deny-"))
        self.addCleanup(shutil.rmtree, rig_tmp, ignore_errors=True)
        from swfactory.config import Config

        config = Config(rig_tmp / "conf" / "config.json", {"data_dir": str(rig_tmp / "data")})
        (rig_tmp / "conf").mkdir()
        data = rig_tmp / "data"
        for rel in ("jobs/r/001-x", "jobs/r/002-y", "jobs/q/001-z", "worktrees/r/001-x", "worktrees/r/002-y",
                    "chats/c-1", "browser/grants", "codex/homes/r--001-x", "codex/homes/r--002-y",
                    "codex/runs/r--002-y", "receipts/r"):
            (data / rel).mkdir(parents=True)
        deny = work.deny_paths(config, {"repo": "r", "id": "001-x"})
        for denied in ("jobs/r/002-y", "jobs/q", "worktrees/r/002-y", "chats", "browser", "receipts",
                       "codex/homes/r--002-y", "codex/runs/r--002-y"):
            self.assertIn(str(data / denied), deny)
        for kept in ("jobs/r/001-x", "worktrees/r/001-x", "jobs", "worktrees", "codex/homes/r--001-x"):
            self.assertNotIn(str(data / kept), deny)
        self.assertIn(str(config.path.parent), deny)


class RunFolderTests(unittest.TestCase):
    """The run's folders: only out/, tmp/ and shell/ are the group's to write, a swapped folder is never followed by
    the harness's deletes or reads, build.md is never waited on, and a job folder's share ends with its run."""

    def setUp(self) -> None:
        from swfactory.config import Config

        self.tmp = Path(tempfile.mkdtemp(prefix="sf-runs-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.group = grp.getgrgid(os.getgid()).gr_name   # the harness's own group: chown to it needs no root
        self.config = Config(self.tmp / "conf" / "config.json", {"data_dir": str(self.tmp / "data"),
                                                                  "codex": {"group": self.group}})
        self.job_dir = self.tmp / "data" / "jobs" / "r" / "001-x"
        self.job_dir.mkdir(parents=True)
        self.job = {"repo": "r", "id": "001-x", "job_dir": str(self.job_dir),
                    "worktree": str(self.tmp / "data" / "worktrees" / "r" / "001-x")}

    def test_only_out_tmp_and_shell_are_the_groups_to_write(self) -> None:
        root = work.work_root(self.config)
        (root / "runs").mkdir(parents=True)
        os.chmod(root / "runs", 0o2770)   # what an older block c2 made
        folders = work.run_folders(self.config, self.job, "02-builder")
        for part in (root, root / "runs", root / "runs" / "r--001-x", folders["base"]):
            with self.subTest(part=str(part)):
                self.assertEqual(os.lstat(part).st_mode & 0o022, 0, "the group never renames or replaces it")
        for name in ("out", "tmp", "shell"):
            self.assertEqual(stat.S_IMODE(os.lstat(folders[name]).st_mode), 0o2770)

    def plant_victim(self) -> tuple[dict, Path]:
        folders = work.run_folders(self.config, self.job, "02-builder")
        victim = self.tmp / "victim"
        for name in ("tmp", "shell", "out"):
            (victim / name).mkdir(parents=True)
            (victim / name / "keep.txt").write_text("the harness user's own\n")
        (victim / "out" / "build.md").write_text("STATUS: READY\nfrom somewhere else\n")
        base = folders["base"]
        base.rename(base.with_name("moved"))
        os.symlink(victim, base)   # a Codex process swapped the run folder for a symlink
        return folders, victim

    def test_a_swapped_run_folder_is_never_followed_by_the_cleanup(self) -> None:
        folders, victim = self.plant_victim()
        work.remove_run_folders(folders)
        for name in ("tmp", "shell"):
            self.assertTrue((victim / name / "keep.txt").is_file(), f"{name} outside the run folders was not deleted")

    def test_a_swapped_run_folder_is_never_read_by_collect(self) -> None:
        folders, _ = self.plant_victim()
        with mock.patch.object(work, "run_launcher", return_value={}):
            found = work.collect(self.config, self.job, folders, self.job_dir)
        self.assertFalse(found["build_md_copied"])
        self.assertFalse((self.job_dir / "build.md").exists())

    def test_a_fifo_named_build_md_never_hangs_collect(self) -> None:
        folders = work.run_folders(self.config, self.job, "02-builder")
        os.mkfifo(folders["out"] / "build.md", 0o666)
        found: list = []
        with mock.patch.object(work, "run_launcher", return_value={}):
            thread = threading.Thread(target=lambda: found.append(work.collect(self.config, self.job, folders,
                                                                               self.job_dir)), daemon=True)
            thread.start()
            thread.join(5)
        self.assertFalse(thread.is_alive(), "collect() returned: the job thread is not stuck on the FIFO")
        self.assertFalse(found[0]["build_md_copied"])
        self.assertFalse((self.job_dir / "build.md").exists())

    def test_build_md_is_copied_from_the_real_run_folder(self) -> None:
        folders = work.run_folders(self.config, self.job, "02-builder")
        (folders["out"] / "build.md").write_text("STATUS: READY\n")
        with mock.patch.object(work, "run_launcher", return_value={}):
            found = work.collect(self.config, self.job, folders, self.job_dir)
        self.assertTrue(found["build_md_copied"])
        self.assertEqual((self.job_dir / "build.md").read_text(), "STATUS: READY\n")
        work.remove_run_folders(folders)
        self.assertEqual(sorted(p.name for p in folders["base"].iterdir()), ["out"], "out/ stays until the job ends")

    @unittest.skipUnless(shutil.which("setfacl") and shutil.which("getfacl"), "setfacl is not installed here")
    def test_the_job_folders_share_ends_with_the_run(self) -> None:
        (self.job_dir / "logs").mkdir()
        (self.job_dir / "spec.md").write_text("spec\n")
        Path(self.job["worktree"]).mkdir(parents=True)
        acl = lambda *paths: subprocess.run(["getfacl", "-pR", *map(str, paths)], capture_output=True,  # noqa: E731
                                            text=True).stdout
        worktree_before = acl(self.job["worktree"])
        work.share_job(self.config, self.job)
        self.assertEqual(acl(self.job["worktree"]), worktree_before, "the run's share never touches the worktree: "
                                                                     "open_worktree did, before the run's start")
        self.assertTrue(work.open_worktree(self.config, self.job))
        self.assertIn(f"default:group:{self.group}:r-x", acl(self.job_dir))
        self.assertIn(f"group:{self.group}:--x", acl(self.job_dir.parent), "jobs/<repo> passable, never listable")
        work.unshare_job(self.config, self.job)
        self.assertNotIn(f"group:{self.group}:", acl(self.job_dir), "nothing in the job folder stays shared")
        (self.job_dir / "logs" / "03-reviewer.jsonl").write_text("written after the run\n")
        self.assertNotIn(f"group:{self.group}:", acl(self.job_dir / "logs" / "03-reviewer.jsonl"))
        self.assertIn(f"group:{self.group}:rwx", acl(self.job["worktree"]), "the worktree stays shared (block c2)")


def getfacl(*paths: Path | str) -> str:
    return subprocess.run(["getfacl", "-pR", *map(str, paths)], capture_output=True, text=True).stdout


class RestartAndJobEndTests(unittest.TestCase):
    """A harness killed during a Codex run never reaches the run's own end (collect, unshare_job): the restart's
    recovery makes what Codex wrote the group's before its reconcile and takes the job folder's share back, the job's
    end takes it back whatever its runs say, the worktree is shared again before the harness removes it, and a run
    that raised still shares it (§3.11)."""

    def setUp(self) -> None:
        from swfactory.config import Config
        from swfactory.events import EventBus
        from swfactory.jobs import JobStore
        from swfactory.orchestrator import Factory
        from tests.helpers import make_repo

        self.tmp = Path(tempfile.mkdtemp(prefix="sf-restart-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.group = grp.getgrgid(os.getgid()).gr_name   # the harness's own group: setfacl to it needs no root
        repo = make_repo(self.tmp, "r")
        self.config = Config(self.tmp / "conf" / "config.json", {
            "data_dir": str(self.tmp / "data"), "repos": [{"name": "r", "path": str(repo)}],
            "codex": {"group": self.group}})
        store = JobStore(self.config.data_dir)
        job = store.new_job("r", "fix the greeting", ["code"], 2)
        job.update(stage="build", codex_run={"run": "02-builder", "agent": "builder", "round": 1,
                                             "started": work.utc_now()})   # killed mid-run: its end never came
        self.job = store.save(job)
        self.job_dir = Path(self.job["job_dir"])
        self.factory = Factory(self.config, EventBus())
        self.addCleanup(self.factory.stop)
        self.calls: list[str] = []

    def record(self, name: str, answer=None):
        return mock.patch.object(work, name, side_effect=lambda *a, **k: self.calls.append(name) or answer)

    def restart(self) -> dict:
        self.factory.start_jobs()
        return self.factory.wait("r", self.job["id"], timeout=30)

    def test_the_restart_shares_the_worktree_before_its_reconcile_and_ends_the_job_folders_share(self) -> None:
        with self.record("share_worktree", {"error": "sudo: a password is required"}), self.record("unshare_job"), \
                self.record("reconcile", {"paths": ["notes.txt"], "diff_sha256": "ab"}):
            job = self.restart()
        self.assertEqual(job["stage"], "failed")
        self.assertEqual(job["reconcile"]["paths"], ["notes.txt"])
        self.assertEqual(self.calls, ["share_worktree", "unshare_job", "reconcile"],
                         "what Codex wrote is the group's before the reconcile looks at it")
        events = (self.job_dir / "logs" / "events.jsonl").read_text()
        self.assertIn("could not be shared before its ignored files were counted and the reconcile: sudo: a password "
                      "is required", events, "a count and a reconcile that may miss what Codex wrote say so")

    def test_the_restart_shares_the_worktree_before_it_counts_the_ignored_files(self) -> None:
        """A folder Codex made 0700 is closed to the harness until share-worktree, and git lists past it without
        failing: the count by time comes after the share, so it sees every file the interrupted run left."""
        listed = mock.patch("swfactory.orchestrator.gitops.ignored_files",
                            side_effect=lambda *a: self.calls.append("ignored_files") or {"build/gen.py": (1, 0, 0)})
        with self.record("share_worktree", {}), self.record("unshare_job"), self.record("reconcile", {"paths": []}), \
                listed:
            job = self.restart()
        self.assertEqual(self.calls[:2], ["share_worktree", "ignored_files"], self.calls)
        self.assertEqual(job["codex_ignored"], ["build/gen.py"], "counted by time: the run's start is unknown")

    @unittest.skipUnless(shutil.which("setfacl") and shutil.which("getfacl"), "setfacl is not installed here")
    def test_a_share_a_killed_harness_left_ends_at_the_restart(self) -> None:
        Path(self.job["worktree"]).mkdir(parents=True)
        work.share_job(self.config, self.job)   # the run started; the harness was killed before the run's end
        self.assertIn(f"default:group:{self.group}:r-x", getfacl(self.job_dir))
        self.restart()
        self.assertNotIn(f"group:{self.group}:", getfacl(self.job_dir), "nothing in the job folder stays shared")
        log = self.job_dir / "logs" / "03-reviewer.jsonl"
        log.parent.mkdir(exist_ok=True)
        log.write_text("written after the restart\n")
        self.assertNotIn(f"group:{self.group}:", getfacl(log))

    def test_a_job_stopped_mid_run_and_then_killed_is_recovered_at_the_restart(self) -> None:
        """Stop sets cancelled at once and leaves the run's wind-down to _drive; a harness killed in that window
        never reached it. The restart records the run unknown, takes the share back and ends the job; a later
        reject shares the worktree before removing it and removes the job's Codex home."""
        from swfactory.jobs import JobStore

        JobStore(self.config.data_dir).save({**self.job, "stage": "cancelled", "reason": "cancelled by a human"})
        with self.record("share_worktree", {}), self.record("unshare_job"), self.record("reconcile", {"paths": []}), \
                self.record("remove_home", {}):
            self.factory.start_jobs()
            job = self.factory.job("r", self.job["id"])
        self.assertEqual(job["stage"], "cancelled")
        self.assertIsNone(job.get("codex_run"))
        self.assertEqual([entry.get("outcome") for entry in job.get("runs") or []], ["unknown"])
        self.assertEqual(self.calls[:3], ["share_worktree", "unshare_job", "reconcile"])
        self.assertIn("remove_home", self.calls, "the job's end that _drive never reached")
        self.calls.clear()
        removed = mock.patch("swfactory.orchestrator.gitops.remove_worktree",
                             side_effect=lambda *a: self.calls.append("remove_worktree"))
        with self.record("share_worktree", {}), self.record("unshare_job"), self.record("remove_home", {}), removed:
            self.factory.reject("r", self.job["id"])
        self.assertEqual(self.calls, ["share_worktree", "remove_worktree", "unshare_job", "remove_home"])

    @unittest.skipUnless(shutil.which("setfacl") and shutil.which("getfacl"), "setfacl is not installed here")
    def test_the_share_of_a_job_stopped_mid_run_ends_at_the_restart(self) -> None:
        from swfactory.jobs import JobStore

        Path(self.job["worktree"]).mkdir(parents=True)
        work.share_job(self.config, self.job)
        JobStore(self.config.data_dir).save({**self.job, "stage": "cancelled"})
        self.factory.start_jobs()
        self.assertNotIn(f"group:{self.group}:", getfacl(self.job_dir), "nothing in the job folder stays shared")

    def test_a_job_whose_codex_run_never_ended_counts_as_one_codex_ran_in(self) -> None:
        self.assertTrue(self.factory._codex_ran({"codex_run": {"run": "02-builder"}, "runs": []}))
        self.assertFalse(self.factory._codex_ran({"codex_run": None, "runs": []}))

    def test_the_jobs_end_shares_the_worktree_before_removing_it_and_ends_the_share(self) -> None:
        with self.record("share_worktree", {}), self.record("reconcile", {"paths": []}):
            self.restart()   # the run's only entry is the recovery's: it names no driver
        self.calls.clear()
        removed = mock.patch("swfactory.orchestrator.gitops.remove_worktree",
                             side_effect=lambda *a: self.calls.append("remove_worktree"))
        with self.record("share_worktree", {}), self.record("unshare_job"), self.record("remove_home", {}), removed:
            job = self.factory.reject("r", self.job["id"])
        self.assertEqual(job["stage"], "rejected")
        self.assertEqual(self.calls, ["share_worktree", "remove_worktree", "unshare_job", "remove_home"])

    def test_a_job_codex_never_ran_in_is_left_to_the_harness(self) -> None:
        from swfactory.jobs import JobStore

        job = JobStore(self.config.data_dir).save({**self.job, "stage": "failed", "codex_run": None, "runs": [
            {"run": "02-builder", "agent": "builder", "driver": "builtin", "ok": False}]})
        with self.record("share_worktree", {}), self.record("unshare_job"), self.record("remove_home", {}):
            self.factory.reject("r", job["id"])
        self.assertEqual(self.calls, ["unshare_job"], "no launcher call for it; the share's take-back is a no-op")

    def test_a_codex_run_whose_driver_left_the_configuration_still_counts(self) -> None:
        from swfactory.jobs import JobStore

        job = JobStore(self.config.data_dir).save({**self.job, "stage": "failed", "codex_run": None, "runs": [
            {"run": "02-builder", "agent": "builder", "driver": "old-codex", "ok": False,
             "codex": {"version": "0.130.0"}}]})
        removed = mock.patch("swfactory.orchestrator.gitops.remove_worktree",
                             side_effect=lambda *a: self.calls.append("remove_worktree"))
        with self.record("share_worktree", {}), self.record("unshare_job"), self.record("remove_home", {}), removed:
            self.factory.reject("r", job["id"])
        self.assertEqual(self.calls, ["share_worktree", "remove_worktree", "unshare_job", "remove_home"])

    def test_a_run_that_raised_still_shares_the_worktree(self) -> None:
        from types import SimpleNamespace

        from swfactory.config import Config
        from swfactory.runners import make_runner

        config = Config(self.tmp / "conf" / "config.json", {**self.config.raw, "codex": {
            "enabled": True, "binary": "/opt/codex", "user": "ai-codex", "group": self.group}})
        runner = make_runner(config, "codex-worker")
        ctx = SimpleNamespace(job=self.job, log_path=self.job_dir / "logs" / "02-builder.jsonl",
                              cwd=Path(self.job["worktree"]), job_dir=self.job_dir, codex_job=True,
                              model=SimpleNamespace(provider=SimpleNamespace(base_url="http://127.0.0.1:9/v1")))
        folders = {name: self.tmp / name for name in ("base", "out", "tmp", "shell")}
        quiet = {name: mock.patch.object(work, name, return_value=value) for name, value in (
            ("run_folders", folders), ("launcher_prefix", ["sf-codex-launch"]), ("deny_paths", []),
            ("share_job", None), ("launcher_env", {}), ("uses_sudo", True))}
        for patcher in quiet.values():
            patcher.start()
            self.addCleanup(patcher.stop)
        with mock.patch.object(runner, "command_line", return_value=["exec"]), \
                mock.patch.object(runner, "model_for", return_value="m"), \
                mock.patch.object(runner, "_stdin_text", return_value="p"), \
                self.record("share_worktree", {}), self.record("remove_run_folders"), self.record("unshare_job"), \
                self.record("collect", {}):
            with mock.patch.object(runner, "_execute", side_effect=RuntimeError("a bug mid-run")):
                with self.assertRaises(RuntimeError):
                    runner._launched(ctx, "p", "s", resume=False)
            self.assertEqual(self.calls, ["share_worktree", "remove_run_folders", "unshare_job"])
            self.calls.clear()
            with mock.patch.object(runner, "_execute", return_value=SimpleNamespace(details={})):
                runner._launched(ctx, "p", "s", resume=False)
            self.assertEqual(self.calls, ["collect", "remove_run_folders", "unshare_job"],
                             "after a run that returned, collect() shares it (once)")


def _make_chain(top: Path, depth: int) -> list[Path]:
    """top and a chain of depth folders below it, made one by one (os.makedirs and Path.mkdir recurse)."""
    top.parent.mkdir(parents=True, exist_ok=True)
    chain = [top]
    os.mkdir(top)
    for _ in range(depth):
        chain.append(chain[-1] / "d")
        os.mkdir(chain[-1])
    return chain


def _remove_chain(chain: list[Path]) -> None:
    """A folder chain deeper than the recursion limit, removed from the bottom (shutil.rmtree recursed before
    Python 3.12)."""
    for path in reversed(chain):
        with contextlib.suppress(OSError):
            for entry in os.listdir(path):
                os.unlink(path / entry)
        with contextlib.suppress(OSError):
            os.rmdir(path)


def walk_modes(*roots: Path) -> dict[str, int]:
    out = {}
    for root in roots:
        for top, dirs, files in os.walk(root):
            for name in (".", *dirs, *files):
                path = os.path.normpath(os.path.join(top, name))
                out[path] = stat.S_IMODE(os.lstat(path).st_mode)
    return out


class DataFolderPrivacyTests(unittest.TestCase):
    """Block c2 lets the Codex user's group pass through the data folder, so what is in it must be the harness's
    alone: the harness runs with umask 077 under its lock, and seals once what older versions left 0755/0644."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-private-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(os.umask, os.umask(0o022))   # the default the harness's unit had

    def test_under_the_lock_chats_and_job_logs_are_the_harnesss_alone(self) -> None:
        from swfactory.__main__ import factory_lock
        from swfactory.chats import next_log_path
        from swfactory.jobs import JobStore

        data = self.tmp / "data"
        with factory_lock(data):
            store = JobStore(data)
            job_id, job_dir = store.allocate("r", "fix the greeting")
            store.next_log_path({"repo": "r", "id": job_id}, "builder")
            next_log_path(data / "chats" / "c-0123456789" / "logs", "turn")
        modes = walk_modes(data / "chats", data / "jobs")
        self.assertGreaterEqual(len(modes), 8)
        for path, mode in modes.items():
            self.assertEqual(mode & 0o077, 0, f"{path} is {oct(mode)}")

    def test_what_older_versions_left_is_sealed_once_and_the_shared_folders_are_kept(self) -> None:
        from swfactory.config import Config

        data = self.tmp / "data"
        config = Config(self.tmp / "config.json", {"data_dir": str(data)})
        log = data / "chats" / "c-0123456789" / "logs" / "01-turn.jsonl"
        job_log = data / "jobs" / "r" / "001-x" / "logs" / "01-builder.jsonl"
        shared = data / "worktrees" / "r" / "001-x" / "app.py"
        runs = work.work_root(config) / "runs"
        outside = self.tmp / "outside.txt"
        for path in (log, job_log, shared, outside):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x\n")
        runs.mkdir(parents=True)
        os.chmod(runs, 0o2750)
        os.symlink(outside, data / "chats" / "link")
        if shutil.which("setfacl"):   # block c2's traverse entry on jobs/ survives the sweep
            subprocess.run(["setfacl", "-m", f"g:{grp.getgrgid(os.getgid()).gr_name}:x", str(data / "jobs")],
                           check=True)
        self.assertGreater(work.seal_data_dir(data, work.shared_dirs(config)), 0)
        for path, mode in walk_modes(data / "chats", data / "jobs").items():
            if os.path.islink(path):
                continue
            self.assertEqual(mode & (0o067 if os.path.isdir(path) else 0o077), 0, f"{path} is {oct(mode)}")
        self.assertEqual(stat.S_IMODE(os.lstat(data / "jobs").st_mode) & 0o010, 0o010,
                         "a folder keeps its group x (the traverse entry of block c2 lives there)")
        self.assertEqual(stat.S_IMODE(os.stat(shared).st_mode), 0o644, "the worktrees are block c2's, never swept")
        self.assertEqual(stat.S_IMODE(os.stat(runs).st_mode), 0o2750, "and so is the work root")
        self.assertEqual(stat.S_IMODE(os.stat(outside).st_mode), 0o644, "a symlink is never followed")
        self.assertTrue((data / work.SEALED_MARK).exists())
        os.chmod(log, 0o644)
        self.assertIsNone(work.seal_data_dir(data, work.shared_dirs(config)), "once per data folder")
        self.assertEqual(stat.S_IMODE(os.stat(log).st_mode), 0o644)

    def test_a_marker_of_an_older_sweep_sweeps_again(self) -> None:
        data = self.tmp / "data"
        log = data / "chats" / "c-0123456789" / "logs" / "01-turn.jsonl"
        log.parent.mkdir(parents=True)
        log.write_text("x\n")
        (data / work.SEALED_MARK).write_text("")   # the first version's marker: empty
        self.assertGreater(work.seal_data_dir(data), 0)
        self.assertEqual(stat.S_IMODE(os.stat(log).st_mode), 0o600)
        self.assertEqual((data / work.SEALED_MARK).read_text().strip(), work.SEAL_VERSION)

    @unittest.skipUnless(shutil.which("setfacl") and shutil.which("getfacl"), "setfacl is not installed here")
    def test_an_older_versions_share_of_a_job_folder_is_taken_off_at_the_first_start(self) -> None:
        """fdd20f0 shared a job folder with `setfacl -R -m g:G:rX -m d:g:G:rX` and never took it back. Its default
        entries would hand every log the harness writes there later to the group, whatever the umask."""
        from swfactory.__main__ import factory_lock
        from swfactory.events import EventBus

        group = grp.getgrgid(os.getgid()).gr_name
        data = self.tmp / "data"
        job_dir = data / "jobs" / "app" / "001-old"
        (job_dir / "logs").mkdir(parents=True)   # under umask 022, as the old unit ran
        (job_dir / "logs" / "01-builder.jsonl").write_text("{}\n")
        subprocess.run(["setfacl", "-R", "-m", f"g:{group}:rX", "-m", f"d:g:{group}:rX", str(job_dir)], check=True)
        self.assertIn("default:", getfacl(job_dir / "logs"))
        with factory_lock(data):   # the first start of this version: umask 077, sealed
            log = job_dir / "logs" / "02-reviewer.jsonl"
            EventBus().publish("log", {"message": "after the upgrade"}, repo="app", job="001-old", log_path=log)
        for path in (job_dir, job_dir / "logs", log, job_dir / "logs" / "01-builder.jsonl"):
            acl = getfacl(path)
            self.assertNotIn(f"group:{group}:", acl, f"{path} keeps the old share: {acl}")
            self.assertNotIn("default:", acl, f"{path} keeps a default ACL: {acl}")
            self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode) & 0o077 & ~(0o010 if path.is_dir() else 0), 0,
                             f"{path} is {oct(os.lstat(path).st_mode)}")

    def test_a_tree_deeper_than_the_recursion_limit_is_sealed(self) -> None:
        """os.walk recursed before Python 3.12: a chain of folders an agent made deeper than the recursion limit
        must never stop the harness's start."""
        import sys

        data = self.tmp / "data"
        depth = sys.getrecursionlimit() + 100
        chain = _make_chain(data / "jobs" / "r" / "j1", depth)
        self.addCleanup(_remove_chain, chain)
        (chain[-1] / "deep.txt").write_text("x\n")
        self.assertGreater(work.seal_data_dir(data), depth)
        self.assertEqual(stat.S_IMODE(os.stat(chain[-1] / "deep.txt").st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(chain[-1]).st_mode), 0o710, "a folder keeps its group x")
        self.assertEqual((data / work.SEALED_MARK).read_text().strip(), work.SEAL_VERSION)

    def test_share_worktree_shares_a_tree_deeper_than_the_recursion_limit(self) -> None:
        import sys

        worktrees = self.tmp / "worktrees"
        chain = _make_chain(worktrees / "r" / "001-x", sys.getrecursionlimit() + 100)
        self.addCleanup(_remove_chain, chain)
        for part in chain:
            os.chmod(part, 0o700)
        settings = {"worktrees": str(worktrees), "work_root": str(self.tmp / "work")}
        with mock.patch("sys.stdout"):
            L.do_share_worktree(settings, {"job": "r/001-x", "worktree": str(chain[0])})
        self.assertEqual(stat.S_IMODE(os.stat(chain[-1]).st_mode), 0o770)


def _as_root_with_users() -> str | None:
    if os.geteuid() != 0:
        return "needs root to act as the Codex user (nobody) and as a non-root harness (daemon)"
    for tool in ("setfacl", "getfacl", "setpriv", "git"):
        if shutil.which(tool) is None:
            return f"{tool} is not installed here"
    try:
        grp.getgrnam(R.NOBODY_GROUP)
        pwd.getpwnam("nobody")
        pwd.getpwnam("daemon")
    except KeyError:
        return "the users nobody and daemon and the group nogroup are needed"
    return None


AS_USERS = _as_root_with_users()
PROBE_AS_CODEX = """
import json, os, sys
def can(do):
    try:
        do()
        return True
    except OSError:
        return False
p = json.loads(sys.argv[1])
print(json.dumps({
    "worktree_write": can(lambda: open(os.path.join(p["worktree"], "w.txt"), "w").close()),
    "out_write": can(lambda: open(os.path.join(p["out"], "build.md"), "w").close()),
    "spec_read": can(lambda: open(p["spec"]).read()),
    "base_write": can(lambda: open(os.path.join(p["base"], "x"), "w").close()),
    "data_list": can(lambda: os.listdir(p["data"])),
    "chats_list": can(lambda: os.listdir(p["chats"])),
    "chat_read": can(lambda: open(p["chat_log"]).read()),
    "jobs_list": can(lambda: os.listdir(p["jobs"])),
    "repo_jobs_list": can(lambda: os.listdir(p["repo_jobs"])),
}))
"""


@unittest.skipUnless(AS_USERS is None, AS_USERS or "")
class AsTheCodexUserTests(unittest.TestCase):
    """Without Codex, with real users: block c2 exactly as printed (nogroup standing in for the group), the Codex
    user's own reads and writes as nobody, and the harness's git as a non-root member of the group (daemon)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-as-users-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.chmod(self.tmp, 0o755)
        from swfactory.config import Config

        if S.closed_ancestors(Config(self.tmp / "c.json", {"data_dir": str(self.tmp)})):
            self.skipTest("the temporary folder's own parents are closed to other users here")
        self.addCleanup(os.umask, os.umask(0o077))   # as the harness runs under its lock
        self.nobody = pwd.getpwnam("nobody")
        self.gid = grp.getgrnam(R.NOBODY_GROUP).gr_gid

    def as_codex(self, argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(argv, user=self.nobody.pw_uid, group=self.gid, extra_groups=[], cwd=cwd,
                              capture_output=True, text=True, timeout=60, env={"PATH": "/usr/bin:/bin"})

    def apply_c2(self, config) -> None:
        """Block c2 as `factory codex setup` prints it, sudo dropped (this test is root), on its own folders only."""
        [c2] = [b for b in S.blocks(config) if b["id"] == "c2"]
        for line in c2["commands"]:
            argv = shlex.split(line)
            self.assertEqual(argv[0], "sudo")
            argv = argv[3:] if argv[1] == "-u" else argv[1:]   # the harness's user is this test's (root)
            outside = [a for a in argv if a.startswith("/") and not (a + "/").startswith(str(self.tmp) + "/")]
            if outside:
                self.skipTest(f"block c2 names {outside} outside this test's folder")
            subprocess.run(argv, check=True, capture_output=True)

    def test_block_c2_lets_the_codex_user_reach_its_job_and_nothing_else(self) -> None:
        from swfactory.__main__ import factory_lock
        from swfactory.chats import next_log_path
        from swfactory.config import Config
        from swfactory.jobs import JobStore

        home = self.tmp / "home"   # a 0700 folder above the data folder, as /var/lib/seraphina is on the server
        home.mkdir(mode=0o700)
        data = home / "data"
        config = Config(self.tmp / "conf" / "config.json", {"data_dir": str(data), "codex": {
            "user": "nobody", "group": R.NOBODY_GROUP, "via": "setpriv"}})
        with factory_lock(data, keep=work.shared_dirs(config)):   # the harness's start: umask 077, sealed
            (data / "jobs").mkdir()
            (data / "worktrees").mkdir()
            chat_log = next_log_path(data / "chats" / "c-0123456789" / "logs", "turn")
            chat_log.write_text('{"text": "a persona chat"}\n')
        self.apply_c2(config)
        store = JobStore(data)
        job_id, job_dir = store.allocate("r", "fix the greeting")
        (job_dir / "spec.md").write_text("# Spec\n")
        worktree = data / "worktrees" / "r" / job_id
        worktree.mkdir(parents=True, mode=0o777)   # as git worktree add makes it (the default ACL decides)
        job = {"repo": "r", "id": job_id, "job_dir": str(job_dir), "worktree": str(worktree)}
        folders = work.run_folders(config, job, "02-builder")
        work.open_worktree(config, job)   # the run's start: the orchestrator's, then the driver's
        work.share_job(config, job)
        paths = {"worktree": str(worktree), "out": str(folders["out"]), "spec": str(job_dir / "spec.md"),
                 "base": str(folders["base"]), "data": str(data), "chats": str(data / "chats"),
                 "chat_log": str(chat_log), "jobs": str(data / "jobs"), "repo_jobs": str(data / "jobs" / "r")}
        done = self.as_codex([R.python(), "-I", "-c", PROBE_AS_CODEX, json.dumps(paths)])
        found = json.loads(done.stdout or "{}")
        self.assertEqual(found, {"worktree_write": True, "out_write": True, "spec_read": True, "base_write": False,
                                 "data_list": False, "chats_list": False, "chat_read": False, "jobs_list": False,
                                 "repo_jobs_list": False}, done.stderr)
        work.unshare_job(config, job)
        done = self.as_codex([R.python(), "-I", "-c", PROBE_AS_CODEX, json.dumps(paths)])
        self.assertFalse(json.loads(done.stdout)["spec_read"], "the job folder's share ended with the run")

    def mixed_worktree(self, job: dict | None = None, run_start=None):
        """The harness (daemon, a non-root member of the group) checks a job's branch out, run_start(config) runs
        (the run's start: work.open_worktree and work.share_job), then Codex (nobody) appends to app.py, writes
        notes.txt through a temporary name (0600) and makes cache/ 0700. Returns the config, the repository, the
        worktree and how to run the harness's git as daemon (harness_git)."""
        from swfactory.config import Config
        from tests.helpers import make_repo

        harness = pwd.getpwnam("daemon")
        repo = make_repo(self.tmp, "r", {"app.py": "print('hi')\n"})
        hhome = self.tmp / "hhome"
        bin_dir = self.tmp / "codex-bin"
        for folder in (hhome, bin_dir):
            folder.mkdir()
            os.chmod(folder, 0o755)   # explicit: this test runs with umask 077
        shutil.copy(L.__file__, bin_dir / "sf-codex-launch")
        os.chmod(bin_dir / "sf-codex-launch", 0o755)
        data = self.tmp / "data"
        (data / "worktrees").mkdir(parents=True)
        config = Config(self.tmp / "conf" / "config.json", {"data_dir": str(data), "repos": [
            {"name": "r", "path": str(repo)}], "codex": {"user": "nobody", "group": R.NOBODY_GROUP, "via": "setpriv",
                                                         "launcher": str(bin_dir / "sf-codex-launch"),
                                                         "work_root": str(self.tmp / "work")}})
        self.apply_c2(config)
        for path in (repo, hhome, data):
            subprocess.run(["chown", "-R", f"{harness.pw_uid}:{harness.pw_gid}", str(path)], check=True)
        pkg = self.tmp / "pkg" / "swfactory"   # what the harness's git runs, readable by its non-root user
        (pkg / "harness").mkdir(parents=True)
        for rel in ("__init__.py", "gitops.py", "events.py", "harness/__init__.py", "harness/sandbox.py"):
            shutil.copy(Path(work.__file__).parents[1] / rel, pkg / rel)
        subprocess.run(["chmod", "-R", "a+rX", str(self.tmp / "pkg")], check=True)
        worktree = Path(job["worktree"]) if job else data / "worktrees" / "r" / "001-fix"
        branch = job["branch"] if job else "factory/001-fix"
        env = {"PATH": "/usr/bin:/bin", "HOME": str(hhome)}
        as_harness = {"user": harness.pw_uid, "group": harness.pw_gid, "extra_groups": [self.gid], "env": env,
                      "capture_output": True, "text": True, "timeout": 120}
        made = subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", branch, str(worktree)],
                              **as_harness)
        self.assertEqual(made.returncode, 0, made.stderr)
        if run_start:
            run_start(config)
        wrote = self.as_codex(["sh", "-c", "echo 'half a fix' >> app.py && t=$(mktemp ./.notes.XXXXXX) && "
                                           "echo new > \"$t\" && mv \"$t\" notes.txt && mkdir -m 700 cache && "
                                           "echo c > cache/data.txt"], cwd=worktree)
        self.assertEqual(wrote.returncode, 0, wrote.stderr)
        self.assertEqual(os.stat(worktree / "notes.txt").st_uid, self.nobody.pw_uid)
        closed = subprocess.run(["cat", str(worktree / "notes.txt")], **as_harness)
        self.assertNotEqual(closed.returncode, 0, "a 0600 file under the group's ACL is closed to the harness")

        def harness_git(*steps: str) -> dict:
            script = ("import json, sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; "
                      "from swfactory import gitops\n"
                      "repo, wt, out = Path(sys.argv[2]), Path(sys.argv[3]), {}\n"
                      "do = {'reconcile': lambda: sorted(gitops.reconcile_diff(wt)['paths']), "
                      "'ignored': lambda: sorted(gitops.ignored_files(wt)), "
                      "'commit': lambda: sorted(gitops.commit_all(wt, 'factory: 001-fix build round 1').paths), "
                      "'remove': lambda: gitops.remove_worktree(repo, wt)}\n"
                      "for step in sys.argv[4:]:\n"
                      "    try:\n        out[step] = do[step]()\n"
                      "    except Exception as exc:\n        out[step] = f'{type(exc).__name__}: {exc}'\n"
                      "print(json.dumps(out))\n")
            done = subprocess.run([R.python(), "-I", "-c", script, str(self.tmp / "pkg"), str(repo), str(worktree),
                                   *steps], **as_harness)
            return json.loads(done.stdout or "{}")
        return config, repo, worktree, harness_git

    def test_the_harness_commits_reconciles_and_removes_what_the_codex_user_wrote(self) -> None:
        """T19's mixed-ownership clause: Codex writes a file through a temporary name (0600) and makes a folder 0700
        in the worktree; after collect() (the launcher's share-worktree, as the Codex user) the harness, a non-root
        member of the group, commits them, reconciles and removes the worktree."""
        config, _, worktree, harness_git = self.mixed_worktree()
        job_dir = self.tmp / "data" / "jobs" / "r" / "001-fix"
        job_dir.mkdir(parents=True)
        job = {"repo": "r", "id": "001-fix", "job_dir": str(job_dir), "worktree": str(worktree)}
        work.collect(config, job, work.run_folders(config, job, "02-builder"), job_dir)   # after every run
        found = harness_git("reconcile", "commit", "remove")
        self.assertEqual(found.get("reconcile"), ["app.py", "cache/data.txt", "notes.txt"], found)
        self.assertEqual(found.get("commit"), ["app.py", "cache/data.txt", "notes.txt"], found)
        self.assertIsNone(found.get("remove", "missing"), found)
        self.assertFalse(worktree.exists(), "the harness removed the worktree, Codex's folders included")

    def test_a_folder_the_codex_user_left_closed_fails_the_listing_and_the_commit(self) -> None:
        """git lists and stages past a folder it cannot open and exits 0. Before share-worktree a folder Codex made
        0700 is closed to the harness, so its ignored-file listing, its reconcile and its commit must fail, never
        come out short; after the share all three see every file."""
        config, _, worktree, harness_git = self.mixed_worktree()
        wrote = self.as_codex(["sh", "-c", "chmod 660 notes.txt && printf 'build/\\n' > .gitignore && "
                                           "mkdir -m 700 build && echo x > build/gen.py"], cwd=worktree)
        self.assertEqual(wrote.returncode, 0, wrote.stderr)
        found = harness_git("ignored", "reconcile", "commit")
        for step in ("ignored", "reconcile", "commit"):
            self.assertIn("could not open directory", str(found.get(step)), found)
        self.assertIn("CommitRefused", found["commit"], found)
        job_dir = self.tmp / "data" / "jobs" / "r" / "001-fix"
        job_dir.mkdir(parents=True)
        job = {"repo": "r", "id": "001-fix", "job_dir": str(job_dir), "worktree": str(worktree)}
        self.assertEqual(work.share_worktree(config, job).get("exit"), 0)
        found = harness_git("ignored", "commit")
        self.assertEqual(found.get("ignored"), ["build/gen.py"], found)
        self.assertEqual(found.get("commit"), [".gitignore", "app.py", "cache/data.txt", "notes.txt"], found)

    def test_after_a_restart_the_harness_reconciles_and_removes_what_the_codex_user_wrote(self) -> None:
        """T16 with a Codex user: the harness was killed mid-run, so neither collect() nor unshare_job ran. The next
        start's recovery shares what Codex wrote before its reconcile and takes the job folder's share back: the
        harness's git, as daemon, sees every file and removes the worktree; the user nobody can read nothing of the
        job folder, nor what the harness writes there afterwards."""
        from swfactory.events import EventBus
        from swfactory.jobs import JobStore
        from swfactory.orchestrator import Factory

        (self.tmp / "data" / "jobs").mkdir(parents=True)
        store = JobStore(self.tmp / "data")
        job = store.new_job("r", "fix the greeting", ["code"], 2)
        job_dir = Path(job["job_dir"])
        (job_dir / "spec.md").write_text("# Spec\n")
        config, _, worktree, harness_git = self.mixed_worktree(
            job, lambda config: (work.open_worktree(config, job), work.share_job(config, job)))
        job = store.save({**job, "stage": "build", "codex_run": {"run": "02-builder", "agent": "builder", "round": 1,
                                                                  "started": work.utc_now()}})
        # the harness is killed here, mid-run: neither collect() nor unshare_job runs
        factory = Factory(config, EventBus())
        self.addCleanup(factory.stop)
        factory.start_jobs()
        job = factory.wait("r", job["id"], timeout=60)
        self.assertEqual((job["stage"], job["reason"]), ("failed", "interrupted (server restarted)"))
        (job_dir / "logs").mkdir(exist_ok=True)
        (job_dir / "logs" / "03-reviewer.jsonl").write_text("written after the restart\n")
        probe = ("import json, os, sys\nout = {}\nfor p in sys.argv[1:]:\n    try:\n"
                 "        out[p] = sorted(os.listdir(p)) if os.path.isdir(p) else open(p).read()\n"
                 "    except OSError:\n        out[p] = None\nprint(json.dumps(out))\n")
        paths = [str(job_dir), str(job_dir / "spec.md"), str(job_dir / "logs" / "03-reviewer.jsonl")]
        done = self.as_codex([R.python(), "-I", "-c", probe, *paths])
        self.assertEqual(json.loads(done.stdout or "{}"), dict.fromkeys(paths), done.stderr)
        found = harness_git("reconcile", "remove")
        self.assertEqual(found.get("reconcile"), ["app.py", "cache/data.txt", "notes.txt"], found)
        self.assertIsNone(found.get("remove", "missing"), found)
        self.assertFalse(worktree.exists(), "a job cancelled after the restart keeps no worktree the harness cannot "
                                            "clean")


class SetupBlockTests(unittest.TestCase):
    """The owner's blocks (O3, O9): printed, never applied; the requirements file is off."""

    def config(self):
        from swfactory.config import Config

        tmp = Path(tempfile.mkdtemp(prefix="sf-setup-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return Config(tmp / "config.json", {"data_dir": str(tmp / "data"), "codex": {"user": "ai-codex",
                                                                                    "launch_settings":
                                                                                    "/etc/seraphina/codex-launch.json"}})

    def test_the_blocks_name_the_user_the_group_the_sudoers_rule_and_the_egress_rule(self) -> None:
        config = self.config()
        text = S.text(config)
        self.assertIn("useradd --system", text)
        self.assertIn("ai-codex", text)
        self.assertIn("setfacl -R -m g:seraphina-work:rwX -m d:g:seraphina-work:rwX", text)
        self.assertIn("visudo -cf /etc/sudoers.d/seraphina-codex.new", text)
        self.assertNotIn("sudo.ws visudo", text, "that runs the one visudo on secure_path, not classic sudo's")
        self.assertIn("nft -c -f", text)
        self.assertIn("never loads it", text)
        self.assertNotIn("install -m 0644 -o root -g root /etc/codex/requirements.toml", text)
        self.assertIn("/etc/codex/requirements.toml is not installed (O9)", text)
        self.assertIn("Accepted by the owner", text)
        rule = S.sudoers_text("factory", "ai-codex")
        self.assertIn("factory ALL=(ai-codex) NOPASSWD: /usr/local/libexec/seraphina/sf-codex-launch\n", rule)
        self.assertNotIn("Defaults", S.sudoers_text("factory", "ai-codex", implementation="sudo-rs"))
        nft = S.nft_text("ai-codex", [("127.0.0.1", 18080)])
        self.assertIn('meta skuid "ai-codex" ip daddr 127.0.0.1 tcp dport 18080 accept', nft)
        self.assertTrue(nft.rstrip().endswith('meta skuid "ai-codex" counter drop\n  }\n}'))

    def test_the_egress_rule_allows_the_codex_routes_model_only(self) -> None:
        from swfactory.config import Config

        tmp = Path(tempfile.mkdtemp(prefix="sf-egress-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        routed = Config(tmp / "config.json", {"data_dir": str(tmp / "data"),
                                              "agents": {"builder": {"driver": "codex-worker", "model": "local"}}})
        self.assertEqual(S.model_ports(routed), [("127.0.0.1", 18080)], "strata, the model the builder runs on")
        nft = S.nft_text("ai-codex", S.model_ports(routed))
        self.assertIn("tcp dport 18080 accept", nft)
        self.assertNotIn("dport 5000", nft, "tabby has a key but no Codex route uses it")
        targets = C.egress_targets(routed)
        self.assertIn("127.0.0.1:5000", targets)
        self.assertIn("127.0.0.1:11434", targets, "keyless loopback providers are probed too")
        self.assertNotIn("127.0.0.1:18080", targets)
        unrouted = Config(tmp / "config.json", {"data_dir": str(tmp / "data")})
        self.assertEqual(S.model_ports(unrouted), [], "no Codex route: nothing is allowed")
        self.assertNotIn("dport", S.nft_text("ai-codex", S.model_ports(unrouted)), "only the drop line")
        self.assertIn("127.0.0.1:18080", C.egress_targets(unrouted))

    def test_block_c2_keeps_the_run_folders_parents_and_its_undo_takes_back_everything(self) -> None:
        config = self.config()
        [c2] = [b for b in S.blocks(config) if b["id"] == "c2"]
        data, root = str(Path(config.data_dir)), str(work.work_root(config))
        self.assertIn(f"sudo install -d -o {S.names(config)['harness']} -g seraphina-work -m 2750 {root} {root}/runs",
                      c2["commands"])
        self.assertNotIn("2770", " ".join(c2["commands"]), "only the harness makes the group-writable run folders")
        self.assertIn(f"sudo setfacl -x g:seraphina-work {data} {data}/jobs", c2["if_not"])
        self.assertIn(f"sudo setfacl -R -x g:seraphina-work -x d:g:seraphina-work {data}/worktrees {data}/jobs",
                      c2["if_not"], "the default entries and the job folders' shares too")

    def test_stage_writes_files_and_installs_nothing(self) -> None:
        config = self.config()
        folder = S.stage(config)
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         ["codex-egress.nft", "codex-key.env", "codex-launch.json", "seraphina-codex",
                          S.EGRESS_UNIT, "sf-codex-launch"])
        self.assertEqual((folder / "sf-codex-launch").read_bytes(), Path(L.__file__).read_bytes())
        self.assertEqual(oct(folder.stat().st_mode & 0o777), "0o700")
        for path in folder.iterdir():
            self.assertEqual(path.stat().st_mode & 0o077, 0, f"{path.name} is the harness's alone")
        self.assertFalse(os.path.exists("/etc/sudoers.d/seraphina-codex.new"))

    def test_staging_is_the_first_block_and_every_staged_file_a_block_installs_is_staged(self) -> None:
        config = self.config()
        blocks = S.blocks(config)
        self.assertEqual(blocks[0]["id"], "c0")
        self.assertEqual(blocks[0]["commands"], [f"{S.invocation(config)} codex setup --stage"])
        staged = str(S.staged_dir(config))
        named = {word[len(staged) + 1:] for block in blocks for line in block["commands"] + block.get("then", [])
                 for word in line.split() if word.startswith(staged + "/")}
        self.assertEqual(named, {"sf-codex-launch", "codex-launch.json", "codex-key.env", "seraphina-codex",
                                 "codex-egress.nft", S.EGRESS_UNIT})
        self.assertTrue(named <= {p.name for p in S.stage(config).iterdir()})

    def test_c0_and_c8_name_this_installs_own_factory_as_the_harnesss_user(self) -> None:
        config = self.config()
        blocks = {b["id"]: b for b in S.blocks(config)}
        argv = shlex.split(blocks["c0"]["commands"][0])
        self.assertEqual(argv[:4], ["sudo", "-u", S.names(config)["harness"], "env"])
        self.assertIn(f"FACTORY_CONFIG={config.path}", argv)
        self.assertIn(f"HOME={os.path.expanduser('~')}", argv)
        self.assertEqual(argv[-4:], [str(Path(S.__file__).resolve().parents[2] / "bin" / "factory"), "codex", "setup",
                                     "--stage"])
        self.assertEqual(shlex.split(blocks["c8"]["commands"][0])[-1], "doctor")
        self.assertNotEqual(blocks["c0"]["commands"][0].split()[0], "factory", "no factory on the server's PATH")

    @unittest.skipUnless(os.geteuid() == 0, "needs root to give the data folder to another user")
    def test_stage_refuses_a_user_who_does_not_own_the_data_folder(self) -> None:
        """sudo env ... factory codex setup --stage would stage as root: a sudoers rule for root and a staged folder
        the harness cannot replace."""
        config = self.config()
        data = Path(config.data_dir)
        data.mkdir(mode=0o700)
        os.chown(data, pwd.getpwnam("nobody").pw_uid, -1)
        with self.assertRaises(work.WorkError) as caught:
            S.stage(config)
        self.assertIn("stage as the harness's user nobody, who owns", str(caught.exception))
        self.assertIn("sudo -u nobody env", str(caught.exception))
        self.assertFalse(S.staged_dir(config).exists(), "nothing was written")

    def test_block_c7_installs_the_standalone_binary_with_a_sha256_check_and_no_npm(self) -> None:
        config = self.config()
        [c7] = [b for b in S.blocks(config) if b["id"] == "c7"]
        lines = " ".join(c7["commands"] + c7["then"])
        self.assertNotRegex(lines, r"\b(npm|npx|node)\b")
        self.assertIn(f"{S.RELEASES}/download/rust-v0.163.0/{S.codex_asset()}.tar.gz", lines)
        self.assertTrue(c7["commands"][-1].startswith("sudo sha256sum "), "the checksum before anything is installed")
        self.assertIn("compare the two yourself", c7["expect"])
        self.assertIn("no checked copy of that checksum", c7["expect"], "honest about who checks it")
        self.assertIn(f"{S.CODEX_DIR}/download/{S.codex_asset()} {S.DEFAULT_BINARY}", c7["then"][1])
        self.assertEqual(json.loads((S.stage(config) / "codex-launch.json").read_text())["binary"], S.DEFAULT_BINARY,
                         "with codex.binary unset the staged settings name the path c7 installs to")

    def test_block_c5_checks_with_each_installed_visudo_and_stages_the_rule_for_the_sudo_in_use(self) -> None:
        installed = ["/usr/lib/cargo/bin/visudo", "/usr/sbin/visudo"]
        self.assertEqual(S.visudo_paths(exists=lambda p: p in installed, realpath=lambda p: p), installed,
                         "sudo-rs's and classic sudo's")
        self.assertEqual(S.visudo_paths(exists=lambda p: p in installed, realpath=lambda p: installed[0]),
                         installed[:1], "two names for one binary are one parser")
        self.assertEqual(S.sudo_implementation(realpath=lambda p: "/usr/lib/cargo/bin/sudo"), "sudo-rs")
        self.assertEqual(S.sudo_implementation(realpath=lambda p: "/usr/bin/sudo"), "sudo")
        self.assertEqual(S.visudo_of("sudo-rs", exists=lambda p: True), "/usr/lib/cargo/bin/visudo")
        self.assertEqual(S.visudo_of("sudo", exists=lambda p: True), "/usr/sbin/visudo")
        config = self.config()
        with mock.patch.object(S, "visudo_paths", lambda: list(installed)), \
                mock.patch.object(S, "sudo_implementation", lambda: "sudo-rs"), \
                mock.patch.object(S, "visudo_of", lambda implementation: "/usr/lib/cargo/bin/visudo"):
            [c5] = [b for b in S.blocks(config) if b["id"] == "c5"]
            rule = (S.stage(config) / "seraphina-codex").read_text()
        self.assertEqual([line.split()[1] for line in c5["commands"][1:]], installed, "each parser checks the rule")
        self.assertTrue(c5["then"][0].startswith("sudo /usr/lib/cargo/bin/visudo -cf "))
        self.assertNotIn("Defaults", rule, "written for sudo-rs, the sudo the harness runs")
        self.assertIn("written for sudo-rs", c5["note"])

    def test_block_c2_lets_the_group_through_a_closed_parent(self) -> None:
        from swfactory.config import Config

        tmp = Path(tempfile.mkdtemp(prefix="sf-c2-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        os.chmod(tmp, 0o755)
        if S.closed_ancestors(Config(tmp / "c.json", {"data_dir": str(tmp)})):
            self.skipTest("the temporary folder's own parents are closed to other users here")
        home = tmp / "home"
        home.mkdir(mode=0o750)
        os.chmod(home, 0o750)   # a home as Ubuntu makes it
        closed = Config(tmp / "c.json", {"data_dir": str(home / "data")})
        [c2] = [b for b in S.blocks(closed) if b["id"] == "c2"]
        self.assertEqual(c2["commands"][0], f"sudo setfacl -m g:seraphina-work:x {home}")
        self.assertIn(f"sudo setfacl -x g:seraphina-work {home}; ", c2["if_not"])
        self.assertIn(f"folders above the data folder that other users cannot enter ({home})", c2["note"])
        self.assertNotIn("without listing or reading anything there: a traverse entry only", c2["note"])
        os.chmod(home, 0o755)
        [c2] = [b for b in S.blocks(closed) if b["id"] == "c2"]
        self.assertTrue(c2["commands"][0].startswith("sudo -u "), "an open chain needs no entry above the data folder")

    def test_block_c4_installs_the_routes_key_for_the_codex_user_alone_and_stage_writes_it(self) -> None:
        """Block c1 puts the harness's user in the worktree group, so the key file is never the group's: only the
        Codex user (and root) reads it, and the harness never needs to."""
        from swfactory.config import Config

        tmp = Path(tempfile.mkdtemp(prefix="sf-key-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        config = Config(tmp / "config.json", {
            "data_dir": str(tmp / "data"), "agents": {"builder": {"driver": "codex-worker", "model": "local"}},
            "codex": {"user": "ai-codex", "launch_settings": "/etc/seraphina/codex-launch.json"}})
        with mock.patch.dict(os.environ, {"STRATA_API_KEY": "strata-key-0123456789"}):
            [c4] = [b for b in S.blocks(config) if b["id"] == "c4"]
            folder = S.stage(config)
        self.assertIn(f"sudo install -m 0400 -o ai-codex -g root {S.staged_dir(config)}/codex-key.env "
                      "/etc/seraphina/codex-key.env.new", c4["commands"])
        key_lines = [line for line in c4["commands"] if "codex-key.env" in line]
        self.assertFalse([line for line in key_lines if "seraphina-work" in line or "0640" in line], key_lines)
        self.assertIn("ai-codex:root 0400, not the group seraphina-work", c4["note"])
        self.assertNotIn("the group may read it", c4["note"])
        self.assertNotIn("has no value", c4["note"], "the harness had the key: nothing to write by hand")
        self.assertIn(f"sudo rm {S.staged_dir(config)}/codex-key.env", c4["then"], "the staged copy goes")
        text = (folder / "codex-key.env").read_text()
        self.assertIn("STRATA_API_KEY=strata-key-0123456789\n", text)
        self.assertIn("Block c4 installs it ai-codex:root 0400", text)
        self.assertNotIn("0640", text)
        settings = json.loads((folder / "codex-launch.json").read_text())
        self.assertEqual({p["key_file"] for p in settings["providers"]}, {"/etc/seraphina/codex-key.env"},
                         "never the provider's own key file, which is the harness's")
        self.assertEqual(work.launch_settings(config)["providers"][0]["key_file"], "/etc/seraphina/codex-key.env")
        self.assertNotIn("/etc/seraphina/codex-key.env", C.read_paths(config))


class DoctorLineTests(unittest.TestCase):
    """The doctor's codex lines with a recorded launcher and sudo (T19's read and egress checks, the launcher's hash,
    the version pin)."""

    def setUp(self) -> None:
        from swfactory.config import Config

        self.tmp = Path(tempfile.mkdtemp(prefix="sf-doctor-codex-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.launcher = self.tmp / "sf-codex-launch"
        shutil.copy(L.__file__, self.launcher)
        self.config = Config(self.tmp / "config.json", {
            "data_dir": str(self.tmp / "data"),
            "codex": {"enabled": True, "binary": "/opt/codex", "user": "ai-codex", "launcher": str(self.launcher),
                      "launch_settings": str(self.tmp / "launch.json")}})
        (self.tmp / "launch.json").write_text(json.dumps(S.staged_settings(self.config)))   # block c4, done

    def checks(self, answers: dict) -> dict:
        def run(argv, timeout=60.0, env=None):
            for key, (code, out, err) in answers.items():
                if key in argv:
                    return subprocess.CompletedProcess(argv, code, out.encode(), err.encode())
            return subprocess.CompletedProcess(argv, 1, b"", b"not expected")
        found = C.all_checks(self.config, lambda n, ok, lvl, d: {"name": n, "ok": ok, "level": lvl, "detail": d},
                             run=run)
        return {c["name"]: c for c in found}

    def test_every_line_passes_on_good_answers(self) -> None:
        enabled = mock.patch.object(C, "egress_unit_enabled", return_value=True)
        self.addCleanup(enabled.stop)
        enabled.start()
        found = self.checks({"version": (0, "codex-cli 0.163.0\n", ""), "-l": (0, str(self.launcher), ""),
                             "sandbox-probe": (0, json.dumps({"ok": True, "bwrap": "/usr/bin/bwrap"}), ""),
                             "can-read": (0, json.dumps({"readable": {"/x": False}}), ""),
                             "egress-probe": (0, json.dumps({"connected": {C.PUBLIC_PROBE: False}}), "")})
        for name in ("codex version", "codex launcher", "codex home", "codex sandbox", "codex reads", "codex egress"):
            self.assertTrue(found[name]["ok"], found[name])
        self.assertIn("/usr/bin/bwrap", found["codex sandbox"]["detail"])

    def test_bad_answers_fail_with_the_reason(self) -> None:
        found = self.checks({"version": (0, "codex-cli 0.164.0\n", ""),
                             "-l": (1, "", "sudo: a password is required"),
                             "sandbox-probe": (0, json.dumps({"ok": False, "canary_read": True, "ran": True,
                                                              "ws_write": True}), ""),
                             "can-read": (0, json.dumps({"readable": {str(self.tmp / "data"): True}}), ""),
                             "egress-probe": (0, json.dumps({"connected": {C.PUBLIC_PROBE: True}}), "")})
        self.assertIn("not the pin 0.163.0", found["codex version"]["detail"])
        self.assertEqual((found["codex launcher"]["ok"], found["codex launcher"]["level"]), (False, "warn"))
        self.assertIn("not checked", found["codex launcher"]["detail"])
        self.assertIn("the denied canary was readable", found["codex sandbox"]["detail"])
        self.assertIn("can read", found["codex reads"]["detail"])
        self.assertIn("nftables rule", found["codex egress"]["detail"])

    def test_installed_settings_that_differ_from_the_configuration_fail_the_home_line(self) -> None:
        (self.tmp / "launch.json").write_text(json.dumps({**S.staged_settings(self.config),
                                                          "binary": S.DEFAULT_BINARY}))
        found = self.checks({})
        self.assertFalse(found["codex home"]["ok"])
        self.assertIn("differs from this configuration in binary: stage again (block c0) and repeat block c4",
                      found["codex home"]["detail"])

    def test_the_reads_line_looks_below_the_data_folder(self) -> None:
        data = self.tmp / "data"
        log = data / "chats" / "c-0123456789" / "logs" / "01-turn.jsonl"
        log.parent.mkdir(parents=True)
        log.write_text("{}\n")
        for rel in ("jobs/r/001-x", "worktrees/r/001-x", "codex/runs"):
            (data / rel).mkdir(parents=True)
        paths = C.read_paths(self.config)
        for wanted in (data, data / "chats", log, data / "jobs" / "r"):
            self.assertIn(str(wanted), paths)
        for shared in (data / "worktrees", data / "codex", data / "jobs"):
            self.assertNotIn(str(shared), paths, "shared with the group by design (block c2)")
        found = self.checks({"can-read": (0, json.dumps({"readable": {str(data): False, str(data / "chats"): True,
                                                                      str(log): True}}), "")})
        self.assertFalse(found["codex reads"]["ok"], "a data folder passable but not listable is not enough")
        self.assertIn(str(log), found["codex reads"]["detail"])

    def test_the_reads_line_probes_every_codex_jobs_folder_but_one_in_a_codex_run(self) -> None:
        jobs = self.tmp / "data" / "jobs" / "r"
        codex_run = {"run": "02-builder", "agent": "builder", "driver": "codex", "codex": {"version": "0.130.0"}}
        recovered = {"run": "02-builder", "agent": "builder", "ok": False, "outcome": "unknown", "receipt": None}
        builtin = {"run": "02-builder", "agent": "builder", "driver": "builtin"}
        for job_id, in_flight, runs in (("001-done", None, [codex_run]), ("002-running", {"run": "03-builder"},
                                                                          [codex_run]),
                                        ("004-recovered", None, [recovered]), ("005-builtin", None, [builtin])):
            (jobs / job_id).mkdir(parents=True)
            (jobs / job_id / "job.json").write_text(json.dumps({"id": job_id, "codex_run": in_flight, "runs": runs}))
        (jobs / "003-unreadable").mkdir()
        paths = C.read_paths(self.config)
        for wanted in (jobs, jobs / "001-done", jobs / "001-done" / "job.json", jobs / "003-unreadable",
                       jobs / "004-recovered", jobs / "004-recovered" / "job.json"):
            self.assertIn(str(wanted), paths)
        self.assertFalse([p for p in paths if "002-running" in p], "its folder is the group's during its run")
        self.assertFalse([p for p in paths if "005-builtin" in p], "a job Codex never ran in was never shared")
        found = self.checks({"can-read": (0, json.dumps({"readable": {str(jobs / "001-done"): True}}), "")})
        self.assertFalse(found["codex reads"]["ok"], "a share a killed harness left is found")
        self.assertIn(str(jobs / "001-done"), found["codex reads"]["detail"])

    def test_the_key_line_fails_when_the_codex_user_cannot_read_its_routes_key(self) -> None:
        from swfactory.config import Config

        self.config = Config(self.tmp / "config.json", {
            "data_dir": str(self.tmp / "data"), "agents": {"builder": {"driver": "codex-worker", "model": "local"}},
            "codex": {"enabled": True, "binary": "/opt/codex", "user": "ai-codex", "launcher": str(self.launcher),
                      "launch_settings": str(self.tmp / "launch.json")}})
        key = "/etc/seraphina/codex-key.env"
        url = self.config.model("local").provider.base_url.rstrip("/")
        cannot = f"the key file {key} cannot be read by the Codex user (uid 990): Permission denied"
        found = self.checks({"key-check": (0, json.dumps({"providers": {url: {
            "env_key": "STRATA_API_KEY", "defined": False, "reason": cannot}}}), "")})
        self.assertFalse(found["codex key"]["ok"])
        self.assertIn(cannot, found["codex key"]["detail"])
        self.assertIn("ai-codex:root 0400", found["codex key"]["detail"])
        found = self.checks({"key-check": (0, json.dumps({"providers": {url: {
            "env_key": "STRATA_API_KEY", "defined": True}}}), ""),
            "can-read": (0, json.dumps({"readable": {key: False}}), "")})
        self.assertTrue(found["codex key"]["ok"], found["codex key"])
        self.assertIn("which the harness never reads", found["codex key"]["detail"])

    def test_every_probe_goes_through_the_runner_the_caller_gives(self) -> None:
        seen: list = []

        def run(argv, timeout=60.0, env=None):
            seen.append(argv)
            return no_egress_probe(argv) if "egress-probe" in argv else subprocess.CompletedProcess(argv, 1, b"", b"")

        outside = AssertionError("a process started outside the runner")
        with mock.patch.object(subprocess, "run", side_effect=outside), \
                mock.patch.object(subprocess, "Popen", side_effect=outside):
            found = {c["name"]: c for c in C.all_checks(self.config, lambda n, ok, lvl, d: {
                "name": n, "ok": ok, "level": lvl, "detail": d}, run=run)}
        self.assertTrue(any("egress-probe" in argv for argv in seen), "the egress probe went through the runner")
        self.assertTrue(found["codex egress"]["detail"].startswith("not checked"), found["codex egress"])

    def test_a_launcher_that_is_not_this_versions_fails(self) -> None:
        self.launcher.write_text("#!/bin/sh\nexec codex \"$@\"\n")
        found = self.checks({})
        self.assertFalse(found["codex launcher"]["ok"])
        self.assertIn("is not this version's launcher", found["codex launcher"]["detail"])

    def test_the_egress_line_warns_while_nothing_loads_the_rule_at_boot(self) -> None:
        with mock.patch.object(C, "egress_unit_enabled", return_value=False):
            found = self.checks({"egress-probe": (0, json.dumps({"connected": {C.PUBLIC_PROBE: False}}), "")})
        self.assertEqual((found["codex egress"]["ok"], found["codex egress"]["level"]), (False, "warn"))
        self.assertIn(f"{S.EGRESS_UNIT} is not enabled, so the rule is gone after the next boot",
                      found["codex egress"]["detail"])

    def test_the_reads_line_probes_a_log_of_every_codex_job(self) -> None:
        """An older version's share left default entries on a job folder, which hand every log written there later
        to the group: one log below each Codex job is probed too."""
        job = self.tmp / "data" / "jobs" / "r" / "001-done"
        (job / "logs").mkdir(parents=True)
        (job / "job.json").write_text(json.dumps({"codex_run": None, "runs": [{"run": "02-builder", "receipt": "x"}]}))
        (job / "logs" / "03-reviewer.jsonl").write_text("{}\n")
        self.assertIn(str(job / "logs" / "03-reviewer.jsonl"), C.read_paths(self.config))

    def test_the_reads_line_checks_what_a_traverse_entry_above_the_data_folder_opens(self) -> None:
        from swfactory.config import Config
        from tests.helpers import make_repo

        os.chmod(self.tmp, 0o755)
        if S.closed_ancestors(Config(self.tmp / "c.json", {"data_dir": str(self.tmp)})):
            self.skipTest("the temporary folder's own parents are closed to other users here")
        home = self.tmp / "home"
        home.mkdir()
        os.chmod(home, 0o750)   # a home as Ubuntu makes it, which block c2 lets the group through
        for name, mode in (("Documents", 0o755), ("notes.txt", 0o644), ("private", 0o700), ("repos", 0o755)):
            path = home / name
            path.mkdir() if "." not in name else path.write_text("x\n")
            os.chmod(path, mode)
        app, finances = make_repo(home / "repos", "app"), make_repo(home / "repos", "finances")
        config = Config(self.tmp / "config.json", {
            "data_dir": str(home / "data"), "repos": [{"name": "app", "path": str(app)},
                                                    {"name": "finances", "path": str(finances)}],
            "codex": {**self.config.raw["codex"]}})
        (home / "data").mkdir()
        self.assertEqual(S.exposed_below(config), [str(home / "Documents"), str(home / "notes.txt"),
                                                   str(home / "repos")])
        paths = C.read_paths(config)
        self.assertIn(str(home / "Documents"), paths)
        self.assertIn(str(home / "notes.txt"), paths)
        self.assertNotIn(str(home / "repos"), paths, "a Codex job reads its own repository's git folder")
        [c2] = [b for b in S.blocks(config) if b["id"] == "c2"]
        self.assertIn(f"among them the harness user's home, {home}" if Path(os.path.expanduser("~")) == home
                      else f"({home})", c2["note"])
        self.assertIn(f"Below them that is now: {home / 'Documents'}, {home / 'notes.txt'}, {home / 'repos'}",
                      c2["note"])
        deny = work.deny_paths(config, {"repo": "app", "id": "001-x", "job_dir": str(home / "data" / "jobs" / "app"
                                                                                       / "001-x"),
                                        "worktree": str(home / "data" / "worktrees" / "app" / "001-x")})
        self.assertIn(str(finances), deny, "another repository's checkout is denied to Codex's commands")
        self.assertNotIn(str(app), deny, "its own is not: its worktree's git folder lives there")

    def test_off_is_one_line(self) -> None:
        from swfactory.config import Config

        found = C.all_checks(Config(self.tmp / "c2.json", {"data_dir": str(self.tmp / "d2")}),
                             lambda n, ok, lvl, d: {"name": n, "ok": ok, "level": lvl, "detail": d})
        self.assertEqual([(c["name"], c["ok"]) for c in found], [("codex", True)])



def _private_netns() -> str | None:
    """Why the live nftables tests cannot run here (None: they can): they load the rule only in a network namespace
    of their own, as root, so nothing on this machine changes."""
    if os.geteuid() != 0:
        return "needs root to load an nftables rule in a private network namespace"
    for tool in ("nft", "unshare", "ip", "setpriv"):
        if shutil.which(tool) is None:
            return f"{tool} is not installed here"
    try:
        pwd.getpwnam("nobody")
        grp.getgrnam(R.NOBODY_GROUP)
    except KeyError:
        return "the user nobody and the group nogroup are needed"
    done = subprocess.run(["unshare", "--net", "nft", "list", "ruleset"], capture_output=True, timeout=30)
    return None if done.returncode == 0 else "nftables does not work in a private network namespace here"


NETNS = _private_netns()


class UnreadableFolderTests(unittest.TestCase):
    """git walks past a folder it cannot open with a warning and exit 0 (checked with git 2.43): a listing, a status
    and an `add -A` that warned are incomplete, so each fails closed instead (AsTheCodexUserTests runs it for real)."""

    WARNING = "warning: could not open directory 'build/': Permission denied\n"

    def git(self, answers: dict) -> mock._patch:
        from swfactory import gitops

        seen: list = []

        def run(repo, args, timeout, text=True, env=None):
            seen.append(args[0])
            out, err = answers.get(args[0], ("", ""))
            if not text:
                out, err = out.encode(), err.encode()
            return subprocess.CompletedProcess(["git", *map(str, args)], 0, out, err)
        self.seen = seen
        return mock.patch.object(gitops, "_run", side_effect=run)

    def test_the_ignored_files_listing_fails_instead_of_coming_out_short(self) -> None:
        from swfactory import gitops
        from swfactory.config import Config
        from swfactory.events import EventBus
        from swfactory.orchestrator import Factory

        with self.git({"ls-files": ("dist/a.js\0", self.WARNING)}):
            with self.assertRaises(gitops.GitError) as caught:
                gitops.ignored_files(Path("/nonexistent"))
            self.assertIn("could not open directory 'build/'", str(caught.exception))
            factory = Factory(Config(Path("/nonexistent/config.json"), {"data_dir": "/nonexistent/data"}), EventBus())
            self.assertIsNone(factory._ignored_files(Path("/nonexistent")), "None: verify then fails closed")

    def test_the_reconcile_fails_instead_of_coming_out_short(self) -> None:
        from swfactory import gitops

        with self.git({"status": ("?? app.py\n", self.WARNING)}), self.assertRaises(gitops.GitError):
            gitops.reconcile_diff(Path("/nonexistent"))

    def test_the_commit_is_refused_and_the_index_put_back(self) -> None:
        from swfactory import gitops

        with self.git({"add": ("", self.WARNING)}), self.assertRaises(gitops.CommitRefused) as caught:
            gitops.commit_all(Path("/nonexistent"), "factory: x")
        self.assertIn("cannot read all of the worktree", str(caught.exception))
        self.assertEqual(self.seen, ["add", "reset"], "nothing staged is left behind, nothing committed")


class KeyFileTests(unittest.TestCase):
    """The Codex route's key file is the Codex user's alone (block c4: <codex user>:root 0400). The harness's user is
    in the worktree group, so a group-readable key would be every agent's to read; the harness never needs it: the
    doctor asks the launcher, as the Codex user, whether a run would get the key."""

    def setUp(self) -> None:
        from swfactory.config import Config

        self.tmp = Path(tempfile.mkdtemp(prefix="sf-codex-key-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        scrub = mock.patch.dict(os.environ)
        scrub.start()
        self.addCleanup(scrub.stop)
        os.environ.pop("STRATA_API_KEY", None)
        self.config = Config(self.tmp / "config.json", {
            "data_dir": str(self.tmp / "data"), "agents": {"builder": {"driver": "codex-worker", "model": "local"}},
            "codex": {"enabled": True, "binary": "/opt/codex", "user": "ai-codex",
                      "launch_settings": str(self.tmp / "launch.json"), "key_file": str(self.tmp / "codex-key.env")}})

    def test_agents_bash_reads_neither_the_codex_key_nor_the_staged_copy(self) -> None:
        from swfactory.config import Config, sandbox_masks
        from swfactory.harness import sandbox as sandbox_mod

        masks = sandbox_masks(self.config)
        self.assertIn(Path(self.config.data_dir) / "codex-setup", masks["dirs"])
        self.assertIn(Path(S.SERVER_KEY_FILE), masks["files"], "the server's key file, whatever its mode")
        self.assertIn(self.tmp / "codex-key.env", masks["files"], "and codex.key_file")
        self.assertIn(Path(S.SERVER_KEY_FILE), sandbox_masks(Config(self.tmp / "c2.json", {
            "data_dir": str(self.tmp / "d2")}))["files"], "with or without a Codex user")
        if not bwrap_available():
            return
        from tests.test_sandbox import _outside_tmp

        base = _outside_tmp()   # bwrap gives every command a private /tmp: a marker there proves nothing
        if base is None:
            self.skipTest("no writable directory outside /tmp")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        config = Config(base / "conf" / "config.json", {**self.config.raw, "data_dir": str(base / "data"), "codex": {
            **self.config.raw["codex"], "key_file": str(base / "keys" / "codex-key.env")}})
        marker = "codex-key-sentinel-0123456789"
        with mock.patch.dict(os.environ, {"STRATA_API_KEY": marker}):
            staged = S.stage(config) / "codex-key.env"
        installed = base / "keys" / "codex-key.env"
        installed.parent.mkdir()
        installed.write_text(f"STRATA_API_KEY={marker}\n")
        self.assertIn(marker, staged.read_text())
        work_dir = base / "work"
        work_dir.mkdir()
        masks = sandbox_masks(config)
        dirs, files, rebind = sandbox_mod.mask_plan(masks["dirs"], masks["files"], [work_dir], masks["fallback"])
        box = sandbox_mod.Sandbox("bwrap", work_dir, [work_dir], False, {}, masked=dirs, masked_files=files,
                                  readonly_rebind=rebind)
        self.addCleanup(box.cleanup)
        _code, out, _ = box.run(f"cat {staged} {installed} 2>&1; true", 20)
        self.assertNotIn(marker, out, "an agent's Bash read a copy of the Codex route's key")
        _code, out, _ = box.run(f"ls -a {staged.parent} 2>&1; true", 20)
        self.assertNotIn("codex-key.env", out, "the staged folder is hidden whole")

    def test_block_c4_and_stage_say_where_a_key_the_harness_lacks_goes(self) -> None:
        self.assertEqual(S.missing_keys(self.config), ["STRATA_API_KEY"])
        [c4] = [b for b in S.blocks(self.config) if b["id"] == "c4"]
        key = str(self.tmp / "codex-key.env")
        self.assertIn(f"The harness has no value for STRATA_API_KEY: after the move, write STRATA_API_KEY=<the key> "
                      f"into {key} yourself (sudo nano {key}; a keyless local server needs a placeholder value), "
                      f"never into the staged copy", c4["note"])
        text = (S.stage(self.config) / "codex-key.env").read_text()
        self.assertNotIn("STRATA_API_KEY=", text.replace("write STRATA_API_KEY=<the key>", ""), text)
        self.assertIn(f"after block c4 write STRATA_API_KEY=<the key> into {key}", text)

    def run_launcher_directly(self, settings: Path) -> Callable:
        """The doctor's runner with the launcher started directly on settings (no sudo: the suite has no Codex
        user), exactly as the launcher answers its verb."""
        def run(argv, timeout=60.0, env=None):
            verb = next(i for i, part in enumerate(argv) if part in ("key-check", "can-read", "egress-probe",
                                                                         "version", "sandbox-probe"))
            env = {key: value for key, value in os.environ.items() if key != "SUDO_UID"}
            return subprocess.run([sys.executable, "-I", L.__file__, "--settings", str(settings), *argv[verb:]],
                                  capture_output=True, timeout=timeout, env=env)
        return run

    def test_the_key_line_fails_on_a_key_file_that_defines_no_key(self) -> None:
        """What c4 installs when the harness had no value: comments only. Every run would be refused, so the line
        must fail, though the file is readable."""
        settings = self.tmp / "launch.json"
        settings.write_text(json.dumps(S.staged_settings(self.config)))
        key = self.tmp / "codex-key.env"
        key.write_text(S.key_text(self.config))
        check = lambda n, ok, lvl, d: {"name": n, "ok": ok, "level": lvl, "detail": d}   # noqa: E731
        found = C._key(self.config, check, self.run_launcher_directly(settings))
        self.assertFalse(found["ok"], found)
        self.assertIn(f"the key file {key} does not define STRATA_API_KEY", found["detail"])
        self.assertIn("placeholder", found["detail"])
        key.write_text(key.read_text() + "STRATA_API_KEY=a-key-0123456789\n")
        found = C._key(self.config, check, self.run_launcher_directly(settings))
        self.assertTrue(found["ok"], found)
        self.assertNotIn("a-key-0123456789", json.dumps(found), "the key's value is never printed")

    def test_the_key_check_verb_prints_names_and_yes_or_no_only(self) -> None:
        settings = self.tmp / "launch.json"
        settings.write_text(json.dumps(S.staged_settings(self.config)))
        (self.tmp / "codex-key.env").write_text("STRATA_API_KEY=secret-value-0123456789\n")
        done = self.run_launcher_directly(settings)(["sudo", "-n", "-u", "ai-codex", "launcher", "key-check"])
        self.assertEqual(done.returncode, 0, done.stderr)
        answer = json.loads(done.stdout)
        self.assertNotIn(b"secret-value", done.stdout + done.stderr)
        url = self.config.model("local").provider.base_url.rstrip("/")
        self.assertEqual(answer["providers"][url]["defined"], True)
        self.assertEqual(answer["providers"][url]["env_key"], "STRATA_API_KEY")


class EgressRuleTests(unittest.TestCase):
    """The uid egress rule (O9: on): the staged file replaces its table whole, a unit loads it at every boot, and a
    Codex run refuses to start while it is not loaded (work.containment_blocker)."""

    def config(self, **codex):
        from swfactory.config import Config

        tmp = Path(tempfile.mkdtemp(prefix="sf-egress-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        launcher = tmp / "sf-codex-launch"
        shutil.copy(L.__file__, launcher)
        return Config(tmp / "config.json", {
            "data_dir": str(tmp / "data"), "agents": {"builder": {"driver": "codex-worker", "model": "local"}},
            "codex": {"enabled": True, "binary": "/opt/codex", "user": "ai-codex", "launcher": str(launcher),
                      "launch_settings": str(tmp / "launch.json"), **codex}})

    def test_the_staged_rule_replaces_its_table_whole(self) -> None:
        nft = S.nft_text("ai-codex", [("127.0.0.1", 5002)])
        head = "table inet seraphina_codex\ndelete table inet seraphina_codex\ntable inet seraphina_codex {\n"
        self.assertIn(head, nft, "declared, deleted and defined again in one file: nft -f applies it at once")
        self.assertEqual(nft.count("counter drop"), 1)

    @unittest.skipUnless(NETNS is None, NETNS or "")
    def test_loading_the_rule_again_after_a_provider_change_keeps_only_the_new_port(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="sf-nft-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        old, new = tmp / "old.nft", tmp / "new.nft"
        old.write_text(S.nft_text("nobody", [("127.0.0.1", 18088)]))
        new.write_text(S.nft_text("nobody", [("127.0.0.1", 5002)]))
        done = subprocess.run(["unshare", "--net", "sh", "-c", f"nft -c -f {new} && nft -f {new} && nft -f {old} && "
                               f"nft -f {new} && nft list table inet seraphina_codex"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("dport 5002 accept", done.stdout)
        self.assertNotIn("18088", done.stdout, "the old route's port is closed again")
        self.assertEqual(done.stdout.count(" drop"), 1, done.stdout)

    def test_block_c6_installs_a_unit_that_loads_the_rule_at_every_boot(self) -> None:
        config = self.config()
        [c6] = [b for b in S.blocks(config) if b["id"] == "c6"]
        staged = S.staged_dir(config)
        self.assertEqual(c6["commands"][3:], [
            f"sudo install -m 0644 -o root -g root {staged}/{S.EGRESS_UNIT} /etc/systemd/system/{S.EGRESS_UNIT}",
            "sudo systemctl daemon-reload", f"sudo systemctl enable {S.EGRESS_UNIT}"])
        self.assertIn(f"sudo systemctl disable {S.EGRESS_UNIT}", c6["if_not"])
        self.assertNotIn("include \"", c6["note"], "no include in /etc/nftables.conf, whose flush ruleset would take "
                                                   "Docker's and Tailscale's rules with it")
        self.assertIn("Do not enable nftables.service", c6["note"])
        unit = (S.stage(config) / S.EGRESS_UNIT).read_text()
        for line in ("Type=oneshot", "RemainAfterExit=yes", "DefaultDependencies=no", "Wants=network-pre.target",
                     f"ExecStart=/usr/sbin/nft -f {S.NFT_FILE}", "WantedBy=sysinit.target"):
            self.assertIn(line + "\n", unit)
        self.assertIn("Before=network-pre.target", unit)
        self.assertNotIn("ExecStop", unit, "stopping it never opens the Codex user's traffic")
        self.assertNotIn("flush", unit)

    def test_the_run_gate_refuses_while_the_rule_is_not_loaded(self) -> None:
        from swfactory.config import Config

        config = self.config()
        provider = config.model("local").provider
        answers: list = []

        def launcher(cfg, verb, *args, timeout=60.0):
            answers.append((verb, args))
            return dict(reply)
        with mock.patch.object(work, "run_launcher", side_effect=launcher):
            self.assertIsNone(work.containment_blocker(Config(config.path, {**config.raw, "codex": {
                **config.raw["codex"], "user": None}}), provider), "no Codex user: nothing to hold")
            self.assertEqual(answers, [])
            reply = {"how": {"127.0.0.1:9": "refused", "127.0.0.1:18080": "connected"}, "exit": 0}
            self.assertIn("the uid egress rule is not loaded: a connect from the Codex user to 127.0.0.1:9 was "
                          "refused, not dropped", work.containment_blocker(config, provider))
            self.assertEqual(answers[-1], ("egress-probe", ("--target", "127.0.0.1:9", "--target", "127.0.0.1:18080",
                                                            "--seconds", "1")), "loopback only: nothing leaves")
            reply = {"how": {"127.0.0.1:9": "dropped", "127.0.0.1:18080": "connected"}, "exit": 0}
            self.assertIsNone(work.containment_blocker(config, provider))
            reply = {"how": {"127.0.0.1:9": "dropped", "127.0.0.1:18080": "dropped"}, "exit": 0}
            self.assertIn("drops the Codex route's own model", work.containment_blocker(config, provider))
            reply = {"connected": {"127.0.0.1:9": False}, "exit": 0}   # an older launcher: it cannot tell
            self.assertIn("could not be checked", work.containment_blocker(config, provider))
            Path(work.launcher_path(config)).write_text("#!/bin/sh\n")
            self.assertIn("is not this version's", work.containment_blocker(config, provider))

    @unittest.skipUnless(NETNS is None, NETNS or "")
    def test_the_launcher_tells_a_dropped_connect_from_a_refused_one(self) -> None:
        """As nobody, in a network namespace of its own: without the rule a loopback connect to a closed port is
        refused at once; with the staged rule it is dropped. Nothing leaves this machine."""
        tmp = Path(tempfile.mkdtemp(prefix="sf-probe-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        os.chmod(tmp, 0o755)
        launcher = tmp / "launch.py"
        shutil.copy(L.__file__, launcher)
        settings = tmp / "launch.json"
        settings.write_text(json.dumps({"v": 1, "binary": "/nonexistent/codex", "worktrees": str(tmp),
                                        "work_root": str(tmp), "providers": []}))
        rule = tmp / "rule.nft"
        rule.write_text(S.nft_text("nobody", [("127.0.0.1", 18080)]))
        for path in (launcher, settings, rule):
            os.chmod(path, 0o644)
        if any(not os.stat(parent).st_mode & 0o001 for parent in tmp.parents):
            self.skipTest("the temporary folder's parents are closed to other users here")
        probe = (f"setpriv --reuid nobody --regid {R.NOBODY_GROUP} --clear-groups {sys.executable} -I {launcher} "
                 f"--settings {settings} egress-probe --target 127.0.0.1:9 --seconds 1")
        done = subprocess.run(["unshare", "--net", "sh", "-c", f"ip link set lo up && {probe} && nft -f {rule} && "
                               f"{probe}"], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        before, after = (json.loads(line) for line in done.stdout.strip().splitlines())
        self.assertEqual(before["how"], {"127.0.0.1:9": "refused"})
        self.assertEqual(after["how"], {"127.0.0.1:9": "dropped"})


if __name__ == "__main__":
    unittest.main()
