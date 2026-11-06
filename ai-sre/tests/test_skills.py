import skills


def test_all_skills_parse():
    names = {s["name"] for s in skills.load()}
    assert names == {"dependency-contract", "currency-locale", "config-drift", "resource-exhaustion",
                     "latency-regression", "generic"}
    for s in skills.load():
        assert s["body"].startswith("# Skill"), s["name"]


def test_select_by_signature():
    jpy = {"templates": ["psp amount <*> exceeds limit <*> for JPY"], "error_types": ["charge_failed"],
           "slice": {"checkout_currency": "JPY"}, "path_kinds": ["code"], "files": ["apps/payment/money.go"]}
    inv = {"templates": ["inventory <*> <*> has no top-level available field"],
           "error_types": ["inventory_lookup_failed"], "slice": {"checkout_sku": "sku-7"},
           "path_kinds": ["code"], "files": ["apps/payment/inventory.go"]}
    cfg = {"templates": ["config PSP_URL not set"], "error_types": [], "slice": {},
           "path_kinds": ["config"], "files": ["deploy/overlays/prod/payment.env"]}
    assert skills.select(jpy)[0]["name"] == "currency-locale"
    assert skills.select(inv)[0]["name"] == "dependency-contract"
    assert skills.select(cfg)[0]["name"] == "config-drift"
    assert skills.select({})[0]["name"] == "generic"
