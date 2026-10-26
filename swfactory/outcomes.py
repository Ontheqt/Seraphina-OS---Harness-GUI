"""Outcomes (DESIGN.md section 40): one rule for every call that changes, or may change, something outside the
harness's own state.

Such a call records one outcome:

- **succeeded**: the other side's response confirmed it;
- **failed**: the other side refused it, or the call provably never left the harness;
- **unknown**: the call may have reached the other side and no confirming response arrived (a timeout after
  sending, a connection reset mid-response, a crash between the journal's intent and the response, an answer of
  500 or more, which says the other side failed while handling it but not what it did).

Unknown is recorded as unknown, never as success or failure. Nothing whose outcome is unknown is replayed
automatically: the next step is a read-only re-observation, which records what it finds, or a tracker item worded
UNKNOWN_TITLE. A call is retried by itself only when it is idempotent by construction; RETRIED names each such
call and why, and section 40 lists the same calls.

A record of a commit keeps what the other side answered (response(): its status, the ids it returned and a hash of
its body), never only the action the harness meant to take.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

SUCCEEDED, FAILED, UNKNOWN = "succeeded", "failed", "unknown"
STATES = (SUCCEEDED, FAILED, UNKNOWN)
UNKNOWN_TITLE = "Outcome unknown: check before retrying"
WORDS = {SUCCEEDED: "succeeded: the other side's answer confirmed it",
         FAILED: "failed: refused, or it never left the harness",
         UNKNOWN: "outcome unknown: it may have happened; check before retrying"}
IDS_KEPT = 20
ID_CHARS = 200

# The calls the harness retries by itself although an earlier try may have reached the other side. Each is
# idempotent by construction (a read; a request carrying a key the other side honours; a call that sets a state,
# whose repetition is harmless). Every other call whose outcome is unknown is recorded as unknown, never replayed.
# A retry after a refusal or a call that never left is not on this list: nothing happened, so it is a new call.
RETRIED = {
    "model call": "a completion from a model server only generates text: a second try changes nothing outside the "
                  "harness. Not under a GPU custody lease (section 37) nor through Open WebUI (its filters and tools "
                  "run on every try): there a call that may have acted is unknown and never replayed",
    "drift source read": "a GET of findings",
    "ntfy subscription": "a GET stream; a message seen twice is dropped by its id",
    "Telegram getUpdates": "a read from the offset the harness keeps; an update seen twice is dropped by its id",
    "Telegram bot menu": "setMyCommands, setMyDescription and setMyShortDescription set the menu to one value",
    "SnapRAID daemon reads": "a GET of the SnapRAID Daemon's /metrics or, through Seraphina's link, its state, "
                             "tasks, array, disks or config: every GET only serializes the daemon's memory and queues "
                             "nothing (BACKUPS_SNAPRAID.md §3.3); its actions are never retried after an unknown "
                             "outcome",
    "event-source snippet": "curl --retry posts an event: a failure opens one incident and a repeat while it is "
                            "open is counted, never a second notice; a recovery closes it once",
}


def retried(call: str) -> str:
    """Mark a place where the harness tries a call again by itself: call must be a key of RETRIED (KeyError
    otherwise), and every key is marked somewhere in swfactory/ (a test keeps the two equal), so the list cannot
    name a call the code does not retry, nor the code retry by itself one the list leaves out. Returns call."""
    if call not in RETRIED:
        raise KeyError(f"{call!r} is not in outcomes.RETRIED (DESIGN.md section 40)")
    return call


def of_status(status: int | None, *, sent: bool = True) -> str:
    """The outcome an HTTP answer gives a call that changes something: 2xx succeeded, 4xx failed (refused),
    anything else unknown (a 5xx: the other side failed while handling it and does not say what it did; 1xx and
    3xx: nothing confirmed it). status None (no answer): failed when the call never left (sent False), else
    unknown."""
    if status is None:
        return UNKNOWN if sent else FAILED
    if 200 <= status < 300:
        return SUCCEEDED
    if 400 <= status < 500:
        return FAILED
    return UNKNOWN


def response(status: Any, ids: Iterable[Any] = (), body: bytes | str | None = b"") -> dict:
    """What the other side answered, for a record: {status, ids, body_sha256} (the ids it returned, as text;
    body_sha256 of the answer's bytes, None when there was no body)."""
    data = body.encode("utf-8") if isinstance(body, str) else body
    kept = [str(value)[:ID_CHARS] for value in ids if value is not None and str(value) != ""][:IDS_KEPT]
    return {"status": status, "ids": kept, "body_sha256": hashlib.sha256(data).hexdigest() if data else None}
