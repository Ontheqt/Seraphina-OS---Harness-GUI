"""Tests for swfactory.docs: procedure parsing, the procedures data and the offline HTML booklet."""

from __future__ import annotations

import hashlib
import html
import html.parser
import re
import unittest
from pathlib import Path

from swfactory import docs
from swfactory.agentdefs import AgentDef
from swfactory.config import Config
from swfactory.settings import resolved_agents

BODY = """You do one thing.

## Working procedure

1. **Open when an invitation is needed.** At the start of an empty conversation,
   introduce yourself.
2. **Pass on what is supplied.** Send it on.
3. Read first: the repo instructions.
4. Short

Prose after the list.

## Template

```markdown
# Not a section
1. not a step
## Also not a section
```

- a bullet <b>bold</b>
- another with `code`

### Sub heading
10) Late step: numbered with a parenthesis.
"""


def agent(name: str, body: str = BODY, model: str = "opus") -> AgentDef:
    return AgentDef(name=name, description=f"The {name} <agent>.", tools=["Read", "Bash"], model=model, skills=[],
                    mcp=[], color=None, body=body, path=Path("/agents") / f"{name}.md")


class ParseTest(unittest.TestCase):
    def test_sections_steps_and_leads(self) -> None:
        parsed = docs.parse_procedure(BODY)
        self.assertEqual(parsed["intro"], "You do one thing.")
        self.assertEqual([s["title"] for s in parsed["sections"]], ["Working procedure", "Template", "Sub heading"])
        steps = parsed["sections"][0]["steps"]
        self.assertEqual(steps, [
            {"n": 1, "lead": "Open when an invitation is needed.",
             "text": "At the start of an empty conversation, introduce yourself."},
            {"n": 2, "lead": "Pass on what is supplied.", "text": "Send it on."},
            {"n": 3, "lead": "Read first:", "text": "the repo instructions."},
            {"n": 4, "lead": "", "text": "Short"}])
        self.assertEqual(parsed["sections"][1]["steps"], [], "items inside a code fence are not steps")
        self.assertIn("# Not a section", parsed["sections"][1]["text"])
        self.assertEqual(parsed["sections"][2]["steps"],
                         [{"n": 10, "lead": "Late step:", "text": "numbered with a parenthesis."}])

    def test_a_lead_keeps_its_own_punctuation_bold_or_not(self) -> None:
        steps = docs.parse_procedure("## S\n1. **Read the spec:** then build.\n2. Plain lead: text\n"
                                     "3. **Bold lead.** more\n4. **Colon outside**: after\n"
                                     "5. **Period outside**. after\n6. **Asks?** yes\n7. **No mark** rest\n"
                                     "8. **Both.**: odd\n")["sections"][0]["steps"]
        self.assertEqual([(s["lead"], s["text"]) for s in steps],
                         [("Read the spec:", "then build."), ("Plain lead:", "text"), ("Bold lead.", "more"),
                          ("Colon outside:", "after"), ("Period outside.", "after"), ("Asks?", "yes"),
                          ("No mark", "rest"), ("Both.", "odd")])
        page = BookletTest().render("## S\n1. **Read the spec:** then build.\n2. **No mark** rest\n")
        self.assertIn('<p class="step">1. <b>Read the spec:</b> then build.</p>', page)
        self.assertIn('<p class="step">2. <b>No mark.</b> rest</p>', page, "a lead without punctuation gets a '.'")

    def test_every_shipped_agent_parses(self) -> None:
        config = Config(Path("/nonexistent/config.json"))
        for name, definition in resolved_agents(config).items():
            with self.subTest(agent=name):
                parsed = docs.parse_procedure(definition.body)
                self.assertTrue(parsed["sections"], "every shipped agent has sections")
                for section in parsed["sections"]:
                    self.assertNotIn("\n", section["title"])
        builder = docs.parse_procedure(resolved_agents(config)["builder"].body)
        titles = [section["title"] for section in builder["sections"]]
        self.assertIn("Read first", titles)
        self.assertNotIn("Summary", titles, "the build.md template's headings stay in its section")
        read_first = next(s for s in builder["sections"] if s["title"] == "Read first")
        self.assertEqual([step["n"] for step in read_first["steps"]], [1, 2, 3, 4, 5])


class DataTest(unittest.TestCase):
    def test_procedures_lists_the_line_first_with_routes(self) -> None:
        config = Config(Path("/nonexistent/config.json"), {"agents": {"builder": {"model": "sonnet"}}})
        agents = {name: agent(name) for name in ("zeta", "approver", "builder", "spec-writer", "reviewer-code")}
        data = docs.procedures(config, agents)
        self.assertEqual([a["name"] for a in data["agents"]],
                         ["spec-writer", "builder", "reviewer-code", "approver", "zeta"])
        builder = data["agents"][1]
        self.assertEqual((builder["model"], builder["tier"], builder["driver"]), ("sonnet", "opus", "builtin"))
        self.assertEqual(builder["markdown"], BODY)
        self.assertEqual(builder["procedure"], docs.parse_procedure(BODY)["sections"])
        self.assertEqual(builder["intro"], "You do one thing.")
        self.assertEqual(set(builder), {"name", "description", "model", "tier", "driver", "tools", "color", "source",
                                        "intro", "procedure", "markdown"})
        self.assertRegex(data["generated_at"], r"Z$")

    def test_maps_are_consistent(self) -> None:
        for diagram in docs.MAPS:
            with self.subTest(map=diagram["id"]):
                ids = [node["id"] for node in diagram["nodes"]]
                self.assertEqual(len(ids), len(set(ids)))
                cells = [(node["col"], node["row"]) for node in diagram["nodes"]]
                self.assertEqual(len(cells), len(set(cells)), "no two nodes share a cell")
                for edge in diagram["edges"]:
                    self.assertIn(edge["from"], ids)
                    self.assertIn(edge["to"], ids)
                for node in diagram["nodes"]:
                    self.assertIn(node["kind"], ("box", "decision", "terminal", "human"))
        line = next(m for m in docs.MAPS if m["id"] == "factory-line")
        labels = {edge["label"] for edge in line["edges"]}
        self.assertTrue({"merge", "branch", "folder", "ESCALATE", "BLOCKED"} <= labels)


class MermaidTest(unittest.TestCase):
    def test_maps_follow_the_pipeline(self) -> None:
        config = Config(Path("/nonexistent/config.json"),
                        {"pipeline": {"reviewers": ["code", "perf", "a11y", "ux", "ui"], "max_resumes": 3}})
        line = next(m for m in docs.maps(config) if m["id"] == "factory-line")
        review = next(n for n in line["nodes"] if n["id"] == "review")
        self.assertEqual(review["label"], "Reviewers in parallel\ncode · perf · a11y · ux\nui")
        self.assertIn("CHANGES: resume (max 3)", {e["label"] for e in line["edges"]})
        none = Config(Path("/nonexistent/config.json"), {"pipeline": {"max_resumes": 0}})
        line = next(m for m in docs.maps(none) if m["id"] == "factory-line")
        self.assertFalse(any(e["from"] == "verdicts" and e["to"] == "build" for e in line["edges"]),
                         "no resumes: no CHANGES loop")
        self.assertEqual(docs.maps(), [dict(m) for m in docs.MAPS], "no config: the shipped drawing")
        bad = Config(Path("/nonexistent/config.json"), {"pipeline": {"reviewers": "code"}})
        self.assertEqual(docs.maps(bad), [dict(m) for m in docs.MAPS])
        docs.maps(config)[0]["nodes"].clear()
        self.assertTrue(docs.MAPS[0]["nodes"], "maps() hands out copies")

    def test_factory_line_as_mermaid(self) -> None:
        source = docs.map_mermaid(next(m for m in docs.MAPS if m["id"] == "factory-line"))
        lines = source.splitlines()
        self.assertEqual(lines[:2], ["%% The factory line", "flowchart TD"])
        self.assertTrue(source.endswith("\n"))
        self.assertIn('    feature(["Feature request"])', lines)
        self.assertIn('    spec["spec-writer<br>writes spec.md"]', lines)
        self.assertIn('    ready{"STATUS: READY?"}', lines)
        self.assertIn('    ready -->|"BLOCKED"| human', lines)
        self.assertIn('    review -->|"wait for all"| verdicts', lines)
        self.assertIn('    verdicts -.->|"CHANGES: resume (max 2)"| build', lines, "a back edge is dotted")
        self.assertIn("    feature --> spec", lines, "an unlabelled route has no label")
        self.assertIn("    class human human", lines)
        self.assertIn("    class feature,merged,branch,folder terminal", lines)
        edges = [line for line in lines if "-->" in line or "-.->" in line]
        line = next(m for m in docs.MAPS if m["id"] == "factory-line")
        self.assertEqual(len(edges), len(line["edges"]))
        for edge in line["edges"]:
            if edge["label"]:
                self.assertIn(f'|"{edge["label"]}"|', source)

    def test_labels_cannot_break_out(self) -> None:
        diagram = {"id": "x", "title": "T\n<b>", "nodes": [
            {"id": "end", "label": 'say "hi" | <b>&#1;</b>', "kind": "box", "col": 0, "row": 0},
            {"id": "2nd", "label": "B", "kind": "human", "col": 0, "row": 1},
            {"id": "a-b", "label": "C", "kind": "weird", "col": 0, "row": 2}],
            "edges": [{"from": "end", "to": "2nd", "label": 'go"]; click x'}, {"from": "2nd", "to": "missing"},
                      {"from": "a-b", "to": "end", "label": "", "side": "left"}]}
        source = docs.map_mermaid(diagram)
        lines = source.splitlines()
        self.assertEqual(lines[0], "%% T <b>", "the title stays on its comment line")
        self.assertIn('    n_end["say #quot;hi#quot; #124; #lt;b#gt;#amp;#35;1;#lt;/b#gt;"]', lines,
                      "a reserved id is renamed; quotes, pipes, brackets, & and # become entity codes")
        self.assertIn('    n_2nd["B"]', lines, "an id must start with a letter")
        self.assertIn('    a_b["C"]', lines, "an unknown kind is a box")
        self.assertIn('    n_end -->|"go#quot;]; click x"| n_2nd', lines)
        self.assertIn("    a_b -.-> n_end", lines)
        self.assertEqual(sum("-->" in line or "-.->" in line for line in lines), 2, "an edge to an unknown node is skipped")
        self.assertNotIn('"]; click', source)

    def test_mermaid_maps_carry_their_sha256(self) -> None:
        config = Config(Path("/nonexistent/config.json"))
        entries = docs.mermaid_maps(config)
        self.assertEqual([e["id"] for e in entries], ["factory-line", "plan-flow"])
        for entry, diagram in zip(entries, docs.maps(config)):
            self.assertEqual(set(entry), {"id", "title", "mermaid", "sha256"})
            self.assertEqual(entry["title"], diagram["title"])
            self.assertEqual(entry["mermaid"], docs.map_mermaid(diagram))
            self.assertEqual(entry["sha256"], hashlib.sha256(entry["mermaid"].encode("utf-8")).hexdigest())
        self.assertEqual(entries, docs.mermaid_maps(config), "stable: the same config gives the same bytes")


class _Collector(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.attrs: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        self.attrs += attrs


class BookletTest(unittest.TestCase):
    def render(self, body: str = BODY) -> str:
        config = Config(Path("/nonexistent/config.json"))
        return docs.procedures_html(docs.procedures(config, {"builder": agent("builder", body)}))

    def test_self_contained_and_offline(self) -> None:
        page = self.render()
        self.assertTrue(page.startswith("<!doctype html>"))
        collector = _Collector()
        collector.feed(page)
        self.assertNotIn("script", collector.tags)
        self.assertNotIn("link", collector.tags)
        self.assertNotIn("img", collector.tags)
        for name, value in collector.attrs:
            if name in ("src", "href") and value:
                self.assertTrue(value.startswith("#"), f"{name}={value} must be an in-page link")
        self.assertNotRegex(page, r"https?://(?!www\.w3\.org/2000/svg)")
        self.assertEqual(page.count("<svg"), len(docs.MAPS))
        self.assertEqual(page.count('<details class="mermaid-source">'), len(docs.MAPS))
        self.assertIn("system-ui", page)
        self.assertIn("background:#fafafa", page)

    def test_steps_in_the_booklet_style(self) -> None:
        page = self.render()
        self.assertIn('<p class="step">1. <b>Open when an invitation is needed.</b> At the start of an empty '
                      'conversation, introduce yourself.</p>', page)
        self.assertIn('<p class="step">3. <b>Read first:</b> the repo instructions.</p>', page)
        self.assertIn('<p class="step">4. Short</p>', page)
        self.assertIn("<pre><code># Not a section\n1. not a step", page)
        self.assertIn("<li>another with <code>code</code></li>", page)
        self.assertIn("<h3>Working procedure</h3>", page)

    def test_everything_from_agent_files_is_escaped(self) -> None:
        evil = ('Hi <script>alert(1)</script>\n\n## <img src=x onerror=alert(2)>\n\n'
                '1. **<b onclick="x">Lead</b>** `<svg onload=alert(3)>` [link](javascript:alert(4))\n')
        page = self.render(evil)
        for raw in ("<script>alert", "<img src=x", "<b onclick", "<svg onload"):
            self.assertNotIn(raw, page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn("&lt;img src=x onerror=alert(2)&gt;", page)
        self.assertNotIn('href="javascript', page)
        self.assertIn("The builder &lt;agent&gt;.", page)

    def test_each_map_embeds_its_mermaid_source(self) -> None:
        page = self.render()
        for diagram in docs.maps(Config(Path("/nonexistent/config.json"))):
            source = html.escape(docs.map_mermaid(diagram), quote=True)
            self.assertIn(f"<summary>Mermaid source</summary><pre><code>{source}</code></pre></details>", page)
        self.assertLess(page.index("<svg"), page.index("Mermaid source"), "the source sits under its map")

    def test_map_svg_labels_are_escaped(self) -> None:
        diagram = {"id": "x", "title": "T <&>", "nodes": [
            {"id": "a", "label": "A <b>\nline 2", "kind": "box", "col": 0, "row": 0},
            {"id": "b", "label": "B?", "kind": "decision", "col": 0, "row": 1},
            {"id": "c", "label": "C", "kind": "human", "col": 1, "row": 1},
            {"id": "d", "label": "D", "kind": "terminal", "col": 1, "row": 0}],
            "edges": [{"from": "a", "to": "b", "label": "<go>"}, {"from": "b", "to": "c", "label": "no"},
                      {"from": "c", "to": "d", "label": "up"}, {"from": "b", "to": "a", "label": "back",
                                                               "side": "left"},
                      {"from": "a", "to": "missing"}]}
        svg = docs.map_svg(diagram)
        self.assertIn("&lt;go&gt;", svg)
        self.assertIn("A &lt;b&gt;", svg)
        self.assertIn('aria-label="T &lt;&amp;&gt;"', svg)
        self.assertEqual(svg.count("marker-end="), 4, "an edge to an unknown node is skipped")
        self.assertEqual(len(re.findall(r"<polygon", svg)), 1)
        self.assertIn('marker-end="url(#arrow-x)"', svg)


if __name__ == "__main__":
    unittest.main()
