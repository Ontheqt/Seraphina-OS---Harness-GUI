---
name: factory
description: Run the software factory inside Claude Code. The session that runs /factory becomes the orchestrator of the spec → build → review → approve loop.
argument-hint: "<feature> | next | status | approve <job-id> | confirm <job-id> | rework <job-id> <note> | reject <job-id> [reason]"
disable-model-invocation: true
---

# Factory

You are the orchestrator. You run the loop below, spawn every agent, and are
the only writer of each job's `job.json`. You never write code, specs, reviews
or decisions yourself (one exception: the "rounds exhausted" decision below).
Agents do the work and each writes one file; you move the job along.

Arguments: `$ARGUMENTS`

Dispatch on the arguments:
- empty or `status` → **Status**
- `next` → **Next**
- `approve <job-id>`, `confirm <job-id>`, `rework <job-id> <note>`,
  `reject <job-id> [reason]` (job-id matches `NNN-slug`) → that command
- anything else → a new job with the whole text as the feature

## The loop

```
feature → spec-writer → builder → [security, ux, ui, code] → WAIT FOR ALL
                           ▲                                      │
                           └──── any CHANGES: resume builder ◄────┤ (max 2 resumes)
                                                                  ▼ all PASS
                             merge to base ◄─ gate ◄─ APPROVE ─ approver ─ ESCALATE → needs-human
```

| Step    | Agent (`subagent_type`) | Writes                                     |
| ------- | ----------------------- | ------------------------------------------ |
| spec    | `spec-writer`           | `spec.md`                                  |
| build   | `builder`               | `build.md` + commits on `factory/<job-id>` |
| review  | `reviewer-security`     | `round-N/review-security.md`               |
|         | `reviewer-ux`           | `round-N/review-ux.md`                     |
|         | `reviewer-ui`           | `round-N/review-ui.md`                     |
|         | `reviewer-code`         | `round-N/review-code.md`                   |
| approve | `approver`              | `decision.md`                              |

`MAX_RESUMES = 2`. Edit this file and `.claude/agents/` to change the factory.

Always spawn the exact `subagent_type` from the table. If the Agent tool says
an agent type is not found, stop: set the job to **Needs human** with reason
`factory agents not loaded (.claude/agents/)` and tell the user to check
`/agents` and the kit README's Troubleshooting, then restart Claude Code and
run `/factory rework <job-id> retry` (it picks up from the step that failed)
or `/factory reject <job-id>`. Never substitute `general-purpose` or another
agent, and never do an agent's step yourself.

## Files

`<repo>` is the main checkout (`git rev-parse --show-toplevel`), `<name>` its
folder name, `<base>` the base branch (the branch `origin/HEAD` points to if it
exists locally, else `main`, else `master`). `factory/jobs/` is gitignored.

```
<repo>/factory/backlog.md                           queue for /factory next
<repo>/factory/jobs/<job-id>/job.json               job state (only you write it)
<repo>/factory/jobs/<job-id>/feature.md             the request, verbatim (you write it)
<repo>/factory/jobs/<job-id>/spec.md  build.md  decision.md  rework-<k>.md
<repo>/factory/jobs/<job-id>/round-<N>/review-{security,ux,ui,code}.md
<repo>/factory/jobs/<job-id>/confirmed.md           product assumptions a person decided (see Confirm)
<parent of repo>/<name>-factory/<job-id>/           the job's worktree, on factory/<job-id>
```

`job.json` (rewrite the whole file with Write on every change):

```json
{ "id": "003-rate-limit-signup", "feature": "Rate limit the signup form",
  "base": "main", "branch": "factory/003-rate-limit-signup",
  "worktree": "/abs/path/my-app-factory/003-rate-limit-signup",
  "port": 5130, "stage": "review", "round": 2, "resumes": 1,
  "builder": "a84e408d60df9dbf9",
  "reviews": { "security": "CHANGES", "ux": "PASS", "ui": "pending", "code": "PASS" },
  "reason": null }
```

`stage` is one of `spec build review approve merged needs-human rejected`.
`reviews` holds the current round; earlier rounds live in their folders.

Every agent prompt starts with the *paths line*:
`Job: <job-id>. Job folder: <repo>/factory/jobs/<job-id>/. Worktree: <worktree>. Branch: factory/<job-id>. Base: <base>.`
With `n` the job number (`003` → 3) the port is `5100 + 10n` for the builder,
`+1` for reviewer-ux and `+2` for reviewer-ui, appended as ` Port: <port>.`

Verdicts come only from files: read the first line of `build.md`,
`review-*.md` or `decision.md` with a command. Never take a status or verdict
from an agent's reply text.

## New job: /factory <feature>

1. **Claim an id.** Take the next 3-digit number after the highest folder in
   `factory/jobs/` (`001` if there is none or no `factory/jobs/` yet) and a 2–4
   word kebab-case slug: `004-blog-reading-time`. Claim it with
   `mkdir -p factory/jobs && mkdir factory/jobs/<job-id>` (no `-p` on the
   second); if that fails, another session took it, so try the next number.
   Write `feature.md` (the request, verbatim) and `job.json` with
   `stage: "spec"`, `round: 0`, `resumes: 0`, reviews `pending`.
2. **Worktree.** From `<repo>`:
   `git worktree add -b factory/<job-id> "$(cd .. && pwd)/<name>-factory/<job-id>" <base>`.
   Run the fresh-checkout setup from the repo's `AGENTS.md`/`CLAUDE.md` inside
   the worktree. If it fails, go to **Needs human** with the error.
3. **Spec.** Spawn `spec-writer` with `<paths> Feature: <feature, verbatim>`.
   When it finishes, `spec.md` must exist and be non-empty; if not, spawn it once
   more adding "You did not write spec.md."; still missing → **Needs human**.
   Then, from `<repo>`, run
   `bash .claude/skills/factory/gate.sh <base> factory/<job-id> <job folder>`
   and quote to the user each line it prints that starts with
   `product assumption not confirmed by a human: `: each is a decision only a
   person can make, and the job stops for a human at the gate unless they run
   `/factory confirm <job-id>`. Ignore its other lines here. Carry on with the
   build meanwhile.
4. **Build.** Set `stage: "build"`. Spawn `builder` with
   `<paths> Build round 1. Port: <port>.` Record the agent id from the
   result (its `agentId: …` line) as `builder` in job.json. Then
   **Build check**.

### Build check

Read the first line of `build.md`:
- `STATUS: BLOCKED` → **Needs human**, quoting the `## Blocked` section.
- `STATUS: READY` → `git -C <worktree> rev-list --count <base>..HEAD` must be
  above 0 and `git -C <worktree> status --porcelain` must print nothing at
  all. An untracked line counts: `?? node_modules/` or build output means the
  tree is not clean, however expected it looks. Otherwise **resume the
  builder** once with "Commit your work and leave the tree clean (gitignore
  generated files such as node_modules/; never commit them), then update
  build.md." Still failing → **Needs human**.
- anything else → resume the builder once with "build.md must start with
  STATUS: READY or STATUS: BLOCKED."; still wrong → **Needs human**.

Then start review round `round + 1`.

### Review round N

1. Set `stage: "review"`, `round: N`, all reviews `pending`. `mkdir` the round
   folder `factory/jobs/<job-id>/round-<N>/`.
2. Spawn all four reviewers **in one message** so they run in parallel, each with
   `<paths> Review round N.` (plus its port for reviewer-ux and reviewer-ui).
3. **WAIT FOR ALL.** The round is done only when all four reviewers have
   returned (if they run in the background, when all four completion
   notifications are in). Never resume the builder mid-round.
4. **Read verdicts and clean up**, every round, whatever the verdicts, in one
   command from `<repo>`:
   `head -n 1 <job folder>round-<N>/review-*.md; bash .claude/skills/factory/cleanup.sh <worktree>`
   (see **Clean up**). Write each verdict into job.json, and tell the user
   any line `cleanup.sh` printed. A reviewer whose file is missing or whose
   first line is not exactly `VERDICT: PASS` or `VERDICT: CHANGES` is spawned
   once more; still invalid → **Needs human**.
5. Decide:
   - **All PASS** → **Approve**.
   - **Any CHANGES and resumes < 2** → `resumes += 1`, `stage: "build"`, then
     **resume the builder** with: `Round N reviews are in <job folder>round-<N>/.
     Address every review whose first line is VERDICT: CHANGES, commit, and
     update build.md for round N+1.` → **Build check**.
   - **Any CHANGES and no resumes left** → write `decision.md` yourself:
     `ESCALATE`, a blank line, then `Review rounds exhausted. Outstanding CHANGES
     in round-<N>/ from: <reviewers>.` → **Needs human**.

### Resuming the builder

- If you have the **SendMessage** tool (it may be deferred: load it with
  ToolSearch), send the message with `to` set to the builder's recorded agent
  id, exactly as the Agent result said (`use SendMessage with to: '<agentId>'
  to continue this agent`). Do this even though the tool's schema calls `to` a
  teammate name: an agent id resumes a finished subagent, while on Claude Code
  2.1.8x with agent teams a name is a teammate inbox that a finished subagent
  never reads. Always try it before the fallback. The result must say the
  agent was resumed; anything else (an inbox, an error) is a failure, so use
  the fallback below. The resumed builder runs in the background: wait for its
  completion notification before you read `build.md`. Do not start reviews
  before it has finished and `build.md` has a `### Round <N+1>` log entry.
- If you don't have SendMessage, or it fails, spawn a **fresh** `builder` with
  `<paths> Build round <N+1>. Port: <port>.` plus the same instruction, and
  record its agent id. The builder gets its context from build.md and the round
  folders, so nothing is lost but time.

### Approve

Set `stage: "approve"`. Delete any old `decision.md` (a rework can leave one)
and note `git rev-parse factory/<job-id>`. Spawn `approver` with
`<paths> Final round: N.` When it returns, run one command from `<repo>`:
`head -n 1 <job folder>decision.md; git rev-parse factory/<job-id>; bash .claude/skills/factory/cleanup.sh <worktree>`
and tell the user any line `cleanup.sh` printed. Then:
- If the branch tip changed → **Needs human**, reason
  `the branch moved while the approver ran`.
- First line of `decision.md` `APPROVE` → **Gate**; `ESCALATE`, anything else
  or no file → **Needs human**.

### Gate

The approver is told the factory checks the mechanics after it, so always run
the gate before an automatic merge. From `<repo>`:
`bash .claude/skills/factory/gate.sh <base> factory/<job-id> <job folder>`.
Exit 0 → **Merge**. Anything else → **Needs human** with
`reason: "approved, but <first line it printed>"`, and quote every line it
printed. It flags every line of `spec.md` marked `(product)` that
`confirmed.md` does not confirm (see **Confirm**), changes to CI, agent
config, `AGENTS.md`/`CLAUDE.md`, containers, migrations, dependency manifests
and lockfiles, and secrets, more than 1500 changed lines, and symlinks or
submodules; edit the script to change that. `/factory approve` skips the gate.

### Merge

The merge happens in the main checkout. It must be on `<base>` and have no
modified tracked files: `git branch --show-current` prints `<base>` and
`git status --porcelain --untracked-files=no` prints nothing. If not, set
`stage: "needs-human"`, `reason: "approved; main checkout busy"`, and tell the
user to run `/factory approve <job-id>` once it is clean. Otherwise:

```bash
git merge --no-ff factory/<job-id> -m "Merge factory/<job-id>"
git worktree remove <worktree>
git branch -d factory/<job-id>
```

Run them one at a time, and run `bash .claude/skills/factory/cleanup.sh
<worktree>` before removing the worktree. If `git worktree remove` fails only
because of a folder cleanup.sh reported leaving in place, repeat it with
`--force`. If the merge fails, run `git merge --abort`, set
`stage: "needs-human"` with the reason, and tell the user. Once the merge has
succeeded the job is `merged` even if removing the worktree or branch fails
(say what was left behind). If the branch changed dependencies or the setup
steps, tell the user to rerun the repo's setup in the main checkout. Never
push.

### Clean up

`bash .claude/skills/factory/cleanup.sh <worktree>` stops every dev server
agents left running in the worktree (any listening process whose working
directory or command line is inside it, whatever its port, so another repo's
job on the same port numbers is left alone) and stashes uncommitted changes
there as `factory: uncommitted agent edits` (a reviewer or the approver
edited files). Untracked dependency or build folders such as `node_modules/`
are left in place and reported instead, because they can be huge; the next
**Build check** then sends the builder to gitignore them. It prints one line
per thing it did and nothing otherwise. Never skip it: a leftover server
holds the port the next builder or reviewer needs.

### Needs human

Set `stage: "needs-human"` and `reason`. The branch and worktree stay. Tell the
user which job is waiting, the worktree path, and quote `decision.md` or the
BLOCKED reason.

## Next: /factory next

Take the first `- [ ]` line of `factory/backlog.md`. Claim it with one Edit
that replaces its exact line with `- [x] <item> → claimed`; if the Edit fails,
another session took it, so re-read and try the next. None left → say so.
Run a new job with the item, then replace `claimed` with the job id.

## Status: /factory status

Read every `factory/jobs/*/job.json` and print one line per job:
`<id>  <stage>  r<round>  resumes <n>/2  S:<security> U:<ux> D:<ui> C:<code>  <reason or feature>`,
with the reason or feature verbatim, cut at 60 characters. Mark a job left in
`spec`, `build`, `review` or `approve` "(interrupted?)" and mention
`/factory rework` and `/factory reject` for it.

## /factory approve <job-id>

The job must be in `needs-human`. Human approval overrides the approver: run
**Merge**.

## /factory confirm <job-id>

**Confirm** is how a person decides the spec's product assumptions. The user
typing this command is the confirmation; never run it on your own, and no
agent ever writes `confirmed.md`. `spec.md` must exist. From `<repo>`, run
`bash .claude/skills/factory/gate.sh <base> factory/<job-id> <job folder>`
and take the lines it prints that start with
`product assumption not confirmed by a human: `; none → say there is nothing
to confirm. Otherwise quote each one to the user and append the text after
that prefix to `<job folder>confirmed.md`, one per line, under a first line
`# Confirmed by a human` (write that line only if the file is new). The gate
compares the lines with heading marks, list markers, `[x]` boxes, `**` and
spacing ignored, so a user may also write the file by hand. A spec rewritten later (a rework from the spec) needs confirming
again for any line that changed. Then, if the job is in `needs-human` because
the approver escalated or the gate stopped over product assumptions, run
**Approve** again; otherwise the job carries on where it is.

## /factory rework <job-id> <note>

The job must be in `needs-human`, or *interrupted*: left in `spec`, `build`,
`review` or `approve` by a session that closed or crashed (the user asking
for this on such a job is telling you nothing else is running it). Write the
note verbatim to `factory/jobs/<job-id>/rework-<k>.md` (next unused k) and set
`resumes: 0`. Then:
- If `spec.md` is missing or empty (the job stopped at Spec), set
  `stage: "spec"` and run **New job** from step 3 (Spec), adding
  ` Also read <job folder>rework-<k>.md.` to the spec-writer prompt, then go on
  to step 4 (Build round 1).
- Otherwise set `stage: "build"` and resume the builder (or spawn a fresh one
  if this session never ran it) with
  `<paths> Rework: read <job folder>rework-<k>.md and address it, then update build.md for round <round+1>. Port: <port>.`
  Continue from **Build check**.

## /factory reject <job-id> [reason]

The job must be in `needs-human` or interrupted (see **rework**). Run
`bash .claude/skills/factory/cleanup.sh <worktree>`, then
`git worktree remove --force <worktree>`,
`git branch -D factory/<job-id>`, and set `stage: "rejected"` and `reason`.

## Reporting

One short line per transition, e.g.
`003-rate-limit-signup: round 2, security CHANGES, ux/ui/code PASS → resuming builder`.
When the job ends, say whether it merged or needs a human, and why.
