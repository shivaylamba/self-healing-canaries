"""AI SRE webhook service.

  POST /hooks/rollout   Argo Rollouts notifications (both clusters) land here.
                        completed -> ledger "passed"
                        aborted   -> ledger "failed" + start an investigation
  POST /investigate     Manually (re)run an investigation for an event (fallback).
  POST /hitl/policy     AURA's HITL webhook: allow or deny each GitHub write (guardrail 2).
  /ledger/mcp           The release ledger as an MCP server, for AURA.
  GET  /api/state       Ledger + investigations as JSON.
  GET  /api/incidents   Incident memory (when INCIDENTS_ENABLED=true); /api/incidents/{id} with timeline.
  GET  /                Live page for the stage: ledger and the agent's steps.
"""
import asyncio
import contextlib
import html
import json
import logging
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import incidents as inc_lib
import policy
import skills as skill_lib
from agent import DRY_RUN, GITHUB_REPO, MODEL, investigate
from changes import ChangeTracker
from incident_service import IncidentService
from ledger import Ledger
from patterns import LokiReader, PatternScanner
from revert import Reverter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("ai-sre")

ledger = Ledger(os.environ.get("LEDGER_PATH", "/data/ledger.db"))

# ── Incident memory (docs/prd-incident-memory.md). Off by default: the demo is unchanged. ──
INCIDENTS_ENABLED = os.environ.get("INCIDENTS_ENABLED", "false").lower() == "true"
incidents: IncidentService | None = None
if INCIDENTS_ENABLED:
    _token = os.environ.get("GITHUB_TOKEN", "")
    _grafana = os.environ.get("GRAFANA_URL")
    incidents = IncidentService(
        ledger,
        tracker=ChangeTracker(ledger, GITHUB_REPO, _token) if _token else None,
        scanner=PatternScanner(ledger, LokiReader(_grafana, os.environ.get("GRAFANA_USERNAME", "admin"),
                                                  os.environ.get("GRAFANA_PASSWORD", "admin")),
                               window_s=int(os.environ.get("PATTERN_SCAN_SECS", "60"))) if _grafana else None,
        reverter=Reverter(GITHUB_REPO, _token) if _token else None,
    )

# ── The release ledger as an MCP server (AURA connects to localhost:8080/ledger/mcp) ──
ledger_mcp = FastMCP(
    "release-ledger", stateless_http=True, json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@ledger_mcp.tool()
def get_release_history(limit: int = 20) -> str:
    """Most recent canary verdicts across ALL environments, newest first.
    Each row: env, image, git_sha, pod_hash, verdict (passed|failed), source_pr, source_branch."""
    return json.dumps(ledger.releases(limit), default=str)


@ledger_mcp.tool()
def compare_environments(image: str) -> str:
    """Every verdict recorded for one image, oldest first. Use it to answer:
    'did this exact image pass in another environment before failing here?'"""
    rows = ledger.image_history(image)
    summary: dict[str, list[str]] = {}
    for r in rows:
        summary.setdefault(r["env"], []).append(r["verdict"])
    return json.dumps({"image": image, "verdicts_by_env": summary, "rows": rows}, default=str)


def _need_incidents() -> str | None:
    return None if incidents else json.dumps({"error": "incident memory is disabled (INCIDENTS_ENABLED=false)"})


@ledger_mcp.tool()
def get_incident(incident_id: int) -> str:
    """One incident: status, attribution (hash|temporal), signature, matched incident, skill,
    fix/revert PRs, and its timeline of events."""
    if (off := _need_incidents()):
        return off
    inc = ledger.incident(incident_id)
    if not inc:
        return json.dumps({"error": f"no incident #{incident_id}"})
    return json.dumps({**inc, "events": ledger.incident_events(incident_id)}, default=str)


@ledger_mcp.tool()
def find_similar_incidents(incident_id: int, limit: int = 3) -> str:
    """Past incidents (resolved or guarded) whose signature resembles this one, best first,
    with their root cause, fix PR and how they were resolved. Treat them as hypotheses to verify."""
    if (off := _need_incidents()):
        return off
    inc = ledger.incident(incident_id)
    if not inc:
        return json.dumps({"error": f"no incident #{incident_id}"})
    past = [i for i in ledger.closed_incidents() if i["id"] != incident_id]
    matches = inc_lib.find_similar(inc["signature"], past, k=limit)
    return json.dumps([incidents._with_root_cause(m) for m in matches], default=str)


@ledger_mcp.tool()
def get_log_patterns(env: str, pod_hash: str) -> str:
    """Error-log templates seen on one ReplicaSet (pod-template-hash), with counts and the
    business slices (currency, region, SKU) they concentrate in."""
    if (off := _need_incidents()):
        return off
    return json.dumps(ledger.patterns_for_hash(env, pod_hash), default=str)


@ledger_mcp.tool()
def get_recent_changes(hours: int = 24) -> str:
    """Commits on main in the last N hours: files, kind of change (code/config/deploy/infra/test),
    PR, and whether an automatic revert would be safe."""
    if (off := _need_incidents()):
        return off
    return json.dumps(ledger.recent_changes(time.time() - hours * 3600), default=str)


@ledger_mcp.tool()
def list_skills() -> str:
    """The specialised debugging skills available, with what each one is for."""
    return json.dumps([{"name": s["name"], "summary": s.get("summary", "")} for s in skill_lib.load()])


@ledger_mcp.tool()
def get_skill(name: str) -> str:
    """The full playbook of one skill."""
    s = skill_lib.get(name)
    return s["body"] if s else json.dumps({"error": f"no skill {name}"})


_ledger_app = ledger_mcp.streamable_http_app()


@contextlib.asynccontextmanager
async def lifespan(_app):
    loops = []
    if incidents and incidents.tracker:
        loops.append(asyncio.create_task(incidents.changes_loop(int(os.environ.get("CHANGES_POLL_SECS", "60")))))
    if incidents and incidents.scanner:
        loops.append(asyncio.create_task(incidents.scan_loop(int(os.environ.get("PATTERN_SCAN_SECS", "60")),
                                                             lambda ev, ctx: start(ev, force=True, ctx=ctx))))
    async with ledger_mcp.session_manager.run():
        yield
    for t in loops:
        t.cancel()


app = FastAPI(title="AI SRE", lifespan=lifespan)
app.mount("/ledger", _ledger_app)
_tasks: set[asyncio.Task] = set()
_active: set[int] = set()   # investigations in flight, so policy decisions show up on stage
_diagnosis_only: set[int] = set()   # in-flight investigations of time-correlated incidents: no writes

REQUIRED = ("event", "env", "image")


@app.post("/hooks/rollout")
async def rollout_hook(req: Request):
    ev = await req.json()
    if not all(ev.get(k) for k in REQUIRED):
        return JSONResponse({"error": f"missing one of {REQUIRED}"}, status_code=400)
    log.info("◀ %s env=%s image=%s canary=%s stable=%s pr=%s",
             ev["event"], ev["env"], ev["image"], ev.get("canaryHash"), ev.get("stableHash"), ev.get("sourcePR"))

    if ev["event"] == "completed":
        ledger.record(ev, "passed")
        if incidents:
            _spawn(incidents.on_completed(ev))
        return {"recorded": "passed"}

    if ev["event"] == "aborted":
        ledger.record(ev, "failed")
        if incidents:
            if (prior := ledger.recent_investigation(ev["env"], ev["image"])):
                return {"skipped": prior["id"]}
            _spawn(_abort_with_incident(ev))
            return {"incident": "opening"}
        return start(ev)

    return {"ignored": ev["event"]}


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def _abort_with_incident(ev: dict) -> None:
    try:
        ctx = await incidents.on_abort(ev)
    except Exception:  # noqa: BLE001 — the incident system must never block the investigation
        log.exception("incident system failed; investigating without it")
        ctx = None
    start(ev, ctx=ctx)


# ── Guardrail 2: AURA's HITL webhook. Every GitHub write is parked until this answers. ──
def _policy(tool: str, a: dict) -> str | None:
    """Return a denial reason, or None to allow (rules in policy.py)."""
    if _diagnosis_only & _active:
        return "a time-correlated incident is being investigated: diagnosis only"
    return policy.check(tool, a)


@app.post("/hitl/policy")
async def hitl_policy(req: Request):
    body = await req.json()
    reasons = []
    for item in body.get("items", []):
        tool = item.get("tool_name", "")
        args = item.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        reason = _policy(tool, args)
        verdict = "DENY " + reason if reason else "ALLOW"
        log.info("  🛡  policy %s → %s", tool, verdict)
        for inv_id in _active:
            ledger.add_step(inv_id, {"kind": "thought", "text": f"policy: {tool} → {verdict}"})
        if reason:
            reasons.append(f"{tool}: {reason}")
    if reasons:
        return {"approved": False, "reason": "; ".join(reasons)}
    return {"approved": True}


@app.post("/investigate")
async def manual(req: Request):
    ev = await req.json()
    ev.setdefault("event", "aborted")
    return start(ev, force=True)


def start(ev: dict, force: bool = False, ctx: dict | None = None) -> dict:
    # Idempotency: Rollouts can re-send; one investigation per (env, image) per 30 min.
    if not force and (prior := ledger.recent_investigation(ev["env"], ev["image"])):
        log.info("  skip: investigation #%s already covers %s/%s", prior["id"], ev["env"], ev["image"])
        return {"skipped": prior["id"]}
    inv_id = ledger.start_investigation(ev)
    if ctx and incidents:
        incidents.link_investigation(ctx["incident_id"], inv_id)
        if ctx.get("diagnosis_only"):
            _diagnosis_only.add(inv_id)
    t = asyncio.create_task(run(inv_id, ev, ctx))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return {"investigation": inv_id}


async def run(inv_id: int, ev: dict, ctx: dict | None = None):
    log.info("▶ investigation #%d: %s canary %s (%s)", inv_id, ev["env"], ev.get("canaryHash"), ev["image"])

    def on_step(step: dict):
        ledger.add_step(inv_id, step)
        if step["kind"] == "tool":
            log.info("  🔧 %s %s", step["name"], step["args"][:160])
        elif step["kind"] == "thought":
            log.info("  💭 %s", step["text"][:200].replace("\n", " "))

    _active.add(inv_id)
    try:
        report = await asyncio.wait_for(investigate(ev, ledger, on_step, ctx), timeout=900)
        ledger.finish_investigation(inv_id, "done", report=report.model_dump())
        if ctx and incidents:
            incidents.on_report(ctx["incident_id"], report.model_dump())
        log.info("✔ #%d %s → %s", inv_id, report.action, report.pr_url)
        log.info("  ROOT CAUSE: %s", report.root_cause)
    except Exception as e:  # noqa: BLE001 — surface everything on stage
        log.exception("✘ #%d failed", inv_id)
        ledger.finish_investigation(inv_id, "error", error=f"{type(e).__name__}: {e}")
        if ctx and incidents:
            incidents.on_investigation_error(ctx["incident_id"], f"{type(e).__name__}: {e}")
    finally:
        _active.discard(inv_id)
        _diagnosis_only.discard(inv_id)


@app.get("/api/state")
def state():
    return {"releases": ledger.releases(), "investigations": ledger.investigations()}


@app.get("/api/incidents")
def list_incidents(limit: int = 50, status: str | None = None):
    if not incidents:
        return {"enabled": False, "incidents": []}
    return {"enabled": True, "incidents": ledger.incidents(limit, status)}


@app.get("/api/incidents/{inc_id}")
def get_incident_api(inc_id: int):
    inc = ledger.incident(inc_id) if incidents else None
    if not inc:
        return JSONResponse({"error": "not found"}, status_code=404)
    patterns = ledger.patterns_for_hash(inc["env"], inc["pod_hash"]) if inc.get("pod_hash") else []
    return {**inc, "events": ledger.incident_events(inc_id), "patterns": patterns}


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ── Stage UI ────────────────────────────────────────────────────────────────

def _t(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


@app.get("/", response_class=HTMLResponse)
def index():
    rel_rows = "".join(
        f"<tr><td>{_t(r['ts'])}</td><td><span class='env {r['env']}'>{_e(r['env'])}</span></td>"
        f"<td><code>{_e(r['image'].split('/')[-1])}</code></td><td><code>{_e(r['pod_hash'])}</code></td>"
        f"<td>{_e('#' + r['source_pr'] if r['source_pr'] else '')}</td>"
        f"<td><span class='v {r['verdict']}'>{_e(r['verdict'])}</span></td></tr>"
        for r in ledger.releases(25)
    ) or "<tr><td colspan=6 class=muted>No rollouts recorded yet.</td></tr>"

    invs = []
    for inv in ledger.investigations(5):
        steps = "".join(
            f"<li class='{s['kind']}'><span class=ts>{_t(s['t'])}</span> "
            + (f"<b>{_e(s['name'])}</b> <code>{_e(s['args'][:220])}</code>" if s["kind"] == "tool"
               else _e(s.get("text", ""))[:300])
            + "</li>"
            for s in inv["steps"] if s["kind"] != "result"
        )
        rep = inv["report"]
        rep_html = ""
        if rep:
            ev_items = "".join(f"<li>{_e(x)}</li>" for x in rep["evidence"])
            pr = f"<a href='{_e(rep['pr_url'])}' target=_blank>{_e(rep['pr_url'])}</a>" if rep.get("pr_url") else "—"
            rep_html = (
                f"<div class=report><h4>Root cause</h4><p>{_e(rep['root_cause'])}</p>"
                f"<h4>Offending change</h4><p>{_e(rep['offending_change'])}</p>"
                + (f"<h4>Environment difference</h4><p>{_e(rep['environment_difference'])}</p>" if rep.get("environment_difference") else "")
                + f"<h4>Evidence</h4><ul>{ev_items}</ul><h4>Fix</h4><p>{_e(rep['fix_summary'])}</p>"
                f"<p><b>{_e(rep['action'])}</b> · {pr} · confidence {_e(rep['confidence'])}</p></div>"
            )
        if inv["error"]:
            rep_html = f"<div class='report err'>{_e(inv['error'])}</div>"
        ev = inv["event"]
        invs.append(
            f"<section class=inv><header><span class='status {inv['status']}'>{_e(inv['status'])}</span> "
            f"#{inv['id']} · <span class='env {ev['env']}'>{_e(ev['env'])}</span> canary "
            f"<code>{_e(ev.get('canaryHash'))}</code> · <code>{_e(ev['image'].split('/')[-1])}</code> · {_t(inv['ts'])}</header>"
            f"<ol class=steps>{steps}</ol>{rep_html}</section>"
        )

    mode = "DRY RUN (diagnose only)" if DRY_RUN else "live (opens PRs; humans merge)"
    return f"""<!doctype html><html><head><meta charset=utf-8><meta http-equiv=refresh content=3>
<title>AI SRE</title><style>
:root{{--bg:#0d1117;--fg:#e6edf3;--mut:#8b949e;--card:#161b22;--bd:#30363d;--ok:#3fb950;--bad:#f85149;--acc:#58a6ff;--warn:#d29922}}
body{{background:var(--bg);color:var(--fg);font:15px/1.45 ui-sans-serif,system-ui,sans-serif;margin:0;padding:24px}}
h1{{margin:0 0 4px;font-size:22px}} .muted{{color:var(--mut)}} code{{font:13px ui-monospace,monospace;color:#d2a8ff}}
.grid{{display:grid;grid-template-columns:minmax(380px,1fr) 2fr;gap:20px;margin-top:16px}}
.card{{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:16px;overflow:auto}}
table{{width:100%;border-collapse:collapse}} td,th{{padding:6px 8px;border-bottom:1px solid var(--bd);text-align:left;font-size:14px}}
.env{{padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600}} .env.staging{{background:#1f6feb33;color:var(--acc)}} .env.prod{{background:#d2992233;color:var(--warn)}}
.v.passed,.status.done{{color:var(--ok);font-weight:700}} .v.failed,.status.error{{color:var(--bad);font-weight:700}} .status.running{{color:var(--warn);font-weight:700}}
.inv{{border:1px solid var(--bd);border-radius:8px;padding:12px;margin-bottom:14px}} .inv header{{font-weight:600;margin-bottom:8px}}
.steps{{margin:0;padding-left:20px;font-size:13px}} .steps li{{margin:3px 0}} .steps li.thought{{color:var(--mut)}} .ts{{color:var(--mut);font-size:11px}}
.report{{margin-top:10px;border-top:1px dashed var(--bd);padding-top:8px}} .report h4{{margin:8px 0 2px;color:var(--acc);font-size:13px;text-transform:uppercase}}
.report p{{margin:2px 0}} .err{{color:var(--bad)}} a{{color:var(--acc)}}
@media(max-width:900px){{.grid{{grid-template-columns:1fr}}}}
</style></head><body>
<h1>AI SRE</h1><div class=muted>Triggered by Argo Rollouts notifications · AURA on Nebius Token Factory · {_e(MODEL)} · {mode}</div>
<div class=grid>
<div class=card><h3>Release ledger</h3><table><tr><th>time</th><th>env</th><th>image</th><th>hash</th><th>PR</th><th>verdict</th></tr>{rel_rows}</table></div>
<div class=card><h3>Investigations</h3>{''.join(invs) or "<p class=muted>Waiting for an aborted rollout…</p>"}</div>
</div></body></html>"""
