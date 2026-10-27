"""Tests for bin/sf-system, Your server's root helper, phases P1a to P1c (DESIGN.md §42; docs/seraphina/YOUR_SERVER.md
§6, §7.2, §8.1, §12.2): T4 the container map from Docker's state files, T6 requests refused before anything starts and
the argv it builds, T7 the installation check and explain, T9 the Tailscale decoder, T36 the one-paste setup and
remove, T37 the ingress reader, and the parsers of ports, disks and gpu (T2, T3 and T18's helper part); and from P1b
and P1c (tests/test_sf_system_checks.py): T8, T14, T18, T20's keep pass, T21-T23's helper part, T34, T35. The helper runs in a
temporary tree (tests/sfsystem_rig.py) with its tools' outputs faked from tests/fixtures/sfsystem/ and root ownership
faked; every name, address and path is a placeholder.
"""

from __future__ import annotations

import io
import json
import os
import unittest
import unittest.mock
from pathlib import Path

from tests.sfsystem_rig import DOCKERPAL, FIXTURES, IDS, KATE, OWNER, SERAPHINA, Rig, sx


def planted(answer) -> list[str]:
    text = json.dumps(answer)
    return [word for word in ("PLANTED", "tskey-", "privkey:", "nodekey:") if word in text]


class RigCase(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = Rig()
        self.addCleanup(self.rig.cleanup)

    def ask(self, verb, uid=None, **fields):
        return self.rig.ask(verb, uid, **fields)


class ContainerTests(RigCase):
    """T4: Docker 29's checkpoint files read into the map; only allow-listed labels and environment keys; nothing
    planted in an environment value, a command line or a log reaches an answer."""

    def test_the_recorded_shape_parses_with_networks_mounts_and_state(self):
        code, answer = self.ask("containers")
        self.assertEqual(code, 0, answer)
        photos = next(c for c in answer["containers"] if c["id"] == IDS["photos"])
        self.assertEqual(photos["name"], "photos-photos-1")
        self.assertEqual(photos["image"], "photoprism/photoprism:241021")
        self.assertEqual(photos["restart_policy"], "unless-stopped")
        self.assertEqual(photos["networks"]["photos_default"],
                         {"ip": "172.19.0.3", "gateway": "172.19.0.1", "aliases": ["photos-photos-1", "photos"],
                          "dns_names": ["photos-photos-1", "photos", "111111111111"]})
        self.assertEqual({m["destination"]: m["source"] for m in photos["mounts"]},
                         {"/photoprism/originals": "/srv/data1/photos/originals",
                          "/photoprism/storage": "/var/lib/docker/volumes/photos_storage/_data"})
        self.assertTrue(photos["running"] and photos["started_before"] and not photos["manually_stopped"])
        self.assertEqual(photos["user"], "", "root inside the container")
        self.assertEqual(answer["stacks_dir"], "/opt/stacks")
        self.assertTrue(answer["compose_dirs"]["/opt/stacks/photos"]["exists"])
        self.assertEqual(answer["compose_dirs"]["/home/owner"]["files"], [True], "a compose.json recipe from its label")

    def test_only_allow_listed_labels_and_environment_keys(self):
        _, answer = self.ask("containers")
        photos = next(c for c in answer["containers"] if c["id"] == IDS["photos"])
        self.assertEqual(set(photos["labels"]) - {k for k in photos["labels"] if k.startswith("com.docker.compose.")},
                         {"org.opencontainers.image.version", "org.opencontainers.image.title"})
        self.assertEqual(photos["env"], {"TS_HOSTNAME": "photos"})
        dockge = next(c for c in answer["containers"] if c["name"] == "dockge")
        self.assertEqual(dockge["env"], {"DOCKGE_STACKS_DIR": "/opt/stacks"})
        self.assertTrue(dockge["docker_socket"])
        self.assertEqual(planted(answer), [], "no environment value, command line, health log or label value planted")
        for key in ("Cmd", "Args", "Entrypoint", "LogPath", "Health", "Env"):
            self.assertNotIn(f'"{key}"', json.dumps(answer))

    def test_a_file_of_an_unknown_shape(self):
        _, answer = self.ask("containers")
        broken = next(c for c in answer["containers"] if c["id"] == IDS["broken"])
        self.assertEqual(broken, {"id": IDS["broken"], "error": "docker_format_unknown"})

    def test_docker_is_never_started(self):
        self.rig.docker_active = False
        code, answer = self.ask("containers")
        self.assertEqual(code, 0)
        self.assertIs(answer["docker_active"], False)
        self.assertTrue(answer["containers"], "answered from the state files")
        tools = {os.path.basename(call["argv"][0]) for call in self.rig.system.calls}
        self.assertLessEqual(tools, {"systemctl"}, "no Docker command or socket")


class RequestTests(RigCase):
    """T6: refusals before anything starts; the argv it builds."""

    def test_bad_requests(self):
        for line, code in ((b"not json\n", "bad_request"), (b'{"v": 2, "verb": "info"}\n', "bad_request"),
                           (b'{"v": 1, "verb": "explode"}\n', "unknown_verb"),
                           (b'{"v": 1, "verb": "info", "extra": 1}\n', "unknown_field"),
                           (b'{"v": 1, "verb": "ingress", "container": "-x"}\n', "bad_field"),
                           (b'{"v": 1, "verb": "compose", "dir": "/opt/../etc"}\n', "bad_field"),
                           (b'{"v": 1, "verb": "compose"}\n', "bad_field"),
                           (b'{"v": 1, "verb": "info", "pad": "' + b"x" * 17000 + b'"}\n', "bad_request")):
            with self.subTest(line=line[:40]):
                exit_code, text = self.rig.main([], line)
                self.assertEqual((exit_code, json.loads(text)["error"]), (2, code))

    def test_an_id_with_no_record(self):
        self.assertEqual(self.ask("compose", dir="/opt/stacks/nothing")[1]["error"], "no_record")
        self.assertEqual(self.ask("ingress", container="f" * 64)[1]["error"], "no_record")

    def test_every_later_verb_is_refused(self):
        for verb in sx.LATER:
            with self.subTest(verb=verb):
                code, answer = self.ask(verb)
                self.assertEqual((code, answer["error"]), (2, "not_in_this_version"))

    def test_callers(self):
        self.assertEqual(self.ask("info", uid=0)[1]["error"], "caller_not_allowed", "root is not a caller")
        self.assertEqual(self.ask("info", uid=4242)[1]["error"], "caller_unknown")
        self.assertEqual(self.ask("containers", uid=DOCKERPAL)[1]["error"], "caller_holds_root")
        self.assertEqual(self.ask("containers", uid=OWNER)[1]["error"], "caller_holds_root", "docker, lxd and sudo")
        code, answer = self.ask("containers", uid=KATE)
        self.assertEqual(code, 0, "a login account may read the layout")
        code, answer = self.rig.main([], b'{"v": 1, "verb": "info"}\n', environ={})
        self.assertEqual(json.loads(answer)["error"], "no_caller")
        helper = sx.Helper(self.rig.paths, self.rig.system, self.rig.lstat, environ={"SUDO_UID": str(KATE)})
        with self.assertRaises(sx.Refused) as caught:
            helper.caller(helper.installation(), "private")
        self.assertEqual(caught.exception.code, "caller_is_login_account", "whatever the config says")
        helper = sx.Helper(self.rig.paths, self.rig.system, self.rig.lstat, environ={"SUDO_UID": str(SERAPHINA)})
        self.assertEqual(helper.caller(helper.installation(), "read")["user"], "seraphina")

    def test_a_class_beyond_read_is_not_in_this_version(self):
        problems = sx.check_config({"v": 1, "allow": ["read", "maintain"], "callers": {"991": ["read", "maintain"]}})
        self.assertTrue(any("has no maintain class" in p for p in problems))
        problems = sx.check_config({"v": 1, "allow": ["read"], "callers": {"991": ["read", "fix"]}})
        self.assertTrue(any("classes that allow does not" in p for p in problems))
        self.rig.config(allow=["read", "fix"])
        self.assertEqual(self.ask("info")[1]["error"], "unsafe_install", "a config beyond reading is refused whole")

    def test_argument_modes(self):
        for argv, code in ((["enrol"], "not_in_this_version"), (["approve", "T-1"], "not_in_this_version"),
                           (["explain", "approval"], "not_in_this_version"), (["run", "nope"], "bad_arguments"),
                           (["--help"], "bad_arguments"), (["info"], "bad_arguments")):
            with self.subTest(argv=argv):
                exit_code, text = self.rig.main(argv)
                self.assertEqual((exit_code, json.loads(text)["error"]), (2, code))

    def test_the_argv_it_builds(self):
        for verb, fields in (("info", {}), ("containers", {}), ("ports", {}), ("tailscale", {}), ("ingress", {}),
                             ("disks", {}), ("gpu", {}), ("compose", {"dir": "/opt/stacks/media"})):
            self.assertEqual(self.ask(verb, **fields)[0], 0, verb)
        fixed = set(dict(self.rig.paths.tools).values()) | {self.rig.paths.compose}
        forbidden = {"prune", "run", "create", "exec", "rm", "pull", "purge", "clean", "autoclean", "start", "stop",
                     "restart", "-a", "system"}
        for call in self.rig.system.calls:
            with self.subTest(argv=call["argv"]):
                self.assertIn(call["argv"][0], fixed, "every binary from the fixed list")
                self.assertNotIn(os.path.basename(call["argv"][0]), ("sh", "bash", "dash", "docker"))
                self.assertFalse(set(call["argv"][1:]) & forbidden, call["argv"])
        compose = next(c for c in self.rig.system.calls if c["argv"][0] == self.rig.paths.compose)
        self.assertEqual(compose["argv"][1:], ["--project-directory", "/opt/stacks/media", "-f",
                                               "/opt/stacks/media/compose.yaml", "config", "--format", "json"])
        self.assertIs(compose["stderr"], False, "Compose's standard error is never read")
        self.assertEqual(compose["env"]["DOCKER_CONFIG"], "/var/lib/seraphina-system/docker-config")


class ComposeTests(RigCase):
    """The compose verb keeps the project name, the service hashes and the image names only (§6.3; T34)."""

    def test_only_the_name_leaves(self):
        code, answer = self.ask("compose", dir="/opt/stacks/media")
        self.assertEqual((code, set(answer)), (0, {"ok", "verb", "at", "dir", "name", "hashes", "images"}))
        self.assertEqual(answer["name"], "media")
        self.assertEqual(planted(answer), [])

    def test_failures_are_one_fixed_error(self):
        self.rig.compose_fail = True
        self.assertEqual(self.ask("compose", dir="/opt/stacks/media")[1],
                         {"ok": False, "error": "compose_parse_failed", "words": "Compose could not read this app's folder."})
        self.rig.compose_fail = False
        self.rig.compose_garbage = True
        self.assertEqual(self.ask("compose", dir="/opt/stacks/media")[1]["error"], "compose_parse_failed")
        os.unlink(self.rig.at("/opt/stacks/media/compose.yaml"))
        self.assertEqual(self.ask("compose", dir="/opt/stacks/media")[1]["error"], "compose_files_missing")


class InstallationTests(RigCase):
    """T7: unsafe_install for a group- or world-writable or non-root file anywhere in the chain; explain."""

    def test_unsafe_files(self):
        self.assertEqual(self.ask("info")[0], 0)
        for path, spoil in ((self.rig.paths.helper, (1000, 0)), (os.path.dirname(self.rig.paths.helper), (0, 0o002)),
                            (self.rig.paths.helper_config, (0, 0o020)), (self.rig.paths.tool("ss"), (1000, 0)),
                            (self.rig.paths.compose, (0, 0o002))):
            with self.subTest(path=path):
                self.rig.lstat.spoiled = {path: spoil}
                verb = {"ss": "ports"}.get(os.path.basename(path), "compose" if path == self.rig.paths.compose
                                           else "info")
                fields = {"dir": "/opt/stacks/media"} if verb == "compose" else {}
                code, answer = self.ask(verb, **fields)
                self.assertEqual((code, answer["error"]), (2, "unsafe_install"), answer)
                self.assertIn(path, answer["words"])
        self.rig.lstat.spoiled = {}

    def test_explain_prints_old_and_new_values(self):
        base = self.rig.config(callers={str(SERAPHINA): ["read"]})
        self.rig.write("/etc/seraphina/system-helper.json.new", {**base, "owners": [OWNER, KATE],
                                                                 "keep": {"images_per_app": 3, "max_gib": 80}})
        code, text = self.rig.main(["explain", "helper-config"])
        self.assertEqual(code, 0, text)
        self.assertIn("Allowed: look at your server's layout", text)
        self.assertIn("Your accounts: [1000] → [1000, 1001]", text)
        self.assertIn('Kept versions: {"images_per_app": 2, "max_gib": 50} → {"images_per_app": 3, "max_gib": 80}',
                      text)
        self.rig.write("/etc/seraphina/system-helper.json.new", {**base, "allow": ["read", "access"]})
        code, text = self.rig.main(["explain", "helper-config"])
        self.assertEqual(code, 2)
        self.assertIn("has no access class", text)


class TailscaleTests(RigCase):
    """T9: three keys decoded and nothing else; links refused; templates handled; no auth key ever read."""

    def test_sidecars_named_from_their_state(self):
        code, answer = self.ask("tailscale")
        self.assertEqual(code, 0)
        found = {s["name"]: s for s in answer["sidecars"]}
        self.assertEqual(set(found), {"photos-ts-1", "media-ts-1", "tsshared-ts-1"})
        photos = found["photos-ts-1"]
        self.assertEqual((photos["source"], photos["tailnet"], photos["node"]), ("state", "example.ts.net", "photos"))
        self.assertEqual(photos["web"], [{"host": "photos.example.ts.net", "port": 443, "path": "/",
                                          "proxy": "http://photos:2342", "kind": "proxy", "funnel": False}])
        media = found["media-ts-1"]
        self.assertEqual((media["state"], media["node"], media["hostname_env"]), ("read", "media", None),
                         "PATH only, state in a named volume at /var/lib/tailscale")
        shared = found["tsshared-ts-1"]
        self.assertEqual(shared["node"], "shared", "named from its state, not its stale TS_HOSTNAME")
        self.assertEqual(shared["hostname_env"], "shared-old")
        self.assertEqual(len(shared["web"]), 2)
        self.assertEqual(planted(answer), [], "no key material, profile secret or auth key")

    def test_a_link_in_the_state_path_is_refused(self):
        state = self.rig.at("/opt/stacks/photos/ts-state/tailscaled.state")
        target = self.rig.at("/opt/stacks/photos/elsewhere.state")
        os.replace(state, target)
        os.symlink(target, state)
        _, answer = self.ask("tailscale")
        photos = next(s for s in answer["sidecars"] if s["name"] == "photos-ts-1")
        self.assertEqual((photos["state"], photos["web"]), ("unreadable", []))
        folder = self.rig.at("/opt/stacks/photos/ts-state")
        os.unlink(state)
        os.replace(target, state)
        os.rename(folder, str(folder) + "-real")
        os.symlink(str(folder) + "-real", folder)
        _, answer = self.ask("tailscale")
        self.assertEqual(next(s for s in answer["sidecars"] if s["name"] == "photos-ts-1")["state"], "unreadable",
                         "a linked folder too")

    def test_a_serve_file_with_the_cert_domain_placeholder(self):
        state = json.loads(self.rig.at("/opt/stacks/tsshared/state/tailscaled.state").read_text())
        del state["_serve/a1b2"]
        self.rig.write("/opt/stacks/tsshared/state/tailscaled.state", json.dumps(state), 0o600)
        _, answer = self.ask("tailscale")
        shared = next(s for s in answer["sidecars"] if s["name"] == "tsshared-ts-1")
        self.assertEqual((shared["source"], shared["template"], shared["tailnet"]), ("serve_config", True,
                                                                                    "example.ts.net"))
        self.assertEqual(shared["web"][0]["host"], None, "the owner names it")
        self.assertEqual(planted(answer), [])

    def test_the_decoder_alone(self):
        decoded = sx.decode_state(self.rig.at("/opt/stacks/photos/ts-state/tailscaled.state").read_bytes())
        self.assertEqual(set(decoded), {"profile", "tailnet", "domain", "serve"})
        self.assertEqual(planted(decoded), [])
        self.assertIsNone(sx.decode_state(b"not json"))


class IngressTests(RigCase):
    """T37: the route table's fields only; no secret from a header value, an included file or a key."""

    def test_routes_and_nothing_else(self):
        code, answer = self.ask("ingress")
        self.assertEqual(code, 0)
        gateway = answer["gateways"][0]
        self.assertEqual((gateway["name"], gateway["host_network"]), ("gateway-nginx-1", True))
        routes = gateway["routes"]
        self.assertEqual({(r["server_names"][0] if r["server_names"] else "", r["path"], r["auth"]) for r in routes},
                         {("", "/", "none"), ("beszel.home.example", "/", "sign-in"),
                          ("beszel.home.example", "/oauth2/", "none"), ("files.home.example", "/", "sign-in")}
                         - {("", "/", "none")})
        beszel = next(r for r in routes if r["server_names"] == ["beszel.home.example"] and r["path"] == "/")
        self.assertEqual(beszel["upstream"], [{"host": "172.21.0.5", "port": 8090, "scheme": "http"}])
        self.assertEqual(beszel["listen"], {"address": "192.0.2.10", "port": 443, "ssl": True})
        for route in routes:
            self.assertEqual(set(route), {"listen", "server_names", "path", "match", "upstream", "upstream_name",
                                          "auth"})
        self.assertEqual(planted(answer), [], "no header value, included file, key path or key")
        self.assertNotIn("certs", json.dumps(answer))

    def test_a_config_outside_the_shape(self):
        self.rig.write("/opt/stacks/gateway/conf.d/broken.conf", (FIXTURES / "nginx" / "broken.conf").read_text())
        _, answer = self.ask("ingress")
        self.assertEqual(answer["gateways"][0], {"container": IDS["gateway-nginx"], "name": "gateway-nginx-1",
                                                 "host_network": True, "error": "ingress_parse_failed"})
        self.assertEqual(planted(answer), [])

    def test_explain_ingress_for_the_owner(self):
        code, text = self.rig.main(["explain", "ingress"])
        self.assertEqual(code, 0)
        self.assertIn("192.0.2.10:443 ssl beszel.home.example / -> 172.21.0.5:8090 (asks for the sign-in)", text)
        self.assertNotIn("PLANTED", text)


class PortsDisksGpuTests(RigCase):
    """ss with processes joined to containers and units; a socket-activated service named while only PID 1 holds it;
    lsblk and statvfs; nvidia-smi's processes joined to containers, and its exit codes."""

    def test_ports(self):
        _, answer = self.ask("ports")
        by = {(l["proto"], l["address"], l["port"]): l for l in answer["listeners"]}
        cockpit = by[("tcp", "0.0.0.0", 9090)]
        self.assertEqual((cockpit["socket_unit"], cockpit["unit"]), ("cockpit.socket", "cockpit.service"))
        self.assertNotIn("container", cockpit)
        nginx = by[("tcp", "192.0.2.10", 443)]
        self.assertEqual((nginx["process"], nginx["pid"], nginx["container"]), ("nginx", 2101, IDS["gateway-nginx"]))
        self.assertEqual(by[("tcp", "0.0.0.0", 22)]["unit"], "ssh.service")
        self.assertEqual(by[("udp", "127.0.0.53", 53)]["process"], "systemd-resolve")
        self.assertNotIn("cmdline", json.dumps(answer))

    def test_ss_without_the_process_column(self):
        rows = sx.parse_ss((FIXTURES / "ss-tcp-noproc.txt").read_text(), "tcp")
        self.assertEqual([(r["port"], r["processes"]) for r in rows], [(9090, []), (22, [])])
        self.assertEqual(sx.parse_sockets("not json"), [])

    def test_disks(self):
        _, answer = self.ask("disks")
        self.assertEqual({d["uuid"] for d in answer["devices"] if d["uuid"]},
                         {"AAAA-0001", "0000aaaa-0000-4000-8000-000000000001", "0000aaaa-0000-4000-8000-000000000002"})
        root = next(f for f in answer["filesystems"] if f["mount"] == "/")
        self.assertEqual(root["uuid"], "0000aaaa-0000-4000-8000-000000000001")
        self.assertGreater(root["total"], 0)
        self.assertEqual([f["fstype"] for f in answer["filesystems"]], ["ext4"], "overlay and tmpfs skipped")

    def test_a_covered_mount_point_is_left_out(self):
        """In the namespace of a unit with InaccessiblePaths=-/backup (the helper not handed to PID 1), /backup's
        disk is still listed but a lookup reaches the empty folder over it: statvfs would answer for that folder, so
        the disk is left out, and Disk use refuses it; one mounted again above its cover is the one reached."""
        self.rig.write("/proc/self/mountinfo",
                       "21 1 259:2 / / rw,relatime - ext4 /dev/nvme0n1p2 rw\n"
                       "22 21 8:17 / /backup rw,relatime - ext4 /dev/sdb1 rw\n"
                       "23 21 0:26 / /run rw,nosuid - tmpfs tmpfs rw\n"
                       "24 21 8:33 / /srv/data2 rw,relatime - xfs /dev/sdc1 rw\n"
                       "25 22 0:26 /systemd/inaccessible/dir /backup ro,nosuid - tmpfs tmpfs ro,mode=000\n"
                       "26 21 0:27 / /srv rw - tmpfs tmpfs rw\n"
                       "27 26 8:33 / /srv/data2 rw,relatime - xfs /dev/sdc1 rw\n")
        self.rig.write("/proc/self/mounts", "/dev/nvme0n1p2 / ext4 rw 0 0\n/dev/sdb1 /backup ext4 rw 0 0\n"
                                            "tmpfs /run tmpfs rw 0 0\n/dev/sdc1 /srv/data2 xfs rw 0 0\n"
                                            "tmpfs /backup tmpfs ro 0 0\ntmpfs /srv tmpfs rw 0 0\n"
                                            "/dev/sdc1 /srv/data2 xfs rw 0 0\n")
        os.makedirs(self.rig.at("/backup"), exist_ok=True)
        os.makedirs(self.rig.at("/srv/data2"), exist_ok=True)
        _, answer = self.ask("disks")
        self.assertEqual([(f["mount"], f["fstype"]) for f in answer["filesystems"]],
                         [("/", "ext4"), ("/srv/data2", "xfs")], "never the cover's numbers for /backup")
        self.assertEqual(self.ask("du", mount="/backup")[1]["error"], "bad_field")

    def test_gpu(self):
        _, answer = self.ask("gpu")
        self.assertEqual([(p["process"], p["used_mib"], p.get("container")) for p in answer["processes"]],
                         [("ollama", 9000, IDS["ollama"]), ("jellyfin", 300, IDS["media"])])
        for code, state in ((9, "driver_not_loaded"), (4, "no_permission"), (6, "not_found")):
            self.rig.gpu_exit = code
            self.assertEqual(self.ask("gpu")[1]["state"], state)


class SetupTests(RigCase):
    """T36: the one paste: a terminal and the owner's own sudo; the staged config checked; the sudoers file from
    the helper's own template; the moves in order; every refusal removes the .new files and leaves the old ones."""

    def stage(self, **changes) -> str:
        cfg = {"v": 1, "allow": ["read"], "callers": {str(SERAPHINA): ["read"]}, "owners": [OWNER],
               "stacks_dir": "/opt/stacks", "docker_api": True, "keep": {"images_per_app": 2, "max_gib": 50},
               "smart_types": {}, **changes}
        self.rig.write("/var/lib/seraphina/yourserver/setup/system-helper.json", cfg, 0o600)
        self.rig.write(sx.HELPER + ".new", (Path(__file__).resolve().parent.parent / "bin" / "sf-system").read_bytes(),
                       0o755)
        for path in (self.rig.paths.sudoers, self.rig.paths.helper_config):
            if os.path.exists(path):
                os.unlink(path)
        return str(self.rig.at("/var/lib/seraphina/yourserver/setup/system-helper.json"))

    def setup_run(self, staged, uid=OWNER, answers=("yes",)):
        self.rig.system.answers = list(answers)
        out = io.StringIO()
        code = sx.main(["setup", staged], stdout=out, paths=self.rig.paths, system=self.rig.system,
                       lstat=self.rig.lstat, environ={"SUDO_UID": str(uid)})
        return code, out.getvalue()

    def news(self):
        return [p for p in (self.rig.paths.helper + ".new", self.rig.paths.helper_config + ".new",
                            self.rig.paths.sudoers + ".new") if os.path.lexists(p)]

    def test_the_one_paste(self):
        staged = self.stage()
        moves = []
        real = os.replace

        def replace(a, b):
            moves.append(os.path.basename(str(b)))
            return real(a, b)
        with unittest.mock.patch.object(sx.os, "replace", replace):
            code, text = self.setup_run(staged)
        self.assertEqual(code, 0, text)
        self.assertEqual(text.strip().splitlines()[-1], sx.OK_WORDS, "the one success line, last")
        self.assertIn("Seraphina may: look at your server's layout. She can change nothing on your server.", text)
        self.assertIn("Callers: the user seraphina (uid 991)", text)
        self.assertIn("Your accounts: owner", text)
        self.assertEqual(moves, ["sf-system", "system-helper.json", "seraphina-system"], "helper, config, sudoers")
        self.assertEqual(Path(self.rig.paths.sudoers).read_text(), sx.sudoers_text("seraphina", "sudo"))
        self.assertIn('seraphina ALL=(root) NOPASSWD: /usr/local/libexec/seraphina/sf-system ""',
                      sx.sudoers_text("seraphina", "sudo"))
        self.assertEqual(oct(os.stat(self.rig.paths.sudoers).st_mode & 0o777), "0o440")
        self.assertEqual(self.news(), [])
        visudo = next(c for c in self.rig.system.calls if os.path.basename(c["argv"][0]) == "visudo")
        self.assertEqual(visudo["argv"][1:], ["-cf", "/etc/sudoers.d/seraphina-system.new"])

    def test_sudo_rs_leaves_out_the_defaults_line(self):
        self.assertNotIn("Defaults", sx.sudoers_text("seraphina", "sudo-rs"))
        os.unlink(self.rig.paths.sudo)
        cargo = self.rig.at("/usr/lib/cargo/bin/sudo")
        cargo.parent.mkdir(parents=True, exist_ok=True)
        cargo.write_text("#!/bin/false\n")
        os.symlink(cargo, self.rig.paths.sudo)
        self.assertEqual(sx.sudo_implementation(self.rig.paths), "sudo-rs")
        code, text = self.setup_run(self.stage())
        self.assertEqual(code, 0, text)
        self.assertNotIn("Defaults", Path(self.rig.paths.sudoers).read_text())

    def test_refusals_remove_the_new_files_and_keep_the_old(self):
        old = {"v": 1, "allow": ["read"], "callers": {str(SERAPHINA): ["read"]}}
        cases = [("no terminal", {}, {"terminal": False}), ("not root", {}, {"root": 1000}),
                 ("unknown class", {"allow": ["read", "explode"]}, {}),
                 ("a class of a later version", {"allow": ["read", "maintain"], "callers": {str(SERAPHINA): [
                     "read", "maintain"]}}, {}),
                 ("private for a login account", {"allow": ["read", "private"], "callers": {str(KATE): [
                     "read", "private"]}}, {}),
                 ("a missing caller", {"callers": {"4242": ["read"]}}, {}),
                 ("a caller in docker", {"callers": {str(DOCKERPAL): ["read"]}}, {}),
                 ("an owner who is no person", {"owners": [SERAPHINA]}, {}),
                 ("the harness itself runs it", {}, {"uid": SERAPHINA}), ("no yes", {}, {"answers": ["no"]}),
                 ("visudo refuses", {}, {"visudo": 1})]
        for name, changes, how in cases:
            with self.subTest(case=name):
                staged = self.stage(**changes)
                self.rig.write("/etc/seraphina/system-helper.json", old)
                self.rig.system.terminal = how.get("terminal", True)
                self.rig.system.root = how.get("root", 0)
                self.rig.visudo_exit = how.get("visudo", 0)
                code, text = self.setup_run(staged, uid=how.get("uid", OWNER), answers=how.get("answers", ["yes"]))
                self.assertEqual(code, 2, text)
                self.assertIn("nothing was changed", text)
                self.assertNotIn(sx.OK_WORDS, text)
                self.assertEqual(self.news(), [], "every .new file removed")
                self.assertEqual(json.loads(Path(self.rig.paths.helper_config).read_text()), old, "the old one kept")
                self.assertFalse(os.path.exists(self.rig.paths.sudoers))
        self.rig.system.terminal, self.rig.system.root, self.rig.visudo_exit = True, 0, 0

    def test_a_staged_link_is_refused(self):
        staged = self.stage()
        os.replace(staged, staged + ".real")
        os.symlink(staged + ".real", staged)
        code, text = self.setup_run(staged)
        self.assertEqual(code, 2)
        self.assertIn("without a link", text)

    def test_the_harness_line_never_reaches_setup(self):
        line = json.dumps({"v": 1, "verb": "setup"}).encode() + b"\n"
        code, text = self.rig.main([], line)
        self.assertEqual(json.loads(text)["error"], "unknown_verb", "setup is no request")

    def test_remove(self):
        self.rig.write(self.rig.paths.sudoers.replace(self.rig.paths.root.rstrip("/"), ""), "x\n")
        self.rig.write("/var/lib/seraphina-system/docker-config/x", "y")
        self.rig.system.answers = ["yes", ""]
        out = io.StringIO()
        code = sx.main(["remove"], stdout=out, paths=self.rig.paths, system=self.rig.system, lstat=self.rig.lstat,
                       environ={"SUDO_UID": str(OWNER)})
        self.assertEqual(code, 0, out.getvalue())
        for path in (self.rig.paths.sudoers, self.rig.paths.helper_config, self.rig.paths.helper):
            self.assertFalse(os.path.exists(path), path)
        self.assertTrue(os.path.isdir(self.rig.paths.state), "kept without a second yes")

    def test_remove_refuses_the_harness_and_no_terminal(self):
        out = io.StringIO()
        code = sx.main(["remove"], stdout=out, paths=self.rig.paths, system=self.rig.system, lstat=self.rig.lstat,
                       environ={"SUDO_UID": str(SERAPHINA)})
        self.assertEqual(code, 2)
        self.assertTrue(os.path.exists(self.rig.paths.helper))
        self.rig.system.terminal = False
        self.assertEqual(sx.main(["remove"], stdout=io.StringIO(), paths=self.rig.paths, system=self.rig.system,
                                 lstat=self.rig.lstat, environ={"SUDO_UID": str(OWNER)}), 2)


class InfoTests(RigCase):
    def test_info(self):
        code, answer = self.ask("info")
        self.assertEqual(code, 0)
        self.assertEqual((answer["phase"], answer["set_up"], answer["config"]["allow"]), ("P1c", True, ["read"]))
        self.assertEqual(answer["caller"], {"uid": SERAPHINA, "user": "seraphina", "login_account": False,
                                            "groups": ["seraphina"]})
        self.assertEqual(answer["boot_id"], "00000000-0000-4000-8000-0000000000b1")
        self.assertEqual(answer["holds"][0]["resources"], ["app:a-photos"])
        os.unlink(self.rig.paths.helper_config)
        code, answer = self.ask("info")
        self.assertEqual((code, answer["set_up"]), (0, False))
        self.assertEqual(self.ask("containers")[1]["error"], "not_set_up")


if __name__ == "__main__":
    unittest.main()
