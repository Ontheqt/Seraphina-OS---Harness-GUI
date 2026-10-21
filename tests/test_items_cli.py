"""Tests for sf-items and its stdio MCP server (swfactory/items_cli.py, bin/sf-items; DESIGN.md section 32).

A real (not started) orchestrator.Factory behind the real HTTP server; an outside agent's token is made through
the Factory and used by the command (a subprocess) and by the MCP server (driven by the harness's own McpClient).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from swfactory import items_cli
from swfactory import server as srv
from swfactory.config import Config
from swfactory.events import EventBus
from swfactory.harness.mcp import McpClient
from swfactory.orchestrator import Factory
from tests.helpers import make_repo

ROOT = Path(__file__).resolve().parents[1]
SF_ITEMS = ROOT / "bin" / "sf-items"
TOKEN = "master-token-0123456789abcdef"


class ItemsCliCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-items-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        home = self.tmp / "home"
        patcher = mock.patch.dict(os.environ, {"HOME": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = make_repo(self.tmp, "repo")
        self.config = Config(home / ".config" / "software-factory" / "config.json", {
            "server": {"token": TOKEN}, "data_dir": str(self.tmp / "data"),
            "repos": [{"name": "app", "path": str(self.repo)}]})
        self.config.save()
        self.bus = EventBus(secrets_provider=lambda: [TOKEN])
        self.factory = Factory(self.config, self.bus, runner_factory=self.no_runner)
        self.addCleanup(self.factory.stop, 5)
        self.server = srv.make_server(self.config, self.factory, self.bus, "127.0.0.1", 0, ping_interval=0.05)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()

        def stop() -> None:
            self.server.shutdown()
            self.server.server_close()
            thread.join(5)
        self.addCleanup(stop)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.token = self.factory.create_agent_token("claude-code")["token"]

    @staticmethod
    def no_runner(config: Config, driver: str) -> None:
        raise RuntimeError("no agent may run in these tests")

    def sf_items(self, *args: str, token: str | None = None) -> subprocess.CompletedProcess:
        env = {**os.environ, "SF_ITEMS_URL": self.url, "SF_ITEMS_TOKEN": self.token if token is None else token}
        return subprocess.run([str(SF_ITEMS), *args], capture_output=True, text=True, env=env, timeout=60,
                              cwd=self.tmp)


class CommandTest(ItemsCliCase):
    def test_add_list_and_done_with_a_token(self) -> None:
        added = self.sf_items("add", "Rotate the backup key", "--detail", "after the new disk")
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual(added.stdout.strip(), "system:S-1  [To do]  Rotate the backup key")
        follow = self.sf_items("add", "Update the README", "--scope", "repo:app")
        self.assertEqual(follow.stdout.split()[0], "repo:app/F-1", follow.stderr)
        listed = self.sf_items("list")
        self.assertEqual(sorted(line.split()[0] for line in listed.stdout.splitlines()),
                         ["repo:app/F-1", "system:S-1"])
        rows = json.loads(self.sf_items("list", "--scope", "system", "--json").stdout)
        self.assertEqual((rows[0]["owner"], rows[0]["verifier"]), ("agent:claude-code", "human"))
        done = self.sf_items("done", "system:S-1", "--command", "restic check", "--result", "no errors")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("[Waiting for a check]", done.stdout)
        item = self.factory.scope_items("system")["items"][0]
        self.assertEqual((item["status"], item["evidence"][0]["kind"], item["evidence"][0]["check"]),
                         ("done", "command", "unchecked"))
        doing = self.sf_items("update", "repo:app/F-1", "doing", "--reason", "started")
        self.assertEqual(doing.returncode, 0, doing.stderr)

    def test_what_the_token_can_never_do(self) -> None:
        self.factory.add_item("system", title="A person's item")
        refused = self.sf_items("update", "system:S-1", "doing")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("is not yours", refused.stderr)
        bad_verb = self.sf_items("update", "system:S-1", "verified")
        self.assertEqual(bad_verb.returncode, 2, "verify is not even a choice")
        self.assertIn("invalid choice", bad_verb.stderr)
        forged = self.sf_items("list", token="sfi_not-a-real-token")
        self.assertEqual(forged.returncode, 1)
        self.assertIn("refused the token", forged.stderr)
        missing = self.sf_items("list", token="")
        self.assertIn("set SF_ITEMS_TOKEN", missing.stderr)
        self.factory.revoke_agent_token("claude-code")
        self.assertIn("refused the token", self.sf_items("list").stderr)


class McpServerTest(ItemsCliCase):
    def test_the_three_tools_over_stdio(self) -> None:
        cfg = SimpleNamespace(name="sf-items", command=str(SF_ITEMS), args=["mcp"],
                              env={"SF_ITEMS_TOKEN": self.token, "SF_ITEMS_URL": self.url}, cwd=None, timeout=30.0,
                              tools=None, startup_timeout=30.0)
        client = McpClient(cfg, self.tmp)
        client.start()
        self.addCleanup(client.close)
        self.assertEqual([tool["name"] for tool in client.tools()], ["items_list", "items_add", "items_update"])
        text, error = client.call("items_add", {"title": "Check the nightly backup log"})
        self.assertFalse(error, text)
        self.assertEqual(text, "Added system:S-1: Check the nightly backup log")
        text, error = client.call("items_list", {})
        self.assertEqual((text, error), ("system:S-1  [To do]  Check the nightly backup log", False))
        text, error = client.call("items_update", {"ref": "system:S-1", "to": "done", "evidence": [
            {"kind": "file", "ref": "/var/log/backup.log"}]})
        self.assertEqual((text, error), ("system:S-1 is now Waiting for a check.", False))
        text, error = client.call("items_update", {"ref": "system:S-1", "to": "verified"})
        self.assertTrue(error)
        self.assertIn("to must be one of doing, done, defer", text)
        text, error = client.call("items_add", {})
        self.assertTrue(error)

    def test_the_server_answers_without_a_harness_until_a_tool_is_called(self) -> None:
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "ping"},
                    {"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
                    {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "items_list"}}]
        stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in requests) + "not json\n")
        stdout = io.StringIO()

        def no_api() -> items_cli.ItemsApi:
            raise items_cli.ItemsCliError("set SF_ITEMS_TOKEN to an agent token")
        self.assertEqual(items_cli.serve_mcp(no_api, stdin, stdout), 0)
        replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3, 4, 5])
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(len(replies[1]["result"]["tools"]), 3)
        self.assertEqual(replies[3]["error"]["code"], -32601)
        self.assertTrue(replies[4]["result"]["isError"])
        self.assertIn("SF_ITEMS_TOKEN", replies[4]["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
