"""Procedures and maps: every agent's working procedure, and the factory's process maps (DESIGN.md §25, §26).

procedures(config, agents) is the data behind GET /api/docs/procedures: for each agent its
name, description, model and the working procedure parsed from its body (the sections,
each with its numbered steps, plus the raw markdown), and the process maps (the factory
line and the plan flow) as nodes on a grid joined by labelled edges.

procedures_html(data) is GET /api/docs/procedures.html: one self-contained, offline page in
the owner's booklet style (system font, light background, white section cards, numbered
steps with bold lead phrases) with the maps drawn as inline SVG: rounded boxes, decision
diamonds and labelled arrows, each with its Mermaid source in a collapsible block. Every
text that comes from an agent file is escaped; the page has no scripts and loads nothing.

mermaid_maps(config) is GET /api/docs/maps: the same maps as Mermaid flowchart source, the
canonical text form (map_mermaid), with its sha256.
"""

from __future__ import annotations

import copy
import hashlib
import html
import re
from typing import TYPE_CHECKING, Any

from .events import utc_now

if TYPE_CHECKING:
    from .agentdefs import AgentDef
    from .config import Config

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_ITEM_RE = re.compile(r"^ {0,3}(\d{1,3})[.)]\s+(.*)$")
_BULLET_RE = re.compile(r"^ {0,3}[-*+]\s+(.*)$")
_FENCE_RE = re.compile(r"^ {0,3}(```|~~~)")
_BOLD_LEAD_RE = re.compile(r"^\*\*(.+?)\*\*\s*([:.]?)\s*(.*)$", re.DOTALL)
_INLINE_RE = re.compile(r"`([^`]+)`|\*\*([^*]+)\*\*")
LEAD_MAX_CHARS = 90


# ---------------------------------------------------------------- the maps


def _node(node_id: str, label: str, col: int, row: int, kind: str = "box") -> dict:
    return {"id": node_id, "label": label, "kind": kind, "col": col, "row": row}


def _edge(source: str, target: str, label: str = "", side: str | None = None, lane: int = 0) -> dict:
    edge: dict[str, Any] = {"from": source, "to": target, "label": label}
    if side:
        edge.update(side=side, lane=lane)
    return edge


GATE_LABEL = "Gate (code)\nreviews · items · conflicts · paths · size"   # items: the tracker's rule (§32)

MAPS: list[dict] = [
    {
        "id": "factory-line",
        "title": "The factory line",
        "description": ("One job from request to delivery. Reviewers run in parallel and the line waits for all "
                        "of them; the gate is deterministic code, so no model can merge past it."),
        "nodes": [
            _node("feature", "Feature request", 1, 0, "terminal"),
            _node("spec", "spec-writer\nwrites spec.md", 1, 1),
            _node("build", "builder\ncommits, writes build.md", 1, 2),
            _node("ready", "STATUS: READY?", 1, 3, "decision"),
            _node("review", "Reviewers in parallel\nsecurity · ux · ui · code", 1, 4),
            _node("verdicts", "All PASS?", 1, 5, "decision"),
            _node("approver", "approver\nwrites decision.md", 1, 6),
            _node("decided", "APPROVE?", 1, 7, "decision"),
            _node("gate", GATE_LABEL, 1, 8),
            _node("delivery", "Delivery?", 1, 9, "decision"),
            _node("merged", "Merged into base", 0, 10, "terminal"),
            _node("branch", "Delivered:\nbranch kept", 1, 10, "terminal"),
            _node("folder", "Delivered:\noutput folder", 2, 10, "terminal"),
            _node("human", "needs-human\napprove · rework · reject", 2, 6, "human"),
        ],
        "edges": [
            _edge("feature", "spec"), _edge("spec", "build"), _edge("build", "ready"),
            _edge("ready", "review", "READY"), _edge("ready", "human", "BLOCKED"),
            _edge("review", "verdicts", "wait for all"),
            _edge("verdicts", "approver", "all PASS"),
            _edge("verdicts", "build", "CHANGES: resume (max 2)", side="left"),
            _edge("verdicts", "human", "rounds used up"),
            _edge("approver", "decided"), _edge("decided", "gate", "APPROVE"),
            _edge("decided", "human", "ESCALATE"),
            _edge("gate", "delivery", "passes"), _edge("gate", "human", "stops"),
            _edge("delivery", "merged", "merge"), _edge("delivery", "branch", "branch"),
            _edge("delivery", "folder", "folder"),
        ],
    },
    {
        "id": "plan-flow",
        "title": "Plans: the lead above the line",
        "description": ("A big request becomes ordered factory jobs. Each job runs the whole line; when every "
                        "job has merged, the lead reviews the combined change."),
        "nodes": [
            _node("request", "Big request", 1, 0, "terminal"),
            _node("plan", "lead: Plan\nwrites plan.md", 1, 1),
            _node("planok", "PLAN: READY?", 1, 2, "decision"),
            _node("questions", "needs-human\nanswer in plan.md", 2, 2, "human"),
            _node("ready", "ready\nread, edit, Run", 1, 3),
            _node("submit", "Submit the jobs whose\ndependencies merged", 1, 4),
            _node("line", "Factory line\n(one job each)", 1, 5),
            _node("jobok", "Job merged?", 1, 6, "decision"),
            _node("blocked", "blocked\na job waits for a human", 2, 6, "human"),
            _node("final", "lead: Final review\nwrites review-k.md", 1, 7),
            _node("verdict", "VERDICT?", 1, 8, "decision"),
            _node("followups", "Follow-up jobs\n(first review only)", 0, 8),
            _node("again", "needs-human", 2, 8, "human"),
            _node("done", "done", 1, 9, "terminal"),
        ],
        "edges": [
            _edge("request", "plan"), _edge("plan", "planok"),
            _edge("planok", "ready", "READY"), _edge("planok", "questions", "QUESTIONS"),
            _edge("questions", "plan", "Retry", side="right"),
            _edge("ready", "submit", "Run"), _edge("submit", "line"), _edge("line", "jobok"),
            _edge("jobok", "blocked", "needs a human"),
            _edge("jobok", "submit", "more jobs", side="left"),
            _edge("jobok", "final", "all merged"),
            _edge("final", "verdict"), _edge("verdict", "done", "PASS"),
            _edge("verdict", "followups", "CHANGES"),
            _edge("followups", "submit", "", side="left", lane=1),
            _edge("verdict", "again", "CHANGES again"),
        ],
    },
]


def maps(config: Config | None = None) -> list[dict]:
    """The process maps (copies of MAPS), the factory line drawn from config's pipeline: its reviewers in the
    review box and its max_resumes on the CHANGES loop (no loop when it is 0), and no items rule at the gate
    when housekeeping.enabled is false (§32). An invalid pipeline, or no config, keeps the shipped drawing."""
    out = [copy.deepcopy(diagram) for diagram in MAPS]
    try:
        pipeline = config.pipeline() if config is not None else None
    except (ValueError, TypeError):
        pipeline = None
    if pipeline is None:
        return out
    line = next(diagram for diagram in out if diagram["id"] == "factory-line")
    names = list(pipeline.reviewers)
    rows = [" · ".join(names[i:i + 4]) for i in range(0, len(names), 4)] or ["(none in this pipeline)"]
    try:
        holds = config.housekeeping().enabled
    except (ValueError, TypeError, AttributeError):
        holds = True
    for node in line["nodes"]:
        if node["id"] == "review":
            node["label"] = "\n".join(["Reviewers in parallel", *rows])
        elif node["id"] == "gate" and not holds:
            node["label"] = node["label"].replace(" · items", "")
    edges = []
    for edge in line["edges"]:
        if edge["from"] == "verdicts" and edge["to"] == "build":
            if not pipeline.max_resumes:
                continue
            edge["label"] = f"CHANGES: resume (max {pipeline.max_resumes})"
        edges.append(edge)
    line["edges"] = edges
    return out


MERMAID_SHAPES = {"box": ('["', '"]'), "decision": ('{"', '"}'), "terminal": ('(["', '"])'),
                  "human": ('["', '"]')}
MERMAID_RESERVED = frozenset({"end", "graph", "flowchart", "subgraph", "direction", "style", "linkstyle",
                              "classdef", "class", "click", "call", "href", "default"})
MERMAID_CLASSES = {"human": "fill:#fff6e5,stroke:#a86b16,color:#17212b",   # the booklet's SVG colours
                   "terminal": "fill:#f2f4f6,stroke:#4a5560,color:#17212b"}


def map_mermaid(diagram: dict) -> str:
    """One map as Mermaid `flowchart TD` source: a `%% <title>` comment, one line per node (boxes
    `id["label"]`, decisions `id{"label"}`, terminals `id(["label"])`), one line per edge with its label
    (`a -->|"label"| b`; back edges along a side lane dotted, `-.->`), then `classDef`/`class` lines for
    the human and terminal nodes. Labels: newlines become `<br>`, and `"`, `<`, `>`, `&`, `|` and `#`
    become Mermaid entity codes (`#quot;` …), so no label can break out of its quotes."""
    ids: dict[str, str] = {}
    for node in diagram.get("nodes") or []:
        if isinstance(node, dict):
            ids[str(node.get("id"))] = _mermaid_id(str(node.get("id")), set(ids.values()))
    lines = [f"%% {_mermaid_comment(diagram.get('title'))}", "flowchart TD"]
    classes: dict[str, list[str]] = {}
    for node in diagram.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        kind = str(node.get("kind") or "box")
        start, end = MERMAID_SHAPES.get(kind, MERMAID_SHAPES["box"])
        node_id = ids[str(node.get("id"))]
        lines.append(f"    {node_id}{start}{_mermaid_label(node.get('label'))}{end}")
        if kind in MERMAID_CLASSES:
            classes.setdefault(kind, []).append(node_id)
    for edge in diagram.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        source, target = ids.get(str(edge.get("from"))), ids.get(str(edge.get("to")))
        if source is None or target is None:
            continue
        arrow = "-.->" if edge.get("side") else "-->"
        label = f'|"{_mermaid_label(edge["label"])}"|' if edge.get("label") else ""
        lines.append(f"    {source} {arrow}{label} {target}")
    for kind, members in classes.items():
        lines.append(f"    classDef {kind} {MERMAID_CLASSES[kind]}")
        lines.append(f"    class {','.join(members)} {kind}")
    return "\n".join(lines) + "\n"


def mermaid_maps(config: Config | None = None) -> list[dict]:
    """GET /api/docs/maps: [{id, title, mermaid, sha256}] for every process map (maps(config))."""
    out = []
    for diagram in maps(config):
        source = map_mermaid(diagram)
        out.append({"id": diagram["id"], "title": diagram["title"], "mermaid": source,
                    "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest()})
    return out


def _mermaid_id(raw: str, taken: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_]", "_", raw) or "n"
    if base.lower() in MERMAID_RESERVED or not base[0].isalpha():
        base = f"n_{base}"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}_{n}", n + 1
    return candidate


_MERMAID_ENTITIES = {"#": "#35;", '"': "#quot;", "<": "#lt;", ">": "#gt;", "&": "#amp;", "|": "#124;"}


def _mermaid_label(value: Any) -> str:
    text = "".join(_MERMAID_ENTITIES.get(ch, ch) for ch in str(value or ""))
    return "<br>".join(part.strip() for part in text.replace("\r", "").split("\n"))


def _mermaid_comment(value: Any) -> str:
    return " ".join(str(value or "").split())


# ---------------------------------------------------------------- parsing agent bodies


def parse_procedure(body: str) -> dict:
    """{intro, sections: [{title, steps: [{n, lead, text}], text}]} from an agent's markdown body.

    A section starts at each heading outside code fences (headings inside a fenced
    template belong to the section's text). Its steps are its top-level numbered items,
    continuation lines folded in; a step's lead is its bold opening phrase, else the words
    up to and including its first ": " or ". " (when that comes early), else empty. A lead
    keeps its own final punctuation (a ":" or "." right after the bold phrase joins it);
    one without any gets a "." when shown.
    """
    intro: list[str] = []
    sections: list[dict] = []
    current = intro
    fenced = False
    for line in body.splitlines():
        if _FENCE_RE.match(line):
            fenced = not fenced
        heading = None if fenced else _HEADING_RE.match(line)
        if heading:
            sections.append({"title": _plain(heading.group(2)), "lines": []})
            current = sections[-1]["lines"]
            continue
        current.append(line)
    result = []
    for section in sections:
        text = "\n".join(section["lines"]).strip("\n")
        result.append({"title": section["title"], "steps": _steps(section["lines"]), "text": text})
    return {"intro": "\n".join(intro).strip("\n"), "sections": result}


def _steps(lines: list[str]) -> list[dict]:
    steps: list[dict] = []
    item: list[str] | None = None
    number = 0
    fenced = False
    for line in lines + [""]:
        if _FENCE_RE.match(line):
            fenced = not fenced
            item = None
            continue
        if fenced:
            continue
        match = _ITEM_RE.match(line)
        if match:
            if item is not None:
                steps.append(_step(number, item))
            number, item = int(match.group(1)), [match.group(2).strip()]
        elif item is not None and line.strip() and (line.startswith((" ", "\t")) or not _BULLET_RE.match(line)):
            if not line.startswith((" ", "\t")) and not item[-1]:
                steps.append(_step(number, item))   # a paragraph after the list ends it
                item = None
                continue
            item.append(line.strip())
        elif item is not None and not line.strip():
            item.append("")
        elif item is not None:
            steps.append(_step(number, item))
            item = None
    if item is not None:
        steps.append(_step(number, item))
    return steps


def _step(number: int, parts: list[str]) -> dict:
    text = " ".join(part for part in parts if part).strip()
    bold = _BOLD_LEAD_RE.match(text)
    if bold:   # the lead keeps its own ":" or ".", or takes the one right after the bold phrase
        lead, after = bold.group(1).strip(), bold.group(2)
        if after and lead[-1:] not in ".?!:":
            lead += after
        return {"n": number, "lead": lead, "text": bold.group(3).strip()}
    cut = min((index for index in (text.find(": "), text.find(". ")) if 0 < index <= LEAD_MAX_CHARS),
              default=-1)
    if cut > 0:   # the lead keeps its ":" or "."
        return {"n": number, "lead": text[:cut + 1].strip(), "text": text[cut + 1:].strip()}
    return {"n": number, "lead": "", "text": text}


def _plain(text: str) -> str:
    return text.replace("**", "").replace("`", "").strip()


# ---------------------------------------------------------------- the data


def procedures(config: Config, agents: dict[str, AgentDef]) -> dict:
    """{generated_at, agents: [...], maps: MAPS} for GET /api/docs/procedures.

    Each agent: {name, description, model (the model it is routed to), tier (its file's), driver,
    tools, color, source, intro (the text before the first heading), procedure (its sections:
    [{title, steps: [{n, lead, text}], text}]), markdown (the raw body)}. The factory line's
    agents come first, in line order, then the rest by name.
    """
    from .settings import agent_source, route_model

    rows = []
    for agent in sorted(agents.values(), key=lambda a: (_line_rank(config, a.name), a.name)):
        try:
            source = agent_source(config, agent)
        except (OSError, ValueError):
            source = "repo"
        parsed = parse_procedure(agent.body)
        rows.append({"name": agent.name, "description": agent.description,
                     "model": route_model(config, agent.name, agent.model), "tier": agent.model,
                     "driver": config.agent_driver(agent.name), "tools": list(agent.tools), "color": agent.color,
                     "source": source, "intro": parsed["intro"], "procedure": parsed["sections"],
                     "markdown": agent.body})
    return {"generated_at": utc_now(), "agents": rows, "maps": maps(config)}


def _line_rank(config: Config, name: str) -> int:
    try:
        reviewers = [f"reviewer-{r}" for r in config.pipeline().reviewers]
    except ValueError:
        reviewers = []
    order = ["spec-writer", "builder", *reviewers, "approver", "lead", "coder", "repo-scout"]
    return order.index(name) if name in order else len(order)


# ---------------------------------------------------------------- the booklet

STYLE = """
body{font:17px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;background:#fafafa;color:#18222c;margin:0}
main{max-width:1180px;margin:auto;padding:32px 16px}
h1{font-size:32px;line-height:1.2;margin:0 0 12px}h2{margin:0 0 8px;font-size:24px}h3{margin:24px 0 6px;font-size:19px}
h4{margin:16px 0 4px;font-size:17px}
nav{padding:16px;border:1px solid #ccc;background:#fff}nav a{display:inline-block;margin:4px 15px 4px 0}
section{background:#fff;border:1px solid #ddd;padding:24px;margin-top:24px}
figure{margin:16px 0;overflow:auto;border-top:1px solid #ddd;padding-top:15px}
figure svg{display:block;max-width:none}figcaption{font-size:15px;color:#39444e;margin-bottom:8px}
p,ul,pre{max-width:1000px}a{color:#175574}.meta{font-size:14px;color:#4a5560;margin-top:0}
details.mermaid-source{margin:4px 0 16px}details.mermaid-source summary{cursor:pointer;font-size:15px;color:#39444e}
p.step{margin:8px 0}code{font:14px/1.4 ui-monospace,"SFMono-Regular",Menlo,monospace;background:#f1f3f5;
padding:1px 4px;border-radius:3px}pre{background:#f6f7f8;border:1px solid #e3e6e8;border-radius:6px;padding:12px;
overflow:auto}pre code{background:none;padding:0}small{font-size:13px}
@media print{body{background:#fff}section{break-inside:avoid-page;border-color:#bbb}nav{display:none}}
"""


def procedures_html(data: dict) -> str:
    """The booklet: one HTML page with inline styles and SVG, no scripts, no external requests."""
    agents = [agent for agent in data.get("agents") or [] if isinstance(agent, dict)]
    maps = [m for m in data.get("maps") or [] if isinstance(m, dict)]
    links = ['<a href="#maps">Process maps</a>'] + [
        f'<a href="#agent-{_esc(_slug(a.get("name")))}">{_esc(a.get("name"))}</a>' for a in agents]
    parts = [
        "<!doctype html>", '<html lang="en">', "<head>", '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        "<title>Software Factory procedures</title>", f"<style>{STYLE}</style>", "</head>", "<body>", "<main>",
        "<h1>Software Factory<br>Working procedures and maps</h1>",
        "<p>Every agent's working procedure, read from its agent file, and the factory's process maps. "
        "This is a viewing copy: the agent files remain what the agents run on."
        f" <small>Generated {_esc(data.get('generated_at') or '')}.</small></p>",
        "<nav>" + "".join(links) + "</nav>",
        '<section id="maps"><h2>Process maps</h2>',
    ]
    for diagram in maps:
        parts.append(f'<h3 id="map-{_esc(_slug(diagram.get("id")))}">{_esc(diagram.get("title"))}</h3>')
        if diagram.get("description"):
            parts.append(f"<p>{_esc(diagram['description'])}</p>")
        parts.append(f"<figure>{map_svg(diagram)}</figure>")
        parts.append(f'<details class="mermaid-source"><summary>Mermaid source</summary>'
                     f"<pre><code>{_esc(map_mermaid(diagram))}</code></pre></details>")
    parts.append("</section>")
    for agent in agents:
        parts.append(_agent_section(agent))
    parts += ["</main>", "</body>", "</html>", ""]
    return "\n".join(parts)


def _agent_section(agent: dict) -> str:
    name = str(agent.get("name") or "")
    meta = [f"model {agent.get('model')}"]
    if agent.get("tier") and agent.get("tier") != agent.get("model"):
        meta.append(f"tier {agent.get('tier')}")
    if agent.get("driver"):
        meta.append(f"driver {agent.get('driver')}")
    if agent.get("tools"):
        meta.append("tools " + ", ".join(str(tool) for tool in agent["tools"]))
    out = [f'<section id="agent-{_esc(_slug(name))}">', f"<h2>{_esc(name)}</h2>",
           f'<p class="meta">{_esc(" · ".join(meta))}</p>']
    if agent.get("description"):
        out.append(f"<p><b>{_esc(agent['description'])}</b></p>")
    if agent.get("intro"):
        out.append(_markdown(str(agent["intro"])))
    for section in agent.get("procedure") if isinstance(agent.get("procedure"), list) else []:
        if not isinstance(section, dict):
            continue
        out.append(f"<h3>{_esc(section.get('title'))}</h3>")
        out.append(_markdown(str(section.get("text") or "")))
    out.append("</section>")
    return "\n".join(out)


def _markdown(text: str) -> str:
    """A small, safe markdown subset: paragraphs, bullet lists, numbered steps in the booklet style
    (number, bold lead phrase, text), fenced code and sub-headings. Everything is escaped first."""
    out: list[str] = []
    paragraph: list[str] = []
    bullets: list[str] = []
    item: list[str] | None = None
    number = 0
    code: list[str] | None = None

    def flush() -> None:
        nonlocal item, paragraph, bullets
        if item is not None:
            step = _step(number, item)
            stop = "" if step["lead"][-1:] in ".?!:" else "."
            lead = f"<b>{_inline(step['lead'])}{stop}</b> " if step["lead"] else ""
            out.append(f'<p class="step">{number}. {lead}{_inline(step["text"])}</p>')
            item = None
        if paragraph:
            out.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph = []
        if bullets:
            out.append("<ul>" + "".join(f"<li>{_inline(b)}</li>" for b in bullets) + "</ul>")
            bullets = []

    for line in text.splitlines():
        if code is not None:
            if _FENCE_RE.match(line):
                out.append("<pre><code>" + _esc("\n".join(code)) + "</code></pre>")
                code = None
            else:
                code.append(line)
            continue
        if _FENCE_RE.match(line):
            flush()
            code = []
            continue
        heading = _HEADING_RE.match(line)
        numbered = _ITEM_RE.match(line)
        bullet = _BULLET_RE.match(line)
        if not line.strip():
            if item is None:
                flush()
            continue
        if heading:
            flush()
            out.append(f"<h4>{_inline(heading.group(2))}</h4>")
        elif numbered:
            flush()
            number, item = int(numbered.group(1)), [numbered.group(2).strip()]
        elif bullet and not line.startswith(("  ", "\t")):
            if item is not None or paragraph:
                flush()
            bullets.append(bullet.group(1).strip())
        elif line.startswith((" ", "\t")) and (item is not None or bullets):
            if item is not None:
                item.append(line.strip())
            else:
                bullets[-1] += " " + line.strip()
        else:
            if item is not None or bullets:
                flush()
            paragraph.append(line.strip())
    if code is not None:
        out.append("<pre><code>" + _esc("\n".join(code)) + "</code></pre>")
    flush()
    return "\n".join(out)


def _inline(text: str) -> str:
    """Escape text, then render `code` and **bold** (the only inline markup kept)."""
    pieces = []
    last = 0
    for match in _INLINE_RE.finditer(text):
        pieces.append(_esc(text[last:match.start()]))
        if match.group(1) is not None:
            pieces.append(f"<code>{_esc(match.group(1))}</code>")
        else:
            pieces.append(f"<b>{_esc(match.group(2))}</b>")
        last = match.end()
    pieces.append(_esc(text[last:]))
    return "".join(pieces)


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", str(value or "").lower()).strip("-") or "x"


# ---------------------------------------------------------------- SVG maps

COL_W = 250
ROW_H = 84
PAD_X = 80        # room for the lanes of back edges, left and right
PAD_Y = 24
CHAR_W = 6.7      # rough width of a 12px system-ui character
LINE_H = 15
LANE_STEP = 16
NODE_STROKE = "#4a5560"
TEXT = "#17212b"
EDGE = "#66727d"
LABEL = "#39444e"
HUMAN_FILL, HUMAN_STROKE = "#fff6e5", "#a86b16"
TERMINAL_FILL = "#f2f4f6"


class _Box:
    def __init__(self, node: dict) -> None:
        self.id = str(node.get("id"))
        self.kind = str(node.get("kind") or "box")
        self.lines = str(node.get("label") or "").split("\n")
        self.cx = PAD_X + int(node.get("col") or 0) * COL_W + COL_W / 2
        self.cy = PAD_Y + int(node.get("row") or 0) * ROW_H + ROW_H / 2
        text_w = max((len(line) for line in self.lines), default=1) * CHAR_W
        if self.kind == "decision":
            self.w, self.h = min(COL_W - 20, text_w + 64), 44.0
        else:
            self.w, self.h = min(COL_W - 24, text_w + 28), 20.0 + LINE_H * len(self.lines)
        self.left, self.right = self.cx - self.w / 2, self.cx + self.w / 2
        self.top, self.bottom = self.cy - self.h / 2, self.cy + self.h / 2

    def svg(self) -> str:
        fill, stroke = ("#fff", NODE_STROKE)
        if self.kind == "human":
            fill, stroke = HUMAN_FILL, HUMAN_STROKE
        elif self.kind == "terminal":
            fill = TERMINAL_FILL
        if self.kind == "decision":
            points = (f"{self.cx:.1f},{self.top:.1f} {self.right:.1f},{self.cy:.1f} "
                      f"{self.cx:.1f},{self.bottom:.1f} {self.left:.1f},{self.cy:.1f}")
            shape = f'<polygon points="{points}" fill="{fill}" stroke="{stroke}" stroke-width="1.3"/>'
        else:
            radius = self.h / 2 if self.kind == "terminal" else 8
            shape = (f'<rect x="{self.left:.1f}" y="{self.top:.1f}" width="{self.w:.1f}" height="{self.h:.1f}" '
                     f'rx="{radius:.1f}" fill="{fill}" stroke="{stroke}" stroke-width="1.3"/>')
        first = self.cy - (len(self.lines) - 1) * LINE_H / 2 + 4
        bold = ' font-weight="600"'
        texts = "".join(
            f'<text x="{self.cx:.1f}" y="{first + i * LINE_H:.1f}" text-anchor="middle" font-size="12" '
            f'fill="{TEXT}"{bold if i == 0 else ""}>{_esc(line)}</text>'
            for i, line in enumerate(self.lines))
        return f"<g>{shape}{texts}</g>"


def map_svg(diagram: dict) -> str:
    """One map as an inline SVG: nodes on a grid (col, row), edges routed straight, as an elbow, or
    for back edges along a lane beside the nodes; every label escaped."""
    boxes = {box.id: box for box in (_Box(node) for node in diagram.get("nodes") or [] if isinstance(node, dict))}
    cols = max((int(n.get("col") or 0) for n in diagram.get("nodes") or []), default=0) + 1
    rows = max((int(n.get("row") or 0) for n in diagram.get("nodes") or []), default=0) + 1
    width, height = PAD_X * 2 + cols * COL_W, PAD_Y * 2 + rows * ROW_H
    marker = f"arrow-{_slug(diagram.get('id'))}"
    edges, labels = [], []
    for edge in diagram.get("edges") or []:
        source, target = boxes.get(str(edge.get("from"))), boxes.get(str(edge.get("to")))
        if source is None or target is None:
            continue
        points, anchor = _route(source, target, edge)
        path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(points))
        edges.append(f'<path d="{path}" fill="none" stroke="{EDGE}" stroke-width="1.3" '
                     f'marker-end="url(#{marker})"/>')
        if edge.get("label"):
            labels.append(_label(str(edge["label"]), *anchor))
    title = _esc(diagram.get("title"))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
            f'viewBox="0 0 {width:.0f} {height:.0f}" role="img" aria-label="{title}" '
            f'font-family="system-ui,-apple-system,Segoe UI,sans-serif"><title>{title}</title>'
            f'<defs><marker id="{marker}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" '
            f'markerHeight="8" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{EDGE}"/></marker>'
            f'</defs>{"".join(edges)}{"".join(box.svg() for box in boxes.values())}{"".join(labels)}</svg>')


def _route(a: _Box, b: _Box, edge: dict) -> tuple[list[tuple[float, float]], tuple[float, float, str]]:
    """The polyline from a to b and where its label goes (x, y, text-anchor).

    An edge with a side (or one back up its own column) runs along a lane beside the nodes;
    one down its column is straight, one along its row horizontal, any other an elbow that
    leaves a's side and enters b from above or below.
    """
    side = edge.get("side")
    same_col = abs(a.cx - b.cx) < 1
    if side in ("left", "right") or (same_col and b.cy < a.cy - 1):
        side = side if side in ("left", "right") else "right"
        lane = int(edge.get("lane") or 0)
        if side == "left":
            x = min(a.left, b.left) - 26 - LANE_STEP * lane
            start, end = (a.left, a.cy), (b.left, b.cy)
        else:
            x = max(a.right, b.right) + 26 + LANE_STEP * lane
            start, end = (a.right, a.cy), (b.right, b.cy)
        anchor = (x - 6, (a.cy + b.cy) / 2, "end") if side == "left" else (x + 6, (a.cy + b.cy) / 2, "start")
        return [start, (x, a.cy), (x, b.cy), end], anchor
    if same_col:
        start, end = (a.cx, a.bottom), (b.cx, b.top)
        return [start, end], (a.cx + 8, (a.bottom + b.top) / 2 + 4, "start")
    start = (a.right, a.cy) if b.cx > a.cx else (a.left, a.cy)
    if abs(a.cy - b.cy) < 1:
        end = (b.left, b.cy) if b.cx > a.cx else (b.right, b.cy)
        return [start, end], ((start[0] + end[0]) / 2, a.cy - 7, "middle")
    end = (b.cx, b.top) if b.cy > a.cy else (b.cx, b.bottom)
    return [start, (b.cx, a.cy), end], ((start[0] + b.cx) / 2, a.cy - 7, "middle")


def _label(text: str, x: float, y: float, anchor: str) -> str:
    width = len(text) * 6.0 + 8
    left = {"start": x - 4, "end": x - width + 4, "middle": x - width / 2}[anchor]
    return (f'<g><rect x="{left:.1f}" y="{y - 11:.1f}" width="{width:.1f}" height="15" rx="3" fill="#fff" '
            f'fill-opacity="0.92"/><text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" font-size="11" '
            f'fill="{LABEL}">{_esc(text)}</text></g>')
