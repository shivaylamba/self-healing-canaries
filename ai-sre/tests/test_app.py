"""End to end through the FastAPI app: notifications in, incident lifecycle out.
Loki, GitHub and the model are faked; the ledger, matching, skills and wiring are real."""
import importlib
import time

import pytest
from fastapi.testclient import TestClient
from helpers import CANARY, STABLE, jpy_entries

import patterns
from agent import Report


class FakeReader:
    def __init__(self):
        self.entries = []

    async def errors(self, env, start, end, limit=2000):
        return [e for e in self.entries if env == "prod"]


class FakeTracker:
    async def ensure(self, sha):
        return {"sha": sha + "000000", "message": "feat(payment): PSP v2 (#10)", "kinds": ["code", "deploy"],
                "paths": ["apps/payment/money.go", "deploy/overlays/prod/kustomization.yaml"],
                "revertable": True}


class FakeReverter:
    def __init__(self):
        self.calls = []

    async def open_pr(self, sha, incident_id, env, reason):
        self.calls.append((sha, incident_id, env, reason))
        return {"pr": "99", "url": "https://github.com/o/r/pull/99", "branch": "ai-sre/revert-x-prod", "files": []}


@pytest.fixture
def app_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("INCIDENTS_ENABLED", "true")
    monkeypatch.setenv("LEDGER_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("AI_SRE_DRY_RUN", "false")
    for k in ("GITHUB_TOKEN", "GRAFANA_URL", "AUTO_REVERT_PR"):
        monkeypatch.delenv(k, raising=False)
    import app as app_module
    app_module = importlib.reload(app_module)

    reader = FakeReader()
    app_module.incidents.scanner = patterns.PatternScanner(app_module.ledger, reader)
    app_module.incidents.tracker = FakeTracker()
    seen = []

    async def fake_investigate(ev, ledger, on_step, ctx=None):
        seen.append(ctx)
        on_step({"kind": "tool", "name": "query_loki_logs", "args": "{}"})
        return Report(root_cause="toMinorUnits multiplies JPY by 100.", offending_change="money.go",
                      evidence=["Loki"], fix_summary="zero-decimal currencies", action="opened_pr",
                      pr_url="https://github.com/o/r/pull/11", confidence="high",
                      skill=ctx["skill"]["name"] if ctx else None, guard_added=True)

    monkeypatch.setattr(app_module, "investigate", fake_investigate)

    async def no_loop(*_a, **_k):  # background polling is tested separately; keep the test deterministic
        return None
    monkeypatch.setattr(app_module.incidents, "changes_loop", no_loop)
    monkeypatch.setattr(app_module.incidents, "scan_loop", no_loop)
    app_module._reader, app_module._seen = reader, seen
    return app_module


def wait_for(client, inc_id, pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        r = client.get(f"/api/incidents/{inc_id}")
        if r.status_code == 200 and pred(r.json()):
            return r.json()
        time.sleep(0.05)
    raise AssertionError(client.get(f"/api/incidents/{inc_id}").json())


def abort(image, canary=CANARY, pr="10"):
    return {"event": "aborted", "env": "prod", "image": image, "canaryHash": canary, "stableHash": STABLE,
            "gitSha": "bad1234", "sourcePR": pr, "sourceBranch": "feat/x"}


def test_full_lifecycle_recurrence_and_revert(app_mod):
    app_mod._reader.entries = jpy_entries(t0=time.time() - 120)
    with TestClient(app_mod.app) as c:
        # 1. prod canary aborts -> incident opens, agent gets the context, PR moves it to mitigating
        assert c.post("/hooks/rollout", json=abort("img:sha-bad")).json() == {"incident": "opening"}
        inc = wait_for(c, 1, lambda i: i["status"] == "mitigating")
        assert inc["attribution"] == "hash" and inc["skill"] == "currency-locale" and inc["fix_pr"] == "11"
        assert inc["signature"]["slice"] == {"checkout_currency": "JPY", "checkout_region": "ap-northeast"}
        assert [e["kind"] for e in inc["events"]][:4] == ["opened", "revert_decision", "investigation_started",
                                                          "agent_report"]
        ctx = app_mod._seen[0]
        assert ctx["new_patterns"][0]["template"].endswith("for JPY") and ctx["change"]["kinds"] == ["code", "deploy"]

        # 2. the fix passes staging (not yet resolved), then prod (resolved, guarded: a test was added)
        c.post("/hooks/rollout", json={"event": "completed", "env": "staging", "image": "img:sha-fix",
                                       "canaryHash": "f1", "sourcePR": "11"})
        c.post("/hooks/rollout", json={"event": "completed", "env": "prod", "image": "img:sha-fix",
                                       "canaryHash": "f1", "sourcePR": "11"})
        inc = wait_for(c, 1, lambda i: i["status"] == "guarded")
        kinds = [e["kind"] for e in inc["events"]]
        assert "fix_passed_other_env" in kinds and inc["resolution"] == "fix" and inc["resolved_ts"]

        # 3. the same failure comes back on a new commit: flagged as a recurrence, matched to #1
        app_mod._reader.entries = jpy_entries(t0=time.time() - 60, h="c2c2c2c2")
        c.post("/hooks/rollout", json=abort("img:sha-bad2", canary="c2c2c2c2", pr="20"))
        inc2 = wait_for(c, 2, lambda i: i["status"] == "mitigating")
        assert inc2["matched_incident_id"] == 1 and inc2["recurrence_of"] == 1
        ctx2 = app_mod._seen[1]
        assert ctx2["similar"][0]["root_cause"] == "toMinorUnits multiplies JPY by 100."
        decision = next(e for e in inc2["events"] if e["kind"] == "revert_decision")["detail"]
        assert decision["revert"] is False  # AUTO_REVERT_PR is off

        # 4. with auto-revert on and a past incident that a revert resolved, the pattern triggers a revert PR
        app_mod.ledger.update_incident(1, resolution="revert")
        app_mod.incidents.auto_revert, app_mod.incidents.reverter = True, FakeReverter()
        app_mod._reader.entries = jpy_entries(t0=time.time() - 60, h="c3c3c3c3")
        c.post("/hooks/rollout", json=abort("img:sha-bad3", canary="c3c3c3c3", pr="30"))
        inc3 = wait_for(c, 3, lambda i: any(e["kind"] == "revert_opened" for e in i["events"]))
        assert inc3["revert_pr"] == "99" and app_mod.incidents.reverter.calls[0][2] == "prod"
        assert app_mod._seen[-1]["revert"]["pr"] == "99"


def test_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("INCIDENTS_ENABLED", raising=False)
    monkeypatch.setenv("LEDGER_PATH", str(tmp_path / "l.db"))
    import app as app_module
    app_module = importlib.reload(app_module)
    assert app_module.incidents is None
    with TestClient(app_module.app) as c:
        assert c.get("/api/incidents").json() == {"enabled": False, "incidents": []}


def test_policy_unchanged_and_diagnosis_only(app_mod):
    with TestClient(app_mod.app) as c:
        def ask(tool, args):
            return c.post("/hitl/policy", json={"items": [{"tool_name": tool, "arguments": args}]}).json()

        repo = {"owner": "shivaylamba", "repo": "self-healing-canaries"}
        assert ask("push_files", {**repo, "branch": "main", "files": [{"path": "a.go"}]})["approved"] is False
        assert ask("push_files", {**repo, "branch": "fix", "files": [{"path": ".github/x.yml"}]})["approved"] is False
        assert ask("push_files", {**repo, "branch": "fix", "files": [{"path": "apps/payment/a.go"}]})["approved"]
        app_mod._active.add(42)
        app_mod._diagnosis_only.add(42)
        r = ask("push_files", {**repo, "branch": "fix", "files": [{"path": "apps/payment/a.go"}]})
        assert r["approved"] is False and "diagnosis only" in r["reason"]
