"""What listens on TCP here: read from /proc/net/tcp and /proc/net/tcp6, which any user may read (no root, no `ss`).

The kernel lists every socket of this network namespace there, whoever owns it, but not the process: only root can
tell which program holds a port (the planned system helper's `ports` verb). Used by job port blocks (jobs.py), the
ntfy setup check (port 80 held by another program) and the setup wizard (doctor.setup_host): all of them only read.
model_servers is the one part that sends anything, a GET /v1/models to each listener: only the owner's look-around
calls it (doctor.look_for_model_servers, from POST /api/setup/look-around or `factory doctor --look-around`).
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path

PROC_NET = Path("/proc/net")
LISTEN = "0A"               # TCP_LISTEN in /proc/net/tcp's st column
PROBE_TIMEOUT = 1.5         # seconds per GET /v1/models when the owner looks for model servers
PROBE_LIMIT = 40            # listeners probed at most
PROBE_BYTES = 65536


def _address(hex_address: str) -> str:
    """/proc/net/tcp's local address (hex, each 32-bit word in host byte order, little-endian here) as text."""
    raw = bytes.fromhex(hex_address)
    words = b"".join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
    return socket.inet_ntop(socket.AF_INET if len(words) == 4 else socket.AF_INET6, words)


def listening(proc_net: Path = PROC_NET) -> list[tuple[str, int]]:
    """(address, port) of each listening TCP socket, IPv4 then IPv6, sorted and each once; [] when /proc does not
    say. An IPv4 address mapped into IPv6 (::ffff:127.0.0.1) is given as the IPv4 address."""
    found: set[tuple[str, int]] = set()
    for name in ("tcp", "tcp6"):
        try:
            lines = (proc_net / name).read_text(encoding="ascii", errors="replace").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or fields[3] != LISTEN or ":" not in fields[1]:
                continue
            hex_address, _, hex_port = fields[1].rpartition(":")
            try:
                address, port = _address(hex_address), int(hex_port, 16)
            except (ValueError, OSError):
                continue
            mapped = ipaddress.ip_address(address)
            if isinstance(mapped, ipaddress.IPv6Address) and mapped.ipv4_mapped is not None:
                address = str(mapped.ipv4_mapped)
            found.add((address, port))
    return sorted(found, key=lambda item: (":" in item[0], item[1], item[0]))


def ports_in_use(proc_net: Path = PROC_NET) -> set[int]:
    """Every TCP port something listens on here, on any address."""
    return {port for _, port in listening(proc_net)}


def is_wildcard(address: str) -> bool:
    return address in ("0.0.0.0", "::")


def probe_models(address: str, port: int, timeout: float = PROBE_TIMEOUT) -> dict | None:
    """What GET http://<address>:<port>/v1/models answers, without a key: {"status": 200, "ids": [...]} for an
    OpenAI-style model list, {"status": 401 or 403} for a JSON refusal (a model server that wants a key answers so;
    so may other programs), None for anything else (another program, no answer). Reads at most PROBE_BYTES;
    follows no redirect."""
    host = "127.0.0.1" if address == "0.0.0.0" else "::1" if address == "::" else address
    url = f"http://{f'[{host}]' if ':' in host else host}:{port}/v1/models"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=timeout) as answer:
            status, body = answer.status, answer.read(PROBE_BYTES)
    except urllib.error.HTTPError as err:
        with err:
            status = err.code
            try:
                body = err.read(PROBE_BYTES)
            except OSError:
                body = b""
    except (OSError, ValueError, http.client.HTTPException):
        return None
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return None
    if status == 200 and isinstance(payload, dict) and isinstance(payload.get("data"), list):
        ids = [str(item.get("id")) for item in payload["data"] if isinstance(item, dict) and item.get("id")]
        return {"status": 200, "ids": ids}
    if status in (401, 403) and isinstance(payload, dict):
        return {"status": status, "ids": []}
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):   # noqa: ANN002, ANN003 - urllib's signature
        return None


def model_servers(skip_ports: set[int] | frozenset[int] = frozenset(), proc_net: Path = PROC_NET,
                  probe=probe_models) -> list[dict]:
    """[{address, port, status, ids}] for each listener here (ports 1024 and up, not in skip_ports, at most
    PROBE_LIMIT) that answers GET /v1/models like a model server, probed in parallel. A port listening on a wildcard
    address is asked on loopback; one listening on several addresses, once."""
    candidates: dict[int, str] = {}
    for address, port in listening(proc_net):
        if port < 1024 or port in skip_ports:
            continue
        if port not in candidates or is_wildcard(address) or ipaddress.ip_address(address).is_loopback:
            candidates[port] = address
    chosen = sorted(candidates.items())[:PROBE_LIMIT]
    results: list[dict | None] = [None] * len(chosen)

    def ask(index: int, port: int, address: str) -> None:
        answer = probe(address, port)
        if answer is not None:
            shown = "127.0.0.1" if address == "0.0.0.0" else "::1" if address == "::" else address
            results[index] = {"address": shown, "port": port, **answer}
    threads = [threading.Thread(target=ask, args=(index, port, address), daemon=True)
               for index, (port, address) in enumerate(chosen)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(PROBE_TIMEOUT * 2)
    return [result for result in results if result is not None]
