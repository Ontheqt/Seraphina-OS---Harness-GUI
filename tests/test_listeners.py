"""Tests for swfactory.listeners: what listens on TCP here, from /proc/net/tcp and tcp6, and the model servers among it."""

from __future__ import annotations

import shutil
import socket
import tempfile
import unittest
from pathlib import Path

from swfactory import listeners
from tests.mock_llm import MockLLM
from tests.test_doctor import AuthServer

HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
TCP = (HEADER +
       "   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 1 1\n"   # 127.0.0.1:8080
       "   1: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 2 1\n"   # 0.0.0.0:22
       "   2: 0100007F:1F90 0100007F:D2F0 01 00000000:00000000 00:00000000 00000000  1000        0 3 1\n"   # established
       "   3: 0500000A:1EB4 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 4 1\n")  # 10.0.0.5:7860
TCP6 = (HEADER +
        "   0: 00000000000000000000000000000000:1F90 00000000000000000000000000000000:0000 0A 0:0 0:0 0 0 0 5 1\n"  # [::]:8080
        "   1: 00000000000000000000000001000000:13B2 00000000000000000000000000000000:0000 0A 0:0 0:0 0 0 0 6 1\n"  # [::1]:5042
        "   2: 0000000000000000FFFF00000100007F:2382 00000000000000000000000000000000:0000 0A 0:0 0:0 0 0 0 7 1\n")  # ::ffff:127.0.0.1:9090


class ProcNetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-net-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "tcp").write_text(TCP)
        (self.tmp / "tcp6").write_text(TCP6)

    def test_listening_sockets_are_read_with_their_addresses(self) -> None:
        self.assertEqual(listeners.listening(self.tmp), [
            ("0.0.0.0", 22), ("10.0.0.5", 7860), ("127.0.0.1", 8080), ("127.0.0.1", 9090),
            ("::1", 5042), ("::", 8080)])
        self.assertEqual(listeners.ports_in_use(self.tmp), {22, 5042, 7860, 8080, 9090})
        self.assertEqual(listeners.listening(self.tmp / "missing"), [], "no /proc: nothing is said")

    def test_the_model_servers_among_them_are_asked_once_per_port(self) -> None:
        asked: list[tuple[str, int]] = []

        def probe(address: str, port: int) -> dict | None:
            asked.append((address, port))
            return {8080: {"status": 200, "ids": ["qwen"]}, 5042: {"status": 401, "ids": []}}.get(port)
        found = listeners.model_servers({9090}, self.tmp, probe=probe)
        self.assertEqual(sorted(asked), [("10.0.0.5", 7860), ("::", 8080), ("::1", 5042)],
                         "ports under 1024 and skipped ones never; a port on several addresses once, on every one")
        self.assertEqual(found, [{"address": "::1", "port": 5042, "status": 401, "ids": []},
                                 {"address": "::1", "port": 8080, "status": 200, "ids": ["qwen"]}])


class ProbeTest(unittest.TestCase):
    def test_a_model_list_a_refusal_and_anything_else(self) -> None:
        with MockLLM() as llm:
            port = int(llm.base_url.rsplit(":", 1)[1].split("/")[0])
            self.assertEqual(listeners.probe_models("127.0.0.1", port), {"status": 200, "ids": ["mock-model"]})
            self.assertEqual(listeners.probe_models("0.0.0.0", port)["status"], 200, "a wildcard is asked on loopback")
        with AuthServer(reject_all=True) as server:
            port = int(server.base_url.rsplit(":", 1)[1].split("/")[0])
            self.assertEqual(listeners.probe_models("127.0.0.1", port), {"status": 401, "ids": []})
        quiet = socket.socket()
        quiet.bind(("127.0.0.1", 0))
        quiet.listen(1)
        self.addCleanup(quiet.close)
        self.assertIsNone(listeners.probe_models("127.0.0.1", quiet.getsockname()[1], timeout=0.3),
                          "no answer in time: not a model server")

    def test_this_machine(self) -> None:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)
        port = server.getsockname()[1]
        if not Path("/proc/net/tcp").exists():
            self.skipTest("no /proc/net/tcp here")
        self.assertIn(("127.0.0.1", port), listeners.listening())


if __name__ == "__main__":
    unittest.main()
