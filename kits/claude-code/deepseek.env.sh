# Point Claude Code at DeepSeek (or any Anthropic-compatible gateway) with the
# factory's two model tiers. Source it, don't run it:
#
#   export DEEPSEEK_API_KEY=sk-...
#   source kits/claude-code/deepseek.env.sh
#   claude
#
# Overrides (set before sourcing):
#   FACTORY_BASE_URL      default https://api.deepseek.com/anthropic
#                         (NanoGPT: https://nano-gpt.com/api)
#   FACTORY_API_KEY       default $DEEPSEEK_API_KEY
#   FACTORY_OPUS_MODEL    default deepseek-v4-pro[1m]    spec-writer, builder, security, approver, the orchestrator
#   FACTORY_SONNET_MODEL  default deepseek-v4-flash[1m]  ux, ui and code reviewers
#                         (set it to deepseek-v4-pro[1m] to run every agent on Pro)
#   FACTORY_EFFORT        default high (low | medium | high | max)

if [ -n "${BASH_SOURCE:-}" ] && [ "${BASH_SOURCE[0]}" = "$0" ]; then
  echo "Source this file instead of running it: source ${0}" >&2
  exit 1
fi

_factory_key="${FACTORY_API_KEY:-${DEEPSEEK_API_KEY:-}}"
if [ -z "$_factory_key" ]; then
  echo "Set DEEPSEEK_API_KEY (or FACTORY_API_KEY) first." >&2
  unset _factory_key
  return 1
fi

# An Anthropic key in the environment would be sent to the gateway instead.
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  echo "Unsetting ANTHROPIC_API_KEY in this shell so it is not sent to ${FACTORY_BASE_URL:-DeepSeek}." >&2
  unset ANTHROPIC_API_KEY
fi
if [ -n "${CLAUDE_CODE_SUBAGENT_MODEL:-}" ]; then
  echo "Unsetting CLAUDE_CODE_SUBAGENT_MODEL: it forces one model on every agent and flattens the tiers." >&2
  unset CLAUDE_CODE_SUBAGENT_MODEL
fi

export ANTHROPIC_BASE_URL="${FACTORY_BASE_URL:-https://api.deepseek.com/anthropic}"
export ANTHROPIC_AUTH_TOKEN="$_factory_key"
export ANTHROPIC_MODEL="${FACTORY_OPUS_MODEL:-deepseek-v4-pro[1m]}"
export ANTHROPIC_DEFAULT_OPUS_MODEL="${FACTORY_OPUS_MODEL:-deepseek-v4-pro[1m]}"
export ANTHROPIC_DEFAULT_SONNET_MODEL="${FACTORY_SONNET_MODEL:-deepseek-v4-flash[1m]}"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="${FACTORY_SONNET_MODEL:-deepseek-v4-flash[1m]}"
export ANTHROPIC_SMALL_FAST_MODEL="$ANTHROPIC_DEFAULT_HAIKU_MODEL"
export CLAUDE_CODE_EFFORT_LEVEL="${FACTORY_EFFORT:-high}"
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
export API_TIMEOUT_MS=600000
# Claude Code 2.1.8x only offers SendMessage (the skill uses it to resume the
# same builder, by agent id) with agent teams enabled.
export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
unset _factory_key

echo "Claude Code → $ANTHROPIC_BASE_URL  (opus=$ANTHROPIC_DEFAULT_OPUS_MODEL, sonnet=$ANTHROPIC_DEFAULT_SONNET_MODEL)"
