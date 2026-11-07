#!/bin/bash
# Terminal 1: AURA inside a Jozu Agent Guard microVM.
set -euo pipefail
cd "$(dirname "$0")"
[ -n "${NEBIUS_API_KEY:-}" ] || { echo "Set NEBIUS_API_KEY in this shell (it goes to the Jozu gateway only)."; exit 1; }
curl -s -m 2 -o /dev/null -X POST localhost:7070/mcp -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  || { echo "Start ./cluster-api.sh in another terminal first."; exit 1; }
docker ps --format '{{.Names}}' | grep -qx agentguard-demo-registry || { echo "Run ./build.sh first (local registry is down)."; exit 1; }
export AGENTGUARD_NO_LOGIN=${AGENTGUARD_NO_LOGIN:-1} AGENTGUARD_NO_UPDATE_CHECK=1
exec agentguard run 127.0.0.1:5050/demo/aura-sre-agent:v1 --plain-http --env aura-sre -w ./workspace "$@"
