#!/usr/bin/env bash
# Install the factory as a Claude Code skill + subagents into a git repo.
#   kits/claude-code/install.sh /path/to/repo [--force]
set -euo pipefail

usage() { echo "usage: $0 <repo> [--force]" >&2; exit 2; }
[ $# -ge 1 ] || usage
target=""; force=0
for arg in "$@"; do
  case "$arg" in
    --force) force=1 ;;
    -h|--help) usage ;;
    *) [ -z "$target" ] && target="$arg" || usage ;;
  esac
done

kit_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
agents_src="$(cd "$kit_dir/../../agents" && pwd)"
command -v git >/dev/null || { echo "git is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
command -v lsof >/dev/null || echo "warning: lsof not found; /factory uses it to stop dev servers agents leave running" >&2

repo="$(cd "$target" && pwd -P)"
top="$(git -C "$repo" rev-parse --show-toplevel 2>/dev/null || true)"
[ "$top" = "$repo" ] || { echo "$repo is not the top level of a git repo" >&2; exit 1; }

agents=(spec-writer builder reviewer-security reviewer-ux reviewer-ui reviewer-code approver)

# Refuse to clobber files the user has edited, unless --force.
conflicts=()
check() { [ -e "$2" ] && ! cmp -s "$1" "$2" && conflicts+=("$2") || true; }
check "$kit_dir/SKILL.md" "$repo/.claude/skills/factory/SKILL.md"
check "$kit_dir/gate.sh" "$repo/.claude/skills/factory/gate.sh"
check "$kit_dir/cleanup.sh" "$repo/.claude/skills/factory/cleanup.sh"
for a in "${agents[@]}"; do check "$agents_src/$a.md" "$repo/.claude/agents/$a.md"; done
if [ ${#conflicts[@]} -gt 0 ] && [ $force -eq 0 ]; then
  echo "These files exist and differ (re-run with --force to overwrite):" >&2
  printf '  %s\n' "${conflicts[@]}" >&2
  exit 1
fi

mkdir -p "$repo/.claude/skills/factory" "$repo/.claude/agents" "$repo/factory"
cp "$kit_dir/SKILL.md" "$repo/.claude/skills/factory/SKILL.md"
cp "$kit_dir/gate.sh" "$repo/.claude/skills/factory/gate.sh"
cp "$kit_dir/cleanup.sh" "$repo/.claude/skills/factory/cleanup.sh"
for a in "${agents[@]}"; do cp "$agents_src/$a.md" "$repo/.claude/agents/$a.md"; done
if [ ! -e "$repo/factory/backlog.md" ]; then
  printf '# Backlog\n\n`/factory next` builds the first unchecked item and checks it off with its job id.\n\n' > "$repo/factory/backlog.md"
fi

# Job folders are runtime state; settings.local.json holds a machine-specific path.
gitignore="$repo/.gitignore"
touch "$gitignore"
grep -qxF '/factory/jobs/' "$gitignore" || printf '/factory/jobs/\n' >> "$gitignore"
if ! git -C "$repo" check-ignore -q .claude/settings.local.json; then
  printf '.claude/settings.local.json\n' >> "$gitignore"
fi

# Worktrees live next to the repo. Claude Code only lets agents touch files there
# if the absolute path is in permissions.additionalDirectories (relative paths
# and the project settings.json did not work in testing on 2.1.89).
worktrees="$(dirname "$repo")/$(basename "$repo")-factory"
mkdir -p "$worktrees"
python3 - "$repo/.claude/settings.local.json" "$worktrees" <<'PY'
import json, os, sys
path, worktrees = sys.argv[1], sys.argv[2]
data = {}
if os.path.exists(path):
    with open(path) as f:
        data = json.load(f)
dirs = data.setdefault("permissions", {}).setdefault("additionalDirectories", [])
if worktrees not in dirs:
    dirs.append(worktrees)
with open(path + ".tmp", "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
os.replace(path + ".tmp", path)
PY

cat <<EOF
Installed the factory into $repo
  .claude/skills/factory/SKILL.md   the orchestrator (/factory)
  .claude/skills/factory/gate.sh    the merge gate (paths that need a human)
  .claude/skills/factory/cleanup.sh stops dev servers agents leave running
  .claude/agents/*.md               the seven agents
  factory/backlog.md                queue for /factory next
  .claude/settings.local.json       lets agents work in $worktrees

Next:
  1. Make sure AGENTS.md or CLAUDE.md says how to set up a fresh checkout,
     start the dev server on a port, and run tests/lint/build.
  2. Commit .claude/ factory/backlog.md .gitignore on the base branch.
  3. For DeepSeek:  export DEEPSEEK_API_KEY=sk-...  &&  source $kit_dir/deepseek.env.sh
  4. cd $repo && claude      then:  /factory <feature>
EOF
