"""SRE tools MCP server for the Agent Guard demo.

A self-contained stand-in for the real cluster: it answers with the same story as
the KubeCon demo (a canary of payments v1.1.0 failing on JPY checkouts), so the
agent has something real-looking to investigate on a laptop with no cluster.

Runs on the laptop at 127.0.0.1:7070, playing the "cluster API" outside the
sandbox. AURA never calls it directly: the agent definition registers it as an
MCP module, and the Jozu AI Gateway inside the microVM reaches it at
192.168.127.254:7070 (the Mac's loopback as seen from the VM), applying
ToolPolicy to every call first. The VM has no direct route to it.
"""
import datetime as dt
import shlex
import sys

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

mcp = FastMCP(
    "sre", host="127.0.0.1", port=7070, stateless_http=True, log_level="WARNING",
    # Keep DNS-rebinding protection, and also accept the Host the VM's gateway sends.
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "192.168.127.254:*"],
        allowed_origins=[],
    ),
)

NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def ts(minutes_ago: float) -> str:
    return (NOW - dt.timedelta(minutes=minutes_ago)).strftime("%H:%M:%S")


CANARY, STABLE = "7c9f6d8b5", "5d8b9c7f4"


def received(call: str) -> None:
    """One line per call that actually reached the cluster side. Blocked calls never show up here."""
    t = dt.datetime.now().strftime("%H:%M:%S")
    print(f"\033[2m{t}\033[0m  cluster API received  \033[1m{call}\033[0m", flush=True)

LOGS = {
    "prod": [
        (6.2, CANARY, "ERROR", "charge_failed", "psp amount 1250000 exceeds limit 1000000 for JPY", "JPY"),
        (5.9, CANARY, "ERROR", "charge_failed", "psp amount 980000.00 rejected: JPY has no minor units", "JPY"),
        (5.1, CANARY, "ERROR", "charge_failed", "psp amount 4200000 exceeds limit 1000000 for JPY", "JPY"),
        (4.4, CANARY, "ERROR", "charge_failed", "psp amount 1890000 exceeds limit 1000000 for JPY", "JPY"),
        (3.0, CANARY, "ERROR", "charge_failed", "psp amount 2310000 exceeds limit 1000000 for JPY", "JPY"),
        (1.7, CANARY, "ERROR", "charge_failed", "psp amount 1100000 exceeds limit 1000000 for JPY", "JPY"),
    ],
    "staging": [],
}


@mcp.tool()
def get_error_logs(namespace: str = "prod", since_minutes: int = 15) -> str:
    """ERROR log lines for the payments service, with the pod-template-hash and currency."""
    received(f"get_error_logs namespace={namespace} since={since_minutes}m")
    rows = [r for r in LOGS.get(namespace, []) if r[0] <= since_minutes]
    if not rows:
        return f"no ERROR logs for payments in {namespace} in the last {since_minutes}m"
    out = [f"payments/{namespace}  ERROR logs, last {since_minutes}m  (canary={CANARY}, stable={STABLE})"]
    for m, h, sev, et, msg, cur in rows:
        out.append(f"{ts(m)}  {sev}  hash={h}  error.type={et}  checkout_currency={cur}  {msg}")
    out.append(f"summary: {len(rows)} errors, all on hash {CANARY} (canary), all checkout_currency=JPY; "
               f"0 errors on {STABLE} (stable)")
    return "\n".join(out)


@mcp.tool()
def get_canary_metrics(namespace: str = "prod") -> str:
    """Canary analysis metrics for the payments rollout (error rate and p95, canary vs stable)."""
    received(f"get_canary_metrics namespace={namespace}")
    if namespace != "prod":
        return f"payments/{namespace}: no canary in progress; stable v1.0.0 healthy, error rate 0.0%"
    return "\n".join([
        "payments/prod  rollout step 2/5 (20% canary)  analysis: FAILED",
        f"  canary-error-rate     canary {CANARY}: 4.8%   threshold < 1%   FAIL",
        "  service-error-rate    all pods: 1.0%           threshold < 2%   pass",
        "  canary-vs-stable-p95  canary 212ms vs stable 205ms               pass",
        "  error breakdown on canary: 100% checkout_currency=JPY (charge_failed)",
    ])


ROLLOUT = f"""Name:            payments
Namespace:       prod
Status:          ✖ Degraded
Message:         RolloutAborted: metric "canary-error-rate" assessed Failed
Strategy:        Canary
  Step:          2/5
  SetWeight:     0
Images:          ghcr.io/shivaylamba/payments:v1.0.0 (stable)
                 ghcr.io/shivaylamba/payments:v1.1.0 (canary, aborted)
Replicas:        Desired 5  Current 5  Updated 0  Ready 5  Available 5
REVISION  HASH        STATUS
2         {CANARY}   ✖ ScaledDown (aborted)   v1.1.0  commit 9e1c2ab "Format amounts in minor units"
1         {STABLE}   ✔ Healthy (stable)        v1.0.0"""

PODS = f"""NAME                         READY   STATUS    RESTARTS   AGE
payments-{STABLE}-2xkqp   1/1     Running   0          3d
payments-{STABLE}-8vwlm   1/1     Running   0          3d
payments-{STABLE}-c4n7t   1/1     Running   0          3d
payments-{STABLE}-hz9rd   1/1     Running   0          3d
payments-{STABLE}-q6bfs   1/1     Running   0          3d"""


@mcp.tool()
def kubectl(command: str) -> str:
    """Run a kubectl command against the payments cluster, e.g. "get pods -n prod",
    "argo rollouts get rollout payments -n prod", "argo rollouts undo payments -n prod"."""
    received(f"kubectl {command.removeprefix('kubectl').strip()}")
    args = shlex.split(command.removeprefix("kubectl").strip())
    joined = " ".join(args)
    ns = args[args.index("-n") + 1] if "-n" in args and args.index("-n") + 1 < len(args) else "default"
    if not args:
        return "error: no command given"
    verb = args[0]
    if verb == "get" and len(args) > 1 and args[1].startswith("pod"):
        return PODS if ns == "prod" else f"payments pods in {ns}: 5/5 Running (v1.0.0)"
    if "rollout" in joined and ("get" in args or "status" in args):
        return ROLLOUT
    if "undo" in args or "abort" in args:
        return f"rollout 'payments' in {ns}: aborted canary scaled down; stable {STABLE} serving 100%"
    if verb == "delete":
        return f"{' '.join(args[1:3])} deleted"
    if verb == "scale":
        return f"{args[1] if len(args) > 1 else 'resource'} scaled"
    if verb in ("logs", "describe", "get", "top"):
        return f"(ok) kubectl {joined}: nothing unusual beyond the canary abort above"
    return f"(ok) kubectl {joined}"


if __name__ == "__main__":
    print("payments cluster API (MCP) listening on 127.0.0.1:7070; the sandbox reaches it only via the Jozu gateway",
          file=sys.stderr, flush=True)
    mcp.run(transport="streamable-http")
