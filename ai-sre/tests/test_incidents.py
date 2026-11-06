import incidents as I

JPY_SIG = {"env": "prod", "templates": ["psp amount <*> exceeds limit <*> for JPY"], "error_types": ["charge_failed"],
           "slice": {"checkout_currency": "JPY"}, "path_kinds": ["code", "deploy"], "dirs": ["apps/payment"],
           "files": ["apps/payment/money.go"]}
INV_SIG = {"env": "staging", "templates": ["inventory <*> <*> has no top-level available field"],
           "error_types": ["inventory_lookup_failed"], "slice": {"checkout_sku": "sku-7"},
           "path_kinds": ["code"], "dirs": ["apps/payment"], "files": ["apps/payment/inventory.go"]}


def test_signature_from_patterns_and_change():
    pats = [{"template": "t1", "error_type": "charge_failed", "count": 30,
             "slices": {"checkout_currency": {"JPY": 30}, "checkout_sku": {"sku-1": 10, "sku-2": 20}}}]
    sig = I.signature("prod", pats, {"paths": ["apps/payment/money.go"], "kinds": ["code"]})
    assert sig["slice"] == {"checkout_currency": "JPY"}  # sku is spread out, so it isn't a slice
    assert sig["dirs"] == ["apps/payment"] and sig["error_count"] == 30


def test_similarity():
    assert I.similarity(JPY_SIG, JPY_SIG) == 1.0
    assert I.similarity(JPY_SIG, INV_SIG) < 0.2
    assert I.similarity({}, {}) == 0.0
    # same failure, different change: still close
    other = {**JPY_SIG, "dirs": ["apps/checkout"], "path_kinds": ["code"]}
    assert I.similarity(JPY_SIG, other) >= 0.85


def test_find_similar_ranks_and_filters():
    past = [{"id": 1, "status": "resolved", "env": "prod", "signature": INV_SIG, "resolution": "fix"},
            {"id": 2, "status": "guarded", "env": "prod", "signature": JPY_SIG, "resolution": "fix", "fix_pr": "11"}]
    m = I.find_similar(JPY_SIG, past)
    assert [x["incident_id"] for x in m] == [2] and m[0]["fix_pr"] == "11"


def test_new_incident_flags_recurrence():
    ev = {"env": "prod", "image": "img:b", "canaryHash": "h2", "gitSha": "s", "sourcePR": "20"}
    match = [{"incident_id": 2, "similarity": 0.95, "status": "guarded"}]
    f = I.new_incident(ev, JPY_SIG, match, "hash", "rollout_aborted")
    assert f["recurrence_of"] == 2 and f["status"] == "open" and f["pod_hash"] == "h2"
    weak = [{"incident_id": 2, "similarity": 0.5, "status": "guarded"}]
    assert I.new_incident(ev, JPY_SIG, weak, "hash", "rollout_aborted")["recurrence_of"] is None


def test_staging_lifecycle_fix_pushed_to_same_pr():
    inc = {"id": 1, "env": "staging", "status": "open", "image": "img:bad", "source_pr": "10"}
    upd = I.on_report(inc, {"action": "pushed_fix_to_pr", "pr_url": "https://github.com/o/r/pull/10",
                            "guard_added": True, "skill": "dependency-contract"})
    assert upd == {"fix_pr": "10", "status": "mitigating", "guard_added": 1, "skill": "dependency-contract"}
    inc.update(upd)
    # the bad image passing again (a retry) doesn't count as a fix
    assert I.on_rollout_completed(inc, {"env": "staging", "sourcePR": "10", "image": "img:bad"}) == {}
    done = I.on_rollout_completed(inc, {"env": "staging", "sourcePR": "10", "image": "img:fixed"})
    assert done["status"] == "guarded" and done["resolution"] == "fix" and done["fix_image"] == "img:fixed"


def test_prod_lifecycle_needs_prod_pass():
    inc = {"id": 2, "env": "prod", "status": "mitigating", "image": "img:bad", "source_pr": "10", "fix_pr": "11"}
    assert I.on_rollout_completed(inc, {"env": "staging", "sourcePR": "11", "image": "img:fix"}) == {}
    done = I.on_rollout_completed(inc, {"env": "prod", "sourcePR": "11", "image": "img:fix"},
                                  change_paths=["deploy/overlays/staging/loadgen.env"])
    assert done["status"] == "guarded"
    unguarded = I.on_rollout_completed(inc, {"env": "prod", "sourcePR": "11", "image": "img:fix"},
                                       change_paths=["apps/payment/money.go"])
    assert unguarded["status"] == "resolved"


def test_revert_resolution():
    inc = {"id": 3, "env": "prod", "status": "open", "image": "img:bad", "source_pr": "10"}
    inc.update(I.on_revert_opened(inc, "12"))
    assert inc["status"] == "mitigating"
    done = I.on_rollout_completed(inc, {"env": "prod", "sourcePR": "12", "image": "img:reverted"})
    assert done["resolution"] == "revert" and done["status"] == "resolved"


def test_should_revert_matrix():
    inc = {"attribution": "hash", "env": "prod"}
    change = {"sha": "abc", "revertable": True}
    revert_match = [{"incident_id": 9, "similarity": 0.9, "resolution": "revert"}]
    fix_match = [{"incident_id": 9, "similarity": 0.9, "resolution": "fix"}]
    assert I.should_revert(inc, revert_match, None, change, enabled=True)[0]
    assert not I.should_revert(inc, revert_match, None, change, enabled=False)[0]
    assert not I.should_revert({**inc, "attribution": "temporal"}, revert_match, None, change, True)[0]
    assert not I.should_revert({**inc, "env": "staging"}, revert_match, None, change, True)[0]
    assert not I.should_revert(inc, revert_match, None, {**change, "revertable": False, "revert_reason": "x"}, True)[0]
    assert not I.should_revert(inc, fix_match, None, change, True)[0]
    assert I.should_revert(inc, [], {"name": "config-drift", "revert_preference": "first"}, change, True)[0]
