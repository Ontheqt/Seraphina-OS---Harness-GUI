#!/usr/bin/env bash
# The factory's merge gate, run by /factory after the approver says APPROVE.
#   bash .claude/skills/factory/gate.sh <base> <branch> [<job folder>]   (from the main checkout)
# Prints one line per reason the branch needs a human and exits 1, or prints
# nothing and exits 0 when it may merge on its own. /factory approve skips it.
# Edit PROTECTED and MAX_LINES to change what needs a human.
#
# With the job folder it also stops on every spec.md line marked (product)
# that the job's confirmed.md does not confirm. A line is marked when it holds
# "(product)" in any case, except the verbatim "> Feature:" line and a mention
# that says "not (product)" or "never (product)". confirmed.md holds the lines
# a person has decided, copied from spec.md, one per line (heading marks, list
# markers, a "[x]" box, ** bold and spacing don't matter; blank lines are
# skipped). Only the user writes it, by hand or with /factory confirm.
set -euo pipefail
[ $# -eq 2 ] || [ $# -eq 3 ] || { echo "usage: $0 <base> <branch> [<job folder>]" >&2; exit 2; }
base="$1"; branch="$2"; job_dir="${3:-}"

MAX_LINES=1500
# Changed paths that always need a human: CI, agent config, containers,
# migrations, dependency manifests and lockfiles, secrets.
PROTECTED='(^|/)(\.github|\.circleci|\.claude|\.qwen|\.codex|\.agents|migrations)/'
PROTECTED+='|(^|/)(\.gitlab-ci\.yml|Jenkinsfile|AGENTS\.md|CLAUDE\.md|QWEN\.md'
PROTECTED+='|Dockerfile[^/]*|docker-compose[^/]*\.ya?ml'
PROTECTED+='|package\.json|package-lock\.json|pnpm-lock\.yaml|yarn\.lock'
PROTECTED+='|requirements[^/]*\.txt|pyproject\.toml|poetry\.lock|uv\.lock'
PROTECTED+='|go\.mod|go\.sum|Cargo\.toml|Cargo\.lock|Gemfile|Gemfile\.lock'
PROTECTED+='|\.env[^/]*|[^/]*\.pem|[^/]*\.key)$'

range="$base...$branch"
git rev-parse --verify --quiet "$branch^{commit}" >/dev/null || { echo "no branch $branch"; exit 1; }

problems=()

# One spelling per line: no heading mark, list marker, checkbox or bold, single spaces.
normalize() {
  sed -E -e 's/\r$//' -e 's/^[[:space:]]*#+[[:space:]]+//' -e 's/^[[:space:]]*([-*+]|[0-9]+[.)])[[:space:]]+//' \
    -e 's/^\[[xX ]\][[:space:]]+//' -e 's/\*\*//g' -e 's/[[:space:]]+/ /g' -e 's/^ //' -e 's/ $//'
}
# The lines of spec.md that carry the mark (see the top of this file).
marked() {
  local line plain
  while IFS= read -r line; do
    plain="$(printf '%s\n' "$line" | tr '[:upper:]' '[:lower:]' | sed -E -e 's/\*\*//g' \
      -e 's/(^|[^[:alnum:]_])(not|never)[[:space:]]+\(product\)//g')"
    case "$plain" in *'(product)'*) printf '%s\n' "$line" ;; esac
  done < <(grep -v -E '^[[:space:]]*>[[:space:]]*Feature:' "$1" | grep -i -F '(product)' || true)
}
if [ -n "$job_dir" ]; then
  spec="${job_dir%/}/spec.md"; confirmed="${job_dir%/}/confirmed.md"
  if [ ! -f "$spec" ]; then
    problems+=("cannot check product assumptions: no $spec")
  else
    decided=""
    [ -f "$confirmed" ] && decided="$(normalize <"$confirmed" | grep -v -E '^$' || true)"
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      grep -qxF -- "$line" <<<"$decided" || problems+=("product assumption not confirmed by a human: $line")
    done < <(marked "$spec" | normalize | awk '!seen[$0]++' || true)
  fi
fi

while IFS= read -r path; do
  problems+=("touches protected path $path")
done < <(git diff --no-renames --name-only "$range" | grep -E "$PROTECTED" || true)

lines=$(git diff --no-renames --numstat "$range" | awk '$1 != "-" { s += $1 + $2 } END { print s + 0 }')
[ "$lines" -le "$MAX_LINES" ] || problems+=("changes $lines lines (more than $MAX_LINES)")

# Old or new mode 120000 (symlink) or 160000 (submodule): added, removed,
# retargeted or turned into one.
while IFS=$'\t' read -r meta path; do
  set -- $meta
  case "${1#:} $2" in
    *160000*) problems+=("touches submodule $path") ;;
    *120000*) problems+=("touches symlink $path") ;;
  esac
done < <(git diff --raw --no-renames "$range")

[ ${#problems[@]} -eq 0 ] && exit 0
printf '%s\n' "${problems[@]}"
exit 1
