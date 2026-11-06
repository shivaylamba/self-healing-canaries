from helpers import CANARY, STABLE, entry, inventory_entries, jpy_entries

import patterns
from ledger import Ledger


def test_tokenize_masks_variables():
    assert patterns.tokenize('user "bob" paid 12.50 on sku-7: ok') == ["user", "<*>", "paid", "<*>", "on", "<*>", "ok"]


def test_miner_merges_variants_and_separates_shapes():
    m = patterns.TemplateMiner()
    a, _, _ = m.add("psp: amount 100000 exceeds limit 50000 for JPY")
    b, _, _ = m.add("psp: amount 250000 exceeds limit 50000 for KRW")
    c, _, _ = m.add("inventory v2: sku-7 has no top-level available field")
    assert a == b != c
    assert m.template(a) == "psp amount <*> exceeds limit <*> for <*>"


def test_ingest_attributes_new_pattern_to_canary_only(tmp_path):
    led = Ledger(str(tmp_path / "l.db"))
    scanner = patterns.PatternScanner(led, reader=None)
    stable_noise = [entry(1_000_000.0 + i, "inventory timeout after 2s", STABLE, "inventory_lookup_failed")
                    for i in range(3)]
    new = scanner.ingest("prod", jpy_entries() + stable_noise + [
        entry(1_000_001.0, "inventory timeout after 3s", CANARY, "inventory_lookup_failed")])
    assert {p["template"] for p in new} == {"psp amount <*> exceeds limit <*> for JPY",
                                            "inventory timeout after <*>"}

    only_canary = patterns.new_on_canary(led, "prod", CANARY, STABLE)
    assert [p["template"] for p in only_canary] == ["psp amount <*> exceeds limit <*> for JPY"]
    p = only_canary[0]
    assert p["count"] == 30 and p["error_type"] == "charge_failed"
    assert patterns.dominant_slice(p["slices"]) == {"checkout_currency": "JPY", "checkout_region": "ap-northeast"}


def test_rescanning_overlapping_range_does_not_double_count(tmp_path):
    led = Ledger(str(tmp_path / "l.db"))
    scanner = patterns.PatternScanner(led, reader=None)
    scanner.ingest("staging", inventory_entries())
    scanner.ingest("staging", inventory_entries())
    assert led.patterns_for_hash("staging", CANARY)[0]["count"] == 30


def test_pattern_ids_survive_restart(tmp_path):
    led = Ledger(str(tmp_path / "l.db"))
    first = patterns.PatternScanner(led, reader=None).ingest("staging", inventory_entries())
    again = patterns.PatternScanner(led, reader=None).ingest("staging", inventory_entries(t0=2_000_000.0))
    assert again == []  # same template, same id: nothing new
    assert len(led.all_patterns()) == len(first) == 1


async def test_loki_reader_parses_streams():
    import httpx

    def handler(req):
        assert req.url.path == "/api/datasources/proxy/uid/loki-prod/loki/api/v1/query_range"
        return httpx.Response(200, json={"data": {"result": [{
            "stream": {"rollouts_pod_template_hash": CANARY, "error_type": "charge_failed"},
            "values": [["1000000000000000000", "boom 1"]]}]}})

    r = patterns.LokiReader("http://grafana", "u", "p",
                            client=httpx.AsyncClient(base_url="http://grafana", transport=httpx.MockTransport(handler)))
    out = await r.errors("prod", 0, 1)
    assert out == [{"ts": 1e9, "line": "boom 1", "labels": {"rollouts_pod_template_hash": CANARY,
                                                              "error_type": "charge_failed"}}]


def test_dominant_slice_allows_a_few_values():
    sl = {"checkout_sku": {"sku-7": 10, "sku-8": 10, "sku-9": 9, "sku-1": 1},
          "checkout_currency": {"USD": 5, "EUR": 5, "JPY": 5, "GBP": 5}}
    assert patterns.dominant_slice(sl) == {"checkout_sku": "sku-7|sku-8|sku-9"}
