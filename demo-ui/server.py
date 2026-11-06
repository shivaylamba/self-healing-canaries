#!/usr/bin/env python3
"""Demo control page: one screen for the whole Self-Healing Canaries story.

Runs on the demo host (the machine with the kind-staging / kind-prod contexts),
bound to localhost only. Reach it through the SSH tunnel: http://localhost:8099

  GET  /               the page
  GET  /api/snapshot   rollouts, analysis, Prometheus series, AI SRE state, PRs, story progress
  POST /api/run/<id>   run one demo action (one at a time); output streams to /api/run
  GET  /api/run        the current/last action's status and output

Standard library only. Shells out to kubectl, gh and the scripts in scripts/demo/.
"""
import json
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = (Path(__file__).parent / "index.html").read_bytes()
INCIDENTS_PAGE = (Path(__file__).parent / "incidents.html").read_bytes()
PORT = int(os.environ.get("DEMO_UI_PORT", "8099"))
AI_SRE = os.environ.get("AI_SRE_URL", "http://localhost:8088")
PROM = {"staging": "http://localhost:9090", "prod": "http://localhost:9091"}


def _env_file(path):
    out = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


DEMO = _env_file(ROOT / "demo.env")
REPO = DEMO.get("GITHUB_REPO", "")
PR1_BRANCH = DEMO.get("PR1_BRANCH", "feat/inventory-v2-psp-v2")
BASE_TAG = DEMO.get("BASE_TAG", "v1.0.0")

ENV = dict(os.environ, NO_PAUSE="1", NO_WATCH="1",
           PATH=f"{Path.home()}/bin:" + os.environ.get("PATH", ""))
tok = Path.home() / ".config/scn/gh-token"
if tok.exists() and "GH_TOKEN" not in ENV:
    ENV["GH_TOKEN"] = tok.read_text().strip()


def sh(args, timeout=20):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=ENV, cwd=ROOT)
        return r.stdout if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError):
        return ""


def kjson(ctx, *args):
    out = sh(["kubectl", "--context", f"kind-{ctx}", *args, "-o", "json"])
    try:
        return json.loads(out) if out else {}
    except json.JSONDecodeError:
        return {}


def http_json(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:  # noqa: BLE001 - a dead backend shows as empty on the page
        return {}


# ── Prometheus ────────────────────────────────────────────────────────────────
ALL = 'rate(http_server_duration_milliseconds_count{app="payment"}[1m])'
BAD = 'rate(http_server_duration_milliseconds_count{app="payment",http_status_code=~"5.."}[1m])'
BY = "sum by (rollouts_pod_template_hash) "
Q_ERR_RS = f"({BY}({BAD}) or 0 * {BY}({ALL})) / {BY}({ALL})"
Q_ERR_ALL = f"(sum({BAD}) or vector(0)) / sum({ALL})"
Q_RPS_RS = f"{BY}({ALL})"


def prom_range(env, q, minutes=12, step=10):
    end = time.time()
    qs = urllib.parse.urlencode({"query": q, "start": end - minutes * 60, "end": end, "step": step})
    d = http_json(f"{PROM[env]}/api/v1/query_range?{qs}")
    series = []
    for s in (d.get("data") or {}).get("result", []):
        series.append({
            "label": s["metric"].get("rollouts_pod_template_hash", "service-wide"),
            "points": [[float(t), float(v)] for t, v in s["values"] if v not in ("NaN", "+Inf", "-Inf")],
        })
    return series


# ── Kubernetes ────────────────────────────────────────────────────────────────
METRIC_ORDER = ["canary-error-rate", "service-error-rate", "canary-vs-stable-p95"]


def rollout_view(env):
    r = kjson(env, "-n", "payments", "get", "rollout", "payment")
    if not r:
        return {"env": env, "error": "unreachable"}
    st, spec = r.get("status", {}), r.get("spec", {})
    rs = kjson(env, "-n", "payments", "get", "rs", "-l", "app=payment")
    img_by_hash = {}
    for item in rs.get("items", []):
        h = item["metadata"].get("labels", {}).get("rollouts-pod-template-hash")
        if h:
            img_by_hash[h] = {"image": item["spec"]["template"]["spec"]["containers"][0]["image"].split(":")[-1],
                              "ready": item.get("status", {}).get("readyReplicas", 0) or 0}
    w = (st.get("canary") or {}).get("weights") or {}
    stable_h, canary_h = st.get("stableRS"), st.get("currentPodHash")
    ann = r["metadata"].get("annotations", {})

    ar = kjson(env, "-n", "payments", "get", "analysisrun", "--sort-by=.metadata.creationTimestamp")
    analysis = None
    # Only the analysis of the revision being rolled out now; an older run would show a stale verdict.
    items = [a for a in ar.get("items", [])
             if a["metadata"].get("labels", {}).get("rollouts-pod-template-hash") == canary_h]
    if items:
        a = items[-1]
        dry = {d.get("metricName") for d in a["spec"].get("dryRun") or []}
        analysis = {
            "name": a["metadata"]["name"], "phase": a.get("status", {}).get("phase"),
            "metrics": [{
                "name": m["name"], "phase": m.get("phase"), "dryRun": m["name"] in dry,
                "value": ((m.get("measurements") or [{}])[-1]).get("value"),
            } for m in sorted(a.get("status", {}).get("metricResults", []),
                              key=lambda m: METRIC_ORDER.index(m["name"]) if m["name"] in METRIC_ORDER else 99)],
        }
    # The traffic this environment receives (kustomize-generated configmap loadgen-<hash>).
    cms = [c for c in kjson(env, "-n", "payments", "get", "configmaps").get("items", [])
           if c["metadata"]["name"].startswith("loadgen")]
    cms.sort(key=lambda c: c["metadata"]["creationTimestamp"])
    lg = cms[-1].get("data", {}) if cms else {}
    return {
        "traffic": {"rps": lg.get("RPS"), "profile": lg.get("PROFILE")},
        "env": env, "phase": st.get("phase"), "message": st.get("message"),
        "step": st.get("currentStepIndex"), "steps": len(spec.get("strategy", {}).get("canary", {}).get("steps", [])),
        "aborted": bool(st.get("abort")),
        "stable": {"hash": stable_h, **img_by_hash.get(stable_h, {})},
        "canary": {"hash": canary_h, **img_by_hash.get(canary_h, {})} if canary_h != stable_h else None,
        "canaryWeight": (w.get("canary") or {}).get("weight", 0),
        "desiredImage": spec["template"]["spec"]["containers"][0]["image"].split(":")[-1],
        "sourcePR": ann.get("scn.dev/source-pr"), "analysis": analysis,
    }


def argo_view():
    apps = kjson("staging", "-n", "argocd", "get", "applications")
    return {a["metadata"]["name"]: {
        "revision": a["spec"]["source"].get("targetRevision"),
        "sync": a.get("status", {}).get("sync", {}).get("status"),
        "health": a.get("status", {}).get("health", {}).get("status"),
    } for a in apps.get("items", [])}


# ── GitHub ────────────────────────────────────────────────────────────────────
def prs_view():
    out = sh(["gh", "pr", "list", "-R", REPO, "--state", "all", "-L", "8",
              "--json", "number,title,headRefName,state,url,createdAt"])
    prs = json.loads(out) if out else []
    runs_out = sh(["gh", "run", "list", "-R", REPO, "--workflow", "pr-image.yml", "-L", "20",
                   "--json", "headBranch,status,conclusion,createdAt"])
    runs = json.loads(runs_out) if runs_out else []
    for p in prs:
        mine = [r for r in runs if r["headBranch"] == p["headRefName"] and r["createdAt"] >= p["createdAt"]]
        p["ci"] = (mine[0]["conclusion"] or mine[0]["status"]) if mine else None
    return prs


# ── Story progress, derived from live state ──────────────────────────────────
STEPS = [
    ("pr", "Feature PR open", "CI green, looks safe"),
    ("stage1", "PR → staging canary", "you: Deploy feature PR"),
    ("abort1", "Staging canary aborted", "gate: canary error rate > 1%"),
    ("agent1", "AI SRE fixes the PR", "Grafana + GitHub MCP, policy-gated push"),
    ("pass1", "Fix passes staging", "same gate, new image"),
    ("merge1", "Human merges → prod", "you: Merge"),
    ("abort2", "Prod canary aborted", "same image, prod-only traffic (JPY)"),
    ("agent2", "AI SRE opens a PR", "fix + JPY in staging traffic"),
    ("pass2", "Fix passes staging on JPY", "you: Deploy agent PR"),
    ("prod", "Merge → prod green", "you: Merge"),
]


def story(ledger, investigations, prs, ro):
    feat = next((p for p in prs if p["headRefName"] == PR1_BRANCH), None)
    feat_n = str(feat["number"]) if feat else None
    agent = next((p for p in prs if p["headRefName"].startswith("ai-sre/") and p["state"] != "CLOSED"), None)
    agent_n = str(agent["number"]) if agent else None

    def row(env, pr, verdict):
        return any(r["env"] == env and r["source_pr"] == pr and r["verdict"] == verdict for r in ledger)

    inv = lambda env: next((i for i in investigations if i["event"]["env"] == env), None)  # noqa: E731
    i1, i2 = inv("staging"), inv("prod")
    done = {
        "pr": bool(feat),
        "stage1": bool(feat_n) and (row("staging", feat_n, "failed") or row("staging", feat_n, "passed")
                                      or ro["staging"].get("sourcePR") == feat_n),
        "abort1": bool(feat_n) and row("staging", feat_n, "failed"),
        "agent1": bool(i1) and i1["status"] == "done",
        "pass1": bool(feat_n) and row("staging", feat_n, "passed"),
        "merge1": bool(feat) and feat["state"] == "MERGED",
        "abort2": bool(feat_n) and row("prod", feat_n, "failed"),
        "agent2": bool(i2) and i2["status"] == "done" and bool(agent),
        "pass2": bool(agent_n) and row("staging", agent_n, "passed"),
        "prod": bool(agent_n) and row("prod", agent_n, "passed"),
    }
    current = next((k for k, *_ in STEPS if not done[k]), None)

    # The one button the presenter needs next (None = wait, it's automatic).
    nxt = None
    if current == "stage1" and feat and feat["ci"] == "success":
        nxt = {"action": "deploy-feature", "label": f"Deploy feature PR #{feat_n} to staging"}
    elif current == "merge1":
        nxt = {"action": "merge", "label": f"Merge PR #{feat_n} → prod"}
    elif current == "pass2" and agent and ro["staging"].get("sourcePR") != agent_n:
        nxt = ({"action": "deploy-latest", "label": f"Deploy agent PR #{agent_n} to staging"}
               if agent["ci"] == "success" else {"wait": f"CI is building agent PR #{agent_n}…"})
    elif current == "prod" and agent and agent["state"] == "OPEN":
        nxt = {"action": "merge", "label": f"Merge PR #{agent_n} → prod"}
    elif current is None:
        nxt = {"done": True}

    return {"steps": [{"id": k, "title": t, "sub": s, "done": done[k]} for k, t, s in STEPS],
            "current": current, "next": nxt, "featurePR": feat_n, "agentPR": agent_n}


# ── Snapshot cache (refreshed in the background) ─────────────────────────────
_snap = {"ts": 0}
_lock = threading.Lock()


def refresh_loop():
    slow = {"prs": [], "argo": {}, "t": 0}
    while True:
        try:
            ro = {e: rollout_view(e) for e in ("staging", "prod")}
            if time.time() - slow["t"] > 8:
                slow.update(prs=prs_view(), argo=argo_view(), t=time.time())
            state = http_json(f"{AI_SRE}/api/state")
            ledger, invs = state.get("releases", []), state.get("investigations", [])
            charts = {e: {"errRS": prom_range(e, Q_ERR_RS), "errAll": prom_range(e, Q_ERR_ALL),
                          "rps": prom_range(e, Q_RPS_RS)} for e in PROM}
            snap = {"ts": time.time(), "repo": REPO, "rollouts": ro, "charts": charts, "argo": slow["argo"],
                    "prs": slow["prs"], "ledger": ledger[:10], "investigations": invs[:3],
                    "story": story(ledger, invs, slow["prs"], ro)}
            with _lock:
                _snap.clear()
                _snap.update(snap)
        except Exception as e:  # noqa: BLE001 - keep serving the last good snapshot
            print("refresh error:", e, flush=True)
        time.sleep(2)


# ── Actions ──────────────────────────────────────────────────────────────────
D = "scripts/demo/"
ACTIONS = {
    "deploy-feature": [D + "deploy-pr-to-staging.sh", PR1_BRANCH],
    "deploy-latest": [D + "deploy-pr-to-staging.sh"],
    "merge": [D + "merge.sh"],
    "trigger-staging": [D + "trigger-ai-sre.sh", "staging"],
    "trigger-prod": [D + "trigger-ai-sre.sh", "prod"],
    "retry-staging": ["kubectl", "argo", "rollouts", "--context", "kind-staging", "-n", "payments", "retry", "rollout", "payment"],
    "retry-prod": ["kubectl", "argo", "rollouts", "--context", "kind-prod", "-n", "payments", "retry", "rollout", "payment"],
    "fallback-pr1": [D + "fallback-fix.sh", "pr1"],
    "fallback-pr2": [D + "fallback-fix.sh", "pr2"],
    "reset": ["bash", "-c", D + "reset.sh && " + D + "prepare.sh"],
    "status": [D + "status.sh"],
}
_run = {"id": None, "running": False, "out": "", "rc": None, "started": None}


def run_action(aid):
    _run.update(id=aid, running=True, out=f"$ {' '.join(ACTIONS[aid])}\n", rc=None, started=time.time())
    try:
        p = subprocess.Popen(ACTIONS[aid], cwd=ROOT, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1)
        for line in p.stdout:
            _run["out"] += line
        _run["rc"] = p.wait()
    except OSError as e:
        _run["out"] += f"\n{e}\n"
        _run["rc"] = 1
    _run["running"] = False


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if self.path == "/api/snapshot":
            with _lock:
                return self._send(200, dict(_snap))
        if self.path == "/api/run":
            return self._send(200, _run)
        # Incident memory (separate page; the stage page is unchanged). Read-only proxy to the AI SRE.
        if self.path in ("/incidents", "/incidents.html"):
            return self._send(200, INCIDENTS_PAGE, "text/html; charset=utf-8")
        if self.path == "/api/incidents":
            return self._send(200, http_json(f"{AI_SRE}/api/incidents") or {"enabled": False, "incidents": []})
        if self.path.startswith("/api/incidents/") and self.path.rsplit("/", 1)[-1].isdigit():
            return self._send(200, http_json(f"{AI_SRE}/api/incidents/{self.path.rsplit('/', 1)[-1]}"))
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        # Only same-origin pages may trigger actions (the page is served from this server).
        origin = self.headers.get("Origin", "")
        if origin and urllib.parse.urlparse(origin).hostname not in ("localhost", "127.0.0.1"):
            return self._send(403, {"error": "cross-origin"})
        aid = self.path.removeprefix("/api/run/")
        if aid not in ACTIONS:
            return self._send(404, {"error": f"unknown action {aid}"})
        if _run["running"]:
            return self._send(409, {"error": f"{_run['id']} is still running"})
        threading.Thread(target=run_action, args=(aid,), daemon=True).start()
        self._send(202, {"started": aid})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    threading.Thread(target=refresh_loop, daemon=True).start()
    print(f"demo UI on http://127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
