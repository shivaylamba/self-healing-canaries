"""The incident system (docs/prd-incident-memory.md): ties the change tracker, the log
pattern scanner, signatures and matching, skills and the deterministic revert into the
rollout lifecycle. app.py calls it; everything here works without a model.

Flags (env):
  INCIDENTS_ENABLED       master switch (default false: the demo's behaviour is unchanged)
  AUTO_REVERT_PR          let a matching pattern open a revert PR (default false; humans still merge)
  PROACTIVE_INVESTIGATE   investigate new patterns seen outside a rollout, diagnosis only (default false)
  PATTERN_SCAN_SECS       scan interval (default 60)
  CHANGES_POLL_SECS       commit poll interval (default 60)
  NEW_PATTERN_MIN_COUNT   errors before a new pattern opens an incident on its own (default 20)
"""
from __future__ import annotations

import asyncio
import logging
import os

import httpx

import incidents as inc_lib
import patterns as pat_lib
import skills as skill_lib

log = logging.getLogger("ai-sre.incidents")


def _flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() == "true"


class IncidentService:
    def __init__(self, ledger, tracker=None, scanner=None, reverter=None,
                 auto_revert: bool | None = None, proactive: bool | None = None,
                 skills: list[dict] | None = None):
        self.ledger, self.tracker, self.scanner, self.reverter = ledger, tracker, scanner, reverter
        self.auto_revert = _flag("AUTO_REVERT_PR") if auto_revert is None else auto_revert
        self.proactive = _flag("PROACTIVE_INVESTIGATE") if proactive is None else proactive
        self.skills = skills if skills is not None else skill_lib.load()
        self.new_pattern_min = int(os.environ.get("NEW_PATTERN_MIN_COUNT", "20"))

    # ── rollout aborted ──────────────────────────────────────────────────────
    async def on_abort(self, ev: dict) -> dict:
        """Open (or join) the incident for a failed canary. Returns the agent's context."""
        env, canary, stable = ev["env"], ev.get("canaryHash"), ev.get("stableHash")
        if (existing := self.ledger.active_incident(env, image=ev.get("image"))):
            self.ledger.add_incident_event(existing["id"], "aborted_again", {"canary": canary})
            return self._ctx(existing, [], None, [], None)

        if self.scanner and canary:
            try:  # pick up the canary's last minutes even if the background scan hasn't yet
                await self.scanner.scan(env, lookback_s=900)
            except httpx.HTTPError as e:
                log.warning("pattern scan on abort failed: %s", e)
        new = pat_lib.new_on_canary(self.ledger, env, canary, stable) if canary else []

        change = None
        if self.tracker and ev.get("gitSha"):
            try:
                change = await self.tracker.ensure(ev["gitSha"])
            except httpx.HTTPError as e:
                log.warning("change lookup failed: %s", e)

        sig = inc_lib.signature(env, new, change)
        matches = inc_lib.find_similar(sig, self.ledger.closed_incidents())
        skill, score = skill_lib.select(sig, self.skills)
        # Hash attribution needs the pattern to be on the canary's ReplicaSet only. A canary that
        # failed the gate with no new pattern (e.g. latency) is still attributed by hash: the gate
        # itself measured that ReplicaSet alone.
        fields = inc_lib.new_incident(ev, sig, matches, attribution="hash" if canary else "temporal",
                                      trigger="rollout_aborted")
        fields["skill"] = skill["name"]
        inc_id = self.ledger.create_incident(**fields)
        inc = self.ledger.incident(inc_id)
        self.ledger.add_incident_event(inc_id, "opened", {
            "trigger": "rollout_aborted", "canary": canary, "new_patterns": len(new),
            "skill": skill["name"], "skill_score": score,
            "match": matches[0] if matches else None, "recurrence_of": inc.get("recurrence_of")})

        revert = None
        ok, why = inc_lib.should_revert(inc, matches, skill, change, self.auto_revert and self.reverter is not None)
        self.ledger.add_incident_event(inc_id, "revert_decision", {"revert": ok, "reason": why})
        if ok:
            try:
                revert = await self.reverter.open_pr(change["sha"], inc_id, env, why)
                self.ledger.update_incident(inc_id, **inc_lib.on_revert_opened(inc, revert["pr"]))
                self.ledger.add_incident_event(inc_id, "revert_opened", revert)
            except Exception as e:  # noqa: BLE001 — a refused revert is an event, not a crash
                self.ledger.add_incident_event(inc_id, "revert_refused", {"reason": str(e)})
                log.warning("revert for incident #%s refused: %s", inc_id, e)
        return self._ctx(self.ledger.incident(inc_id), new, change, matches, revert, skill, score)

    def _ctx(self, inc, new, change, matches, revert, skill=None, score=0) -> dict:
        skill = skill or skill_lib.get(inc.get("skill") or "generic", self.skills)
        return {
            "incident_id": inc["id"], "attribution": inc["attribution"], "signature": inc.get("signature"),
            "new_patterns": new, "change": change, "similar": [self._with_root_cause(m) for m in matches],
            "revert": revert, "skill": skill, "skill_score": score,
            "diagnosis_only": inc["attribution"] == "temporal",
        }

    def _with_root_cause(self, m: dict) -> dict:
        past = self.ledger.incident(m["incident_id"]) or {}
        inv = self.ledger.investigation(past["investigation_id"]) if past.get("investigation_id") else None
        rep = (inv or {}).get("report") or {}
        return {**m, "root_cause": rep.get("root_cause"), "fix_summary": rep.get("fix_summary")}

    def link_investigation(self, inc_id: int, inv_id: int) -> None:
        self.ledger.update_incident(inc_id, investigation_id=inv_id)
        self.ledger.add_incident_event(inc_id, "investigation_started", {"investigation": inv_id})

    def on_report(self, inc_id: int, report: dict) -> None:
        inc = self.ledger.incident(inc_id)
        if not inc:
            return
        upd = inc_lib.on_report(inc, report)
        self.ledger.update_incident(inc_id, **upd)
        self.ledger.add_incident_event(inc_id, "agent_report", {
            "action": report.get("action"), "pr_url": report.get("pr_url"),
            "confidence": report.get("confidence"), "guard_added": report.get("guard_added")})

    def on_investigation_error(self, inc_id: int, error: str) -> None:
        self.ledger.add_incident_event(inc_id, "investigation_failed", {"error": error})

    # ── rollout completed ────────────────────────────────────────────────────
    async def on_completed(self, ev: dict) -> list[int]:
        """Resolve incidents whose fix or revert just passed the canary in their env."""
        pr = ev.get("sourcePR")
        if not pr:
            return []
        resolved = []
        for inc in self.ledger.incidents_for_pr(pr):
            paths: list[str] = []
            if self.tracker and ev.get("gitSha"):
                try:
                    paths = (await self.tracker.ensure(ev["gitSha"]) or {}).get("paths", [])
                except httpx.HTTPError:
                    pass
            upd = inc_lib.on_rollout_completed(inc, ev, paths)
            if upd:
                self.ledger.update_incident(inc["id"], **upd)
                self.ledger.add_incident_event(inc["id"], upd["status"], {
                    "via": upd["resolution"], "image": ev.get("image"), "pr": pr})
                resolved.append(inc["id"])
            elif ev.get("env") != inc["env"]:
                self.ledger.add_incident_event(inc["id"], "fix_passed_other_env",
                                               {"env": ev.get("env"), "image": ev.get("image"), "pr": pr})
        return resolved

    # ── background loops ─────────────────────────────────────────────────────
    async def changes_loop(self, every: int):
        while True:
            try:
                if (n := await self.tracker.poll()):
                    log.info("changes: %d new commit(s)", n)
            except Exception as e:  # noqa: BLE001
                log.warning("changes poll failed: %s", e)
            await asyncio.sleep(every)

    async def scan_loop(self, every: int, start_investigation):
        while True:
            for env in self.scanner.envs:
                try:
                    for p in await self.scanner.scan(env):
                        await self.on_new_pattern(p, start_investigation)
                except Exception as e:  # noqa: BLE001
                    log.warning("pattern scan %s failed: %s", env, e)
            await asyncio.sleep(every)

    async def on_new_pattern(self, p: dict, start_investigation) -> int | None:
        """A template never seen before, outside any failed rollout. Opens a time-correlated
        incident once it's frequent enough; never reverts."""
        if p["count"] < self.new_pattern_min or self.ledger.active_incident(p["env"]):
            return None
        since = self.ledger.hash_first_seen(p["env"], p["pod_hash"]) or 0
        recent = self.ledger.recent_changes(since - 6 * 3600, limit=10) if since else []
        sig = inc_lib.signature(p["env"], [p], recent[0] if recent else None)
        matches = inc_lib.find_similar(sig, self.ledger.closed_incidents())
        skill, score = skill_lib.select(sig, self.skills)
        ev = {"event": "new_pattern", "env": p["env"], "image": None, "canaryHash": p["pod_hash"]}
        fields = inc_lib.new_incident(ev, sig, matches, attribution="temporal", trigger="new_pattern")
        fields.update(skill=skill["name"], pod_hash=p["pod_hash"])
        inc_id = self.ledger.create_incident(**fields)
        self.ledger.add_incident_event(inc_id, "opened", {
            "trigger": "new_pattern", "template": p["template"], "count": p["count"],
            "recent_changes": [c["sha"][:7] for c in recent], "skill": skill["name"]})
        if self.proactive and start_investigation:
            ctx = self._ctx(self.ledger.incident(inc_id), [p], recent[0] if recent else None, matches, None,
                            skill, score)
            start_investigation({**ev, "image": f"pattern:{p['id']}"}, ctx)
        return inc_id
