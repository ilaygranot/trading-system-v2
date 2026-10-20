"""Stages 1-3: global macro, the macro stance, market state (AT-S01-*, AT-S02-*, AT-S03-*)."""
import pytest

from qt import macro as M
from qt.calc.portfolio import Portfolio
from qt.rules.checks import Context, TradeProposal, check_macro_alignment, check_scope, evaluate
from qt.rules.engine import Severity


# ---------------------------------------------------------------- stage 1

def test_at_s01_1_spread_and_direction():
    assert M.spread(10, 1) == 9.00
    assert M.spread(10, 2) == 8.00
    series = [8.0] * 300 + [8 + 8 * i / 125 for i in range(1, 126)]       # 8 -> 16 in half a year
    s = M.credit_stress(series)
    assert s.value == pytest.approx(16.00)
    assert s.dist_trend == "growing" and s.state != "calm" and s.state == "panic"
    assert "yoy_change_positive" in s.flags


def test_credit_stress_states():
    calm = M.credit_stress([5.0 - 0.001 * i for i in range(400)])
    assert calm.state == "calm"
    # above the MA but coming back down: easing (S01-J3)
    up_then_down = [4.0] * 300 + [4 + 0.05 * i for i in range(60)] + [7 - 0.03 * i for i in range(40)]
    assert M.credit_stress(up_then_down).state == "easing"


def test_at_s01_2_inverted_curve():
    c = M.yield_curve([3.0] * 21, [5.0] * 21)
    assert c.value == pytest.approx(-2.00, abs=0.05) and c.shape == "inverted" and c.recession_flag
    assert M.yield_curve([4.0] * 21, [1.0] * 21).shape == "normal"


def test_at_s01_4_commodities_extreme():
    now = {"copper": 110, "lumber": 150, "wti": 120, "corn": 80}
    before = {"copper": 40, "lumber": 60, "wti": -10, "corn": 20}
    sig = M.commodities(now, before)
    assert sig.extreme and sig.direction == "up" and set(sig.shocks) == {"copper", "lumber", "wti"}


def test_at_s01_5_real_rate():
    assert M.real_rate(3, 5) == (-2, True)


def test_stage1_consistency():
    stress = M.credit_stress([5.0 - 0.001 * i for i in range(400)])
    normal = M.yield_curve([4.0], [1.0])
    assert M.stage1_consistency(stress, normal, "+") == "agree"
    assert M.stage1_consistency(stress, normal, "-") == "conflict"


# ---------------------------------------------------------------- stage 2

def test_at_s02_1_ism_services_3():
    assert M.score_indicator("ism_nmi", 55.1, 55.5, 56.1, 56.5, 50).score == 3
    assert M.score_indicator("ism_nmi", 55.1, 55.5, 56.1, 58.4, 50).score == 3


def test_at_s02_2_small_business_0():
    assert M.score_indicator("small_business", 90.9, 91.9, 91.8, 95.7, 97.0).score == 0


def test_at_s02_3_4_michigan_and_permits_7():
    assert M.score_indicator("michigan", 67, 56.8, 58.2, 62.8, 85).score == 7
    assert M.score_indicator("permits", 1550, 1400, 1500, 1800, 1500).score == 7


def test_reference_levels():
    assert M.reference_level("ism_pmi", [40, 60]) == 50
    assert M.reference_level("permits", list(range(1, 25))) == pytest.approx(12.5)
    assert M.reference_level("michigan", [80, 82, 84, 86]) == pytest.approx(86)       # trend line at the end
    s = M.score_series("permits", [1.0] * 12 + [2.0])
    assert s.level_ok and s.yoy_pos and s.score == 10


def _scores_2023():
    return [M.score_indicator("ism_nmi", 55.1, 55.5, 56.1, 56.5, 50),
            M.score_indicator("ism_pmi", 47.7, 49.0, 52.8, 58.6, 50),
            M.score_indicator("michigan", 67, 56.8, 58.2, 62.8, 85),
            M.score_indicator("permits", 1550, 1400, 1500, 1800, 1500),
            M.score_indicator("small_business", 90.9, 91.9, 91.8, 95.7, 97.0)]


def test_at_s02_5_6_total_17_cold_bearish():
    sc = M.activity_scale(_scores_2023())
    assert [s.score for s in _scores_2023()] == [3, 0, 7, 7, 0]
    assert (sc.total, sc.max, round(sc.pct, 2), sc.zone, sc.bias) == (17, 50, 34.00, "cold", "bearish")


def test_at_s02_6_counting_six_fails():
    with pytest.raises(M.ScaleError):
        M.activity_scale(_scores_2023() + [M.score_indicator("ism_pmi", 47.7, 49, 52.8, 58.6, 50)])


@pytest.mark.parametrize("total,zone,bias", [
    (4, "extreme_cold", "cautious_optimism"), (8, "grey_low", None), (10, "grey_low", None),
    (24, "cold", "bearish"), (25, "hot", "bullish"), (39, "hot", "bullish"), (40, "grey_high", None),
    (46, "extreme_heat", "neutral"),
])
def test_at_s02_7_zones(total, zone, bias):
    z, cand = M.zone_of(100 * total / 50)
    assert z == zone
    assert (M.bias_of(cand[0]) if len(cand) == 1 else None) == bias


def test_grey_band_resolution():
    scores = [M.IndicatorScore(n, 0, 0, 0, 0, 0, True, True, True, True, 8, "expansion")
              for n in ("ism_pmi", "ism_nmi", "michigan", "permits", "small_business")]   # 40/50 = 80%
    sc = M.activity_scale(scores)
    assert sc.zone == "grey_high" and sc.bias is None and sc.candidates == ("hot", "extreme_heat")
    assert M.resolve_grey(sc, "extreme_heat", None) == ("extreme_heat", "neutral")
    assert M.resolve_grey(sc, None, "extreme_heat") == ("extreme_heat", "neutral")    # last month's zone
    assert M.resolve_grey(sc, None, "cold") == ("hot", "bullish")                    # else the non-extreme
    with pytest.raises(ValueError):
        M.resolve_grey(sc, "cold", None)
    st = M.stance(scores)
    assert st.bias is None and st.cards[0] == "DC-01"


def test_at_s02_8_ffr_not_in_activity_scale():
    with pytest.raises(M.ScaleError, match="S02-R43"):
        M.score_indicator("ffr", 4.6, 3.8, 2.3, 0.1, 2.0)
    ffr = [0.1] * 3 + [0.1, 0.3, 0.8, 1.2, 1.7, 2.3, 2.6, 3.1, 3.8, 4.1, 4.3, 4.6]
    fed = M.fed_scale(ffr, [5.0] * 20 + [-1.0], [8.9, 8.9] + [8.8 - 0.05 * i for i in range(13)])
    assert fed.components["ffr_down"] is False and fed.points == 0
    assert (fed.period, fed.state) == ("tightening", "acting_continuing")
    st = M.stance(_scores_2023(), fed=fed)
    assert st.bias == "bearish"                    # the Fed doesn't change the bias, only confidence


def test_fed_against_bias_lowers_confidence():
    scores = [M.IndicatorScore(n, 0, 0, 0, 0, 0, True, True, True, True, 7, "expansion")
              for n in ("ism_pmi", "ism_nmi", "michigan", "permits", "small_business")]   # 70%: hot
    fed = M.fed_scale([1.0] * 3 + [1.0 + 0.25 * i for i in range(12)], [5.0] * 15, [1.0] * 15)
    st = M.stance(scores, fed=fed)
    assert st.bias == "bullish" and st.fed_against_bias and "fed_against_bias_lower_confidence" in st.flags


@pytest.mark.parametrize("values,state", [
    ([51, 52, 53], "bullish"),
    ([55, 56, 54, 55, 51, 51], "weak_positive"),
    ([49, 48, 47], "strong_negative"),
])
def test_at_s02_9_ism_states(values, state):
    assert M.ism_state(values).state == state


def test_at_s02_9_ism_below_50_rising_is_ambiguous_near_40():
    s = M.ism_state([44, 43, 42, 47, 48, 49])
    assert s.state == "weak_negative_accumulate"
    assert s.alternatives == ("weak_negative_accumulate", "positive_cycle")
    assert M.ism_state([39, 41, 44, 46, 48, 49]).state == "positive_cycle"
    assert M.ism_state([47, 46, 45.5, 46, 47, 48]).alternatives == ()


def test_ism_events():
    assert "crossed_below_50_reduce_risk" in M.ism_state([56, 54, 52, 50.5, 49.5]).events
    assert "trough_turning_up" in M.ism_state([48, 46, 44, 43, 45, 47]).events


@pytest.mark.parametrize("cpi,exp", [([0.4, 0.5, 0.5, 0.5], "heat"), ([4.0, 4.8, 5.5, 6.0], "cool"),
                                     ([2.5, 2.5, 2.5, 2.5], "wait")])
def test_at_s02_10_inflation_vs_target(cpi, exp):
    assert M.inflation(cpi).fed_expected == exp


def test_deflation_flag_and_labor():
    assert "deflation_recession_flag" in M.inflation([0.5, 0.2, -0.1, -0.4]).flags
    u = [5.0] * 60 + [4.0] * 59 + [6.5]
    lab = M.labor(u)
    assert lab.crossed_lt_up and lab.above_lt_avg


def test_compass_now():
    sc = M.activity_scale(_scores_2023())
    q, flags = M.compass_now(sc, M.inflation([6.5, 6.4, 6.2, 6.0]), lead_direction="up")
    assert q == "growth-_inflation-" and flags == ["inflation_lead_disagrees_with_cpi"]


# ---------------------------------------------------------------- stage 3

def test_at_s03_1_bear_line():
    st = M.market_regime([100, 85, 79, 81])
    assert st.bear_level == pytest.approx(80.00)
    assert st.history == ["bull", "correction", "bear", "bull"]


def test_at_s03_3_weekly_breaks():
    b = M.breadth(closed_below=200, members=500, index_prev_week=(110, 100), index_week=(108, 101))
    assert b.index_candle_w == "inside" and b.pct_closed_below_prior_week_low == pytest.approx(40.00)
    assert "internal_weakness" in b.flags
    assert M.breadth(pct_above_ma200=85).ma200_zone == "high"


def test_at_s03_4_quadrant_assets():
    a = M.preferred_assets("growth+_inflation+")
    assert a["preferred"] == ["equities", "commodities", "real_estate"]
    assert a["tradable_in_v1"] == ["equities"] and "commodities" in a["context_only"]


def _ctx(**kw):
    return Context(Portfolio(equity=100_000, positions=[]), **kw)


@pytest.mark.parametrize("kw,blocked", [
    (dict(symbol="0700.HK", country="HK"), True),
    (dict(symbol="TLT", underlying="bond"), True),
    (dict(symbol="XLE", underlying="sector_etf"), False),
])
def test_at_s03_2_v1_scope(kw, blocked):
    p = TradeProposal(direction="long", **kw)
    f = check_scope(p, _ctx())
    assert bool(f) is blocked
    if blocked:
        v = evaluate(p, _ctx())
        assert any(x.rule_id == "V1-SCOPE" and x.severity is Severity.BLOCK for x in v.findings)


def test_at_s03_5_no_short_against_rising_market():
    p = TradeProposal("BLDR", "short")
    f = check_macro_alignment(p, _ctx(market_trend="up", macro_bias="bearish"))
    assert [x.rule_id for x in f] == ["S03-R13"]
    v = evaluate(p, _ctx(market_trend="up"))
    assert any(x.rule_id == "S03-R13" and x.severity is Severity.NEEDS_OVERRIDE for x in v.findings)
