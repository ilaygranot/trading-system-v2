"""Acceptance tests, stage 10 (playbook/acceptance-tests.md, AT-S10-*)."""
from datetime import time

import pytest

from qt.calc import trade as T
from qt.calc.portfolio import Portfolio
from qt.calc.volatility import monthly_from_annual
from qt.rules.checks import Context, TradeProposal, evaluate
from qt.rules.engine import Severity, Status


def test_at_s10_1_ba_spread_a():
    s = T.VerticalSpread("put", 190, 7.20, 170, 2.80)
    assert s.cost == pytest.approx(4.40)
    assert s.width == 20
    assert s.max_profit == pytest.approx(15.60)
    assert s.ratio == pytest.approx(3.55, abs=0.01)


def test_at_s10_2_ba_spread_b_corrects_lecturer_arithmetic():
    s = T.VerticalSpread("put", 185, 5.90, 170, 2.80)
    assert s.cost == pytest.approx(3.10)
    assert s.max_profit == pytest.approx(11.90)   # transcript said 12.9
    assert s.ratio == pytest.approx(3.84, abs=0.01)
    assert s.ratio < 4
    a = T.VerticalSpread("put", 190, 7.20, 170, 2.80)
    assert (1 - s.cost / a.cost) * 100 == pytest.approx(29.55, abs=0.01)


def test_at_s10_4_ba_direct_short_blocked_by_1_to_3():
    p = TradeProposal("BA", "short", entry=198, stop=218, target=178, target_source="technical_level",
                      horizon_months=2, adr_pct=1.0, requested_pct=6, market_cap=120e9, sector="XLI",
                      reasons={"DC-19": "n/a"})
    v = evaluate(p, Context(Portfolio(10_000)))
    assert v.status is Status.BLOCKED
    rr = [f for f in v.blocks if f.rule_id == "S10-R3"]
    assert rr and rr[0].data["ratio"] == pytest.approx(1.0)
    assert "option" in rr[0].message


def test_at_s10_5_bldr_spread():
    s = T.VerticalSpread("put", 85, 4.00, 70, 1.50)
    assert s.cost == pytest.approx(2.50)
    assert s.max_profit == pytest.approx(12.50)
    assert s.ratio == pytest.approx(5.00)       # lecturer's "1:6" was width / cost
    assert s.width / s.cost == pytest.approx(6.0)


def test_at_s10_6_zs_call():
    assert T.single_option_ratio("call", 120, 3.0, 135) == pytest.approx(4.00)


@pytest.mark.parametrize("months,chain,expected", [
    (2.2, "", Status.READY_FOR_APPROVAL),                                 # BA put, July
    (3.5, "", Status.NEEDS_OVERRIDE),                                     # BLDR August, no chain written
    (3.5, "earnings cycle + housing data + analysts", Status.READY_FOR_APPROVAL),
])
def test_at_s10_7_horizon(months, chain, expected):
    p = TradeProposal("BLDR", "short", instrument="option", target=70, target_source="technical_level",
                      horizon_months=months, catalyst_chain=chain, market_cap=15e9, sector="XLI",
                      option=__import__("qt.rules.checks", fromlist=["OptionLeg"]).OptionLeg(250, 2, 100, 5.0))
    v = evaluate(p, Context(Portfolio(50_000)))
    assert v.status is expected
    if chain:
        assert any(f.rule_id == "D-S10-5" and f.severity is Severity.FLAG for f in v.findings)


def test_at_s10_8_first_half_hour_is_default_rule_and_needs_override():
    p = TradeProposal("XYZ", "long", entry=50, stop=44, target=70, target_source="technical_level",
                      horizon_months=2, adr_pct=3.0, requested_pct=6, planned_time_ny=time(9, 40))
    v = evaluate(p, Context(Portfolio(100_000)))
    assert "S10-R25" in v.rule_ids(Severity.NEEDS_OVERRIDE)
    exit_ = evaluate(TradeProposal("XYZ", "long", action="exit", planned_time_ny=time(9, 40)), Context(Portfolio(100_000)))
    assert exit_.status is Status.READY_FOR_APPROVAL


def test_at_s10_9_acmr_cagr_is_61_not_50():
    assert T.cagr_pct(36, 388, 5) == pytest.approx(60.9, abs=0.1)


def test_at_s10_10_ratio_trade_return():
    assert T.pct_move(2.50, 7.44) == pytest.approx(197.60, abs=0.01)


def test_at_s10_11_iv_to_monthly_divides_by_sqrt12():
    assert monthly_from_annual(47.46) == pytest.approx(13.70, abs=0.02)
    assert monthly_from_annual(50.07) == pytest.approx(14.45, abs=0.02)
    assert monthly_from_annual(47.46) != pytest.approx(47.46 / 12, abs=0.5)


def test_at_s10_12_swks_target_sets_horizon_not_block():
    # synthetic stop/ADR chosen so 1:3 passes and only the horizon is tested
    p = TradeProposal("SWKS", "long", entry=100, stop=87, target=140, target_source="own_multiple_target",
                      horizon_months=1, monthly_sigma_pct=9.5, adr_pct=3.25, requested_pct=6, catalyst_chain="")
    v = evaluate(p, Context(Portfolio(100_000)))
    realism = [f for f in v.findings if f.rule_id == "S10-R35"]
    assert realism and realism[0].data["implied_months"] == pytest.approx(4.21, abs=0.01)
    assert realism[0].severity is Severity.FLAG
    # 4.2 months > 3 => needs a catalyst chain, never a hard block
    assert "D-S10-5" in v.rule_ids(Severity.NEEDS_OVERRIDE)
    assert v.status is not Status.BLOCKED
