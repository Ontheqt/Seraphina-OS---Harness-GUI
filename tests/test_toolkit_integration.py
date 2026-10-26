"""Where the operator's toolkit meets the rest of the harness (DESIGN.md sections 23, 36, 37 and 38).

- An operator's activity lists what the harness asked of her GPU custody register and what it answered (source
  ``custody``, from the custody journal), and her drift checks (source ``drift``, from each source's check
  journal); any other agent sees only the custody lines whose lease purpose names it, and no drift.
- Drift findings and the operator's receipt claims share the system scope: the same words from both are two items
  with keys of their own, and neither family's checks open, close or reopen the other's items.
- A judge's placement is shaped by both the independence guard and custody: an independent alternate the register
  gives no lease for is no way out (the judge stays on its own model, reduced, saying why); one it grants runs
  with its lease; and every lease taken on the way (an overflow the guard moved off) goes back.
"""

from __future__ import annotations

import json
import stat
import time
from pathlib import Path
from typing import Any

from swfactory import activity as act
from swfactory import custody
from swfactory.config import ProviderConfig, deep_merge, sandbox_masks
from swfactory.custody import Claims, CustodySettings
from swfactory.driftsources import dedupe_key
from swfactory.events import EventBus
from swfactory.orchestrator import Factory
from swfactory.receipts import Receipts
from tests.mock_custody import MockRegister
from tests.test_driftsources import HOST, SOURCE_TOKEN, MockSource
from tests.test_operator import OperatorCase, at, receipt
from tests.test_orchestrator import REPO, reviews
from tests.test_routing import PlacementTestCase


class OperatorToolkitTest(OperatorCase):
    """Persona "ops" is the operator; a mock custody register and a mock drift source (her checker)."""

    def setUp(self) -> None:
        super().setUp()
        self.register = MockRegister()
        self.register.start()
        self.addCleanup(self.register.stop)
        self.source = MockSource()
        self.addCleanup(self.source.stop)
        token = self.tmp / "drift.token"
        token.write_text(SOURCE_TOKEN + "\n")
        self.config = self.make_config(drift_sources={"checker": {
            "url": self.source.url, "token_file": str(token), "host_header": HOST, "schema_versions": ["1"]}})
        self.factory = Factory(self.config, EventBus())
        self.addCleanup(custody.set_journal, None)
        self.data = Path(self.config.data_dir)
        self.data.mkdir(parents=True, exist_ok=True)   # the server makes it at start; the journal never does
        self.gpu = ProviderConfig(name="gpu", type="openai", base_url="http://127.0.0.1:9/v1", lazy_load=True,
                                  custody=CustodySettings(url=self.register.url, timeout=3))

    def custody_calls(self) -> None:
        """A job's builder run takes a lease and gives it back; an operator's wake turn is refused one."""
        claims = Claims("job demo/001-add-x builder")
        self.assertEqual(claims(self.gpu, "flash"), (True, ""))
        claims.release("the run ended")
        self.register.maintenance = True
        self.assertEqual(Claims("wake ops W-000001")(self.gpu, "flash")[0], False)
        self.register.maintenance = False

    def drift_item(self) -> dict:
        return next(item for item in self.factory.items(scope="system")["items"]
                    if (item.get("source") or {}).get("type") == "drift:checker")

    def receipt_items(self) -> list[dict]:
        return [item for item in self.factory.items(scope="system")["items"]
                if (item.get("source") or {}).get("type") == "receipt"]

    def test_her_activity_lists_custody_and_drift_by_name(self) -> None:
        self.custody_calls()
        self.source.observe([{"key": "logrotate", "title": "logrotate keeps 7 days, not 14"}])
        self.assertEqual(self.factory.check_drift_source("checker")["status"], "ok")
        self.source.clock[0] += 120
        self.source.observe([])
        self.assertEqual(self.factory.check_drift_source("checker")["result"]["closed"], [self.drift_item()["ref"]])
        self.source.clock[0] += 120
        self.source.observe([])   # nothing changed: not worth a record
        self.factory.check_drift_source("checker")

        result = self.factory.activity("ops", since="1h")
        self.assertTrue(result["operator"])
        custody_entries = [e for e in result["entries"] if e["source"] == "custody"]
        self.assertEqual([e["kind"] for e in custody_entries], ["lease granted", "lease released", "lease denied"])
        granted, released, denied = custody_entries
        self.assertEqual((granted["purpose"], granted["provider"], granted["model"]),
                         ("job demo/001-add-x builder", "gpu", "flash"))
        self.assertIn("lease granted: flash on gpu for job demo/001-add-x builder", granted["summary"])
        self.assertIn("resident afterwards: nothing", released["summary"])
        self.assertIn("MAINTENANCE", denied["summary"])
        drift = [e for e in result["entries"] if e["source"] == "drift"]
        self.assertEqual([e["summary"] for e in drift], ["checker: ok (was never); opened 1", "checker: ok; closed 1"],
                         "a check that changed nothing is not recorded")
        self.assertEqual([e["items"] for e in drift], [[self.drift_item()["ref"]]] * 2)
        self.assertEqual((result["sources"]["custody"], result["sources"]["drift"]), (3, 2))
        for entry in custody_entries + drift:
            path = Path(entry["evidence"]["file"])
            line = json.loads(path.read_text().splitlines()[entry["evidence"]["line"] - 1])
            self.assertTrue(line, "the evidence names the very line")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(Path(custody_entries[0]["evidence"]["file"]).parent, self.data / "custody")
        self.assertIn(self.data / "custody", [Path(p) for p in sandbox_masks(self.config)["dirs"]],
                      "a lease's purpose names wakes and personas: hidden from agents' Bash")
        journals = "".join(path.read_text() for path in self.data.glob("drift-sources/*/checks-*.jsonl"))
        self.assertNotIn(SOURCE_TOKEN, journals)
        self.assertNotIn("logrotate keeps", journals, "a finding's title stays in the tracker")

    def test_another_agent_sees_its_own_leases_and_no_drift(self) -> None:
        self.custody_calls()
        self.source.observe([{"key": "k1", "title": "drifted"}])
        self.factory.check_drift_source("checker")
        builder = act.activity(self.config, agent="builder", since="1h")
        self.assertFalse(builder["operator"])
        self.assertEqual([e["kind"] for e in builder["entries"]], ["lease granted", "lease released"],
                         "the lines whose purpose names it; never the operator's drift checks")
        other = act.activity(self.config, agent="ops-two", since="1h")
        self.assertEqual(other["entries"], [], "a persona that is not the operator")
        unmarked = self.make_config()
        unmarked.raw["agents"]["ops"] = {}
        ops = act.activity(unmarked, agent="ops", since="1h")
        self.assertEqual([e["kind"] for e in ops["entries"]], ["lease denied"],
                         "unmarked, she sees only her own wake's lease")

    def test_the_journal_never_holds_a_lease_up(self) -> None:
        """A journal that cannot be written skips the line: the claim and the release go on."""
        (self.data / "custody").write_text("not a folder")
        claims = Claims("job demo/002 builder")
        self.assertEqual(claims(self.gpu, "flash"), (True, ""))
        self.assertEqual(claims.release("done")[0]["state"], "released")
        for journal in (None, self.tmp / "gone" / "data"):   # none, or a data dir that does not exist
            custody.set_journal(journal)
            claims = Claims("job demo/003 builder")
            self.assertEqual(claims(self.gpu, "flash"), (True, ""))
            claims.release("done")   # leases are process-wide: none may outlive the test
        self.assertEqual(self.register.leases, {})
        self.assertFalse((self.tmp / "gone").exists(), "the journal never makes a data dir")

    def test_drift_and_receipt_items_share_the_system_scope_without_duplicate_keys(self) -> None:
        words = "restore drill not done"
        self.source.observe([{"key": words, "title": words}])
        self.factory.check_drift_source("checker")
        self.write("CHG-1.json", receipt(id="CHG-1", time=at(0.5), not_verified=[words]))
        self.factory.operator_scan()
        drift, [claim] = self.drift_item(), self.receipt_items()
        self.assertNotEqual(drift["source"]["hash"], claim["source"]["hash"])
        self.assertEqual(drift["source"]["hash"], dedupe_key("checker", words))
        self.assertEqual(Receipts.claim_hash(words), claim["source"]["hash"])
        self.assertEqual((drift["status"], claim["status"]), ("open", "proposed"))
        self.assertEqual(len(self.factory.items(scope="system")["items"]), 2)

        self.source.clock[0] += 120
        self.source.observe([])   # the checker no longer reports it: only its own item closes
        self.factory.check_drift_source("checker")
        self.factory.operator_scan()
        self.assertEqual(self.drift_item()["status"], "verified")
        self.assertEqual([item["status"] for item in self.receipt_items()], ["proposed"])

        self.write("CHG-2.json", receipt(id="CHG-2", time=at(0.2), not_verified=[], verified=[words]))
        self.factory.operator_scan()   # she reports it verified: her claim moves, the drift item does not
        self.assertEqual(self.drift_item()["status"], "verified")
        self.source.clock[0] += 120
        self.source.observe([{"key": words, "title": words}])   # it recurs: its own item reopens, nothing new
        self.factory.check_drift_source("checker")
        self.assertEqual(self.drift_item()["status"], "open")
        self.assertEqual(len(self.factory.items(scope="system")["items"]), 2, "no duplicate on either side")
        self.assertEqual(len(self.receipt_items()), 1)


class JudgeUnderCustodyTest(PlacementTestCase):
    """The independence guard and GPU custody both place a judge: the code lens shares the builder's family
    after an escalation (round 2's builder is on GPT, like the lens), and its independent alternate is on a
    server under custody."""

    def setUp(self) -> None:
        super().setUp()
        self.register = MockRegister()
        self.register.start()
        self.addCleanup(self.register.stop)

    def guard_config(self, **sections: Any) -> Any:
        under = {"lazy_load": True, "custody": {"url": self.register.url, "timeout": 3}}
        return self.placed_config(**deep_merge({
            "providers": {"altp": {"type": "openai", "base_url": "http://127.0.0.1:59003/v1", "api_key_env": None,
                                   **under},
                          "lensp": {"type": "openai", "base_url": "http://127.0.0.1:59004/v1", "api_key_env": None,
                                    **under},
                          "llp": {"type": "openai", "base_url": "http://127.0.0.1:59005/v1", "api_key_env": None,
                                  **under}},
            "models": {"flash": {"family": "qwen"}, "cheap": {"family": "xiaomi"},
                       "strong": {"family": "openai"}, "stronger": {"family": "openai"},
                       "alt": {"provider": "altp", "model": "alt-1", "max_tokens": 1000, "family": "meta"},
                       "judge": {"provider": "api", "model": "judge-1", "max_tokens": 1000, "family": "anthropic"},
                       "lens": {"provider": "lensp", "model": "lens-1", "max_tokens": 1000, "family": "google",
                                "overflow": "local-lens"},
                       "local-lens": {"provider": "llp", "model": "local-lens-1", "max_tokens": 1000,
                                      "family": "qwen"}},
            "agents": {"reviewer-code": {"model": "strong", "independent_alternate": "alt"},
                       "approver": {"model": "judge"}},
        }, sections))

    def held_by_another(self, provider: str) -> None:
        self.register.leases["held"] = {"id": "held", "provider": provider, "model": "owners-model",
                                        "holder": "operator", "purpose": "maintenance", "until": time.time() + 3600}

    def runs(self, job: dict, agent: str) -> list[dict]:
        return [run for run in job["runs"] if run["agent"] == agent]

    def test_a_granted_alternate_runs_with_its_lease(self) -> None:
        job = self.run_job(self.start_placed(self.guard_config(), reviewer_code=reviews("CHANGES", "PASS")))
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual([call.ctx.model.key for call in self.recorder.of("reviewer-code")], ["strong", "alt"])
        start = [e.data for e in self.events(job, "agent.start") if e.agent == "reviewer-code"][1]
        self.assertEqual((start["independence"], start["custody"]["provider"], start["custody"]["model"]),
                         ("alternate", "altp", "alt-1"))
        self.assertEqual(self.runs(job, "reviewer-code")[1]["custody"]["lease_id"], start["custody"]["lease_id"])
        self.assertEqual(self.register.leases, {}, "the alternate's lease went back when the run ended")
        purposes = [body.get("purpose") for _, path, _, body in self.register.requests if path == "/leases"]
        self.assertEqual(purposes, [f"job {REPO}/{job['id']} reviewer-code"])
        self.assertNotIn("independence", job)

    def test_an_alternate_the_register_refuses_is_no_way_out(self) -> None:
        self.held_by_another("altp")
        job = self.run_job(self.start_placed(self.guard_config(), reviewer_code=reviews("CHANGES", "PASS")))
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual([call.ctx.model.key for call in self.recorder.of("reviewer-code")], ["strong", "strong"])
        run = self.runs(job, "reviewer-code")[1]
        self.assertEqual((run["independence"], run["shared_model"]["builder_model"]), ("reduced", "strong"))
        self.assertNotIn("custody", run)
        logs = self.logs(job)
        self.assertIn("build altp/alt-1 is unavailable: GPU custody: LEASED", logs)
        self.assertIn("independence reduced: strong is of the openai family, like the builder's strong in round 2 "
                      "(its independent alternate alt does not answer)", logs)
        self.assertEqual(list(self.register.leases), ["held"], "nothing of the harness's is left at the register")

    def test_an_overflow_the_guard_moves_off_gives_its_lease_back(self) -> None:
        """The lens's own server refuses a lease, so it overflows onto a Qwen model, the builder's family, and is
        granted a lease there; the guard moves it to its alternate instead, and that overflow's lease goes back
        before the run starts."""
        self.held_by_another("lensp")
        config = self.guard_config(agents={"reviewer-code": {"model": "lens", "independent_alternate": "alt"}})
        job = self.run_job(self.start_placed(config))
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual([call.ctx.model.key for call in self.recorder.of("reviewer-code")], ["alt"])
        start = self.start_event(job, "reviewer-code").data
        self.assertEqual((start["independence"], start["shared_model"]["model"], start["custody"]["provider"]),
                         ("alternate", "local-lens", "altp"))
        acquired = [body.get("provider") for _, path, _, body in self.register.requests if path == "/leases"]
        self.assertEqual(acquired, ["lensp", "llp", "altp"])
        self.assertEqual(list(self.register.leases), ["held"], "the overflow's and the alternate's leases went back")
        self.assertIn("build lensp/lens-1 is unavailable: GPU custody: LEASED", self.logs(job))
        self.assertIn("GPU custody lease", self.logs(job))


if __name__ == "__main__":
    import unittest

    unittest.main()
