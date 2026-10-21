# Skills: Matt Pocock's seven, in the factory

The owner's plan (phase I) installs seven skills from
[mattpocock/skills](https://github.com/mattpocock/skills) (MIT, copyright 2026
Matt Pocock) per project in `<repo>/.agents/skills/<name>/`. This page says
which factory agent each one suits, how to switch it on, and what to watch.

**Verified** on 2026-10-21 against a clone at `5beb35b` (one commit after the
plan's pin `49dd158`; the seven folders are identical at both): all seven load
in `SkillIndex` with no errors, both from the clone and from the plan's
`.agents/skills/<name>` layout (25 files, as the plan counts), and preloading
`tdd` into the builder and `diagnosing-bugs` into the coder gives sane system
prompts (harness rules, the agent's own body, then `# Skill: <name>` with the
skill's folder path and body; the coder also gets the catalog of the other
model-invocable skills). `tests/test_tools_integration.py` repeats this with
`FACTORY_E2E_TOOLS=1 FACTORY_SKILLS_CLONE=<clone>`.

## The seven

| skill | Skill tool | what it does | recommended for | caveats in the factory |
| ----- | ---------- | ------------ | --------------- | ---------------------- |
| `setup-matt-pocock-skills` | no (`disable-model-invocation`) | Sets up a repo for the others: issue tracker, triage labels, domain-doc layout. Explores, then asks the user one section at a time, then writes `docs/agents/*.md` and an `## Agent skills` block in `CLAUDE.md` or `AGENTS.md`. | **Chat**, once per repo: a coder copy that preloads it, or ask the coder to read its `SKILL.md`. | Interactive by design, so never a factory-line agent. It edits `AGENTS.md`/`CLAUDE.md`, which are protected paths: handing that branch to the factory ends at needs-human, as it should. It may run `gh label create`; the plan wants external actions only with explicit authority. |
| `diagnosing-bugs` | yes | Build a tight red-capable feedback loop, reproduce and minimise, 3 to 5 falsifiable hypotheses, tagged instrumentation, regression test at a real seam, cleanup. | **Chat** coder (it loads it from the catalog when a bug is reported). **builder** by preload in repos where most jobs are bug fixes. | It shows its hypotheses to the user but proceeds when nobody answers, which fits the builder. Its fallback when no loop can be built is to ask the user: in the builder that is `STATUS: BLOCKED` with what was tried. The human-in-the-loop script needs a person and does not apply to factory runs. |
| `tdd` | yes | Red, then green, one vertical slice at a time, tests through public interfaces only. | **builder** by preload in repos with a real test runner; **Chat** coder from the catalog. | It says to confirm the test seams with the user before writing any test: a builder override must add "the spec's acceptance criteria are the agreed seams; do not wait for confirmation". It tells the agent to load `codebase-design` with the Skill tool, which the builder lacks: preload both, or add `Skill` to the builder's tools. Refactoring is left to review, which matches the line. |
| `codebase-design` | yes | Shared vocabulary for deep modules: module, interface, depth, seam, adapter, leverage, locality; the deletion test. | **Chat** coder; optional for **lead** (planning), **spec-writer** or **reviewer-code** as vocabulary. | Pure reference, safe anywhere. `DESIGN-IT-TWICE.md` spins up parallel sub-agents, which the builtin harness does not have: do the designs one after another. |
| `code-review` | yes | Reviews a diff against a fixed point on two separate axes, Standards (repo docs plus a Fowler smell baseline) and Spec, in two parallel sub-agents. | **Chat** coder, before handing a chat branch to the factory. Not the factory's **reviewer-code** by default. | No sub-agents in the builtin harness: run the two axes in turn. Its report (`## Standards`, `## Spec`) is not the factory's review file, whose first line must be `VERDICT: PASS` or `VERDICT: CHANGES`; a reviewer-code override must say the factory's format wins, the spec is `<job folder>/spec.md` and the fixed point is the job's base. It expects an issue tracker from setup; with none, point it at the spec file. The smell baseline is a good addition to reviewer-code. |
| `handoff` | no | Writes a compact handoff document for a fresh session. | **Chat** only, explicitly (a coder copy that preloads it, or ask the coder to read it). | It saves to `$TMPDIR`, outside the chat's write paths, so the Write tool refuses (and under bwrap `/tmp` is gone after each call). Tell it to write into the checkout instead, as the plan's adaptation does (`docs/ai/handoffs/<task>.md`). Factory agents do not need it: the job folder is the handoff. |
| `writing-for-agents` | yes | Reference for writing skills, `AGENTS.md` and `CLAUDE.md`: context pointers, information hierarchy, completion criteria, pruning. | **repo-scout** by preload (it drafts `AGENTS.md`); **Chat** coder from the catalog when editing agent docs. | Pure reference (11 KB). The files it helps write are protected paths, so changes to them still go to a human. |

## How to enable

1. **Install the folders** in one place the factory searches (the first skill
   of a name wins, so keep one copy):
   - per project, as the owner's plan does: `<repo>/.agents/skills/<name>/`,
     ideally committed. The factory looks in the job's or chat's worktree
     first, then the repo's main checkout, so an uncommitted copy in the
     checkout is found too;
   - per user: `~/.agents/skills/`, `~/.claude/skills/` or
     `~/.config/software-factory/skills/`;
   - anywhere else: `"harness": {"skills_dirs": ["/srv/ai-work/skills"]}`.

   Copy only the seven folders (and the upstream `LICENSE`). A whole clone
   works too (collections nest up to four levels) but puts all 38 of its skills
   in the catalog.
2. **Let an agent pick them up.** Agents whose `tools:` include `Skill` (the
   Chat `coder` and the `analyst`) get a catalog of every model-invocable skill
   and load one when it fits. Nothing else to configure.
3. **Preload one into an agent** with a `skills:` line in an override of that
   agent: GUI → Agents → builder → Edit (saved as
   `~/.config/software-factory/agents/builder.md`), or a repo's
   `.factory/agents/builder.md` (a protected path: committing it goes to a
   human). For example:

   ```markdown
   ---
   name: builder
   description: Factory step 2. ...
   tools: Read, Grep, Glob, Bash, Edit, Write
   model: opus
   skills: tdd, codebase-design
   color: green
   ---
   <the shipped body, unchanged>

   The spec's acceptance criteria are the agreed test seams: write tests at
   them without waiting for anyone to confirm.
   ```

   `disable-model-invocation: true` (setup and handoff) keeps a skill out of
   the catalog and the Skill tool, but a `skills:` line still preloads it. A
   preloaded skill that is not installed is skipped with a warning on every
   run.

## Why the shipped agents stay unchanged

- The skills are not part of the factory: they are installed per project or
  per user. A default `skills:` line would warn on every run for everyone who
  has not installed them, and the agents that can use the catalog (coder,
  analyst) already find the model-invocable ones once installed.
- None of the seven is safe and beneficial unchanged for a non-interactive
  factory agent: `tdd` and `setup-matt-pocock-skills` wait for the user,
  `code-review` wants sub-agents and its own report format, `handoff` writes
  outside the write paths, and `diagnosing-bugs` falls back to asking the user.
  `writing-for-agents` (for repo-scout) and `codebase-design` are harmless
  references, but they help only where they are installed, so they belong in
  an override too.
