"""Pattern-based revert (PRD FR6). Deterministic code, not the model.

When an incident is attributed to a commit by the canary's hash and its pattern says
"revert" (see incidents.should_revert), this opens a PR on a new branch that puts every
file the offending commit touched back to its parent version. A human still merges it.
The agent then works on the real fix; the revert is mitigation, the fix is resolution.

Safety:
  * every write is checked by policy.check, the same rules the agent's writes go through;
  * it refuses when any of those files changed on main after the offending commit
    (restoring the parent version would silently undo someone else's work);
  * protected deploy/overlays/prod/ files may be written only because the content is the
    exact previous version: the image prod ran before the bad merge.

    python revert.py --plan <sha>      read-only: what would be reverted, and is it safe
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os

import httpx

import policy

PROD_OVERLAY = ("deploy/overlays/prod/",)


class RevertRefused(Exception):
    pass


class Reverter:
    def __init__(self, repo: str, token: str, client: httpx.AsyncClient | None = None,
                 api: str = "https://api.github.com", dry_run: bool | None = None):
        self.repo, self.dry_run = repo, dry_run
        self.owner, self.name = repo.split("/", 1)
        self.c = client or httpx.AsyncClient(
            base_url=api, timeout=30,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
        )

    async def _json(self, method: str, url: str, **kw):
        r = await self.c.request(method, f"/repos/{self.repo}{url}", **kw)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json() if r.content else {}

    async def _content(self, path: str, ref: str) -> bytes | None:
        d = await self._json("GET", f"/contents/{path}", params={"ref": ref})
        if d is None:
            return None
        return base64.b64decode(d["content"]) if d.get("encoding") == "base64" else d.get("content", "").encode()

    async def plan(self, sha: str, base: str = "main") -> dict:
        """Read-only. What a revert of `sha` onto `base` would write, or why it can't."""
        commit = await self._json("GET", f"/commits/{sha}")
        if commit is None:
            raise RevertRefused(f"commit {sha} not found")
        parents = commit.get("parents", [])
        if len(parents) != 1:
            raise RevertRefused("only single-parent (squash) commits are reverted automatically")
        parent = parents[0]["sha"]
        files, conflicts = [], []
        for f in commit.get("files", []):
            path = f["filename"]
            after = await self._content(path, sha)     # what the bad commit left
            now = await self._content(path, base)      # what main has today
            if after != now:
                conflicts.append(path)
                continue
            if f["status"] in ("added", "renamed"):
                files.append({"path": path, "status": f["status"], "content": None})  # delete the new path
                if f["status"] == "renamed" and f.get("previous_filename"):
                    old = f["previous_filename"]
                    files.append({"path": old, "status": "restore", "content": await self._content(old, parent)})
            else:  # modified / removed: bring back the parent's version
                files.append({"path": path, "status": f["status"], "content": await self._content(path, parent)})
        if conflicts:
            raise RevertRefused("changed on main since the offending commit: " + ", ".join(conflicts))
        return {"sha": commit["sha"], "parent": parent, "title": commit["commit"]["message"].splitlines()[0],
                "files": files}

    def _check(self, tool: str, args: dict) -> None:
        reason = policy.check(tool, {"owner": self.owner, "repo": self.name, **args},
                              allow_paths=PROD_OVERLAY, dry_run=self.dry_run, repo_name=self.repo)
        if reason:
            raise RevertRefused(f"policy: {tool}: {reason}")

    async def open_pr(self, sha: str, incident_id: int, env: str, reason: str, base: str = "main") -> dict:
        p = await self.plan(sha, base)
        branch = f"ai-sre/revert-{p['sha'][:7]}-{env}"
        self._check("create_branch", {"branch": branch})
        self._check("push_files", {"branch": branch, "files": [{"path": f["path"]} for f in p["files"]]})
        self._check("create_pull_request", {"head": branch})

        ref = await self._json("GET", f"/git/ref/heads/{base}")
        base_sha = ref["object"]["sha"]
        base_tree = (await self._json("GET", f"/git/commits/{base_sha}"))["tree"]["sha"]
        tree = []
        for f in p["files"]:
            if f["content"] is None:  # the commit added this file: delete it
                tree.append({"path": f["path"], "mode": "100644", "type": "blob", "sha": None})
            else:
                blob = await self._json("POST", "/git/blobs", json={
                    "content": base64.b64encode(f["content"]).decode(), "encoding": "base64"})
                tree.append({"path": f["path"], "mode": "100644", "type": "blob", "sha": blob["sha"]})
        new_tree = await self._json("POST", "/git/trees", json={"base_tree": base_tree, "tree": tree})
        msg = f"revert: {p['title']}\n\nThis reverts commit {p['sha']}.\nIncident #{incident_id} ({env}): {reason}"
        commit = await self._json("POST", "/git/commits", json={
            "message": msg, "tree": new_tree["sha"], "parents": [base_sha]})
        await self._json("POST", "/git/refs", json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
        pr = await self._json("POST", "/pulls", json={
            "title": f"revert: {p['title']} (incident #{incident_id}, pattern match)",
            "head": branch, "base": base, "body": _body(p, incident_id, env, reason)})
        try:
            await self._json("POST", f"/issues/{pr['number']}/labels", json={"labels": ["auto-revert"]})
        except httpx.HTTPError:
            pass  # the label is a convenience
        return {"pr": str(pr["number"]), "url": pr["html_url"], "branch": branch,
                "files": [f["path"] for f in p["files"]]}


def _body(p: dict, incident_id: int, env: str, reason: str) -> str:
    files = "\n".join(f"- `{f['path']}` ({'delete' if f['content'] is None else 'restore previous version'})"
                      for f in p["files"])
    return (
        f"## Why\nIncident #{incident_id} in **{env}**: {reason}.\n\n"
        f"The failing log pattern appeared only on the canary ReplicaSet built from `{p['sha'][:7]}`, so the "
        f"incident is attributed to that commit by hash, not by timing.\n\n"
        f"## What\nReverts `{p['sha'][:7]}` ({p['title']}): every file it touched goes back to its parent "
        f"`{p['parent'][:7]}` version.\n{files}\n\n"
        "## Verification\nCI builds this revert like any PR, the staging canary judges it, and a human merges. "
        "The AI SRE is separately preparing the root-cause fix.\n\n"
        "— AI SRE incident system (deterministic revert; no model wrote this diff)"
    )


async def _cli():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", metavar="SHA", required=True, help="print what would be reverted; writes nothing")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPO", "shivaylamba/self-healing-canaries"))
    a = ap.parse_args()
    r = Reverter(a.repo, os.environ["GITHUB_TOKEN"])
    try:
        p = await r.plan(a.plan)
        print(json.dumps({"safe": True, "sha": p["sha"], "title": p["title"],
                          "files": [{"path": f["path"], "action": "delete" if f["content"] is None else "restore"}
                                    for f in p["files"]]}, indent=2))
    except RevertRefused as e:
        print(json.dumps({"safe": False, "reason": str(e)}, indent=2))


if __name__ == "__main__":
    asyncio.run(_cli())
