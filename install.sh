#!/usr/bin/env bash
# Software Factory installer for Ubuntu 22.04 / 24.04 (./install.sh --help).
# Run it as the user who will run the factory (e.g. ai-dev). It never uses sudo.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

Installs the `factory` command for the current user (no sudo needed):
  - checks python3 >= 3.10 and git (warns if git is older than 2.38)
  - links bin/factory into ~/.local/bin (and bin/sf-items, the Housekeeping
    Tracker command for agents you run by hand)
  - runs `factory init --preset NAME` when there is no config yet (a config
    with no server token, e.g. one `factory repo add` wrote, gets the token
    and keeps the rest), and creates the API key file
    ~/.config/software-factory/env (mode 600)
Running it again is safe: your config, keys and data are kept.

Options:
  --preset NAME    starting config for a new install: deepseek (default),
                   anthropic, nanogpt, nanogpt-value, ollama, strata, hybrid,
                   local-first, mixed, gpt-oss
  --data-dir DIR   where jobs, worktrees and logs go (only when this run writes
                   the token; default ~/.local/share/software-factory)
  --systemd-user   install the systemd user unit ~/.config/systemd/user/factory.service
  --start          with --systemd-user: enable and start it now
  --uninstall      remove the link and the user unit (never the config or data)
  -h, --help       show this help

Environment: FACTORY_PYTHON (interpreter, default python3), FACTORY_CONFIG (config path).
EOF
}

say() { printf '%s\n' "$*"; }
warn() { printf 'install.sh: warning: %s\n' "$*" >&2; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
launcher="$repo_dir/bin/factory"
python=${FACTORY_PYTHON:-python3}
bin_dir="$HOME/.local/bin"
link="$bin_dir/factory"
items_launcher="$repo_dir/bin/sf-items"
items_link="$bin_dir/sf-items"
config=${FACTORY_CONFIG:-$HOME/.config/software-factory/config.json}
env_file="$(dirname "$config")/env"
unit_dir="$HOME/.config/systemd/user"
unit="$unit_dir/factory.service"

preset=deepseek
preset_given=0
data_dir=
systemd_user=0
start=0
uninstall=0
while [ $# -gt 0 ]; do
  case $1 in
    --preset) [ $# -ge 2 ] || die "--preset needs a name"; preset=$2; preset_given=1; shift 2 ;;
    --preset=*) preset=${1#*=}; preset_given=1; shift ;;
    --data-dir) [ $# -ge 2 ] || die "--data-dir needs a directory"; data_dir=$2; shift 2 ;;
    --data-dir=*) data_dir=${1#*=}; shift ;;
    --systemd-user) systemd_user=1; shift ;;
    --start) start=1; shift ;;
    --uninstall) uninstall=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) printf 'install.sh: unknown option %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done
if [ "$start" -eq 1 ] && [ "$systemd_user" -eq 0 ]; then
  die "--start needs --systemd-user"
fi

check_requirements() {
  have "$python" || die "$python not found: sudo apt install python3 (or set FACTORY_PYTHON)"
  "$python" -c 'import sys; sys.exit(sys.version_info < (3, 10))' ||
    die "Python 3.10 or newer is required; $python is $("$python" -V 2>&1). Ubuntu 22.04+ ships it, or set FACTORY_PYTHON."
  have git || die "git not found: sudo apt install git"
  local version major minor
  version=$(git --version | sed -n 's/^git version \([0-9][0-9]*\)\.\([0-9][0-9]*\).*/\1.\2/p')
  major=${version%%.*}
  minor=${version#*.}
  if [ -n "$version" ] && { [ "$major" -lt 2 ] || { [ "$major" -eq 2 ] && [ "$minor" -lt 38 ]; }; }; then
    warn "git $version is older than 2.38: merge conflict checks use a slower fallback (everything still works)"
  fi
}

install_link() {
  [ -x "$launcher" ] || chmod +x "$launcher"
  mkdir -p "$bin_dir"
  if [ -e "$link" ] && [ ! -L "$link" ]; then
    die "$link exists and is not a symlink; move it away and run again"
  fi
  ln -sfn "$launcher" "$link"
  say "linked $link -> $launcher"
  if [ -f "$items_launcher" ]; then
    [ -x "$items_launcher" ] || chmod +x "$items_launcher"
    if [ -e "$items_link" ] && [ ! -L "$items_link" ]; then
      warn "$items_link exists and is not a symlink; sf-items not linked"
    else
      ln -sfn "$items_launcher" "$items_link"
      say "linked $items_link -> $items_launcher"
    fi
  fi
  case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) warn "$bin_dir is not on your PATH: log in again (Ubuntu's ~/.profile adds it once it exists) or add it" ;;
  esac
}

config_state() {  # prints new, token, no-token or invalid
  if [ ! -e "$config" ]; then
    say new
    return 0
  fi
  "$python" -I -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        server = json.load(handle).get("server")
except Exception:
    print("invalid")
else:
    print("token" if isinstance(server, dict) and server.get("token") else "no-token")' "$config"
}

init_config() {
  local args=(init)
  case $(config_state) in
    token)
      say "config: $config (kept)"
      if [ -n "$data_dir" ]; then
        warn "--data-dir ignored because $config exists; edit data_dir there"
      fi
      if [ "$preset_given" -eq 1 ]; then
        warn "--preset ignored because $config exists; apply a preset in the GUI (Settings -> Routing -> Preset)"
      fi
      return 0 ;;
    new)
      args+=(--preset "$preset") ;;
    no-token)
      # e.g. written by `factory repo add` before installing: init adds the token, keeps the rest,
      # and applies a preset only when asked for one.
      say "config: $config (kept; adding the server token)"
      if [ "$preset_given" -eq 1 ]; then
        args+=(--preset "$preset")
      fi ;;
    *)
      warn "$config is not a valid config (a JSON object): fix it, or replace it with: factory init --force"
      return 0 ;;
  esac
  if [ -n "$data_dir" ]; then
    args+=(--data-dir "$data_dir")
  fi
  "$launcher" "${args[@]}"
}

create_env_file() {
  if [ -e "$env_file" ]; then
    return 0
  fi
  mkdir -p "$(dirname "$env_file")"
  (
    umask 077
    cat >"$env_file" <<'EOF'
# API keys for Software Factory: one NAME=value per line. `factory` reads this
# file at startup (variables already set win), and so does the systemd unit.
# Keep it mode 600. Uncomment what your presets use:
# DEEPSEEK_API_KEY=
# ANTHROPIC_API_KEY=
# OPENROUTER_API_KEY=
# NANOGPT_API_KEY=
# TABBY_API_KEY=
EOF
  )
  say "created $env_file (mode 600): put your API keys there"
}

unit_escape() {  # a value for a systemd unit file: \ and " escaped, % doubled
  local value=${1//\\/\\\\}
  value=${value//\"/\\\"}
  printf '%s' "${value//\%/%%}"
}

substitute() {  # substitute LINE PLACEHOLDER VALUE: every PLACEHOLDER replaced by VALUE, verbatim
  # Not ${line//placeholder/$value}: bash 5.2 (Ubuntu 24.04) reads "&" in the replacement
  # as the matched text (patsub_replacement), which would break a path with "&" in it.
  local rest=$1 out=
  while [[ $rest == *"$2"* ]]; do
    out+="${rest%%"$2"*}$3"
    rest=${rest#*"$2"}
  done
  printf '%s\n' "$out$rest"
}

render_unit() {
  local line bin
  bin=$(unit_escape "$launcher")
  bin=${bin//\$/\$\$}  # ExecStart= expands $NAME; "$$" is a literal "$"
  printf '# Written by %s from systemd/factory.service; run it again to update.\n' "$repo_dir/install.sh"
  while IFS= read -r line || [ -n "$line" ]; do
    if [ "$line" = "@ENVIRONMENT@" ]; then
      printf 'Environment="PATH=%s"\n' "$(unit_escape "$PATH")"
      if [ -n "${FACTORY_PYTHON:-}" ]; then
        printf 'Environment="FACTORY_PYTHON=%s"\n' "$(unit_escape "$FACTORY_PYTHON")"
      fi
      if [ -n "${FACTORY_CONFIG:-}" ]; then
        printf 'Environment="FACTORY_CONFIG=%s"\n' "$(unit_escape "$FACTORY_CONFIG")"
      fi
    else
      substitute "$line" @FACTORY_BIN@ "$bin"
    fi
  done <"$repo_dir/systemd/factory.service"
}

install_unit() {
  mkdir -p "$unit_dir"
  render_unit >"$unit.tmp"
  mv -f "$unit.tmp" "$unit"
  say "wrote $unit"
  if ! have systemctl; then
    warn "systemctl not found: the unit is written but not loaded"
  elif ! systemctl --user daemon-reload; then
    warn "systemctl --user daemon-reload failed (no user session?): log in over SSH or the console and rerun"
  elif [ "$start" -eq 1 ]; then
    systemctl --user enable --now factory.service
    say "started: systemctl --user status factory   (logs: journalctl --user -u factory -f)"
  else
    say "start it with: systemctl --user enable --now factory.service"
  fi
  say "keep it running after you log out: sudo loginctl enable-linger $(id -un)"
}

uninstall_all() {
  if [ -L "$items_link" ] && [ "$(readlink -f "$items_link")" = "$items_launcher" ]; then
    rm -f "$items_link"
    say "removed $items_link"
  fi
  if [ -L "$link" ] && [ "$(readlink -f "$link")" = "$launcher" ]; then
    rm -f "$link"
    say "removed $link"
  elif [ -e "$link" ] || [ -L "$link" ]; then
    warn "$link does not point to $launcher; left alone"
  fi
  if [ -e "$unit" ]; then
    if have systemctl; then
      systemctl --user disable --now factory.service >/dev/null 2>&1 || true
    fi
    rm -f "$unit"
    if have systemctl; then
      systemctl --user daemon-reload >/dev/null 2>&1 || true
    fi
    say "removed $unit"
  fi
  say "kept your config, API keys and data ($(dirname "$config") and the data_dir it names)"
}

server_port() {
  "$python" -I -c '
import json, sys
try:
    print(int(json.load(open(sys.argv[1]))["server"].get("port") or 8765))
except Exception:
    print(8765)' "$config"
}

if [ "$uninstall" -eq 1 ]; then
  uninstall_all
  exit 0
fi
check_requirements
install_link
init_config
create_env_file
if [ "$systemd_user" -eq 1 ]; then
  install_unit
fi
port=$(server_port)
say ""
say "Next:"
say "  API keys:    $env_file"
say "  Run it:      factory serve   (or ./install.sh --systemd-user --start)"
say "  Login URL:   factory url"
say "  SSH tunnel:  ssh -N -L $port:127.0.0.1:$port $(id -un)@$(uname -n)   (from your laptop, then open the URL)"
