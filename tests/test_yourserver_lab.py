"""The lab checks of docs/seraphina/YOUR_SERVER.md §12.2 that P1a and P1b name (DESIGN.md §42): L1, L3, L7, L15 and
L16, then L2, L4 and L5. None of them can run in the unit suite: they need the owner's own host (one read-only read,
Q3), a lab machine with classic sudo and sudo-rs, Docker and Compose, or a systemd-booted Ubuntu 26.04. Each is
reported as skipped with its reason, as the SnapRAID lab does. With SF_YOURSERVER_LAB=1 on such a machine, L1, L2 and
L15 run as read-only reads as the current user (they change nothing); L3, L4, L5, L7 and L16 stay manual steps,
written out below, until their rigs exist (L4 and L5 stop and recreate containers, so they never run on the owner's
server).

    SF_YOURSERVER_LAB=1 python3 -m unittest tests.test_yourserver_lab -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest

from swfactory import yourserver as ys

LAB = os.environ.get("SF_YOURSERVER_LAB") == "1"
WHY = "the Your server lab runs only with SF_YOURSERVER_LAB=1, on the owner's host or a lab machine"


@unittest.skipUnless(LAB, WHY)
class OwnerHostReads(unittest.TestCase):
    def test_l1_one_container_scope_is_readable_as_this_user(self):
        """L1: memory.current, memory.stat, cpu.stat, io.stat and pids.current of one docker-<id>.scope are readable as
        the harness user, and RAM is what the cards would show (compare with `docker stats --no-stream` yourself)."""
        scopes = ys.scan_cgroups()
        running = [s for s in scopes.values() if s["running"]]
        if not running:
            self.skipTest("no running container scope on this machine")
        scope = running[0]
        for name in ("memory.current", "memory.stat", "cpu.stat", "io.stat", "pids.current"):
            with self.subTest(file=name):
                with open(os.path.join(scope["path"], name), encoding="utf-8") as handle:
                    self.assertTrue(handle.read(4096) is not None)
        self.assertIsNotNone(scope["ram"])

    def test_l15_socket_units_map_to_their_services(self):
        """L15: `systemctl list-sockets --all --output=json` as this user maps each address to its socket unit and the
        service it starts (with cockpit.socket and ssh.socket idle and in use, run it twice)."""
        if not shutil.which("systemctl"):
            self.skipTest("no systemctl here")
        done = subprocess.run(["systemctl", "list-sockets", "--all", "--output=json", "--no-pager"], capture_output=True,
                              text=True, timeout=30, check=False)
        rows = json.loads(done.stdout or "[]")
        self.assertIsInstance(rows, list)
        self.assertTrue(all("listen" in row and "unit" in row for row in rows))


@unittest.skipUnless(LAB, WHY)
class Ubuntu2604Reads(unittest.TestCase):
    def test_l2_apt_simulation_as_this_user(self):
        """L2: `apt-get -s upgrade` as a user who is not root (a simulation: it changes nothing): apt 3.x's Inst lines,
        summary and phasing text still match Check 1's parser."""
        from swfactory import serverchecks

        if not shutil.which("apt-get") or os.geteuid() == 0:
            self.skipTest("needs apt-get and a user who is not root")
        done = subprocess.run(["apt-get", "-s", "-o", "Debug::NoLocking=1", "upgrade"], capture_output=True, text=True,
                              timeout=120, check=False, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
        parsed = serverchecks.parse_apt_simulation(done.stdout)
        self.assertTrue(parsed["parsed"], "the summary line parses")
        self.assertEqual(len(parsed["packages"]), parsed["summary"]["upgraded"] + parsed["summary"]["new"])


class ManualLabs(unittest.TestCase):
    def test_l4_the_owners_docker_version(self):
        self.skipTest("L4 not run: needs a lab with the owner's Docker version; check that the contract test's fields "
                      "exist in config.v2.json and hostconfig.json, that HasBeenManuallyStopped is set after docker "
                      "stop and cleared at the next start, and whether container labels carry the image's version "
                      "label")

    def test_l5_compose_from_its_plugin_path_with_the_daemon_stopped(self):
        self.skipTest("L5 not run: needs a lab with the owner's Compose version; run "
                      "/usr/libexec/docker/cli-plugins/docker-compose config --hash '*', --images and --format json "
                      "with dockerd stopped; after up -d the hashes equal the containers' config-hash labels, and they "
                      "differ after the file changes")

    def test_l3_apps_answer_with_the_harness_stopped_and_the_helper_removed(self):
        self.skipTest("L3 not run: needs a lab machine with a fixture app behind a local proxy and a published port; "
                      "stop the harness, probe both; remove the helper, probe both again (N5, N6)")

    def test_l7_the_sudoers_line_under_both_sudo_programs(self):
        self.skipTest("L7 not run: needs Ubuntu 26.04 with sudo-rs as sudo and classic sudo as sudo.ws; as Seraphina's "
                      "user with §6.11's line: a piped request works, any argument is refused, inside bwrap sudo -n "
                      "fails; the Defaults!<path> !use_pty line, the \"\" rule, @includedir skipping dotted names and "
                      "visudo -cf under each program")

    def test_l16_the_gateway_config_reads_in_shape(self):
        self.skipTest("L16 not run: needs the owner's host; he runs `sudo /usr/local/libexec/seraphina/sf-system "
                      "explain ingress` and checks each route's upstream against the app he opens there, with no "
                      "secret in the answer")

    def test_l1_and_l15_need_the_owners_host(self):
        if LAB:
            return
        self.skipTest("L1 and L15 not run: " + WHY)


if __name__ == "__main__":
    unittest.main()
