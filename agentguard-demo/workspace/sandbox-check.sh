#!/bin/sh
# What can an agent inside Jozu Agent Guard actually reach? Read-only checks.
# Run it on the laptop first, then ask AURA to run it inside the sandbox.
HOSTHOME=$(echo "$PWD" | cut -d/ -f1-3)   # /Users/<you>: the workspace keeps its laptop path
echo "== who and where"
echo "user=$(whoami)  host=$(hostname)  kernel=$(uname -sm)"
echo "== laptop secrets"
for d in "$HOSTHOME/.ssh" "$HOSTHOME/.aws" "$HOSTHOME/.kube" "$HOSTHOME/.config/gh" "$HOSTHOME/Library/Keychains"; do
  [ -e "$d" ] && echo "VISIBLE  $d" || echo "absent   $d"
done
echo "home dir contents: $(ls "$HOSTHOME" 2>/dev/null | head -8 | tr '\n' ' ')"
echo "== keys in env (names only)"
env | grep -E '^[A-Za-z_]*(KEY|TOKEN|SECRET)[A-Za-z_]*=' | cut -d= -f1 | sort | tr '\n' ' '; echo
[ -n "$AGENTGUARD_SESSION_ID" ] && echo "(OPENAI_API_KEY here is the Jozu gateway's per-session token, not a provider key)"
[ -n "$NEBIUS_API_KEY" ] && echo "NEBIUS_API_KEY present" || echo "NEBIUS_API_KEY absent"
echo "== network"
for u in https://example.com https://pastebin.com https://api.tokenfactory.nebius.com/v1/models; do
  c=$(curl -s -m 6 -o /dev/null -w '%{http_code}' "$u" 2>/dev/null); [ "$c" = "000" ] && c="BLOCKED"
  echo "$u -> $c"
done
