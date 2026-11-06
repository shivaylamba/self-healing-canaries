"""The AI SRE agent: AURA (mezmo/aura) on Nebius Token Factory.

This module no longer runs an agent loop itself. AURA does, in the `aura` sidecar
(config: aura.toml): the model, the MCP servers (Grafana, GitHub, release ledger),
the tool allowlist and the HITL policy gate all live there. Here we send AURA the
rollout notification over its OpenAI-compatible API and stream its tool calls to
the stage UI.

What it CAN do:   read telemetry (Grafana MCP, read-only), read code and history
                  (GitHub MCP), push a fix commit to a PR branch or open a new PR.
What it CANNOT do: merge, delete, change workflows or settings, touch the cluster.
                  Those tools are not in AURA's mcp_filter, every GitHub write is
                  checked by our /hitl/policy webhook, the token can't do them, and
                  branch protection on main requires a PR anyway.
"""
import json
import os
import re
from collections.abc import Awaitable, Callable
from typing import Literal

import httpx
from pydantic import BaseModel, ValidationError

from ledger import Ledger

AURA_URL = os.environ.get("AURA_URL", "http://localhost:8090")
MODEL = os.environ.get("AI_SRE_MODEL", "moonshotai/Kimi-K3.1-Pro")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "shivaylamba/self-healing-canaries")
DRY_RUN = os.environ.get("AI_SRE_DRY_RUN", "false").lower() == "true"


class Report(BaseModel):
    root_cause: str
    offending_change: str
    evidence: list[str]
    environment_difference: str | None = None
    fix_summary: str
    action: Literal["pushed_fix_to_pr", "opened_pr", "diagnosis_only"]
    pr_url: str | None = None
    confidence: Literal["high", "medium", "low"]
    # Incident memory (all optional, so older reports still parse)
    skill: str | None = None
    guard_added: bool = False
    similar_incident: int | None = None


REPORT_FORMAT = (
    "When you are finished, end your reply with the Report as one JSON object in a ```json block, "
    "with exactly these keys: root_cause (string), offending_change (string), evidence (list of "
    "strings), environment_difference (string or null), fix_summary (string), action "
    '("pushed_fix_to_pr" | "opened_pr" | "diagnosis_only"), pr_url (string or null), '
    'confidence ("high" | "medium" | "low"), skill (string or null: the skill you followed), '
    "guard_added (boolean: did your change add a test, a traffic profile or a canary metric that "
    "would catch this class of bug next time), similar_incident (integer or null: the past incident "
    "that matched, if it helped)."
)


def incident_block(ctx: dict | None) -> str:
    """What the incident system already knows, as untrusted-data context for the agent."""
    if not ctx:
        return ""
    parts = [f"<incident id=\"{ctx['incident_id']}\" attribution=\"{ctx['attribution']}\">"]
    if ctx.get("signature"):
        sig = ctx["signature"]
        parts.append("Signature: " + json.dumps({k: sig.get(k) for k in
                                                 ("templates", "error_types", "slice", "path_kinds", "dirs")}))
    if ctx.get("new_patterns"):
        parts.append("Log patterns only on the canary ReplicaSet (count, slices):")
        parts += [f"  - {p['template']}  (n={p['count']}, error_type={p.get('error_type')}, "
                  f"slices={json.dumps(p.get('slices', {}))})" for p in ctx["new_patterns"][:8]]
    if ctx.get("change"):
        c = ctx["change"]
        parts.append(f"Offending change: {c['sha'][:7]} {c.get('message', '')!r} kinds={c.get('kinds')} "
                     f"revertable={bool(c.get('revertable'))}")
    if ctx.get("similar"):
        parts.append("Similar past incidents (verify, don't assume):")
        for m in ctx["similar"]:
            parts.append(f"  - #{m['incident_id']} similarity={m['similarity']} resolution={m.get('resolution')} "
                         f"fix_pr={m.get('fix_pr')} root_cause={m.get('root_cause')!r}")
    if ctx.get("revert"):
        parts.append(f"The incident system already opened revert PR #{ctx['revert']['pr']} ({ctx['revert']['url']}) "
                     "as mitigation. Do not revert again; produce the root-cause fix.")
    parts.append("</incident>")
    if ctx.get("skill"):
        parts.append(f"\nSelected skill: {ctx['skill']['name']} (score {ctx.get('skill_score', 0)}). "
                     "Follow it alongside the playbook:\n<skill>\n" + ctx["skill"]["body"] + "\n</skill>")
    return "\n".join(parts) + "\n\n"


def task_prompt(ev: dict, ctx: dict | None = None) -> str:
    diagnose_only = DRY_RUN or (ctx or {}).get("diagnosis_only")
    mode = (
        "DIAGNOSIS ONLY: GitHub writes will be denied by policy. Diagnose and describe the fix only; "
        "action=diagnosis_only."
        if diagnose_only else
        "Follow the playbook through to the GitHub action that matches this environment."
    )
    what = ("Argo Rollouts aborted a canary and rolled back. The notification it sent is below.\n"
            if ev.get("event") != "new_pattern" else
            "The incident system saw a new error pattern without a failed rollout. Details are below.\n")
    return (
        what + "Investigate, fix through GitHub, and return your Report.\n\n"
        f"GITHUB_REPO: {GITHUB_REPO}\n"
        f"Prometheus datasource: Prometheus-{ev['env']}   Loki datasource: Loki-{ev['env']}\n"
        "Other environments' datasources follow the same naming (Prometheus-staging, Loki-prod, ...).\n\n"
        "<rollout_notification>\n" + json.dumps(ev, indent=2) + "\n</rollout_notification>\n\n"
        + incident_block(ctx)
        + mode + "\n\n" + REPORT_FORMAT
    )


async def investigate(ev: dict, ledger: Ledger, on_step: Callable[[dict], Awaitable[None] | None],
                      ctx: dict | None = None) -> Report:
    """Run one investigation through AURA and return the parsed Report."""
    body = {
        "model": "ai-sre",
        "stream": True,
        "messages": [{"role": "user", "content": task_prompt(ev, ctx)}],
    }
    text: list[str] = []
    timeout = httpx.Timeout(connect=10, read=300, write=30, pool=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", f"{AURA_URL}/v1/chat/completions", json=body) as r:
            r.raise_for_status()
            event = None
            async for line in r.aiter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    if not line:
                        event = None
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                await _handle(event, payload, text, on_step)

    final = "".join(text)
    report = parse_report(final)
    if report is None:
        raise ValueError("AURA finished without a parseable Report. Last output: " + final[-400:])
    return report


async def _handle(event: str | None, p: dict, text: list[str], on_step) -> None:
    if event == "aura.tool_requested":
        args = p.get("arguments")
        args = args if isinstance(args, str) else json.dumps(args)
        await _emit(on_step, {"kind": "tool", "name": p.get("tool_name", "tool"), "args": args[:600]})
    elif event == "aura.tool_complete":
        out = p.get("result") if p.get("success") else f"ERROR: {p.get('error')}"
        await _emit(on_step, {"kind": "result", "text": str(out)[:400]})
    elif event == "aura.approval_completed":
        await _emit(on_step, {"kind": "thought", "text": "policy: " + json.dumps(p)[:300]})
    elif event is None and "choices" in p:
        for ch in p["choices"]:
            delta = (ch.get("delta") or {}).get("content")
            if delta:
                text.append(delta)


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def parse_report(final: str) -> Report | None:
    """The last JSON object in AURA's final message that validates as a Report."""
    candidates = _JSON_BLOCK.findall(final)
    if not candidates and "{" in final:
        candidates = [final[final.index("{"): final.rindex("}") + 1]]
    for raw in reversed(candidates):
        try:
            return Report.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError):
            continue
    return None


async def _emit(cb, step):
    r = cb(step)
    if hasattr(r, "__await__"):
        await r
