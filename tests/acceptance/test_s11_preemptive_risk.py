"""Acceptance tests, stage 11 (AT-S11-*), under the 20.10 decisions: k in 3x..4x, default 4x, hard minimum 3x."""
import pytest

from qt.calc import trade as T
from qt.calc.portfolio import Portfolio, Position, leverage_loss_pct
from qt.calc.volatility import annual_from_monthly
from qt.rules.checks import Context, OptionLeg, TradeProposal, check_options, check_sectors, evaluate, review_position
from qt.rules.engine import Severity, Status, decide


def stock(entry, stop, **kw):
    base = dict(target=entry * 1.6, target_source="technical_level", horizon_months=2, adr_pct=3.0,
                requested_pct=6, sector="XLK")
    base.update(kw)
    return TradeProposal("TEST", "long", entry=entry, stop=stop, **base)


def test_at_s11_1_atr_stop_264_default():
    assert T.stop_price(300, 3.0, 4) == pytest.approx(264.0)
    assert T.stop_price(300, 3.0, 3) == pytest.approx(273.0)
    ok = evaluate(stock(300, 264), Context(Portfolio(100_000)))
    assert ok.status is Status.READY_FOR_APPROVAL
    # 3x..4x needs a written reason (DC-19), then passes
    mid = evaluate(stock(300, 273), Context(Portfolio(100_000)))
    assert "DC-19" in mid.rule_ids(Severity.NEEDS_OVERRIDE)
    mid_ok = evaluate(stock(300, 273, reasons={"DC-19": "high daily range"}), Context(Portfolio(100_000)))
    assert mid_ok.status is Status.READY_FOR_APPROVAL
    # chart-based stop at 287 (4.33%) is blocked: below 3x and drawn from the chart
    bad = evaluate(stock(300, 287, stop_basis="chart"), Context(Portfolio(100_000)))
    assert {"S11-R11", "S11-R10"} <= bad.rule_ids(Severity.BLOCK)


def test_at_s11_2_risk_per_trade():
    assert T.risk_pct(12, 12) == pytest.approx(1.44)
    assert T.risk_pct(12, 30) == pytest.approx(3.60)
    sz = T.size_position(100_000, 50, 30, "full", requested_pct=12)
    assert sz.allowed_pct == pytest.approx(10.00)
    assert sz.reduced_by_risk_cap


def test_at_s11_3_twelve_percent_cap():
    p = stock(50, 44, requested_pct=15)
    v = evaluate(p, Context(Portfolio(25_000)))
    f = [f for f in v.blocks if f.rule_id == "S11-R6"][0]
    assert f.data["max_qty"] == 60


def test_at_s11_4_existing_position_over_12_is_a_flag_not_a_block():
    pos = Position("SWKS", "long", "XLK", 3_500)
    findings = review_position(pos, Context(Portfolio(25_000, [pos])))
    v = decide("review", findings)
    assert [f.rule_id for f in findings] == ["S11-R6"]
    assert v.status is Status.READY_FOR_APPROVAL


def test_at_s11_5_gross_and_net():
    p1 = Portfolio(50_000, [Position("L", "long", "XLK", 40_000), Position("S", "short", "XLF", 20_000)])
    assert p1.gross() == pytest.approx(1.20)
    assert p1.net() == pytest.approx(0.40)
    p2 = Portfolio(50_000, [Position("L", "long", "XLK", 80_000), Position("S", "short", "XLF", 20_000)])
    assert p2.gross() == pytest.approx(2.00)
    v = evaluate(stock(50, 44, sector="XLE", requested_pct=1), Context(p2))
    assert "S11-R15" in v.rule_ids(Severity.BLOCK)
    # Ilay raised the limit explicitly to 1.5: still blocked at 2.0
    assert "S11-R15" in evaluate(stock(50, 44, sector="XLE", requested_pct=1), Context(p2, gross_limit=1.5)).rule_ids(Severity.BLOCK)


def test_at_s11_6_leverage_x4():
    assert leverage_loss_pct(4, 25) == 100


def test_at_s11_7_beta():
    p = Portfolio(100, [Position(str(i), "long", "XLK", 20, beta=b) for i, b in enumerate([1.3, 1.2, 0.8, 1.0])]
                  + [Position("s", "short", "XLF", 20, beta=1.1)])
    assert p.beta() == pytest.approx(0.64)


@pytest.mark.parametrize("drop,loss", [(10, 263), (20, 526), (30, 789)])
def test_at_s11_8_scenarios(drop, loss):
    assert 2_630 * drop / 100 == pytest.approx(loss, abs=3)


def test_at_s11_9_sector_concentration():
    three_long = Portfolio(100_000, [Position(s, "long", s, 10_000) for s in ("XLK", "XLF", "XLE")])
    held = TradeProposal("N", "long", sector="XLK")
    new = TradeProposal("N", "long", sector="XLV")
    assert check_sectors(held, Context(three_long))[0].kind == "violation"
    assert check_sectors(new, Context(three_long)) == []          # adding a new sector is allowed (fix 6)
    with_shorts = Portfolio(100_000, [Position("A", "long", "XLK", 10_000), Position("B", "short", "XLF", 10_000),
                                      Position("C", "short", "XLE", 10_000)])
    f = check_sectors(held, Context(with_shorts))
    assert f[0].kind == "approvable" and f[0].approval_key == "three_sector_exception"
    four = Portfolio(100_000, [Position(s, "long", s, 10_000) for s in ("XLK", "XLF", "XLE", "XLV")])
    assert check_sectors(held, Context(four)) == []


def test_at_s11_10_options_caps():
    existing = [Position("O1", "long", "XLK", 6_000, "option", entry_cost=6_000, days_to_expiry=20),
                Position("O2", "long", "XLF", 3_000, "option", entry_cost=3_000, days_to_expiry=90)]
    ctx = Context(Portfolio(50_000, existing))
    ten = TradeProposal("BA", "short", instrument="option", option=OptionLeg(310, 10, 60, 3.84))
    f = check_options(ten, ctx)
    assert f[0].data["total_pct"] == pytest.approx(24.2)
    assert f[0].data["max_contracts"] == 3
    three = TradeProposal("BA", "short", instrument="option", option=OptionLeg(310, 3, 60, 3.84))
    assert check_options(three, ctx) == []
    more_short = TradeProposal("X", "long", instrument="option", option=OptionLeg(2_000, 1, 25, 3.5))
    assert any(f.data.get("short_pct") == pytest.approx(16.0) for f in check_options(more_short, ctx))
    # blocked until Ilay approves this specific case (small-account exception)
    v = decide("enter", check_options(ten, ctx))
    assert v.status is Status.AWAITING_APPROVAL
    v2 = decide("enter", check_options(ten, ctx), approvals=["small_account_options_override:BA"])
    assert v2.status is Status.READY_FOR_APPROVAL


def test_at_s11_11_swks_quarterly_atr_stop():
    adr60 = 3.69
    assert 4 * adr60 == pytest.approx(14.76)
    assert 3 * adr60 == pytest.approx(11.07)
    for stop_pct, blocked in ((4, True), (8, True), (14.76, False)):
        v = evaluate(stock(100, 100 - stop_pct, adr_pct=adr60, requested_pct=12), Context(Portfolio(100_000)))
        assert ("S11-R11" in v.rule_ids(Severity.BLOCK)) is blocked
    assert T.risk_pct(12, 14.76) == pytest.approx(1.77, abs=0.01)
    assert 3 / 14.76 * 100 == pytest.approx(20.33, abs=0.01)


def test_at_s11_12_sigma_conversions():
    assert annual_from_monthly(9.5) == pytest.approx(32.91, abs=0.01)
    assert annual_from_monthly(2.5) == pytest.approx(8.66, abs=0.01)
