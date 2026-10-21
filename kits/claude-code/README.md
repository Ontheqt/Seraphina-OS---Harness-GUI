# The factory as a Claude Code skill

The same factory line as the web harness, run from inside Claude Code: one
skill (`/factory`), seven subagents and plain markdown files. The Claude Code
session that runs `/factory` is the orchestrator.

```
feature → spec-writer → builder → [security, ux, ui, code] → WAIT FOR ALL
                           ▲                                      │
                           └──── any CHANGES: resume builder ◄────┤ (max 2)
                                                                  ▼ all PASS
                             merge to base ◄─ gate ◄─ APPROVE ─ approver ─ ESCALATE → needs-human
```

Use this when you live in Claude Code. Use the web harness (`bin/factory serve`)
when you want the GUI, local models through the builtin agent loop, Chat, or
other CLIs (Qwen Code, Codex) as runtimes.

## Install into a repo

```bash
kits/claude-code/install.sh /path/to/your/repo
```

It copies `SKILL.md`, `gate.sh` and `cleanup.sh` to `.claude/skills/factory/`,
the seven agents from `agents/` to `.claude/agents/`, creates
`factory/backlog.md`, gitignores `/factory/jobs/`, and adds the worktree folder
`../<repo>-factory` to `permissions.additionalDirectories` in
`.claude/settings.local.json`.
Claude Code refuses file access there otherwise; in testing on 2.1.89 only an
absolute path in `settings.local.json` (or `--add-dir`) worked.

`gate.sh` is the merge gate the web harness runs in code: after the approver
says APPROVE, a branch that touches CI, agent config, `AGENTS.md`/`CLAUDE.md`,
containers, migrations, dependency manifests or lockfiles, or secrets, changes
more than 1500 lines, or adds a symlink or submodule goes to needs-human
instead of merging. Edit its `PROTECTED` and `MAX_LINES` to change that. It
also stops on every assumption the spec-writer marked **(product)** (a
decision only a person can make) that you have not confirmed: run
`/factory confirm <job-id>`, or write the lines from `spec.md` into the job's
`confirmed.md` yourself, one per line. A line counts as marked when it holds
`(product)`, except the spec's verbatim `> Feature:` line and a mention that
says "not (product)" or "never (product)". Only you write that file; nothing in
Claude Code stops an agent from writing it, so the kit relies on the agents'
instructions here as everywhere else.
`cleanup.sh` runs after every review round and the approver: it stops dev
servers agents left running in the job's worktree (matched by path, not port)
and stashes any edits a reviewer made there (untracked `node_modules/` and
similar folders are reported, not stashed). Re-run `install.sh --force` to
upgrade; it overwrites your edits to the kit's files.

Then make sure the repo's `AGENTS.md` or `CLAUDE.md` says how to set up a fresh
checkout, start the dev server on a given port, and run tests, lint and build.
Commit, and run `/factory <feature>` from the base branch.

## Run it on DeepSeek

```bash
export DEEPSEEK_API_KEY=sk-...
source kits/claude-code/deepseek.env.sh
cd /path/to/your/repo && claude
```

The script points Claude Code at `https://api.deepseek.com/anthropic` and maps
the agents' tiers: `model: opus` → `deepseek-v4-pro[1m]`, `model: sonnet` →
`deepseek-v4-flash[1m]` (override with `FACTORY_OPUS_MODEL` /
`FACTORY_SONNET_MODEL`). Any Anthropic-compatible gateway works the same way,
for example NanoGPT with `FACTORY_BASE_URL=https://nano-gpt.com/api` and
`FACTORY_API_KEY`.

It also sets `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`. On Claude Code 2.1.8x
that flag is what provides `SendMessage`, which the skill uses to resume the
same builder after a review round. The skill sends to the builder's agent id:
with agent teams on, `SendMessage` to a *name* is delivered to a teammate inbox
(`~/.claude/teams/default/inboxes/<name>.json`) that a finished subagent never
reads, and the job stalls. Without `SendMessage` the skill falls back to a
fresh builder that reads `build.md` and the review files, which works but costs
a re-read of the code. (Recent releases such as 2.1.305 offer `SendMessage`
without the flag.)

Live-tested headless (`claude -p "/factory …"` with
`--permission-mode bypassPermissions`) with Claude models standing in for
DeepSeek, on Claude Code 2.1.89 with agent teams and on 2.1.305 with
`SendMessage` disallowed: new jobs merged, CHANGES rounds resumed the builder
(by agent id on 2.1.89, as a fresh builder on 2.1.305), dependency changes went
to needs-human (approver ESCALATE, or the gate when the approver let one
through), and `status`, `rework`, `approve` and `reject` worked. Background
reviewers and a resumed builder report back as notifications; in `-p` mode
Claude Code keeps the session alive until they arrive. A second pass on 2.1.89
re-ran every run that had ended badly: a session without the factory agents
stopped as needs-human and `/factory rework <job-id> retry` then took the job
from its spec to a merge, the stalled and wrongly merged dependency jobs went
to needs-human, and the builder was resumed by agent id for both a dirty tree
and a CHANGES round.

Model tiers: when the orchestrator's model is from the same family as an
agent's alias (`ANTHROPIC_MODEL` is a Sonnet and the agent says
`model: sonnet`), Claude Code gives the agent the orchestrator's model and
ignores `ANTHROPIC_DEFAULT_SONNET_MODEL`. DeepSeek model names don't trigger
this. DeepSeek itself could not be reached from the test sandbox, so its model
IDs come from DeepSeek's published Claude Code guide.

Note: DeepSeek's Anthropic endpoint reportedly drops image blocks, so the UX and
UI reviewers inspect pages as text (accessibility snapshots, computed styles)
rather than screenshots.

## Troubleshooting

- **`Agent type 'spec-writer' not found`** (or `/agents` doesn't list the seven
  factory agents): Claude Code finds `.claude/agents/*.md` with its bundled
  ripgrep, and if that binary can't run (an npm install that dropped its
  execute bit, an unsupported platform) the project agents silently disappear
  while skills still load. Install ripgrep and run with
  `USE_BUILTIN_RIPGREP=0`, or fix the bundled binary's permissions
  (`chmod +x .../@anthropic-ai/claude-code/vendor/ripgrep/*/rg`). The skill
  stops the job as needs-human instead of running the steps with
  `general-purpose` agents. After the fix, start a new Claude Code session and
  run `/factory rework <job-id> retry` to continue the job from its spec, or
  `/factory reject <job-id>` to drop it.
- **`Permission denied` / "outside the allowed working directories" in an
  agent**: `.claude/settings.local.json` must list the absolute worktree folder
  under `permissions.additionalDirectories`; re-run `install.sh` after moving
  the repo, or start Claude Code with `--add-dir ../<repo>-factory`.

## Commands

```
/factory <feature>               run a new job through the loop
/factory next                    run the first unchecked item in factory/backlog.md
/factory status                  one line per job
/factory approve <job-id>        merge a needs-human job
/factory confirm <job-id>        confirm the spec's (product) assumptions (you decide them)
/factory rework <job-id> <note>  send a needs-human job back to the builder
/factory reject <job-id> [why]   drop a needs-human job and its branch
```

`rework` and `reject` also take a job left mid-loop by a session that closed
or crashed (`status` marks it "interrupted?").

Job state lives in `factory/jobs/<job-id>/` (gitignored): `job.json`,
`feature.md`, `spec.md`, `build.md`, `round-N/review-*.md`, `decision.md`. The
web harness's dashboard can't read these (it keeps its own state), but they are
plain markdown you can open directly.
