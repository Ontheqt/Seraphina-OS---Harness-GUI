"""A git receive-pack for tests that updates the ref but loses the remote's report for it (DESIGN.md section 40).

Run as remote.<name>.receivepack: "<python> <this file>", then git appends the repository path. It relays a real
`git receive-pack <path>` with two changes: it takes side-band and report-status-v2 out of the capabilities, so the
report comes back as plain pkt-lines, and it drops each "ok <ref>" or "ng <ref>" line of that report. git push then
prints "!  <ref>  [remote failure] (remote failed to report status)" and exits 1, although the remote's ref moved.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading


def main() -> None:
    proc = subprocess.Popen(["git", "receive-pack", sys.argv[-1]], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert proc.stdin is not None and proc.stdout is not None
    stdin, stdout = proc.stdin, proc.stdout

    def pump() -> None:   # everything git push sends goes through unchanged
        while True:
            data = os.read(0, 65536)
            if not data:
                break
            stdin.write(data)
            stdin.flush()
        stdin.close()

    threading.Thread(target=pump, daemon=True).start()

    def read() -> bytes | None:
        head = stdout.read(4)
        if not head:
            return None
        size = int(head, 16)
        return stdout.read(size - 4) if size > 4 else b""

    def write(body: bytes) -> None:
        os.write(1, b"0000" if body == b"" else b"%04x" % (len(body) + 4) + body)

    first = True
    while (body := read()) is not None:   # the ref advertisement, up to its flush
        if first and b"\0" in body:
            refs, caps = body.split(b"\0", 1)
            kept = [cap for cap in caps.strip().split(b" ")
                    if not cap.startswith(b"side-band") and cap != b"report-status-v2"]
            body = refs + b"\0" + b" ".join(kept) + b"\n"
        first = False
        write(body)
        if body == b"":
            break
    while (body := read()) is not None:   # the report: "unpack ok" and the flush pass, the ref's own line does not
        if not body.startswith((b"ok ", b"ng ")):
            write(body)
        if body == b"":
            break
    proc.wait()
    os.close(1)


if __name__ == "__main__":
    main()
