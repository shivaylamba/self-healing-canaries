#!/bin/sh
# AURA in the Agent Guard microVM. Model and tool endpoints come from the
# Jozu gateway (OPENAI_BASE_URL, AGENTGUARD_MCP_GATEWAY_URL); no provider key here.
export HOME="${AGENTGUARD_STATE_DIR:-/tmp}"          # writable, persists per environment
cd "${AGENTGUARD_WORKSPACE:-$HOME}" || exit 1
[ -d .aura ] && export AURA_LOG_FILE="$PWD/.aura/aura.log"
exec /usr/local/bin/aura --config /app/aura.toml --enable-client-tools "$@"
