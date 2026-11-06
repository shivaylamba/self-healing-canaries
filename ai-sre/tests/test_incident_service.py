from helpers import STABLE, jpy_entries

import patterns
from incident_service import IncidentService
from ledger import Ledger


async def test_new_pattern_outside_rollout_is_temporal_and_diagnosis_only(tmp_path):
    led = Ledger(str(tmp_path / "l.db"))
    scanner = patterns.PatternScanner(led, reader=None)
    svc = IncidentService(led, scanner=scanner, auto_revert=True, proactive=True, skills=None)
    started = []

    few = scanner.ingest("prod", jpy_entries(n=5, h=STABLE))
    assert await svc.on_new_pattern(few[0], lambda ev, ctx: started.append(ctx)) is None  # below threshold

    led2 = Ledger(str(tmp_path / "l2.db"))
    scanner2 = patterns.PatternScanner(led2, reader=None)
    svc2 = IncidentService(led2, scanner=scanner2, auto_revert=True, proactive=True)
    many = scanner2.ingest("prod", jpy_entries(n=40, h=STABLE))
    inc_id = await svc2.on_new_pattern(many[0], lambda ev, ctx: started.append(ctx))
    inc = led2.incident(inc_id)
    assert inc["attribution"] == "temporal" and inc["trigger"] == "new_pattern" and inc["skill"] == "currency-locale"
    assert started[0]["diagnosis_only"] is True and started[0]["revert"] is None
    # one open incident per env at a time
    assert await svc2.on_new_pattern(many[0], lambda ev, ctx: None) is None
