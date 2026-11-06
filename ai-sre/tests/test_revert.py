import base64
import json

import httpx
import pytest

import revert

REPO = "o/r"


def b64(s):
    return {"content": base64.b64encode(s.encode()).decode(), "encoding": "base64"}


class FakeGitHub:
    """Just enough of the GitHub REST API for revert.py."""

    def __init__(self, files_by_ref, commit_files, parents=("p",)):
        self.files, self.commit_files, self.parents = files_by_ref, commit_files, parents
        self.writes = []

    def __call__(self, req: httpx.Request):
        path = req.url.path.removeprefix(f"/repos/{REPO}")
        if req.method == "GET" and path.startswith("/commits/"):
            return httpx.Response(200, json={"sha": "bad1234567", "parents": [{"sha": p} for p in self.parents],
                                             "commit": {"message": "feat: psp v2 (#10)"},
                                             "files": self.commit_files})
        if req.method == "GET" and path.startswith("/contents/"):
            f = self.files.get(req.url.params["ref"], {}).get(path.removeprefix("/contents/"))
            return httpx.Response(200, json=b64(f)) if f is not None else httpx.Response(404, json={})
        if req.method == "GET" and path == "/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "mainsha"}})
        if req.method == "GET" and path == "/git/commits/mainsha":
            return httpx.Response(200, json={"tree": {"sha": "maintree"}})
        body = json.loads(req.content or b"{}")
        self.writes.append((req.method, path, body))
        if path == "/git/blobs":
            return httpx.Response(201, json={"sha": "blob-" + str(len(self.writes))})
        if path == "/git/trees":
            return httpx.Response(201, json={"sha": "newtree"})
        if path == "/git/commits":
            return httpx.Response(201, json={"sha": "revertsha"})
        if path == "/git/refs":
            return httpx.Response(201, json={})
        if path == "/pulls":
            return httpx.Response(201, json={"number": 12, "html_url": "https://github.com/o/r/pull/12"})
        if path.endswith("/labels"):
            return httpx.Response(200, json=[])
        return httpx.Response(500, json={"unexpected": path})


def make(gh, dry_run=False):
    return revert.Reverter(REPO, "tok", dry_run=dry_run, client=httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(gh)))


FILES = {
    "p": {"apps/payment/money.go": "old money", "deploy/overlays/prod/kustomization.yaml": "tag: v1.0.0"},
    "bad1234567": {"apps/payment/money.go": "bad money", "deploy/overlays/prod/kustomization.yaml": "tag: sha-bad",
                   "apps/payment/new.go": "new file"},
}
FILES["main"] = dict(FILES["bad1234567"])
COMMIT_FILES = [{"filename": "apps/payment/money.go", "status": "modified"},
                {"filename": "deploy/overlays/prod/kustomization.yaml", "status": "modified"},
                {"filename": "apps/payment/new.go", "status": "added"}]


async def test_plan_and_open_pr():
    gh = FakeGitHub(FILES, COMMIT_FILES)
    r = make(gh)
    p = await r.plan("bad1234567")
    assert {f["path"]: f["content"] for f in p["files"]} == {
        "apps/payment/money.go": b"old money", "deploy/overlays/prod/kustomization.yaml": b"tag: v1.0.0",
        "apps/payment/new.go": None}
    out = await r.open_pr("bad1234567", 7, "prod", "matches incident #3")
    assert out["pr"] == "12" and out["branch"] == "ai-sre/revert-bad1234-prod"
    tree = next(b for m, p_, b in gh.writes if p_ == "/git/trees")
    assert tree["base_tree"] == "maintree"
    assert {e["path"]: e["sha"] is None for e in tree["tree"]}["apps/payment/new.go"] is True
    ref = next(b for m, p_, b in gh.writes if p_ == "/git/refs")
    assert ref["ref"] == "refs/heads/ai-sre/revert-bad1234-prod"
    pr = next(b for m, p_, b in gh.writes if p_ == "/pulls")
    assert pr["base"] == "main" and "This reverts" not in pr["title"] and "Incident #7" in pr["body"]


async def test_refuses_when_main_moved_on():
    files = {**FILES, "main": {**FILES["bad1234567"], "apps/payment/money.go": "someone else's later change"}}
    with pytest.raises(revert.RevertRefused, match="changed on main"):
        await make(FakeGitHub(files, COMMIT_FILES)).plan("bad1234567")


async def test_refuses_merge_commits():
    with pytest.raises(revert.RevertRefused, match="single-parent"):
        await make(FakeGitHub(FILES, COMMIT_FILES, parents=("a", "b"))).plan("bad1234567")


async def test_policy_still_applies():
    files = {k: {**v, ".github/workflows/ci.yml": "x" if k != "p" else "y"} for k, v in FILES.items()}
    gh = FakeGitHub(files, COMMIT_FILES + [{"filename": ".github/workflows/ci.yml", "status": "modified"}])
    with pytest.raises(revert.RevertRefused, match="protected path"):
        await make(gh).open_pr("bad1234567", 1, "prod", "r")
    assert gh.writes == []  # refused before any write
    with pytest.raises(revert.RevertRefused, match="dry-run"):
        await make(FakeGitHub(FILES, COMMIT_FILES), dry_run=True).open_pr("bad1234567", 1, "prod", "r")
