"""Acceptance tests, stage 12 (AT-S12-*). VIX jump = flag only (Ilay, 20.10)."""
from datetime import date, timedelta

import pytest

from qt.calc.portfolio import (Portfolio, PortfolioStop, sixty_forty_option_sell_fraction, sixty_forty_stock_stop,
                               tighten_only, winner_action)
from qt.calc.volatility import (Bar, atr_20_60_series, current_regime, monthly_from_annual, regime_transitions,
                                vix_check)
from qt.rules.checks import Context, TradeProposal, evaluate
from qt.rules.engine import Severity, Status


def test_at_s12_1_portfolio_stop_latches_until_ilay_releases():
    ps = PortfolioStop(peak=60_000)
    assert ps.update(50_700) is True
    assert ps.drawdown_pct(50_700) == pytest.approx(15.50)
    assert ps.update(51_500) is True                       # back above -15%: still latched
    with pytest.raises(PermissionError):
        ps.release(51_500, approved_by_ilay=False, reassessment_note="x")
    ps.release(51_500, approved_by_ilay=True, reassessment_note="documented reassessment")
    assert ps.trigger_level() == pytest.approx(43_775)
    fresh = PortfolioStop(peak=60_000)
    assert fresh.update(51_500) is False


def test_portfolio_stop_blocks_entries_not_exits():
    ps = PortfolioStop(peak=60_000)
    ps.update(50_000)
    ctx = Context(Portfolio(50_000), portfolio_stop=ps)
    entry = evaluate(TradeProposal("X", "long", entry=10, stop=8.8, target=14, target_source="technical_level",
                                   horizon_months=2, adr_pct=3, requested_pct=6), ctx)
    assert "S12-R1" in entry.rule_ids(Severity.BLOCK)
    assert evaluate(TradeProposal("X", "long", action="exit"), ctx).status is Status.READY_FOR_APPROVAL


def test_at_s12_2_sixty_forty():
    assert sixty_forty_stock_stop(10, 9, 13) == pytest.approx(11.80)
    assert sixty_forty_stock_stop(10, 9, 12.9) is None                 # below 3R
    assert sixty_forty_option_sell_fraction(2.00, 5.00) == pytest.approx(0.36)
    assert sixty_forty_stock_stop(10, 9, 20) == pytest.approx(16.0)
    assert sixty_forty_stock_stop(100, 110, 70, "short") == pytest.approx(82.0)


def test_stop_never_moves_away():
    assert tighten_only(11.8, 11.0) == 11.8
    assert tighten_only(90, 95, "short") == 90


@pytest.mark.parametrize("w,act", [(18, "none"), (24, "rebalance_flag"), (31, "rebalance_mandatory")])
def test_at_s12_3_growing_winner(w, act):
    assert winner_action(w) == act


def test_at_s12_5_vix_jump_is_flag_only():
    closes = [15.0] * 20 + [21.0]
    r = vix_check(closes)
    assert r.ratio_to_low == pytest.approx(1.40)
    assert r.jump and r.above_mean
    assert not vix_check([15.0] * 20 + [20.9]).jump
    p = TradeProposal("X", "long", entry=10, stop=8.8, target=14, target_source="technical_level",
                      horizon_months=2, adr_pct=3, requested_pct=6)
    v = evaluate(p, Context(Portfolio(100_000), vix=r))
    vix = [f for f in v.findings if f.rule_id == "S12-R24"]
    assert vix and vix[0].severity is Severity.FLAG
    assert v.status is Status.READY_FOR_APPROVAL          # no regime switch, no block


def test_at_s12_6_vix_monthly_move():
    assert monthly_from_annual(20) == pytest.approx(5.77, abs=0.01)
    assert monthly_from_annual(25) == pytest.approx(7.22, abs=0.01)


def _bars(ranges):
    d0 = date(2020, 1, 1)
    return [Bar(d0 + timedelta(days=i), 100, 100 + r, 100, 100) for i, r in enumerate(ranges)]


def test_at_s12_7_atr_20_60_regime_hysteresis_synthetic():
    # calm, then a volatility spike, then calm again (synthetic shape of 2020)
    ranges = [1.0] * 80 + [3.0] * 15 + [1.0] * 60 + [0.5] * 40
    series = atr_20_60_series(_bars(ranges))
    ev = regime_transitions(series)
    assert [e.regime for e in ev] == ["tactical", "portfolio"]
    # switch to tactical the day the spread crosses +0.325 (subtraction, not division)
    up = ev[0]
    prev = dict(series)[up.day - timedelta(days=1)]
    assert prev <= 0.325 < up.value
    # going back below 0 is not enough: needs -0.325
    assert ev[1].value < -0.325
    assert current_regime(series)[0] == "portfolio"


def test_tactical_regime_blocks_new_risk_only():
    ctx = Context(Portfolio(100_000), regime="tactical")
    p = TradeProposal("X", "long", entry=10, stop=8.8, target=14, target_source="technical_level",
                      horizon_months=2, adr_pct=3, requested_pct=6)
    assert "S12-R3" in evaluate(p, ctx).rule_ids(Severity.BLOCK)
    assert evaluate(TradeProposal("X", "long", action="reduce"), ctx).status is Status.READY_FOR_APPROVAL


def test_at_s12_7_sp500_2023_series_is_computable(bars):
    gspc = bars("GSPC_2023.json")
    series = atr_20_60_series(gspc)
    assert series and all(abs(v) < 3 for _, v in series)
    # TODO(AT-S12-7): full check needs S&P 500 daily since 2020 (March 2020 peak ~2.9%, May 2020 low ~-1.6%)
