from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from swfactory.agentdefs import (DEFAULT_TOOLS, MAX_FILE_BYTES, AgentDef, clean_value, discover_agents,
                                 parse_agent_file, parse_bool, parse_frontmatter, read_small_text,
                                 required_agents, split_list)
from swfactory.config import PROJECT_DIR

DESIGN_EXAMPLE = """---
name: reviewer-code
description: One line.
tools: Read, Grep, Glob, Bash, Write
model: sonnet
skills: code-review          # optional, preloaded into the system prompt
mcp: rea                     # optional, MCP servers (builtin driver only)
color: cyan                  # optional (GUI + Claude Code)
---
Body = the system prompt.
"""


class FrontmatterTest(unittest.TestCase):
    def test_basic_and_body(self) -> None:
        meta, body = parse_frontmatter("---\nname: x\ndescription: hi there\n---\n\n# Title\n\ntext\n", "f.md")
        self.assertEqual(meta, {"name": "x", "description": "hi there"})
        self.assertEqual(body, "# Title\n\ntext")

    def test_splits_on_first_colon_and_keeps_dashed_keys(self) -> None:
        meta, _ = parse_frontmatter(
            "---\ndescription: Use when: the user asks\nhomepage: https://x.dev:8080/a\n"
            "disable-model-invocation: true\nargument-hint:<feature>\n---\n", "f.md")
        self.assertEqual(meta["description"], "Use when: the user asks")
        self.assertEqual(meta["homepage"], "https://x.dev:8080/a")
        self.assertEqual(meta["disable-model-invocation"], "true")
        self.assertEqual(meta["argument-hint"], "<feature>")

    def test_quotes_comments_and_blank_lines(self) -> None:
        text = ('---\n# a comment\n\nname: "quoted"\nsingle: \'it is\'\nmismatched: "abc\'\n'
                'hash: "a # b"   # trailing\nplain: value # comment\nkeep: issue#12\nempty:\n---\nbody')
        meta, body = parse_frontmatter(text, "f.md")
        self.assertEqual(meta["name"], "quoted")
        self.assertEqual(meta["single"], "it is")
        self.assertEqual(meta["mismatched"], "\"abc'")
        self.assertEqual(meta["hash"], "a # b")
        self.assertEqual(meta["plain"], "value")
        self.assertEqual(meta["keep"], "issue#12")
        self.assertEqual(meta["empty"], "")
        self.assertEqual(body, "body")

    def test_crlf_bom_and_trailing_spaces_on_delimiters(self) -> None:
        meta, body = parse_frontmatter("﻿---  \r\nname: x\r\n---\r\nline1\r\nline2\r\n", "f.md")
        self.assertEqual(meta, {"name": "x"})
        self.assertEqual(body, "line1\nline2")

    def test_multiline_values(self) -> None:
        text = ("---\ndescription: >\n  Folded text\n  over lines.\nnotes: |\n  line a\n  - not a list\n"
                "tools:\n  - Read\n  - \"Grep\"\n- Bash\nlong: starts here\n  and continues\n"
                "metadata:\n  short-description: nested\n---\n")
        meta, _ = parse_frontmatter(text, "f.md")
        self.assertEqual(meta["description"], "Folded text over lines.")
        self.assertEqual(meta["notes"], "line a\n- not a list")
        self.assertEqual(meta["tools"], "Read, Grep, Bash")
        self.assertEqual(meta["long"], "starts here and continues")
        self.assertEqual(meta["metadata"], "short-description: nested")

    def test_empty_frontmatter_and_last_duplicate_wins(self) -> None:
        self.assertEqual(parse_frontmatter("---\n---\n", "f.md"), ({}, ""))
        self.assertEqual(parse_frontmatter("---\na: 1\na: 2\n---\n", "f.md")[0], {"a": "2"})

    def test_body_may_contain_delimiters_and_indented_dashes(self) -> None:
        meta, body = parse_frontmatter("---\na: 1\n  ---\n---\nx\n---\ny\n", "f.md")
        self.assertEqual(meta, {"a": "1 ---"})
        self.assertEqual(body, "x\n---\ny")

    def test_errors_name_the_path(self) -> None:
        cases = {
            "": "missing frontmatter",
            "# Title\n---\nname: x\n---\n": "missing frontmatter",
            "---\nname: x\nbody without end\n": "expected 'key: value'",
            "---\nname: x\n": "not closed",
            "---\n  indented: first\n---\n": "continuation line before any key",
            "---\n: novalue\n---\n": "expected 'key: value'",
        }
        for text, message in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(ValueError) as caught:
                    parse_frontmatter(text, "/some/agent.md")
                self.assertIn("/some/agent.md", str(caught.exception))
                self.assertIn(message, str(caught.exception))


class ValueHelpersTest(unittest.TestCase):
    def test_clean_value(self) -> None:
        self.assertEqual(clean_value('  "x"  '), "x")
        self.assertEqual(clean_value("'x' # c"), "x")
        self.assertEqual(clean_value('"'), '"')
        self.assertEqual(clean_value("a\t# c"), "a")

    def test_split_list(self) -> None:
        self.assertEqual(split_list("Read, Grep,Glob"), ["Read", "Grep", "Glob"])
        self.assertEqual(split_list("[a, 'b', \"c\"]"), ["a", "b", "c"])
        self.assertEqual(split_list("[]"), [])
        self.assertEqual(split_list(" a ,, b , "), ["a", "b"])
        self.assertEqual(split_list(""), [])
        self.assertEqual(split_list(None), [])
        self.assertEqual(split_list("single"), ["single"])

    def test_parse_bool(self) -> None:
        for word in ("true", "True", " YES ", "on", "1"):
            self.assertTrue(parse_bool(word))
        for word in ("false", "FALSE", "no", "off", "0"):
            self.assertFalse(parse_bool(word, True))
        self.assertTrue(parse_bool(None, True))
        self.assertFalse(parse_bool("  "))
        with self.assertRaises(ValueError):
            parse_bool("maybe")


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def write(self, rel: str, text: str) -> Path:
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path


class AgentFileTest(TempDirCase):
    def test_design_example(self) -> None:
        path = self.write("reviewer-code.md", DESIGN_EXAMPLE)
        agent = parse_agent_file(path)
        self.assertEqual(agent, AgentDef(
            name="reviewer-code", description="One line.", tools=["Read", "Grep", "Glob", "Bash", "Write"],
            model="sonnet", skills=["code-review"], mcp=["rea"], color="cyan",
            body="Body = the system prompt.", path=path, meta=agent.meta))
        self.assertEqual(agent.meta["model"], "sonnet")

    def test_defaults(self) -> None:
        agent = parse_agent_file(self.write("my-agent.md", "---\ndescription: d\n---\nPrompt"))
        self.assertEqual(agent.name, "my-agent")
        self.assertEqual(agent.model, "opus")
        self.assertEqual(agent.tools, list(DEFAULT_TOOLS))
        self.assertEqual((agent.skills, agent.mcp, agent.color), ([], [], None))

    def test_tools_none_is_an_agent_that_only_talks(self) -> None:
        for line, tools in (("tools: none\n", []), ("tools: None\n", []), ("tools:\n", list(DEFAULT_TOOLS)),
                            ("", list(DEFAULT_TOOLS)), ("tools: Read, none\n", ["Read", "none"])):
            with self.subTest(line=line):
                agent = parse_agent_file(self.write("talker.md", f"---\nname: talker\n{line}---\nTalk."))
                self.assertEqual(agent.tools, tools)

    def test_inherit_model_and_bracket_lists(self) -> None:
        agent = parse_agent_file(self.write("a.md", "---\nname: b\nmodel: inherit\nskills: [tdd, ddd]\n"
                                                    "tools: [Read]\n---\n"))
        self.assertEqual((agent.name, agent.model, agent.skills, agent.tools), ("b", "opus", ["tdd", "ddd"], ["Read"]))

    def test_to_dict_is_json_ready(self) -> None:
        path = self.write("reviewer-code.md", DESIGN_EXAMPLE)
        data = parse_agent_file(path).to_dict()
        self.assertEqual(data["path"], str(path))
        self.assertEqual(data["body"], "Body = the system prompt.")
        json.dumps(data)

    def test_invalid_agent_files(self) -> None:
        cases = {"no-front.md": "Just a prompt", "bad-name.md": "---\nname: ../evil\n---\n"}
        for name, text in cases.items():
            path = self.write(name, text)
            with self.subTest(name), self.assertRaises(ValueError) as caught:
                parse_agent_file(path)
            self.assertIn(str(path), str(caught.exception))
        with self.assertRaisesRegex(ValueError, "cannot read"):
            parse_agent_file(self.tmp / "missing.md")
        binary = self.tmp / "bin.md"
        binary.write_bytes(b"---\nname: \xff\n---\n")
        with self.assertRaisesRegex(ValueError, "cannot read"):
            parse_agent_file(binary)


class ReadSmallTextTest(TempDirCase):
    def test_reads_regular_utf8_files_up_to_the_limit(self) -> None:
        path = self.write("a.txt", "héllo\r\nwörld")
        self.assertEqual(read_small_text(path, 100), "héllo\r\nwörld")
        self.assertEqual(read_small_text(str(path), len(path.read_bytes())), "héllo\r\nwörld")
        with self.assertRaisesRegex(OSError, "larger than"):
            read_small_text(path, 5)

    def test_refuses_dirs_devices_fifos_missing_and_binary(self) -> None:
        open_fds = len(os.listdir("/proc/self/fd"))
        with self.assertRaisesRegex(OSError, "not a regular file"):
            read_small_text(self.tmp, 10)
        with self.assertRaisesRegex(OSError, "not a regular file"):
            read_small_text("/dev/zero", 10)
        self.assertEqual(len(os.listdir("/proc/self/fd")), open_fds)
        with self.assertRaises(FileNotFoundError):
            read_small_text(self.tmp / "missing", 10)
        binary = self.tmp / "bin"
        binary.write_bytes(b"\xff\xfe")
        with self.assertRaises(UnicodeDecodeError):
            read_small_text(binary, 10)
        fifo = self.tmp / "pipe"
        os.mkfifo(fifo)
        errors: list[BaseException] = []

        def read_fifo() -> None:
            try:
                read_small_text(fifo, 10)
            except OSError as exc:
                errors.append(exc)

        worker = threading.Thread(target=read_fifo, daemon=True)
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive(), "read_small_text blocked on a FIFO")
        self.assertIn("not a regular file", str(errors[0]))

    def test_agent_file_size_limit_and_device_symlink(self) -> None:
        big = self.write("big.md", "---\nname: big\n---\n" + "x" * MAX_FILE_BYTES)
        with self.assertRaisesRegex(ValueError, "cannot read agent file.*larger than"):
            parse_agent_file(big)
        os.symlink("/dev/zero", self.tmp / "zero.md")
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            parse_agent_file(self.tmp / "zero.md")


class DiscoverTest(TempDirCase):
    def test_later_dirs_override_by_name(self) -> None:
        self.write("project/builder.md", "---\nname: builder\nmodel: opus\n---\nproject builder")
        self.write("project/approver.md", "---\nname: approver\n---\nproject approver")
        self.write("user/custom-builder.md", "---\nname: builder\nmodel: sonnet\n---\nuser builder")
        self.write("repo/approver.md", "---\n---\nrepo approver")
        agents = discover_agents([self.tmp / "project", self.tmp / "missing", self.tmp / "user", self.tmp / "repo"])
        self.assertEqual(sorted(agents), ["approver", "builder"])
        self.assertEqual((agents["builder"].body, agents["builder"].model), ("user builder", "sonnet"))
        self.assertEqual(agents["approver"].body, "repo approver")

    def test_skips_readme_dotfiles_other_extensions_and_subdirs(self) -> None:
        self.write("a/README.md", "# Agents\nno frontmatter")
        self.write("a/.hidden.md", "no frontmatter")
        self.write("a/notes.txt", "x")
        self.write("a/nested/deep.md", "---\n---\n")
        self.write("a/ok.md", "---\n---\nok")
        self.assertEqual(sorted(discover_agents([self.tmp / "a"])), ["ok"])

    def test_duplicate_name_in_one_dir_is_an_error(self) -> None:
        self.write("a/one.md", "---\nname: same\n---\n")
        self.write("a/two.md", "---\nname: same\n---\n")
        with self.assertRaisesRegex(ValueError, "already defined"):
            discover_agents([self.tmp / "a"])

    def test_bad_file_raises_with_path(self) -> None:
        bad = self.write("a/broken.md", "no frontmatter")
        with self.assertRaisesRegex(ValueError, str(bad)):
            discover_agents([self.tmp / "a"])

    def test_no_dirs(self) -> None:
        self.assertEqual(discover_agents([]), {})


class RequiredAgentsTest(unittest.TestCase):
    def test_required_agents(self) -> None:
        self.assertEqual(required_agents(["security", "ux"]),
                         ["spec-writer", "builder", "approver", "reviewer-security", "reviewer-ux"])
        self.assertEqual(required_agents([]), ["spec-writer", "builder", "approver"])

    def test_project_agents_cover_the_default_line(self) -> None:
        agents = discover_agents([PROJECT_DIR / "agents"])
        for name in required_agents(["security", "ux", "ui", "code"]) + ["repo-scout", "coder"]:
            with self.subTest(agent=name):
                self.assertIn(name, agents)
                self.assertIn(agents[name].model, ("opus", "sonnet"))
                self.assertTrue(agents[name].description)
                self.assertTrue(agents[name].body)


if __name__ == "__main__":
    unittest.main()


class ShippedPromptRulesTest(unittest.TestCase):
    """The rules live kit runs added to the shipped prompts (DESIGN.md §7): installs, dependencies,
    tautological tests, spec'd accessibility, the dev-server recipe and when an assumption is (product)."""

    REVIEWERS = ("reviewer-security", "reviewer-ux", "reviewer-ui", "reviewer-code")

    def text(self, name: str) -> str:
        return " ".join(parse_agent_file(PROJECT_DIR / "agents" / f"{name}.md").body.split())

    def test_reviewers_and_the_approver_never_install(self) -> None:
        for name in (*self.REVIEWERS, "approver"):
            with self.subTest(agent=name):
                self.assertIn("Never run `npm install`, `pip install` or any other install in the worktree",
                              self.text(name))
        for name in self.REVIEWERS:
            self.assertIn("If a check can't run without an install, don't install; report it under Findings",
                          self.text(name))

    def test_dependencies_and_tests(self) -> None:
        code = self.text("reviewer-code")
        self.assertIn("A tautological test is a finding", code)
        self.assertIn("an unused one is a finding", code)
        self.assertIn("a dependency the branch adds but never uses", self.text("reviewer-security"))
        builder = self.text("builder")
        self.assertIn("add only one your code imports or runs: never one you don't use", builder)
        self.assertIn("commit the manifest and its lockfile together", builder)
        self.assertIn("if the repo doesn't ignore them yet, add them to `.gitignore`", builder)

    def test_spec_demanded_accessibility_is_changes(self) -> None:
        self.assertIn("When the spec demands a contrast ratio or an accessibility level", self.text("reviewer-ui"))
        self.assertIn("that is CHANGES, never a Note", self.text("reviewer-ui"))
        self.assertIn("When the spec demands an accessibility behaviour or level", self.text("reviewer-ux"))
        self.assertIn("that is CHANGES, never a Note", self.text("reviewer-ux"))

    def test_the_dev_server_recipe(self) -> None:
        for name, role in (("builder", "builder"), ("reviewer-ux", "ux"), ("reviewer-ui", "ui")):
            with self.subTest(agent=name):
                text = self.text(name)
                self.assertIn(f"`(cd <worktree> && exec <dev command>) > /tmp/<job-id>-{role}.log 2>&1 & echo $!`",
                              text)
                self.assertIn("never `pkill` or `killall` by name", text.replace("Never", "never"))
                self.assertIn("`curl -s -m 2 -o /dev/null http://127.0.0.1:<port>/ && echo still running || "
                              "echo stopped`", text)
                self.assertIn("`ss -ltnp 'sport = :<port>'`", text)

    def test_product_assumptions_are_for_people(self) -> None:
        spec = self.text("spec-writer")
        self.assertIn("End an Assumptions item with **(product)** only when a person must decide it", spec)
        self.assertIn("Ordinary engineering choices", spec)
        self.assertIn("are never marked", spec)
        self.assertIn("write it only as a mark, never in passing elsewhere in the spec", spec)
        body = parse_agent_file(PROJECT_DIR / "agents" / "spec-writer.md").body
        template = body.split("```markdown", 1)[1].split("```", 1)[0]
        self.assertNotIn("(product)", template.lower(), "a spec copied from the template carries no mark of its own")
