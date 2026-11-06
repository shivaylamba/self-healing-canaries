"""Change tracker (PRD FR1): every commit on main, what kind of change it is, and
whether it can be reverted safely.

Most incidents start with a change. The ledger already knows which *image* failed;
this module knows which *commit* (and so which files, and which PR) that image came
from, and keeps the history so an incident can be lined up against everything that
changed before it.
"""
from __future__ import annotations

import fnmatch
import logging
import re
import time
from datetime import datetime, timezone

import httpx

log = logging.getLogger("ai-sre.changes")

# Ordered: first match wins for a path.
KIND_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("ci", (".github/*",)),
    ("test", ("*_test.go", "*/tests/*", "*/test_*.py")),
    ("config", ("*loadgen.env", "*.env", "deploy/*/release.yaml", "*configmap*.yaml")),
    ("deploy", ("deploy/*",)),
    ("infra", ("platform/*", "scripts/*")),
    ("code", ("apps/*", "ai-sre/*.py", "demo-ui/*")),
    ("docs", ("*.md", "docs/*", "slides*")),
]

# A revert of any of these can't restore the old world by itself (data was written,
# or the change is to the guardrails), so it goes to a human instead.
REVERT_BLOCKERS: list[tuple[str, str]] = [
    ("*migration*", "touches a migration; data written in the new shape stays"),
    ("*schema*", "touches a schema"),
    ("*.sql", "touches SQL"),
    (".github/*", "changes CI or repository workflows"),
    ("apps/payment/pspsandbox/*", "changes the simulated vendor PSP"),
    ("ai-sre/*", "changes the AI SRE itself"),
]

_PR_RE = re.compile(r"\(#(\d+)\)|Merge pull request #(\d+)")


def classify(paths: list[str]) -> list[str]:
    kinds: list[str] = []
    for p in paths:
        for kind, globs in KIND_RULES:
            if any(fnmatch.fnmatch(p, g) for g in globs):
                if kind not in kinds:
                    kinds.append(kind)
                break
        else:
            if "other" not in kinds:
                kinds.append("other")
    return kinds


def revert_safety(paths: list[str], message: str = "", parents: int = 1) -> tuple[bool, str | None]:
    """(revertable, reason-if-not)."""
    if parents > 1:
        return False, "merge commit with several parents; revert the PR's squash commit instead"
    if message.lower().startswith("revert"):
        return False, "already a revert; reverting it would re-apply the original change"
    if not paths:
        return False, "no files changed"
    for p in paths:
        for glob, why in REVERT_BLOCKERS:
            if fnmatch.fnmatch(p, glob):
                return False, f"{p} {why}"
    return True, None


def pr_number(message: str) -> str | None:
    m = _PR_RE.search(message or "")
    return (m.group(1) or m.group(2)) if m else None


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def to_change(detail: dict) -> dict:
    """GitHub's GET /commits/{sha} payload -> a changes row."""
    paths = [f["filename"] for f in detail.get("files", [])]
    msg = detail["commit"]["message"]
    parents = detail.get("parents", [])
    ok, why = revert_safety(paths, msg, len(parents))
    return {
        "sha": detail["sha"],
        "ts": _ts(detail["commit"]["committer"]["date"]),
        "author": (detail.get("author") or {}).get("login") or detail["commit"]["author"]["name"],
        "message": msg.splitlines()[0][:300],
        "pr": pr_number(msg),
        "paths": paths,
        "kinds": classify(paths),
        "parent": parents[0]["sha"] if parents else None,
        "revertable": ok,
        "revert_reason": why,
    }


class ChangeTracker:
    """Polls GitHub for new commits on a branch and stores them in the ledger."""

    def __init__(self, ledger, repo: str, token: str, branch: str = "main",
                 client: httpx.AsyncClient | None = None, api: str = "https://api.github.com"):
        self.ledger, self.repo, self.branch = ledger, repo, branch
        self.client = client or httpx.AsyncClient(
            base_url=api, timeout=20,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
        )

    async def poll(self, lookback_s: int = 7 * 86400) -> int:
        """Fetch commits newer than the newest one stored. Returns how many were added."""
        since = self.ledger.latest_change_ts() or (time.time() - lookback_s)
        iso = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = await self.client.get(f"/repos/{self.repo}/commits",
                                  params={"sha": self.branch, "since": iso, "per_page": 50})
        r.raise_for_status()
        added = 0
        for c in r.json():
            if self.ledger.change(c["sha"]) and self.ledger.change(c["sha"])["sha"] == c["sha"]:
                continue
            await self.fetch(c["sha"])
            added += 1
        return added

    async def fetch(self, sha: str) -> dict:
        """Fetch one commit (any branch) and store it."""
        d = await self.client.get(f"/repos/{self.repo}/commits/{sha}")
        d.raise_for_status()
        change = to_change(d.json())
        self.ledger.upsert_change(change)
        return change

    async def ensure(self, sha: str | None) -> dict | None:
        """The stored change for a SHA, fetching it if we haven't seen it (e.g. a PR branch commit)."""
        if not sha:
            return None
        return self.ledger.change(sha) or await self.fetch(sha)
