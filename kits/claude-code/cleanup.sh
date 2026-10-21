#!/usr/bin/env bash
# Tidy a factory job's worktree after agents ran in it. /factory runs it after
# every review round, after the approver, and before removing the worktree.
#   bash .claude/skills/factory/cleanup.sh <worktree>
# 1. Stops dev servers agents left running: every listening process whose
#    working directory or command line is inside the worktree. It matches the
#    path, not the port, because another repo's job can use the same ports.
# 2. Stashes uncommitted changes in the worktree (an agent that should only
#    read edited files) as "factory: uncommitted agent edits".
# Prints one line per thing it did and nothing when there was nothing to do.
set -uo pipefail
[ $# -eq 1 ] || { echo "usage: $0 <worktree>" >&2; exit 2; }
[ -d "$1" ] || { echo "no worktree at $1" >&2; exit 2; }
paths=("${1%/}")
physical="$(cd "$1" && pwd -P)"
[ "$physical" = "${paths[0]}" ] || paths+=("$physical")

inside() {  # inside <text>: does any line of <text> name a path in the worktree?
  local line p
  while IFS= read -r line; do
    for p in "${paths[@]}"; do
      case "$line" in *"$p"|*"$p/"*|*"$p "*) return 0 ;; esac
    done
  done <<< "$1"
  return 1
}

if command -v lsof >/dev/null; then
  stopped=()
  for pid in $(lsof -ti tcp -sTCP:LISTEN 2>/dev/null | sort -u); do
    info="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p'; ps -o args= -p "$pid" 2>/dev/null)"
    inside "$info" || continue
    echo "stopped dev server pid $pid: $(ps -o args= -p "$pid" 2>/dev/null)"
    kill "$pid" 2>/dev/null && stopped+=("$pid")
  done
  if [ ${#stopped[@]} -gt 0 ]; then
    sleep 1
    for pid in "${stopped[@]}"; do kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null; done
  fi
else
  echo "lsof not found: could not check for dev servers left running in $1"
fi

# Untracked dependency and build folders (an install or build whose output is
# not gitignored) are reported but not stashed: they can be very large.
BULKY=' node_modules .venv venv target dist build .next .nuxt .turbo '
excludes=(); rest=()
while IFS= read -r line; do
  [ -n "$line" ] || continue
  path="${line:3}"
  name="$(basename "${path%/}")"
  if [ "${line:0:2}" = "??" ] && [ "${path%/}" != "$path" ] && [[ "$BULKY" == *" $name "* ]]; then
    echo "left untracked $path in the worktree: not gitignored, so it shows as uncommitted; not stashed because it can be large"
    excludes+=(":(exclude)$path")
  else
    rest+=("$path")
  fi
done <<< "$(git -C "$1" status --porcelain 2>/dev/null)"

if [ ${#rest[@]} -gt 0 ]; then
  if git -C "$1" stash push -u -q -m "factory: uncommitted agent edits" -- . ${excludes[@]+"${excludes[@]}"}; then
    echo "stashed uncommitted changes in the worktree (${rest[*]}) as \"factory: uncommitted agent edits\""
  else
    echo "could not stash uncommitted changes in the worktree: ${rest[*]}"
    exit 1
  fi
fi
exit 0
