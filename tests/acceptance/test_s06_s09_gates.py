"""Floors (S06, decision 11) and the traffic light (S09, decision 16, fix 9)."""
import pytest

from qt.calc.portfolio import Portfolio
from qt.calc.traffic_light import aggregate, combine, price_behavior_color
from qt.rules.checks import Context, TradeProposal, check_floors, evaluate
from qt.rules.engine import Severity


@pytest.mark.parametrize("kw,rule", [
    (dict(direction="long", market_cap=0.4e9), "S06-R6"),
    (dict(direction="long", price=4.5), "S06-R9"),
    (dict(direction="long", revenue=280e6), "S06-R10"),
    (dict(direction="short", market_cap=9e9), "S06-R8"),
])
def test_floors_block(kw, rule):
    d = kw.pop("direction")
    assert [f.rule_id for f in check_floors(TradeProposal("X", d, **kw), None)] == [rule]


def test_short_floor_is_10b_not_30b():
    # BBY / BLDR, spring 2023, $10-20B: the lecturer's own shorts pass
    assert check_floors(TradeProposal("BLDR", "short", market_cap=15e9), None) == []


def test_acmr_long_floor_passes():
    assert check_floors(TradeProposal("ACMR", "long", market_cap=0.6e9, price=10.7, revenue=388e6), None) == []


G, O, R = "green", "orange", "red"


def src(**kw):
    s = dict(levels=G, price_behavior=G, sma=G, rsi=G, macd=G, psar=G, volatility_stop=G)
    s.update(kw)
    return s


@pytest.mark.parametrize("sources,veto,color", [
    (src(), False, G),
    (src(rsi=O), False, G),                         # 2 of 3 daily still green
    (src(rsi=O, macd=O), False, O),
    (src(price_behavior=R), False, R),              # long-term price behavior red => red
    (src(), True, R),                               # high-interval veto (S09-R7)
    (src(levels=R, sma=R, rsi=R, macd=R), False, R),  # 4 of 7 red
    (src(levels=R, sma=R, rsi=R), False, O),
])
def test_traffic_light_aggregate(sources, veto, color):
    assert aggregate(sources, veto) == color


def test_price_behavior_intervals():
    assert price_behavior_color(dict(year=True, quarter=True, month=True, week=True)) == G
    assert price_behavior_color(dict(year=True, quarter=True, month=True, week=False)) == O
    assert price_behavior_color(dict(year=True, quarter=True, month=False, week=False)) == R


def test_combine_stock_sector_market():
    assert combine(G, G, R) == O
    assert combine(G, R, R) == R
    assert combine(O, G, G) == O


def test_red_light_blocks_orange_allows_initial_only():
    base = dict(entry=10, stop=8.8, target=14, target_source="technical_level", horizon_months=2, adr_pct=3)
    ctx = Context(Portfolio(100_000))
    assert "S09-R13" in evaluate(TradeProposal("X", "long", traffic_light=R, requested_pct=6, **base), ctx).rule_ids(Severity.BLOCK)
    assert "S09-R10" in evaluate(TradeProposal("X", "long", traffic_light=O, stage="full", requested_pct=12, **base), ctx).rule_ids(Severity.BLOCK)
    assert evaluate(TradeProposal("X", "long", traffic_light=O, stage="initial", requested_pct=6, **base), ctx).blocks == []
