"""End-to-end: a real `factory serve` subprocess driven over HTTP, with every agent played by a scripted model.

Gated (each scenario starts a server and runs whole jobs, ~5-25 s each):

    FACTORY_E2E=1 python3 -m unittest tests.e2e.test_e2e_server -v

Optional: FACTORY_E2E_FIXTURE (fixture repo template, copied with cp -a per
scenario), FACTORY_E2E_SCRATCH (where scenario folders go), FACTORY_E2E_KEEP=1,
FACTORY_E2E_TIMINGS (a JSON file the per-scenario wall times are written to).
See tests/e2e/harness.py and tests/e2e/scripted_llm.py.

Every scenario checks the HTTP API, the job folder, the fixture repo's git state
and the event stream, that the scripted model saw no protocol violation
(reasoning passback, thinking signatures, tool-result pairing) and no tool call
fail that should have worked, and that no process of the scenario outlives it.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import sys
import time
import unittest
from pathlib import Path

from tests.e2e.harness import REPO, FactoryServer, bwrap_scratch
from tests.e2e.scripted_llm import DOC_COMMENT, HTML_BUTTON, PLAN_JOBS, REWORK_COMMENT, SPEC_FROM_BRANCH, Scenario

E2E = os.environ.get("FACTORY_E2E") == "1"
TIMINGS: list[dict] = []
FEATURE = "Add clearCompleted(items) to src/todo.js that drops done items, with a test"
REVIEWERS = ("security", "ux", "ui", "code")
OPUS_AGENTS = ("spec-writer", "builder", "reviewer-security", "approver")
SONNET_AGENTS = ("reviewer-ux", "reviewer-ui", "reviewer-code")


def _bwrap_works() -> bool:
    try:
        from swfactory.harness.sandbox import bwrap_available
        return bwrap_available()
    except Exception:
        return False


def tearDownModule() -> None:
    if not TIMINGS:
        return
    width = max(len(t["test"]) for t in TIMINGS)
    print("\nE2E wall times:", file=sys.stderr)
    for t in TIMINGS:
        print(f"  {t['test']:<{width}}  {t['seconds']:6.1f}s  {t['outcome']}", file=sys.stderr)
    target = os.environ.get("FACTORY_E2E_TIMINGS")
    if target:
        Path(target).write_text(json.dumps(TIMINGS, indent=1) + "\n", encoding="utf-8")


@unittest.skipUnless(E2E, "end-to-end server tests: set FACTORY_E2E=1")
class ServerE2E(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self._started = time.monotonic()
        self.addCleanup(self._record_time)

    def _failed(self) -> bool:
        """Whether this test has recorded a failure or error so far (portable across 3.10-3.13)."""
        outcome = getattr(self, "_outcome", None)
        if outcome is None:
            return False
        errors = getattr(outcome, "errors", None)
        if errors is not None:   # 3.11+: (test, exc_info) pairs on this run
            return any(exc is not None for _, exc in errors)
        result = getattr(outcome, "result", None)   # 3.10
        return bool(result) and any(test is self for test, _ in [*result.errors, *result.failures])

    def _record_time(self) -> None:
        TIMINGS.append({"test": self.id().rsplit(".", 1)[-1], "seconds": round(time.monotonic() - self._started, 2),
                        "outcome": "FAILED" if self._failed() else "ok", "python": sys.version.split()[0]})

    def server(self, scenario: Scenario | None = None, **kwargs) -> FactoryServer:
        server = FactoryServer(self.id().rsplit(".", 1)[-1], scenario, **kwargs)
        self.addCleanup(self._close, server)
        return server

    def _close(self, server: FactoryServer) -> None:
        if self._failed():
            server.failed = True
        strays = server.close()
        if server.failed or strays:
            print(f"\n[e2e] kept {server.root}", file=sys.stderr)
        self.assertEqual(strays, [], "processes of this scenario outlived the server (killed now)")

    # -- shared checks ---------------------------------------------------------------

    def assert_clean_run(self, s: FactoryServer) -> None:
        self.assertEqual(s.scripted.violations, [], "the scripted model saw protocol violations")
        self.assertEqual(s.scripted.tool_errors, [], "tool calls failed that should have worked")

    def assert_merged(self, s: FactoryServer, job: dict, other_worktrees: tuple = ()) -> None:
        """Merged: a merge commit on main, branch and worktree gone, main clean, `node --test` passes on main."""
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        head = s.git("rev-parse", "main").strip()
        self.assertEqual(job["merged_commit"], head)
        self.assertEqual(len(s.git("rev-list", "--parents", "-n", "1", "main").split()), 3, "main's head is a merge")
        self.assertIn(job["branch"], s.git("log", "-1", "--format=%s", "main"))
        self.assertEqual(s.git("branch", "--list", job["branch"]).strip(), "", "the job branch is deleted")
        self.assertFalse(Path(job["worktree"]).exists(), "the job worktree is removed")
        listed = [str(Path(line[len("worktree "):]).resolve())
                  for line in s.git("worktree", "list", "--porcelain").splitlines() if line.startswith("worktree ")]
        expected = [str(Path(s.repo).resolve()), *(str(Path(w).resolve()) for w in other_worktrees)]
        self.assertEqual(sorted(listed), sorted(expected))
        self.assertEqual(s.git("status", "--porcelain"), "", "the main checkout is clean")
        self.assertEqual(s.git("rev-parse", "--abbrev-ref", "HEAD").strip(), "main")
        tests = s.node_test()
        self.assertEqual(tests.returncode, 0, tests.stdout + tests.stderr)

    def stages(self, s: FactoryServer, job_id: str) -> list[str]:
        """The job's stages in event order, consecutive repeats collapsed."""
        seen: list[str] = []
        for event in s.events.of("job.updated", job=job_id):
            stage = event["data"]["job"]["stage"]
            if not seen or seen[-1] != stage:
                seen.append(stage)
        return seen

    def assertSubsequence(self, part: list, whole: list) -> None:
        it = iter(whole)
        self.assertTrue(all(item in it for item in part), f"{part} is not a subsequence of {whole}")

    def requests(self, s: FactoryServer, agent: str, mock=None) -> list:
        mock = mock or s.mock
        return [r for r in list(mock.requests) if s.scripted.agent_of(r.body, r.path) == agent]

    # -- 1 ------------------------------------------------------------------------------

    def test_01_happy_path_with_one_changes_round(self) -> None:
        self._happy_path("none")

    def _happy_path(self, sandbox: str, scratch=None) -> None:
        s = self.server(Scenario(reviews={"code": ["CHANGES", "PASS"]}), sandbox=sandbox, scratch=scratch)
        job = s.submit(FEATURE)
        job_id, job_dir = job["id"], job["job_dir"]
        self.assertRegex(job_id, r"^001-[a-z0-9-]+$")
        self.assertEqual(job["stage"], "queued")
        for agent in OPUS_AGENTS:
            self.assertEqual((job["routing"][agent]["model"], job["routing"][agent]["provider"]), ("opus", "mock-openai"))
        for agent in SONNET_AGENTS:
            self.assertEqual((job["routing"][agent]["model"], job["routing"][agent]["provider"]),
                             ("sonnet", "mock-anthropic"))
        job = s.wait_job(job_id)
        self.assert_merged(s, job)
        self.assertEqual((job["round"], job["resumes"], job["decision"]), (2, 1, "APPROVE"))
        self.assertEqual(job["history"], [
            {"round": 1, "reviews": {"security": "PASS", "ux": "PASS", "ui": "PASS", "code": "CHANGES"}},
            {"round": 2, "reviews": {"security": "PASS", "ux": "PASS", "ui": "PASS", "code": "PASS"}}])
        self.assertEqual(job["reviews"], {r: "PASS" for r in REVIEWERS})
        self.assertEqual((job["agents"]["builder"]["runs"], job["agents"]["builder"]["session"]),
                         (2, "sessions/builder.json"))
        self.assertGreater(job["usage"]["calls"], 20)
        self.assertGreater(job["usage"]["output_tokens"], 0)

        # Job folder (API), in run order.
        files = s.job_files(job_id)
        for rel in ("feature.md", "spec.md", "build.md", "decision.md", "logs/events.jsonl", "logs/01-spec-writer.jsonl",
                    "logs/02-builder.jsonl", "logs/07-builder.jsonl", "logs/12-approver.jsonl",
                    *[f"round-{n}/review-{r}.md" for n in (1, 2) for r in REVIEWERS]):
            self.assertIn(rel, files)
        self.assertFalse([f for f in files if f.startswith("sessions/")], "sessions/ is not listed")
        self.assertEqual(sorted(f for f in files if re.fullmatch(r"logs/\d\d-.*", f))[-1], "logs/12-approver.jsonl")
        self.assertEqual(s.job_file(job_id, "feature.md").strip(), FEATURE)
        self.assertIn("> Feature: " + FEATURE, s.job_file(job_id, "spec.md"))
        build_md = s.job_file(job_id, "build.md")
        self.assertTrue(build_md.startswith("STATUS: READY"))
        self.assertIn("### Round 1", build_md)
        self.assertIn("### Round 2", build_md)
        self.assertTrue(s.job_file(job_id, "round-1/review-code.md").startswith("VERDICT: CHANGES"))
        self.assertTrue(s.job_file(job_id, "round-2/review-code.md").startswith("VERDICT: PASS"))
        self.assertTrue(s.job_file(job_id, "decision.md").startswith("APPROVE"))
        session = Path(job_dir) / "sessions" / "builder.json"
        self.assertEqual(session.stat().st_mode & 0o777, 0o600)
        log = s.get(f"/api/jobs/{REPO}/{job_id}/log?file=logs/02-builder.jsonl")
        self.assertTrue(any(line["type"] == "agent.tool_call" for line in log["lines"]))  # parsed objects

        # The merged code (the round-2 fix included) and the diff view of a merged job.
        todo = s.git("show", "main:src/todo.js")
        self.assertIn("export function clearCompleted(items)", todo)
        self.assertIn(DOC_COMMENT, todo)
        self.assertIn("clearCompleted drops done items", s.git("show", "main:test/todo.test.js"))
        self.assertEqual(s.git("log", "--format=%s", f"{job['base_commit']}..main^2").splitlines(),
                         ["Address round 1 reviews", "Add clearCompleted"])
        diff = s.get(f"/api/jobs/{REPO}/{job_id}/diff")
        self.assertIn("+export function clearCompleted(items) {", diff["diff"])
        self.assertIn("src/todo.js", diff["stat"])

        # The round-2 builder resumed the round-1 session: its requests carry round 1's history.
        fix = [c for c in s.scripted.of("builder", key=job_dir) if c["instruction"].startswith(
            f"Job folder: {job_dir}.") and "Round 1 reviews are in" in c["instruction"]]
        self.assertTrue(fix, "the builder was resumed with the round-1 reviews")
        for c in fix:
            self.assertTrue(c["user_texts"][0].endswith("Build round 1.\nPort: " + str(job["port"]) + "."), c["user_texts"])
            self.assertGreater(c["messages"], 12, "round 1's tool loop is in the resumed history")
        resumed = [r for r in self.requests(s, "builder") if "Round 1 reviews are in" in json.dumps(r.body["messages"][-1])]
        self.assertTrue(resumed)
        history = resumed[0].body["messages"]
        writes = [m for m in history if m.get("role") == "assistant" and "build.md" in json.dumps(m.get("tool_calls"))]
        self.assertTrue(writes, "round 1's Write of build.md is in the round-2 request")
        self.assertTrue(all(m.get("reasoning_content") for m in history if m.get("tool_calls")),
                        "every round-1 tool-call message carries its reasoning_content back (DeepSeek rule)")

        # Wire formats, models, extra_body and credentials per provider.
        openai = [r for r in s.mock.requests if r.path == "/chat/completions"]
        anthropic = [r for r in s.mock.requests if r.path == "/v1/messages"]
        self.assertEqual({s.scripted.agent_of(r.body, r.path) for r in openai}, set(OPUS_AGENTS))
        self.assertEqual({s.scripted.agent_of(r.body, r.path) for r in anthropic}, set(SONNET_AGENTS))
        self.assertTrue(all(r["model"] == "mock-gpt-e2e" and r["reasoning_effort"] == "high" and r["stream"]
                            for r in openai))
        self.assertTrue(all(r["model"] == "mock-claude-e2e" and r["thinking"]["type"] == "adaptive" for r in anthropic))
        self.assertTrue(all(r.headers.get("Authorization") == f"Bearer {s.openai_key}" for r in openai))
        self.assertTrue(all(r.headers.get("x-api-key") == s.anthropic_key and r.headers.get("anthropic-version")
                            for r in anthropic))
        self.assertTrue(all(r.headers.get("x-api-key") is None for r in openai))

        # Events: the stage sequence, the builder's runs, streamed reasoning on both formats.
        self.assertSubsequence(["spec", "build", "review", "build", "review", "approve", "merging", "merged"],
                               self.stages(s, job_id))
        self.assertEqual(len(s.events.of("job.created", job=job_id)), 1)
        starts = s.events.of("agent.start", job=job_id, agent="builder")
        self.assertEqual([(e["data"]["resume"], e["data"]["round"]) for e in starts], [(False, 1), (True, 2)])
        self.assertTrue(all(e["data"]["model_id"] == "mock-gpt-e2e" for e in starts))
        ends = s.events.of("agent.end", job=job_id)
        self.assertEqual(len(ends), 12)
        self.assertTrue(all(e["data"]["ok"] for e in ends), [e["data"] for e in ends if not e["data"]["ok"]])
        for agent in ("builder", "reviewer-ux"):
            self.assertTrue(s.events.of("agent.reasoning", job=job_id, agent=agent), f"no reasoning events of {agent}")
        tools = {e["data"]["name"] for e in s.events.of("agent.tool_call", job=job_id)}
        self.assertTrue({"Read", "Write", "Edit", "Bash", "Glob", "Grep"} <= tools, tools)
        self.assertTrue(all(e["data"]["ok"] for e in s.events.of("agent.tool_result", job=job_id)))
        self.assertTrue(all(e["data"].get("run") for e in s.events.of("agent.text", job=job_id)))

        # Tracked items (section 32): the spec's criteria and the round-1 finding, each proven by its checker.
        items = s.get(f"/api/jobs/{REPO}/{job_id}/items")
        kinds = sorted(item["source"]["type"] for item in items["items"])
        self.assertEqual(kinds.count("spec"), 2, kinds)
        self.assertIn("review", kinds)
        self.assertEqual([(i["id"], i["status"]) for i in items["items"] if i["blocking"] and i["status"] != "verified"],
                         [], "every must-do item was proven before the merge")
        self.assertEqual((items["summary"]["blocking_open"], items["unparsed"]), (0, []))
        self.assertEqual(job["items"]["blocking_open"], 0)
        self.assertTrue(s.events.of("items.updated"))
        self.assert_clean_run(s)

    # -- 2 ------------------------------------------------------------------------------

    def test_02_protected_path_escalates_then_human_approves(self) -> None:
        s = self.server()
        job = s.submit("Add an npm test script to package.json")
        job = s.wait_job(job["id"], "needs-human")
        self.assertEqual(job["reason"], "touches protected path package.json")
        self.assertEqual(job["decision"], "APPROVE", "the approver approved; the deterministic gate stopped it")
        self.assertEqual(s.git("rev-parse", "main").strip(), job["base_commit"], "nothing merged")
        self.assertTrue(Path(job["worktree"]).is_dir())
        self.assertIn(job["branch"], s.git("branch", "--list", job["branch"]))
        self.assertEqual(s.api("POST", f"/api/jobs/{REPO}/{job['id']}/retry").status, 409)

        approved = s.post(f"/api/jobs/{REPO}/{job['id']}/approve")["job"]
        self.assertIn(approved["stage"], ("merging", "merged"))
        job = s.wait_job(job["id"])
        self.assert_merged(s, job)
        self.assertIn('"test": "node --test"', s.git("show", "main:package.json"))
        self.assertEqual(s.api("POST", f"/api/jobs/{REPO}/{job['id']}/approve").status, 409)
        self.assertSubsequence(["approve", "needs-human", "merging", "merged"], self.stages(s, job["id"]))
        messages = [e["data"]["message"] for e in s.events.of("log", job=job["id"])]
        self.assertIn("stage needs-human: touches protected path package.json", messages)
        self.assertEqual(len(s.scripted.of("approver")), 4, "the approver ran once; the human approval needs no run")
        self.assert_clean_run(s)

    # -- 3 ------------------------------------------------------------------------------

    def test_03_approver_escalates_rework_resumes_builder(self) -> None:
        s = self.server(Scenario(approver=["ESCALATE", "APPROVE"]))
        job = s.submit(FEATURE)
        job_id, job_dir = job["id"], job["job_dir"]
        job = s.wait_job(job_id, "needs-human")
        self.assertEqual(job["decision"], "ESCALATE")
        self.assertTrue(job["reason"].startswith("approver escalated: The spec leaves open whether clearing"),
                        job["reason"])
        self.assertTrue(s.job_file(job_id, "decision.md").startswith("ESCALATE"))
        action = f"/api/jobs/{REPO}/{job_id}/rework"
        self.assertEqual(s.api("POST", action, {"note": "   "}).status, 400)
        self.assertEqual(s.api("POST", action, {}).status, 400)

        note = "Make clearCompleted keep the original array untouched and say so in a comment above it."
        reworked = s.post(action, {"note": note})["job"]
        self.assertEqual((reworked["stage"], reworked["round"], reworked["resumes"]), ("build", 2, 0))
        job = s.wait_job(job_id)
        self.assert_merged(s, job)
        self.assertEqual(s.job_file(job_id, "rework-1.md"), note + "\n")
        self.assertEqual([h["round"] for h in job["history"]], [1, 2])
        self.assertTrue(s.job_file(job_id, "decision.md").startswith("APPROVE"))
        self.assertIn(REWORK_COMMENT, s.git("show", "main:src/todo.js"))

        rework = [c for c in s.scripted.of("builder", key=job_dir) if f"Rework: read {job_dir}/rework-1.md" in c["instruction"]]
        self.assertTrue(rework, "the builder got the rework prompt")
        self.assertIn("then update build.md for round 2.", rework[0]["instruction"])
        for c in rework:
            self.assertTrue(any(text.startswith(f"Job folder: {job_dir}.") and "Build round 1." in text
                                for text in c["user_texts"][:1]), "the rework resumed the round-1 session")
        starts = s.events.of("agent.start", job=job_id, agent="builder")
        self.assertEqual([e["data"]["resume"] for e in starts], [False, True])
        self.assertIn("Rework (rework-1.md)", s.job_file(job_id, "build.md"))
        self.assertEqual(s.api("POST", action, {"note": "again"}).status, 409, "no rework of a merged job")
        self.assert_clean_run(s)

    # -- 4 ------------------------------------------------------------------------------

    def test_04_chat_two_messages_transcript_then_handoff(self) -> None:
        s = self.server(Scenario(coder_delay=1.0))
        chat = s.post("/api/chats", {"repo": REPO, "model": "sonnet"}, status=201)["chat"]
        chat_id = chat["id"]
        self.assertRegex(chat_id, r"^c-[0-9a-f]{10}$")
        self.assertEqual((chat["mode"], chat["branch"], chat["status"], chat["model"]),
                         ("worktree", f"chat/{chat_id}", "idle", "sonnet"))
        worktree = Path(chat["worktree"])
        self.assertTrue((worktree / "src" / "todo.js").is_file())
        self.assertEqual(Path(chat["cwd"]), worktree)

        first = "Add a clearCompleted(items) helper to src/todo.js that drops done items, with a test."
        sent = s.api("POST", f"/api/chats/{chat_id}/messages", {"text": first})
        self.assertEqual(sent.status, 202, sent)
        self.assertEqual(sent.json["chat"]["status"], "running")
        busy = s.api("POST", f"/api/chats/{chat_id}/messages", {"text": "and one more thing"})
        self.assertEqual(busy.status, 409, "one turn at a time")
        reply = s.wait_chat(chat_id)
        self.assertEqual((reply["chat"]["status"], reply["chat"]["turns"], reply["chat"]["title"]),
                         ("idle", 1, first[:60]))
        self.assertEqual(s.git("status", "--porcelain", cwd=worktree).split(), ["M", "src/todo.js", "M",
                                                                                "test/todo.test.js"])

        second = "Looks good. Commit it."
        self.assertEqual(s.api("POST", f"/api/chats/{chat_id}/messages", {"text": second}).status, 202)
        reply = s.wait_chat(chat_id)
        self.assertEqual((reply["chat"]["status"], reply["chat"]["turns"], reply["chat"]["error"]), ("idle", 2, None))
        messages = reply["messages"]
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user", "assistant"])
        self.assertEqual((messages[0]["text"], messages[2]["text"]), (first, second))
        self.assertIn("node --test passes", messages[1]["text"])
        self.assertTrue({"Read", "Edit", "Bash"} <= set(messages[1]["tools"]), messages[1])
        self.assertTrue(messages[3]["text"].startswith("Committed"))
        resumed = s.scripted.of("coder", instruction=second)
        self.assertTrue(resumed)
        self.assertTrue(all(c["user_texts"][:1] == [first] and c["fmt"] == "anthropic" for c in resumed),
                        "the second message continued the first's session (Anthropic, thinking kept)")
        anthropic = self.requests(s, "coder")
        self.assertTrue(anthropic and all(r.path == "/v1/messages" for r in anthropic))
        later = [r for r in anthropic if second in json.dumps(r.body["messages"])]
        thinking = [b for m in later[0].body["messages"] if m["role"] == "assistant" for b in m["content"]
                    if b["type"] == "thinking"]
        self.assertTrue(thinking and all(b["signature"].startswith("e2e-sig:") for b in thinking))
        chat_events = s.events.of("agent.text", data__chat=chat_id)
        self.assertTrue(chat_events and all(e["job"] is None for e in chat_events))
        self.assertEqual(len(s.git("log", "--oneline", f"main..chat/{chat_id}").splitlines()), 1)
        self.assertEqual(s.git("status", "--porcelain", cwd=worktree), "")
        base = s.git("rev-parse", "main").strip()

        job = s.post(f"/api/chats/{chat_id}/handoff", {"feature": "Add a clearCompleted(items) helper"})["job"]
        self.assertEqual(job["from_branch"], f"chat/{chat_id}")
        self.assertEqual(s.api("POST", f"/api/chats/{chat_id}/handoff", {"feature": "again"}).status, 409)
        job = s.wait_job(job["id"])
        self.assert_merged(s, job, other_worktrees=(worktree,))
        self.assertEqual(job["base_commit"], base)
        build_md = s.job_file(job["id"], "build.md")
        self.assertTrue(build_md.startswith("STATUS: READY"))
        self.assertIn(f"Built interactively in chat {chat_id}", build_md)
        self.assertEqual(s.scripted.of("builder"), [], "round 1 skipped the builder")
        self.assertNotIn("builder", job["agents"])
        self.assertFalse([f for f in s.job_files(job["id"]) if "builder" in f])
        spec = s.scripted.of("spec-writer", step=0)
        self.assertTrue(spec and SPEC_FROM_BRANCH in spec[0]["instruction"])
        chat_head = s.git("rev-parse", f"chat/{chat_id}").strip()
        self.assertIn(chat_head, s.git("rev-list", "main"))
        self.assertEqual(s.get(f"/api/chats/{chat_id}")["chat"]["job"], job["id"])

        self.assertEqual(s.post(f"/api/chats/{chat_id}/delete"), {"ok": True})
        self.assertFalse(worktree.exists())
        self.assertEqual(s.git("branch", "--list", f"chat/{chat_id}").strip(), "")
        self.assertEqual(s.api("GET", f"/api/chats/{chat_id}").status, 404)
        self.assert_clean_run(s)

    # -- 5 ------------------------------------------------------------------------------

    def test_05_plan_runs_two_jobs_in_order_then_final_review(self) -> None:
        s = self.server()
        request = "Let users clear completed todos: a clearCompleted helper, then a Clear completed button."
        plan = s.post("/api/plans", {"repo": REPO, "request": request}, status=201)["plan"]
        plan_id, plan_dir = plan["id"], plan["plan_dir"]
        self.assertRegex(plan_id, r"^P001-[a-z0-9-]+$")
        self.assertEqual(plan["stage"], "planning")
        reply = s.wait_plan(plan_id, "ready")
        self.assertEqual([(j["n"], j["feature"], j["after"], j["job_id"]) for j in reply["plan"]["jobs"]],
                         [(1, PLAN_JOBS[0], [], None), (2, PLAN_JOBS[1], [1], None)])
        self.assertIn("plan.md", reply["files"])
        self.assertTrue(s.get(f"/api/plans/{REPO}/{plan_id}/file?path=plan.md").startswith("PLAN: READY"))
        self.assertEqual(s.get("/api/state")["jobs"], [], "nothing runs before the human runs the plan")

        running = s.post(f"/api/plans/{REPO}/{plan_id}/run")["plan"]
        self.assertEqual(running["stage"], "running")
        self.assertEqual(s.api("POST", f"/api/plans/{REPO}/{plan_id}/run").status, 409)
        jobs = s.get(f"/api/plans/{REPO}/{plan_id}")["plan"]["jobs"]
        first = jobs[0]["job_id"]
        self.assertTrue(first)
        self.assertIsNone(jobs[1]["job_id"], "job 2 waits for job 1")
        s.wait_job(first)
        final = s.wait_plan(plan_id, "done")
        plan = final["plan"]
        second = plan["jobs"][1]["job_id"]
        self.assertEqual([j["stage"] for j in plan["jobs"]], ["merged", "merged"])
        self.assertEqual(plan["reviews"], [{"k": 1, "verdict": "PASS"}])
        self.assertIn("review-1.md", final["files"])
        self.assertTrue(s.get(f"/api/plans/{REPO}/{plan_id}/file?path=review-1.md").startswith("VERDICT: PASS"))

        # Job 2 was created only after job 1 merged, and built on it.
        merged = [e for e in s.events.of("job.updated", job=first) if e["data"]["job"]["stage"] == "merged"]
        created = s.events.of("job.created", job=second)
        self.assertTrue(merged and created)
        self.assertLess(merged[0]["seq"], created[0]["seq"])
        job1, job2 = s.job(first), s.job(second)
        self.assertEqual(job2["base_commit"], job1["merged_commit"])
        for job in (job1, job2):
            self.assertTrue(job["feature"].endswith(f"Plan context: {plan_id} — see {plan_dir}/plan.md"))
            self.assertEqual(job["stage"], "merged")
        self.assert_merged(s, job2)
        self.assertEqual(len(s.git("log", "--first-parent", "--merges", "--oneline", "main").splitlines()), 2)
        self.assertIn(HTML_BUTTON.strip(), s.git("show", "main:index.html"))
        self.assertIn("clearCompleted", s.git("show", "main:src/app.js"))
        self.assertTrue(s.job_file(second, "build.md").startswith("STATUS: READY"))

        # The lead: planned, then reviewed the combined change; its checkout is gone.
        tasks = [c["instruction"].splitlines()[1] for c in s.scripted.of("lead", step=0)]
        self.assertEqual([t.split(".")[0] for t in tasks], ["Task: Plan", "Task: Final review 1"])
        self.assertIn(f"git diff {plan['base_commit']}..main", s.scripted.of("lead", step=0)[1]["instruction"])
        self.assertFalse((s.data_dir / "worktrees" / REPO / f"{plan_id}-lead").exists())
        # The spec-writers read plan.md (the plan folder is a read root of the plan's jobs).
        reads = [e for e in s.events.of("agent.tool_call", agent="spec-writer") if e["data"]["name"] == "Read"
                 and e["data"]["input"].get("file_path") == f"{plan_dir}/plan.md"]
        self.assertEqual(len(reads), 2)
        self.assertTrue(s.events.of("plan.updated"))
        self.assert_clean_run(s)

    # -- 6 ------------------------------------------------------------------------------

    def test_06_per_run_routing_to_a_local_one_slot_provider(self) -> None:
        s = self.server(Scenario(reviewer_delay={"ux": 1.0, "ui": 1.0}))
        routing = {"reviewer-ux": {"model": "mock-local"}, "reviewer-ui": {"model": "mock-local"}}
        job = s.submit(FEATURE, routing=routing)
        snapshot = job["routing"]
        for agent in ("reviewer-ux", "reviewer-ui"):
            route = snapshot[agent]
            self.assertEqual((route["model"], route["provider"], route["model_id"], route["driver"]),
                             ("mock-local", "mock-local-server", "mock-local-model-7b", "builtin"))
            self.assertEqual((route["provider_entry"]["max_concurrency"], route["provider_entry"]["concurrency_scope"],
                              route["provider_entry"]["api_key_env"]), (1, "run", None))
            self.assertEqual(route["model_entry"]["context_tokens"], 65536)
        self.assertEqual((snapshot["reviewer-code"]["model"], snapshot["builder"]["model"]), ("sonnet", "opus"))
        self.assertNotIn(s.openai_key, json.dumps(job))
        self.assertEqual(s.get(f"/api/jobs/{REPO}/{job['id']}")["job"]["routing"], snapshot)
        job = s.wait_job(job["id"])
        self.assert_merged(s, job)
        self.assertEqual(job["routing"], snapshot)

        local = list(s.local_mock.requests)
        agents = [s.scripted.agent_of(r.body, r.path) for r in local]
        self.assertEqual(set(agents), {"reviewer-ux", "reviewer-ui"})
        self.assertTrue(all(r.path == "/v1/chat/completions" and r["model"] == "mock-local-model-7b" for r in local))
        self.assertTrue(all(r.headers.get("Authorization") is None and r.headers.get("x-api-key") is None
                            for r in local), "a keyless provider sends no credentials")
        self.assertEqual(self.requests(s, "reviewer-ux") + self.requests(s, "reviewer-ui"), [])
        blocks = [a for i, a in enumerate(agents) if i == 0 or agents[i - 1] != a]
        self.assertEqual(len(blocks), 2, f"scope run: one reviewer's whole run, then the other's: {agents}")
        self.assertEqual(s.local_mock.max_concurrent, 1)
        waiting = s.events.of("agent.waiting", job=job["id"])
        self.assertTrue(waiting and all(e["data"]["provider"] == "mock-local-server" for e in waiting))
        start = s.events.of("agent.start", job=job["id"], agent="reviewer-ux")[0]["data"]
        self.assertEqual((start["model"], start["model_id"], start["provider"]),
                         ("mock-local", "mock-local-model-7b", "mock-local-server"))
        self.assert_clean_run(s)

    # -- 7 ------------------------------------------------------------------------------

    def test_07_cancel_during_build(self) -> None:
        s = self.server(Scenario(builder_sleep=47.25))
        job = s.submit(FEATURE)
        job_id = job["id"]

        def sleeping() -> list:
            return [p for p in s.strays() if "sleep 47.25" in p[1]]

        s.wait_for(sleeping, 60, "the builder's Bash command to run")
        self.assertEqual(s.job(job_id)["stage"], "build")
        cancelled = s.post(f"/api/jobs/{REPO}/{job_id}/cancel")["job"]
        self.assertEqual((cancelled["stage"], cancelled["reason"]), ("cancelled", "cancelled by a human"))
        s.wait_for(lambda: not sleeping(), 15, "the cancelled command's process group to be killed")
        s.wait_for(lambda: s.events.of("agent.end", job=job_id, agent="builder"), 15, "the builder run to end")
        time.sleep(1.0)
        job = s.job(job_id)
        self.assertEqual((job["stage"], job["reason"]), ("cancelled", "cancelled by a human"),
                         "the worker's own stop must not overwrite the human's cancel")
        end = s.events.of("agent.end", job=job_id, agent="builder")
        self.assertEqual(len(end), 1)
        self.assertFalse(end[0]["data"]["ok"])
        self.assertEqual([c["agent"] for c in s.scripted.calls if c["agent"] != "spec-writer"],
                         ["builder", "builder"], "nothing ran after the cancel")
        self.assertTrue(Path(job["worktree"]).is_dir(), "the worktree is kept until reject")
        for action in ("retry", "approve", "cancel"):
            self.assertEqual(s.api("POST", f"/api/jobs/{REPO}/{job_id}/{action}").status, 409, action)
        self.assertEqual(s.api("POST", f"/api/jobs/{REPO}/{job_id}/rework", {"note": "x"}).status, 409)
        rejected = s.post(f"/api/jobs/{REPO}/{job_id}/reject", {"reason": "not now"})["job"]
        self.assertEqual((rejected["stage"], rejected["reason"]), ("rejected", "not now"))
        self.assertFalse(Path(job["worktree"]).exists())
        self.assertEqual(s.git("branch", "--list", job["branch"]).strip(), "")
        self.assertEqual(s.git("rev-parse", "main").strip(), job["base_commit"])
        self.assertEqual(s.scripted.violations, [])

    # -- 8 ------------------------------------------------------------------------------

    def _wait_ui_running(self, s: FactoryServer, job_id: str) -> dict:
        def ready() -> dict | None:
            job = s.job(job_id)
            done = all(job["reviews"].get(r) == "PASS" for r in ("security", "ux", "code"))
            return job if job["stage"] == "review" and done and job["reviews"].get("ui") == "running" else None
        return s.wait_for(ready, 90, "three reviews PASS and reviewer-ui still running")

    def _retry_to_merged(self, s: FactoryServer, job_id: str) -> dict:
        retried = s.post(f"/api/jobs/{REPO}/{job_id}/retry")["job"]
        self.assertEqual((retried["stage"], retried["failed_step"], retried["reason"]), ("review", None, None))
        job = s.wait_job(job_id)
        self.assert_merged(s, job)
        self.assertEqual((job["round"], job["resumes"]), (1, 0))
        self.assertEqual(job["history"], [{"round": 1, "reviews": {r: "PASS" for r in REVIEWERS}}])
        runs = {lens: len([c for c in s.scripted.of(f"reviewer-{lens}", step=0) if "Review round 1." in c["instruction"]])
                for lens in REVIEWERS}
        self.assertEqual(runs, {"security": 1, "ux": 1, "code": 1, "ui": 2},
                         "the retried round kept the verdicts on file and re-ran only reviewer-ui")
        return job

    def test_08_server_killed_mid_review_then_restarted_and_retried(self) -> None:
        s = self.server(Scenario(reviewer_delay={"ui": 20}))
        job_id = s.submit(FEATURE)["id"]
        self._wait_ui_running(s, job_id)
        old_pid = s.server_pid()
        s.kill()
        s.restart()
        job = s.job(job_id)
        self.assertEqual((job["stage"], job["reason"], job["failed_step"]),
                         ("failed", "interrupted (server restarted)", "review"))
        self.assertEqual(job["reviews"], {"security": "PASS", "ux": "PASS", "code": "PASS", "ui": "pending"})
        self.assertNotEqual(s.lock_pid(), old_pid)
        self.assertEqual(s.lock_pid(), s.server_pid())
        self._retry_to_merged(s, job_id)
        self.assert_clean_run(s)

    def test_08b_sigterm_mid_review_stops_gracefully(self) -> None:
        s = self.server(Scenario(reviewer_chunk_delay={"ui": 0.4}))
        job_id = s.submit(FEATURE)["id"]
        self._wait_ui_running(s, job_id)
        seconds = s.stop(signal.SIGTERM)
        self.assertLess(seconds, 15, "SIGTERM stops the server promptly, a model reply streaming or not")
        self.assertEqual(s.proc.returncode, 0, s.log_tail())
        self.assertEqual(s.lock_pid(), None, "the lock file is emptied on release")
        on_disk = json.loads((s.data_dir / "jobs" / REPO / job_id / "job.json").read_text())
        self.assertEqual((on_disk["stage"], on_disk["reason"], on_disk["failed_step"]),
                         ("failed", "interrupted (server stopped)", "review"))
        self.assertEqual(on_disk["reviews"]["ui"], "pending")
        s.restart()
        self.assertEqual(s.job(job_id)["reason"], "interrupted (server stopped)")
        self._retry_to_merged(s, job_id)
        self.assert_clean_run(s)

    # -- 9 ------------------------------------------------------------------------------

    def test_09_cli_against_the_live_server(self) -> None:
        s = self.server()
        done = s.cli("status")
        self.assertEqual((done.returncode, done.stdout), (0, ""), done.stderr)
        self.assertIn("no jobs yet", done.stderr)
        done = s.cli("submit", REPO, FEATURE)
        self.assertEqual(done.returncode, 0, done.stderr)
        ref = done.stdout.strip()
        self.assertRegex(ref, r"^todo/001-[a-z0-9-]+$")
        self.assertIn(f"factory logs {ref} -f", done.stderr)
        job = s.wait_job(ref.split("/", 1)[1])
        self.assert_merged(s, job)

        done = s.cli("status")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertRegex(done.stdout.strip(), rf"^{re.escape(ref)}\s+merged\s+r1 0/2\s+{job['merged_commit'][:12]}$")
        done = s.cli("status", REPO, "--json")
        listed = json.loads(done.stdout)
        self.assertEqual([(j["id"], j["stage"]) for j in listed], [(job["id"], "merged")])

        done = s.cli("logs", ref)
        self.assertEqual(done.returncode, 0, done.stderr)
        out = done.stdout
        for agent in ("spec-writer", "builder", "reviewer-security", "reviewer-ux", "reviewer-ui", "reviewer-code",
                      "approver"):
            self.assertRegex(out, rf"(?m)^\d\d:\d\d:\d\d  {re.escape(agent)}\s+started \(builtin, ")
        self.assertIn("started (builtin, opus, round 1)", out)
        self.assertIn("> Write {", out)
        self.assertIn("< Bash ok", out)
        self.assertIn("(thinking) ", out)
        self.assertIn("finished (", out)
        self.assertRegex(out, r"(?m)^\d\d:\d\d:\d\d  log\s+info: stage merged")
        self.assertNotIn("Traceback", out + done.stderr)

        done = s.cli("logs", "todo/999-nope")
        self.assertEqual(done.returncode, 1)
        self.assertIn("no job todo/999-nope", done.stderr)
        done = s.cli("status", "nosuchrepo")
        self.assertEqual(done.returncode, 1)
        self.assertIn("unknown repo 'nosuchrepo'", done.stderr)
        done = s.cli("approve", ref)
        self.assertEqual(done.returncode, 1)
        self.assertIn("cannot approve", done.stderr)
        done = s.cli("url")
        self.assertEqual(done.stdout.strip(), f"http://127.0.0.1:{s.port}/login?token={s.token}")

        done = s.cli("submit", REPO, "-", input="Add an npm test script to package.json\n")
        self.assertEqual(done.returncode, 0, done.stderr)
        second = done.stdout.strip()
        self.assertTrue(second.startswith("todo/002-"))
        done = s.cli("cancel", second)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertRegex(done.stdout, rf"^{re.escape(second)}\s+cancelled")
        s.wait_for(lambda: s.api("POST", f"/api/jobs/{second}/reject").status == 200, 30,
                   "the cancelled job to accept a reject (409 while its worker finishes)")
        self.assertEqual(s.job(second.split("/")[1])["stage"], "rejected")
        self.assertEqual(s.scripted.violations, [])

    # -- 10 -----------------------------------------------------------------------------

    def test_10_keys_never_reach_disk_events_or_agent_shells(self) -> None:
        s = self.server(Scenario(probe_env=("MOCK_", "E2E_MARKER")))
        keys = (s.openai_key, s.anthropic_key)
        job = s.wait_job(s.submit(FEATURE)["id"])
        self.assert_merged(s, job)

        # The agent's shell sees neither key (every api_key_env is scrubbed), though other variables pass.
        probes = [m["content"] for r in self.requests(s, "builder") for m in r.body["messages"]
                  if m.get("role") == "tool" and "E2E_MARKER=" in str(m.get("content"))]
        self.assertTrue(probes, "the builder's env probe ran")
        self.assertNotIn("MOCK_OPENAI_KEY", probes[0])
        self.assertNotIn("MOCK_ANTHROPIC_KEY", probes[0])
        # The keys went to the providers, as credentials only.
        for request in [*s.mock.requests, *s.local_mock.requests]:
            body = json.dumps(request.body)
            for key in keys:
                self.assertNotIn(key, body)
        self.assertTrue(any(r.headers.get("x-api-key") == s.anthropic_key for r in s.mock.requests),
                        "the key from the env file reached the anthropic provider")

        # Nowhere under data_dir, in the event stream (live and replayed), the API or the server log.
        leaks = []
        for path in sorted(s.data_dir.rglob("*")):
            if path.is_file():
                data = path.read_bytes()
                leaks += [f"{path.relative_to(s.data_dir)}" for key in keys if key.encode() in data]
        self.assertEqual(leaks, [])
        texts = {"events (live)": s.events.text(), "events (replay)": self._replay(s),
                 "server log": s.log_path.read_text(errors="replace"), "config.json": s.config_path.read_text()}
        for path in ("/api/state", "/api/config", "/api/doctor", f"/api/jobs/{REPO}/{job['id']}", "/api/chats",
                     "/api/plans", f"/api/jobs/{REPO}/{job['id']}/diff"):
            texts[path] = s.api("GET", path).text
        for where, text in texts.items():
            for key in keys:
                self.assertNotIn(key, text, where)
        providers = s.get("/api/config")["providers"]
        self.assertTrue(providers["mock-openai"]["key_present"] and providers["mock-anthropic"]["key_present"])
        self.assertIn(s.marker, probes[0], "a plain variable does reach the shell")
        self.assert_clean_run(s)

    def test_10b_a_key_the_model_echoes_is_redacted_from_events_and_logs(self) -> None:
        s = self.server()
        s.scenario.echo_secret = s.openai_key
        job = s.wait_job(s.submit(FEATURE)["id"])
        self.assert_merged(s, job)
        texts = [e["data"]["text"] for e in s.events.of("agent.text", job=job["id"], agent="builder")]
        self.assertTrue(any("debug: key ***" in text for text in texts), texts[-3:])
        self.assertNotIn(s.openai_key, s.events.text())
        self.assertNotIn(s.openai_key, self._replay(s))
        job_dir = Path(job["job_dir"])
        for path in [job_dir / "job.json", *sorted((job_dir / "logs").glob("*.jsonl"))]:
            self.assertNotIn(s.openai_key, path.read_text(), path.name)

    def _replay(self, s: FactoryServer) -> str:
        """The raw text /api/events?since=0 replays (read until the stream goes quiet)."""
        sock = socket.create_connection(("127.0.0.1", s.port), timeout=1.5)
        sock.sendall(f"GET /api/events?since=0 HTTP/1.1\r\nHost: 127.0.0.1:{s.port}\r\n"
                     f"Authorization: Bearer {s.token}\r\n\r\n".encode())
        chunks = []
        try:
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                chunks.append(data)
        except socket.timeout:
            pass
        finally:
            sock.close()
        text = b"".join(chunks).decode("utf-8", "replace")
        self.assertIn("event: job.created", text)
        return text

    # -- 11 -----------------------------------------------------------------------------

    def test_11_doctor_over_http(self) -> None:
        s = self.server()
        self.assertEqual(s.api("GET", "/api/doctor", token=False).status, 401)
        checks = s.get("/api/doctor")["checks"]
        self.assertIsInstance(checks, list)
        for check in checks:
            self.assertEqual(set(check), {"name", "ok", "detail", "level"}, check)
            self.assertIsInstance(check["name"], str)
            self.assertIsInstance(check["ok"], bool)
            self.assertIsInstance(check["detail"], str)
            self.assertIn(check["level"], ("error", "warn", "info"))
            if check["ok"]:
                self.assertEqual(check["level"], "info", check)
        names = [c["name"] for c in checks]
        for name in ("python", "git", "config file", "server token", "data dir", "sandbox", f"repo {REPO}",
                     "agents", "routing", "provider mock-openai key", "provider mock-openai reachable",
                     "provider mock-anthropic key", "provider mock-anthropic reachable"):
            self.assertIn(name, names)
        failing = [c for c in checks if not c["ok"] and c["level"] == "error"]
        self.assertEqual(failing, [], "no error-level checks against a healthy config")
        for name in ("provider mock-openai key", "provider mock-openai reachable", "provider mock-anthropic key",
                     "provider mock-anthropic reachable", f"repo {REPO}", "agents", "routing"):
            check = next(c for c in checks if c["name"] == name)
            self.assertTrue(check["ok"], check)
        # The mock /models answers without the key, so the auth check warns (as it should for an open local server).
        auth = [c for c in checks if c["name"].endswith(" auth")]
        self.assertTrue(auth and all(c["level"] == "warn" and not c["ok"] for c in auth), auth)
        text = json.dumps(checks)
        self.assertNotIn(s.openai_key, text)
        self.assertNotIn(s.anthropic_key, text)
        self.assertNotIn(s.token, text)
        done = s.cli("doctor", "--json")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        cli_names = [c["name"] for c in json.loads(done.stdout)]
        self.assertEqual(sorted(set(cli_names)), sorted(set(names)), "the CLI and the API run the same checks")

    # -- 12 -----------------------------------------------------------------------------

    @unittest.skipUnless(_bwrap_works(), "bubblewrap is not usable on this machine")
    def test_12_happy_path_under_bwrap(self) -> None:
        base = bwrap_scratch()
        if base is None:
            self.skipTest("bwrap replaces /tmp with a tmpfs, hiding the repo; set FACTORY_E2E_BWRAP_SCRATCH to a "
                          "non-/tmp path (production repos live under /srv or /home, not /tmp)")
        self._happy_path("bwrap", scratch=base)

    # -- 14 -----------------------------------------------------------------------------

    def test_14_folder_delivery_then_human_merge_with_setup_secrets_and_docs(self) -> None:
        s = self.server(config_patch={"merge": {"delivery": "folder"}})
        setup = s.get("/api/setup")
        self.assertTrue(setup["configured"], setup)
        self.assertIn("folder: approved jobs are exported to", setup["steps"]["output"]["detail"])
        before = s.git("rev-parse", "main").strip()
        job = s.submit(FEATURE)
        self.assertEqual(job["delivery"], "folder", "merge.delivery is the default")
        job = s.wait_job(job["id"], "delivered")
        folder = s.data_dir / "output" / REPO / job["id"]
        self.assertEqual(job["delivered"]["path"], str(folder))
        self.assertEqual(s.git("rev-parse", "main").strip(), before, "a delivery merges nothing")
        self.assertIn(job["branch"], s.git("branch", "--list", job["branch"]))
        self.assertFalse(Path(job["worktree"]).exists())
        self.assertTrue((folder / "files" / "src" / "todo.js").is_file())
        self.assertIn("clearCompleted", (folder / "files" / "src" / "todo.js").read_text())
        self.assertTrue((folder / "changes.patch").read_text().startswith("From "))
        report = (folder / "REPORT.md").read_text()
        self.assertIn(FEATURE, report)
        self.assertIn("| 1 | PASS | PASS | PASS | PASS |", report)
        self.assertSubsequence(["approve", "delivered"], self.stages(s, job["id"]))

        approved = s.post(f"/api/jobs/{REPO}/{job['id']}/approve")["job"]
        self.assertIn(approved["stage"], ("merging", "merged"))
        self.assert_merged(s, s.wait_job(job["id"]))

        secrets = s.get("/api/secrets")
        self.assertEqual(secrets["env_file"], str(s.config_dir / "env"))
        names = {entry["name"]: entry for entry in secrets["secrets"]}
        self.assertEqual((names["MOCK_ANTHROPIC_KEY"]["set"], names["MOCK_ANTHROPIC_KEY"]["in_env_file"]),
                         (True, True))
        self.assertNotIn(s.anthropic_key, json.dumps(secrets))
        value = "e2e-service-token-0123456789"
        reply = s.post("/api/secrets", {"name": "EXTRA_SERVICE_TOKEN", "value": value})
        self.assertEqual(reply["secret"]["name"], "EXTRA_SERVICE_TOKEN")
        self.assertNotIn(value, json.dumps(reply))
        env_file = s.config_dir / "env"
        self.assertIn(f"EXTRA_SERVICE_TOKEN={value}", env_file.read_text())
        self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(value, s.events.text(), "the value never reaches the event stream")

        docs = s.get("/api/docs/procedures")
        self.assertEqual(docs["agents"][0]["name"], "spec-writer")
        booklet = s.api("GET", "/api/docs/procedures.html")
        self.assertEqual(booklet.status, 200)
        self.assertIn("<svg", booklet.text)
        self.assert_clean_run(s)

    # -- 13 -----------------------------------------------------------------------------

    def test_13_repo_scout_drafts_agents_md(self) -> None:
        s = self.server()
        head = s.git("rev-parse", "HEAD").strip()
        self.assertEqual(s.post(f"/api/repos/{REPO}/scout"), {"started": True})

        def finished() -> dict | None:
            status = s.get(f"/api/repos/{REPO}/scout")
            return status if not status["running"] and status["draft"] else None

        status = s.wait_for(finished, 60, "repo-scout to write its draft")
        self.assertTrue(status["draft"].startswith("# AGENTS.md"))
        self.assertIn("src/todo.js", status["draft"])
        self.assertEqual((s.data_dir / "jobs" / REPO / "AGENTS.draft.md").read_text(), status["draft"])
        self.assertTrue((s.data_dir / "jobs" / REPO / "scout" / "logs" / "01-repo-scout.jsonl").is_file())
        self.assertEqual(s.git("rev-parse", "HEAD").strip(), head)
        self.assertEqual(s.git("status", "--porcelain"), "", "the scout never writes the repo")
        scout = self.requests(s, "repo-scout")
        self.assertTrue(scout and all(r.path == "/v1/messages" for r in scout), "repo-scout is on the sonnet tier")
        self.assert_clean_run(s)


if __name__ == "__main__":
    unittest.main()
