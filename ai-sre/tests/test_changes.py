import changes


def test_classify():
    assert changes.classify(["apps/payment/money.go", "apps/payment/money_test.go",
                             "deploy/overlays/staging/loadgen.env", "deploy/overlays/prod/kustomization.yaml",
                             "platform/grafana/x.json", "README.md"]) == ["code", "test", "config", "deploy",
                                                                         "infra", "docs"]


def test_revert_safety():
    assert changes.revert_safety(["apps/payment/money.go", "deploy/overlays/prod/kustomization.yaml"]) == (True, None)
    ok, why = changes.revert_safety(["apps/payment/db/migrations/002.sql"])
    assert not ok and "migration" in why
    assert not changes.revert_safety([".github/workflows/ci.yml"])[0]
    assert not changes.revert_safety(["apps/payment/money.go"], message="Revert \"feat: x\"")[0]
    assert not changes.revert_safety(["apps/payment/money.go"], parents=2)[0]


def test_to_change_from_github_payload():
    c = changes.to_change({
        "sha": "abc1234def", "parents": [{"sha": "p1"}],
        "author": {"login": "shivay"},
        "commit": {"message": "feat(payment): migrate to inventory v2 + PSP v2 (#10)\n\nbody",
                   "committer": {"date": "2026-11-06T22:06:52Z"}, "author": {"name": "S"}},
        "files": [{"filename": "apps/payment/money.go"}, {"filename": "deploy/overlays/prod/kustomization.yaml"}],
    })
    assert c["pr"] == "10" and c["kinds"] == ["code", "deploy"] and c["revertable"] and c["parent"] == "p1"
    assert c["message"] == "feat(payment): migrate to inventory v2 + PSP v2 (#10)"


async def test_tracker_poll_and_ensure(tmp_path):
    import httpx

    from ledger import Ledger

    def handler(req):
        if req.url.path.endswith("/commits"):
            return httpx.Response(200, json=[{"sha": "aaa111"}])
        sha = req.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={
            "sha": sha, "parents": [{"sha": "p"}], "author": None,
            "commit": {"message": f"change {sha}", "committer": {"date": "2026-11-06T10:00:00Z"},
                       "author": {"name": "n"}},
            "files": [{"filename": "apps/payment/main.go"}]})

    led = Ledger(str(tmp_path / "l.db"))
    t = changes.ChangeTracker(led, "o/r", "tok", client=httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)))
    assert await t.poll() == 1
    assert await t.poll() == 0
    assert led.change("aaa")["sha"] == "aaa111"
    assert (await t.ensure("bbb222"))["kinds"] == ["code"]
