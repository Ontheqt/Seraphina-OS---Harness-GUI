"""The Blueprint's helper mode (docs/seraphina/YOUR_SERVER.md §11.9; DESIGN.md §42): `factory snapshot --helper`
asks Seraphina's system access (bin/sf-system's read verbs) for what used to need `sudo -n`: the Docker section and
indexes.containers from `containers` (and `docker` while it answers), listeners with their processes from `ports`,
SMART from `smart`, Compose's hashes and images from `compose`, and from P1c a users section from `access`. The
host is the Blueprint tests' fixture runner; the helper is the placeholder server of tests/sfsystem_rig.py."""

from __future__ import annotations

import json
import unittest

from swfactory.blueprint.collect import SECTIONS, collect
from swfactory.helpers import HelperClient
from tests.sfsystem_rig import SERAPHINA, Rig, sx
from tests.test_blueprint import KNOWN, FakeRunner


class HelperModeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = Rig()
        self.addCleanup(self.rig.cleanup)

    def capture(self) -> tuple[dict, FakeRunner]:
        runner = FakeRunner(self)
        helper = HelperClient(sx.HELPER, runner=self.rig.runner, name="Seraphina's system access")
        return collect(runner, known_secrets=[KNOWN], helper=helper), runner

    def test_root_probes_come_from_the_helper(self) -> None:
        snapshot, runner = self.capture()
        self.assertFalse(any(call[0] in ("sudo", "docker", "smartctl") for call in runner.calls),
                         "no sudo, no Docker socket, no smartctl of its own")
        self.assertFalse(any(call[0] == "ss" for call in runner.calls))
        self.assertEqual(snapshot["docker"]["containers"]["command"], ["sf-system", "containers"])
        self.assertEqual(snapshot["docker"]["containers"]["status"], "ok")
        containers = snapshot["indexes"]["containers"]
        self.assertIn("photos-db-1", containers)
        self.assertEqual(containers["photos-db-1"]["evidence"], "sf-system.containers")
        self.assertEqual(containers["photos-photos-1"]["health"], "healthy", "from the read-only API while it runs")
        self.assertTrue(any(e["source"] == "sf-system.ports" for e in snapshot["indexes"]["endpoints"]))
        self.assertEqual(snapshot["storage"]["smart"]["status"], "ok")
        compose = [n for n in snapshot["compose_files"] if n.startswith("helper:")]
        self.assertTrue(compose)
        text = json.dumps(snapshot)
        for word in ("PLANTED", "tskey-", "privkey:"):
            self.assertNotIn(word, text)
        self.assertTrue(any(line.startswith("Helper mode:") for line in snapshot["scope_limits"]))
        self.assertEqual(snapshot["users"]["access"]["status"], "denied", "the private class is not allowed")
        self.assertEqual([k for k in snapshot if k in SECTIONS], list(SECTIONS))

    def test_users_once_the_private_class_is_allowed(self) -> None:
        classes = ["private", "read"]
        self.rig.config(allow=classes, callers={str(SERAPHINA): classes})
        self.rig.ask("access")
        job = self.rig.units[-1][-1]
        self.rig.write("/proc/self/cgroup", f"0::/system.slice/{sx.unit_name('access', job)}.service\n")
        sx.run_job(job, self.rig.paths, self.rig.system, self.rig.lstat, out=lambda _t: None)
        snapshot, _ = self.capture()
        record = snapshot["users"]["access"]
        self.assertEqual(record["status"], "ok")
        rows = json.loads(record["stdout"])["result"]["rows"]
        self.assertTrue(any(r["kind"] == "sudo" and r["subject"] == "owner" for r in rows), "names kept")
        self.assertNotIn("PLANTED", record["stdout"])


if __name__ == "__main__":
    unittest.main()
