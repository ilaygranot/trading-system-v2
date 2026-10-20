"""The lecturer's trades under the 20.10 rules (audit-rerun.md section 6). Expected:
SYM and ACMR pass only at k=3 (high ADR); TSLA, META, ZS, BA-stock blocked by 1:3 at any k;
the BA and BLDR put spreads pass; SPOT/AEO blocked by process; APPS depends on the revenue date."""
from datetime import date

import pytest

from qt.calc.portfolio import Portfolio
from qt.replay import CASES, run_stock_case, spread_cases
from qt.rules.checks import Context, TradeProposal, check_floors, evaluate
from qt.rules.engine import Severity, Status

FILES = {"BA": "BA_prices_2023.json"}
BY = {c.symbol: c for c in CASES}


def run(bars, sym, k):
    c = BY[sym]
    return run_stock_case(c, bars(FILES.get(sym, f"{sym}_daily_2022_2024.json")), k)


@pytest.mark.parametrize("sym,lo,hi", [("SYM", 7.0, 8.0), ("ACMR", 5.1, 5.6), ("TSLA", 3.8, 4.2),
                                       ("META", 2.4, 2.8), ("ZS", 4.0, 4.4), ("BA", 2.45, 2.55)])
def test_measured_adr60_matches_audit(bars, sym, lo, hi):
    assert lo <= run(bars, sym, 4).adr60 <= hi


@pytest.mark.parametrize("sym", ["SYM", "ACMR"])
def test_high_adr_cases_pass_only_at_k3(bars, sym):
    at4, at3 = run(bars, sym, 4), run(bars, sym, 3)
    assert "S10-R3" in at4.verdict.rule_ids(Severity.BLOCK)
    assert at3.ratio >= 3
    assert at3.verdict.blocks == []
    assert at3.verdict.status is Status.READY_FOR_APPROVAL


def test_sym_target_from_analyst_is_flagged(bars):
    v = run(bars, "SYM", 3).verdict
    assert any(f.rule_id == "S10-R3" and f.severity is Severity.FLAG for f in v.findings)


@pytest.mark.parametrize("sym", ["TSLA", "META", "ZS", "BA"])
def test_near_targets_blocked_at_any_k(bars, sym):
    for k in (4, 3):
        r = run(bars, sym, k)
        assert r.ratio < 3
        assert "S10-R3" in r.verdict.rule_ids(Severity.BLOCK)


def test_ba_stock_short_ratio(bars):
    r = run(bars, "BA", 4)
    assert r.stop == pytest.approx(217.7, abs=0.1)
    assert r.ratio == pytest.approx(1.42, abs=0.01)


def test_lecturer_spreads_pass():
    res = {name: (ratio, v) for name, _, ratio, v in spread_cases()}
    ba_ratio, ba = res["BA puts 185/170 (101)"]
    bldr_ratio, bldr = res["BLDR puts 85/70 (100)"]
    assert ba_ratio == pytest.approx(3.84, abs=0.01) and ba.status is Status.READY_FOR_APPROVAL
    assert bldr_ratio == pytest.approx(5.00, abs=0.01) and bldr.status is Status.READY_FOR_APPROVAL


def test_spot_and_aeo_blocked_by_process():
    base = dict(entry=175, stop=150, target=260, target_source="technical_level", horizon_months=2, adr_pct=4.5,
                requested_pct=6)
    spot = evaluate(TradeProposal("SPOT", "long", on_watchlist=False, **base), Context(Portfolio(100_000)))
    aeo = evaluate(TradeProposal("AEO", "long", process_complete=False, **base), Context(Portfolio(100_000)))
    assert "S08-R2" in spot.rule_ids(Severity.BLOCK)
    assert "S13-R1" in aeo.rule_ids(Severity.BLOCK)


@pytest.mark.parametrize("revenue,blocked", [(138.7e6, True), (313.6e6, False)])
def test_apps_revenue_floor_depends_on_date(revenue, blocked):
    f = check_floors(TradeProposal("APPS", "long", revenue=revenue), None)
    assert bool(f) is blocked
