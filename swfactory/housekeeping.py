"""The system scope of the Housekeeping Tracker and outside agents' tokens (DESIGN.md section 32).

The system scope holds the machine's own items: what a person, an outside agent (DSH,
Qwen Code, Claude Code run by hand, through ``sf-items`` and an agent token) or a
persona (Wake & Messages) must do or check that belongs to no job, chat or plan. Its
files are ``<data_dir>/housekeeping/system.json`` and ``system.log.jsonl``; the
HousekeepingManager is their only writer, and its ids are ``S-<n>`` (``system:S-4``,
``host:<id>/system:S-4`` in full; this machine is host ``local``).

Agent tokens (``<data_dir>/auth/agent-tokens.json``, mode 0600) hold only each token's
sha256, its name, when it was made and last used. A token is valid for the item
routes only, adds items owned by ``agent:<name>``, updates its own items and lists
only the items it owns (items.agent_visible: never a persona's, nor a private turn's).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets as token_bytes
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from . import items as tracker
from .events import EventBus, utc_now

SYSTEM_DIR = tracker.SYSTEM_DIR
TOKENS_FILE = Path("auth") / "agent-tokens.json"
TOKEN_PREFIX = "sfi_"
TOKEN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MAX_TOKENS = 50
LAST_USED_EVERY = 60.0   # seconds between writes of a token's last_used


class HousekeepingManager:
    """The system scope's only writer; Factory delegates its system-scope calls here.

    secrets: what is redacted from every text before it is stored (the bus's list plus credential-named
    environment values). by is "human", "factory", "code", agent:<token name> or persona:<name>.
    """

    def __init__(self, data_dir: Path, bus: EventBus, secrets: Callable[[], list[str]] = list,
                 max_proposals: int = tracker.MAX_PROPOSALS) -> None:
        self.root = Path(data_dir) / SYSTEM_DIR
        self.bus = bus
        self.secrets = secrets
        self.max_proposals = max_proposals
        self.scope = tracker.system_scope()

    def store(self) -> tracker.ItemStore:
        return tracker.system_store(self.root.parent, max_proposals=self.max_proposals)

    @contextlib.contextmanager
    def transaction(self, timeout: float | None = None) -> Iterator[tracker.Ledger]:
        """A transaction on the system scope; items.updated (ref "system") after a change."""
        store = self.store()
        with store.transaction(timeout=timeout, secrets=self.secrets()) as ledger:
            yield ledger
        if ledger.dirty:
            self.bus.publish("items.updated", {"ref": self.scope.ref, "summary": ledger.summary(),
                                               "changed": list(ledger.changed)})

    # -- reading ---------------------------------------------------------------------

    def rows(self, now: str, *, for_agent: str | None = None) -> list[dict]:
        """Every system item as the tracker lists them (for_agent, an outside agent's token name: only those it
        may read, items.agent_visible)."""
        store = self.store()
        if not store.path.is_file():
            return []
        return [tracker.decorate(item, self.scope, now=now) for item in store.ledger().items
                if for_agent is None or tracker.agent_visible(item, for_agent)]

    def scope_items(self) -> dict:
        """{items, summary, log} of the system scope."""
        store = self.store()
        ledger = store.ledger()
        now = utc_now()
        return {"items": [tracker.decorate(item, self.scope, now=now) for item in ledger.items],
                "summary": ledger.summary(), "log": store.log_tail()}

    def item_history(self, item_id: str) -> dict:
        """{item, log}: one system item and every transition it went through (KeyError if unknown)."""
        store = self.store()
        item = store.ledger().get(item_id)
        return {"item": tracker.decorate(item, self.scope, now=utc_now()), "log": store.history(item_id)}

    # -- a person's and an outside agent's operations -------------------------------------------

    def add_item(self, fields: dict, *, by: str = tracker.HUMAN, timeout: float | None = tracker.BUSY_SECONDS,
                 taint: Iterable[str] = ()) -> dict:
        """A new system item ({title, detail?, kind?, blocking?, due?, owner?, verifier?}): a person's is owned
        by the person unless they name an owner; an outside agent's (by agent:<name>) is its own, verified by
        a person. Returns it as the API serves it."""
        fields = dict(fields)
        if by.startswith("agent:"):
            fields.update(owner=by, verifier=tracker.HUMAN)
        else:
            fields.setdefault("owner", fields.get("owner") or tracker.HUMAN)
        with self.transaction(timeout) as ledger:
            item = tracker.human_add(ledger, fields, by=by, taint=taint)
        return tracker.decorate(item, self.scope, now=utc_now())

    def item_action(self, item_id: str, action: str, *, by: str = tracker.HUMAN,
                    timeout: float | None = tracker.BUSY_SECONDS, **kwargs: Any) -> dict:
        """A person's action on a system item ({item}); an outside agent may only update its own items. defer
        moves it to Later here (with a date when given), as on a repo's follow-ups. ItemError (ValueError) with
        the rule when refused, KeyError for an unknown item."""
        if action == "make-job":
            raise ValueError("make-job works on a repo's follow-ups (repo:<repo>/F-<n>); a system item has no repo")
        with self.transaction(timeout) as ledger:
            ledger.get(item_id)
            if by == tracker.HUMAN:
                action = tracker.person_action(action, kwargs.get("to"))   # update to defer is defer, ...
                item = tracker.human_action(ledger, item_id, action, **kwargs)
            elif action == "update" and by.startswith("agent:"):
                item = agent_update(ledger, by, item_id, kwargs.get("to"), evidence=kwargs.get("evidence"),
                                    reason=kwargs.get("reason") or kwargs.get("note"))
            else:
                raise tracker.ItemError("an agent token may only update its own items")
        return {"item": tracker.decorate(item, self.scope, now=utc_now())}

    # -- the factory's own items (DESIGN §33 and §15) --------------------------------------------

    def open_once(self, fields: dict, dedupe: str | None = None) -> dict:
        """Open one item by the factory for a person (an Open WebUI profile that changed, §33): {title,
        detail?, kind?, source?, due?}. With dedupe, an item still open whose source.hash is dedupe is
        returned instead of a new one. {ref, item, added}."""
        with self.transaction() as ledger:
            item, added = tracker.open_once(ledger, fields, dedupe)
            item = dict(item)
        return {"ref": self.scope.item_ref(item["id"]), "item": item, "added": added}

    def add_carried(self, entries: list[dict]) -> list[str]:
        """Carry items here by the factory (a deleted chat without a repository, §15); their refs."""
        with self.transaction() as ledger:
            refs = [self.scope.item_ref(tracker.add_carried(ledger, dict(fields))["id"]) for fields in entries]
        return refs

    # -- personas and wakes (Wake & Messages §2.3 and Appendix A) --------------------------------

    def open_persona_item(self, persona: str, title: Any, *, for_owner: bool = False, due: str | None = None,
                          wake: str | None = None, taint: Iterable[str] = (), detail: Any = "") -> dict:
        """A persona's own promise (for: me: open, owner persona:<name>, verified by code when it is a wake,
        else by a person) or a suggestion for the owner (for: owner: proposed, owner human). From an untrusted
        turn both are suggestions (Wake & Messages §2.6): a for-me one stays the persona's. The WakeManager
        calls it through Factory; title is what the tracker shows (a code-written one for a tainted turn: the
        model's own words stay in the persona's masked store). Never served to an agent token."""
        name = str(persona or "")
        if not tracker.OWNER_RE.fullmatch(f"persona:{name}"):
            raise ValueError(f"invalid persona name {persona!r}")
        taint = sorted({str(t) for t in taint if t in tracker.TAINTS})
        proposed = for_owner or "untrusted" in taint
        owner = tracker.HUMAN if for_owner else f"persona:{name}"
        with self.transaction() as ledger:
            item = ledger.add(tracker.FACTORY, title, kind="commitment", detail=detail, owner=owner,
                              verifier=tracker.CODE if wake and not for_owner else tracker.HUMAN,
                              status="proposed" if proposed else "open", due=tracker.check_due(due), taint=taint,
                              links={"wake": wake} if wake else None,
                              source={"type": "persona", "agent": f"persona:{name}", "wake": wake})
        return tracker.decorate(item, self.scope, now=utc_now())

    def close_wake_item(self, item_id: str, run: str) -> dict:
        """The wake ran: code proves its item with the run as its receipt ({kind: wake, ref: run})."""
        with self.transaction() as ledger:
            item = ledger.change(tracker.CODE, item_id, "verified", note="the wake ran",
                                 evidence=[{"kind": "wake", "ref": str(run), "detail": "the wake ran"}])
        return tracker.decorate(item or self.store().ledger().get(item_id), self.scope, now=utc_now())

    def drop_wake_item(self, item_id: str, reason: str = "the persona cancelled its wake") -> dict:
        """The persona cancelled its own pending wake: code drops the item with the reason."""
        with self.transaction() as ledger:
            item = ledger.change(tracker.CODE, item_id, "dropped", reason=reason)
        return tracker.decorate(item or self.store().ledger().get(item_id), self.scope, now=utc_now())


def agent_update(ledger: tracker.Ledger, by: str, item_id: str, to: Any, *, evidence: Any = None,
                 reason: Any = "") -> dict:
    """An outside agent's update (by agent:<name>): only of an item it owns, only to doing, done (with
    evidence) or defer (a non-blocking one, with a reason); never verify, drop, dismiss or unblock."""
    item = ledger.get(item_id)
    if item.get("owner") != by:
        raise tracker.ItemError(f"{item_id} is not yours: an agent token updates only the items it added")
    word = " ".join(str(to or "").strip().lower().split())
    if word not in ("doing", "done", "defer"):
        raise tracker.ItemError("an agent token moves its own items to doing, done (with evidence) or defer only")
    return tracker.item_update(ledger, by, item_id, word, evidence=evidence, reason=reason)


# ---------------------------------------------------------------- agent tokens


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AgentTokens:
    """Outside agents' tokens: made, named and revoked by a person; only their sha256 is kept."""

    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / TOKENS_FILE
        self._lock = threading.Lock()
        self._used: dict[str, float] = {}

    def _read(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            raise ValueError(f"{self.path} is not readable ({type(exc).__name__}); fix or move it") from None
        tokens = data.get("tokens") if isinstance(data, dict) else None
        return [entry for entry in tokens if isinstance(entry, dict)] if isinstance(tokens, list) else []

    def _write(self, tokens: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.path.parent, 0o700)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".agent-tokens.", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "tokens": tokens}, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    def list(self) -> list[dict]:
        """[{name, created, last_used}] (never a token or its hash)."""
        with self._lock:
            return [{"name": entry.get("name"), "created": entry.get("created"), "last_used": entry.get("last_used")}
                    for entry in self._read()]

    def create(self, name: Any) -> dict:
        """A new token for name: {name, token} (the token is shown this once). ValueError for a bad or taken
        name."""
        if not isinstance(name, str) or not TOKEN_NAME_RE.fullmatch(name):
            raise ValueError("a token name is 1-32 lower-case letters, digits, '-' or '_' (e.g. qwen-code)")
        with self._lock:
            tokens = self._read()
            if any(entry.get("name") == name for entry in tokens):
                raise ValueError(f"an agent token named {name!r} exists; revoke it first")
            if len(tokens) >= MAX_TOKENS:
                raise ValueError(f"at most {MAX_TOKENS} agent tokens; revoke one first")
            token = TOKEN_PREFIX + token_bytes.token_urlsafe(32)
            tokens.append({"name": name, "sha256": _digest(token), "created": utc_now(), "last_used": None})
            self._write(tokens)
        return {"name": name, "token": token}

    def revoke(self, name: str) -> None:
        """Remove the token named name (KeyError if there is none)."""
        with self._lock:
            tokens = self._read()
            kept = [entry for entry in tokens if entry.get("name") != name]
            if len(kept) == len(tokens):
                raise KeyError(f"unknown agent token {name!r}")
            self._write(kept)

    def match(self, token: str, now: Callable[[], float] | None = None) -> str | None:
        """The name of the token, or None (compared in constant time against every stored hash). Its
        last_used is recorded, at most once a minute."""
        if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX) or len(token) > 200:
            return None
        digest = _digest(token)
        with self._lock:
            try:
                tokens = self._read()
            except ValueError:
                return None
            found = None
            for entry in tokens:
                if hmac.compare_digest(str(entry.get("sha256") or ""), digest):
                    found = entry
            if found is None:
                return None
            import time

            clock = (now or time.monotonic)()
            name = str(found.get("name"))
            if clock - self._used.get(name, -LAST_USED_EVERY) >= LAST_USED_EVERY:
                self._used[name] = clock
                found["last_used"] = utc_now()
                with contextlib.suppress(OSError):
                    self._write(tokens)
            return name
