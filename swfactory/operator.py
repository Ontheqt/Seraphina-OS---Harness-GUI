"""Operator personas (DESIGN.md section 36): the persona the owner marks as the operator of this server.

An operator holds root on the server and acts through her own tools, outside the harness. The harness never
gates her own actions and gives her no power: it is her toolkit. It reads the change receipts she writes
(receipts.py), keeps on the tracker the checks she reports as not done, answers "what did I do since X"
(activity.py) and shows her open items at the start of each of her turns, as factory agents get theirs. Its
guarantees (no agent holds root, changes only through the executor, approval codes, the build phases) are for
the harness's own agents; of an operator's actions it only records what she reports, labelled as reported.

Only the owner's config marks an operator: ``agents.<name>.role: "operator"`` (the settings API or the config
file; agents never reach either). A ``role:`` line in a persona file means nothing, and no harness code path
writes the mark. At most one persona is the operator unless ``operator.several`` lists every one marked; a
config that breaks that rule (edited by hand) has no operator at all until it is fixed (fail closed), and the
doctor says why.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Config
    from .personas import Persona

ROLE = "operator"
ROLES = (ROLE,)
REPORTED = "reported by the operator"   # the one label every ingested receipt carries (never "pre-authorised")
OPERATOR_KINDS = ("openwebui", "wake")  # an Open WebUI-bound persona or one that wakes itself (not a snapshot)


def _agents(raw: dict) -> dict:
    agents = raw.get("agents") if isinstance(raw, dict) else None
    return agents if isinstance(agents, dict) else {}


def section(raw: dict) -> dict:
    """The operator section as configured (the config's defaults merged in by Config)."""
    value = raw.get("operator") if isinstance(raw, dict) else None
    return value if isinstance(value, dict) else {}


def marked(raw: dict) -> list[str]:
    """The names the owner's config marks agents.<name>.role "operator", sorted."""
    return sorted(str(name) for name, entry in _agents(raw).items()
                  if name != "*" and isinstance(entry, dict) and entry.get("role") == ROLE)


def rule_problem(raw: dict) -> str | None:
    """Why the marks break the one-operator rule (several marked and not all listed in operator.several), or None."""
    names = marked(raw)
    if len(names) <= 1:
        return None
    several = section(raw).get("several")
    listed = {str(name) for name in several} if isinstance(several, list) else set()
    missing = [name for name in names if name not in listed]
    if not missing:
        return None
    return (f"agents: {', '.join(names)} are all marked role: operator; at most one persona is the operator unless "
            f"operator.several lists every one of them (not listed: {', '.join(missing)})")


def can_be_operator(persona: Persona | None) -> bool:
    """Whether a persona may hold the mark: an Open WebUI-bound one or one that wakes itself, with a valid file."""
    return persona is not None and persona.kind in OPERATOR_KINDS and persona.error is None


def operators(config: Config | None) -> list[str]:
    """The operator personas' names: the marked ones when the rule holds and each is a persona that can be one
    (can_be_operator: not a factory agent, an imported snapshot, a missing or invalid file), else none; the
    personas cannot be read: none (fail closed)."""
    if config is None:
        return []
    raw = config.raw
    names = [] if rule_problem(raw) else marked(raw)
    if not names:
        return []
    from .personas import discover_personas   # personas imports this module

    try:
        found = discover_personas(config)
    except Exception:  # noqa: BLE001 - a persona file that does not parse: nobody is the operator until fixed
        return []
    return [name for name in names if can_be_operator(found.get(name))]


def is_operator(config: Config | None, name: Any) -> bool:
    """Whether the owner's config marks this persona the operator (the one-operator rule holds and the persona
    can be one)."""
    return isinstance(name, str) and name in operators(config)


def role(config: Config | None, persona: Persona) -> str | None:
    """"operator" for a marked persona of a kind that can be one (Open WebUI-bound or wake) without errors (the
    persona already found: operators() without discovering them again)."""
    if config is None or not can_be_operator(persona):
        return None
    raw = config.raw
    return ROLE if persona.name in marked(raw) and not rule_problem(raw) else None


def owner(name: str) -> str:
    """The tracker owner of an operator's items: persona:<name>."""
    return f"persona:{name}"


def factory_agents(config: Config | None) -> set[str]:
    """The names of the agent files in the persona dirs that are not personas (the factory line's agents)."""
    from .agentdefs import discover_agents
    from .personas import persona_dirs, persona_of

    return {name for name, agent in discover_agents(persona_dirs(config)).items() if persona_of(agent) is None}


def mark_problem(name: str, persona: Persona | None, agents: set[str]) -> str | None:
    """Why the name the owner marked cannot be the operator (it then is not one: operators()), or None."""
    if persona is None:
        if name in agents:
            return f"{name} is a factory agent, not a persona: only a persona can be the operator"
        return f"there is no persona {name}"
    if persona.kind not in OPERATOR_KINDS:
        return (f"{name} is an imported offline snapshot: only an Open WebUI-bound persona or one that wakes itself "
                "can be the operator")
    if persona.error:
        return f"its persona file is invalid: {persona.error}"
    return None


def doctor_problems(config: Config, personas: dict[str, Persona]) -> list[tuple[str, str]]:
    """[(level, words)] for the doctor: a broken one-operator rule, a mark on a name that cannot be an operator (no
    persona, a factory agent, a snapshot, an invalid file: nobody holds that mark until it is fixed), a role: line
    in a persona file (it means nothing)."""
    problems: list[tuple[str, str]] = []
    raw = config.raw
    rule = rule_problem(raw)
    if rule:
        problems.append(("error", rule + "; until then no persona is the operator"))
    try:
        agents = factory_agents(config)
    except Exception:  # noqa: BLE001 - an agent file that does not parse is the agents check's to name
        agents = set()
    for name in marked(raw):
        problem = mark_problem(name, personas.get(name), agents)
        if problem:
            problems.append(("warn", f"agents.{name}.role is operator, but {problem}; until then {name} is not the "
                                     "operator"))
    for name, persona in sorted(personas.items()):
        if persona.agent is not None and "role" in persona.agent.meta:
            problems.append(("warn", f"persona {name}: a role: line in its file means nothing (only the owner's "
                                     f"config marks the operator: agents.{name}.role)"))
    return problems


def profile_of(persona: Persona | None) -> str | None:
    """The Open WebUI profile (workspace model id) a persona stands for: an Open WebUI-bound persona's model, or
    the one a wake persona was imported from (source_model); None otherwise."""
    if persona is None:
        return None
    if persona.kind == "openwebui":
        return str((persona.binding or {}).get("model") or "") or None
    if persona.kind == "wake" and (persona.source or {}).get("type") == "openwebui":
        return str((persona.source or {}).get("model") or "") or None
    return None


def wake_pairs(raw: dict, personas: dict[str, Persona]) -> list[tuple[str, str, str]]:
    """[(her live profile's persona, the wake persona, the profile id)] for each marked operator: a live
    (Open WebUI-bound) persona and a wake persona imported from the same Open WebUI profile, either one marked."""
    names = set(marked(raw))
    pairs = set()
    for live, persona in personas.items():
        model = profile_of(persona) if persona.kind == "openwebui" else None
        if not model:
            continue
        for wake, other in personas.items():
            if other.kind == "wake" and profile_of(other) == model and names & {live, wake}:
                pairs.add((live, wake, model))
    return sorted(pairs)


def wake_mark_problems(config: Config, personas: dict[str, Persona]) -> list[tuple[bool, str]]:
    """[(ok, words)] for the doctor, one per wake_pairs pair: the wake persona carries the mark itself
    (agents.<wake>.role), so do her live profile's, operator.several lists both names exactly, and the acting
    layer agrees (is_operator for both, as a wake turn asks it). Without that her wakes read no receipts. The words
    say exactly what to set."""
    raw = config.raw
    names = set(marked(raw))
    several = section(raw).get("several")
    listed = [str(name) for name in several] if isinstance(several, list) else []
    out: list[tuple[bool, str]] = []
    for live, wake, model in wake_pairs(raw, personas):
        where = f"{wake} is the wake persona of {live} (both from Open WebUI profile {model})"
        unmarked = [name for name in (wake, live) if name not in names]
        unlisted = [name for name in (live, wake) if name not in listed]
        if unmarked or unlisted:
            missing = [f"agents.{name}.role is not operator" for name in unmarked]
            if unlisted:
                missing.append(f"operator.several does not list {' or '.join(unlisted)}")
            sets = [f'agents.{name}.role to "operator"' for name in unmarked]
            if unlisted:
                sets.append(f"operator.several to {json.dumps(sorted({*listed, live, wake}))}")
            out.append((False, f"{where}, but {'; '.join(missing)}: without that her wakes read no receipts. "
                               f"Set {' and '.join(sets)}"))
            continue
        acting = operators(config)
        refused = [name for name in (live, wake) if name not in acting]
        if refused:
            why = rule_problem(raw) or "; ".join(filter(None, (mark_problem(name, personas.get(name), set())
                                                               for name in refused))) or "it cannot be the operator"
            out.append((False, f"{where}: both are marked and listed in operator.several, but the harness does not "
                               f"treat {' and '.join(refused)} as the operator ({why}), so her wakes read no receipts"))
            continue
        out.append((True, f"{where}: the wake persona carries role: operator itself and operator.several lists "
                          f"both {live} and {wake}, so her wakes read her receipts"))
    return out
