#!/bin/bash
# The audit trail: every decision the gateway made in the latest session, blocks highlighted.
f=$(ls -t ~/Library/Logs/AgentGuard/policy_audit.*.jsonl 2>/dev/null | head -1)
[ -n "$f" ] || { echo "no Agent Guard sessions yet"; exit 1; }
echo "session audit: $f"
python3 - "$f" <<'PY'
import json, sys, datetime as dt
for line in open(sys.argv[1]):
    r = json.loads(line); t = r.get("tool") or {}
    if not t.get("server"):        # skip the model calls and the boot self-test
        continue
    ok = r.get("allowed")
    args = json.dumps(t.get("arguments", {}))[:90]
    mark = "\033[32mALLOW\033[0m" if ok else "\033[1;31mBLOCK\033[0m"
    when = dt.datetime.fromisoformat(r['timestamp'][:19] + '+00:00').astimezone().strftime('%H:%M:%S')
    print(f"{when}  {mark}  {t['server']}/{t['name']}  {args}")
    for v in r.get("violations") or []:
        if not ok:
            print(f"          rule {v.get('policy')}/{v.get('rule')}")
PY
