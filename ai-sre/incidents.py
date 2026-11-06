"""Incidents (PRD FR3, FR4, FR7): signatures, matching against past incidents, lifecycle.

A signature is what an incident looks like, in terms that survive across incidents:
the log templates that appeared, their error types, the business slice the errors
concentrate in (currency=JPY), and the kind of change and directories involved.

Lifecycle (status):
    open ──(fix or revert PR opened)──▶ mitigating ──(that PR's image passes the canary
                                                      in the incident's env)──▶ resolved
                                                                          └─(fix left a test or
                                                                             a traffic guard)──▶ guarded
    A new incident that matches a resolved/guarded one with similarity >= RECUR_AT gets
    `recurrence_of` set: the guard didn't hold, which is the metric that matters most.

The functions here are pure (dicts in, dicts out) so the state machine is testable
without a database, GitHub or a model.
"""
from __future__ import annotations

import re
import time

from patterns import dominant_slice

RECUR_AT = 0.8          # a match this close to a closed incident is a recurrence
REVERT_MATCH_AT = 0.8   # ... and is close enough to repeat that incident's revert
WEIGHTS = {"templates": 0.5, "error_types": 0.2, "slice": 0.2, "paths": 0.1}
GUARD_PATHS = ("_test.go", "loadgen.env", "/tests/", "analysis-template")
MAX_TEMPLATES = 8


def signature(env: str, patterns: list[dict], change: dict | None) -> dict:
    """patterns: rows from Ledger.patterns_for_hash / patterns.new_on_canary (sorted by count)."""
    top = patterns[:MAX_TEMPLATES]
    merged: dict[str, dict[str, int]] = {}
    for p in top:
        for key, vals in (p.get("slices") or {}).items():
            agg = merged.setdefault(key, {})
            for v, n in vals.items():
                agg[v] = agg.get(v, 0) + n
    paths = (change or {}).get("paths", [])
    return {
        "env": env,
        "templates": [p["template"] for p in top],
        "error_types": sorted({p["error_type"] for p in top if p.get("error_type")}),
        "slice": dominant_slice(merged),
        "path_kinds": (change or {}).get("kinds", []),
        "dirs": sorted({"/".join(p.split("/")[:2]) for p in paths}),
        "files": paths[:50],
        "error_count": sum(p.get("count", 0) for p in top),
    }


def _jaccard(a, b) -> float | None:
    a, b = set(a), set(b)
    if not a and not b:
        return None  # no information on either side: leave this component out
    return len(a & b) / len(a | b)


def similarity(a: dict, b: dict) -> float:
    parts = {
        "templates": _jaccard(a.get("templates", []), b.get("templates", [])),
        "error_types": _jaccard(a.get("error_types", []), b.get("error_types", [])),
        "slice": _jaccard([f"{k}={v}" for k, v in (a.get("slice") or {}).items()],
                          [f"{k}={v}" for k, v in (b.get("slice") or {}).items()]),
        "paths": _jaccard(a.get("dirs", []) + a.get("path_kinds", []),
                          b.get("dirs", []) + b.get("path_kinds", [])),
    }
    known = {k: v for k, v in parts.items() if v is not None}
    if not known:
        return 0.0
    total_w = sum(WEIGHTS[k] for k in known)
    return round(sum(WEIGHTS[k] * v for k, v in known.items()) / total_w, 3)


def find_similar(sig: dict, past: list[dict], k: int = 3, min_sim: float = 0.3) -> list[dict]:
    """past: incident rows with a decoded `signature`. Best matches first."""
    scored = []
    for inc in past:
        s = similarity(sig, inc.get("signature") or {})
        if s >= min_sim:
            scored.append({"incident_id": inc["id"], "similarity": s, "status": inc["status"],
                           "env": inc["env"], "resolution": inc.get("resolution"), "fix_pr": inc.get("fix_pr"),
                           "revert_pr": inc.get("revert_pr"), "skill": inc.get("skill")})
    scored.sort(key=lambda m: -m["similarity"])
    return scored[:k]


# ── lifecycle ────────────────────────────────────────────────────────────────

def new_incident(ev: dict, sig: dict, matches: list[dict], attribution: str, trigger: str) -> dict:
    """Fields for a new incident row."""
    best = matches[0] if matches else None
    recur = best and best["similarity"] >= RECUR_AT and best["status"] in ("resolved", "guarded", "closed")
    return {
        "env": ev["env"], "status": "open", "trigger": trigger, "attribution": attribution,
        "image": ev.get("image"), "pod_hash": ev.get("canaryHash"), "git_sha": ev.get("gitSha"),
        "source_pr": ev.get("sourcePR"), "signature": sig,
        "matched_incident_id": best["incident_id"] if best else None,
        "similarity": best["similarity"] if best else None,
        "recurrence_of": best["incident_id"] if recur else None,
    }


def pr_from_url(url: str | None) -> str | None:
    m = re.search(r"/pull/(\d+)", url or "")
    return m.group(1) if m else None


def on_report(inc: dict, report: dict) -> dict:
    """The agent finished. A PR (pushed-to or opened) moves the incident to mitigating."""
    upd: dict = {}
    pr = pr_from_url(report.get("pr_url"))
    if pr and report.get("action") in ("pushed_fix_to_pr", "opened_pr"):
        upd["fix_pr"] = pr
        if inc["status"] == "open":
            upd["status"] = "mitigating"
    if report.get("guard_added"):
        upd["guard_added"] = 1
    if report.get("skill") and not inc.get("skill"):
        upd["skill"] = report["skill"]
    return upd


def on_revert_opened(inc: dict, pr: str) -> dict:
    upd = {"revert_pr": pr}
    if inc["status"] == "open":
        upd["status"] = "mitigating"
    return upd


def touches_guard(paths: list[str]) -> bool:
    return any(g in p for p in paths for g in GUARD_PATHS)


def on_rollout_completed(inc: dict, ev: dict, change_paths: list[str] | None = None) -> dict:
    """A rollout passed. It resolves the incident when it is the incident's fix (or revert)
    passing the canary in the incident's own environment."""
    if inc["status"] not in ("open", "mitigating") or ev.get("env") != inc["env"]:
        return {}
    pr = ev.get("sourcePR")
    via = None
    if pr and pr == inc.get("revert_pr"):
        via = "revert"
    elif pr and pr in (inc.get("fix_pr"), inc.get("source_pr")) and ev.get("image") != inc.get("image"):
        via = "fix"
    if not via:
        return {}
    guarded = bool(inc.get("guard_added")) or touches_guard(change_paths or [])
    return {"status": "guarded" if guarded and via == "fix" else "resolved", "resolution": via,
            "fix_image": ev.get("image"), "guard_added": 1 if guarded else 0, "resolved_ts": time.time()}


def should_revert(inc: dict, matches: list[dict], skill: dict | None, change: dict | None,
                  enabled: bool) -> tuple[bool, str]:
    """The pattern-based revert decision. Deterministic and conservative; see PRD safety rules."""
    if not enabled:
        return False, "AUTO_REVERT_PR is off"
    if inc["attribution"] != "hash":
        return False, "only correlated by time; a human decides"
    if inc["env"] != "prod":
        return False, "staging failures are fixed on the PR branch, nothing to revert on main"
    if not change:
        return False, "offending commit unknown"
    if not change.get("revertable", True):
        return False, f"not safely revertable: {change.get('revert_reason')}"
    best = matches[0] if matches else None
    if best and best["similarity"] >= REVERT_MATCH_AT and best.get("resolution") == "revert":
        return True, f"matches incident #{best['incident_id']} ({best['similarity']:.2f}), which a revert resolved"
    if skill and skill.get("revert_preference") == "first":
        return True, f"skill {skill['name']} prefers revert first"
    return False, "no pattern says revert; the agent proposes a fix"
