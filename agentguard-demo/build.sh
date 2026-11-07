#!/bin/bash
# One-time (or after edits): local registry, AURA runtime image, policy bundle, agent definition.
set -euo pipefail
cd "$(dirname "$0")"
REG=127.0.0.1:5050
docker info >/dev/null 2>&1 || { echo "Docker is not running. Start Docker Desktop first."; exit 1; }
if ! docker ps --format '{{.Names}}' | grep -qx agentguard-demo-registry; then
  docker start agentguard-demo-registry >/dev/null 2>&1 || \
    docker run -d -p 127.0.0.1:5050:5000 --name agentguard-demo-registry --restart unless-stopped registry:2 >/dev/null
fi
if [ ! -f app/aura-linux-arm64 ]; then
  (cd app && gh release download v0.2.20 -R mezmo/aura -p aura-linux-arm64 -p checksums.txt --clobber \
     && grep ' aura-linux-arm64$' checksums.txt | shasum -a 256 -c -)
fi
[ -x .venv/bin/python ] || { python3 -m venv .venv && .venv/bin/pip install -q "mcp==1.30.0"; }
echo "▸ AURA runtime image";  docker buildx build -q --platform linux/arm64 -t $REG/demo/aura-sre:v1 --push app
echo "▸ policy bundle";       kit pack policies   -t $REG/demo/aura-sre-policies:v1 >/dev/null && kit push --plain-http $REG/demo/aura-sre-policies:v1 >/dev/null
echo "▸ agent definition";    kit pack definition -t $REG/demo/aura-sre-agent:v1    >/dev/null && kit push --plain-http $REG/demo/aura-sre-agent:v1 >/dev/null
echo "✓ built. Terminal 2: ./cluster-api.sh   Terminal 1: ./run.sh"
