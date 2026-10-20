"""Stages 7-8: deep dive and watchlist (AT-S07-*, AT-S08-*)."""
from datetime import date

import pytest

from qt import deepdive as D
from qt import watchlist as W
from qt.calc.portfolio import Portfolio
from qt.rules.checks import Context, TradeProposal, check_watchlist_gate, evaluate
from qt.rules.engine import Severity


def _ctx(**kw):
    return Context(Portfolio(equity=100_000, positions=[]), **kw)


def _sev(v, rule):
    return {x.severity for x in v.findings if x.rule_id == rule}


# ---------------------------------------------------------------- stage 7

def test_at_s07_1_negative_operating_leverage():
    ol = D.operating_leverage(100, 120, 70, 100)
    assert (ol.gross_before, ol.gross_after) == (30, 20)
    assert ol.revenue_growth == pytest.approx(20.0) and ol.cogs_growth == pytest.approx(42.857, abs=0.001)
    assert ol.direction == "negative" and ol.flags == ["negative_operating_leverage"]
    card = D.Card("X", "long", answers={q: True for q in D._d().questions}, reasons=["growth_engine"],
                  thesis_breakers=["x"], flags=ol.flags)
    g = D.gate(card)
    assert g.outcome == "watchlist" and "negative_operating_leverage" in g.lower_confidence   # not a rejection


def test_at_s07_2_unhealthy_finances():
    h = D.health([-3.0, -5.0], debt=(100, 130), interest=(5, 7))
    assert h.state == "unhealthy" and not h.ok
    card = D.Card("X", "long", answers={q: True for q in D._d().questions}, reasons=["growth_engine"],
                  thesis_breakers=["x"], health=h)
    assert D.gate(card).outcome == "rejected" and "S07-R7_unhealthy_long" in D.gate(card).reasons
    hs = D.health([-3.0, -5.0], debt=(100, 130), direction="short")
    assert hs.ok and "support the short" in hs.note
    v = evaluate(TradeProposal("X", "long", unhealthy_finances=True), _ctx())
    assert _sev(v, "S07-R7") == {Severity.NEEDS_OVERRIDE}


def test_at_s07_3_premium_and_narrowing_gap():
    p = D.premium(30, 15)
    assert p.pct == pytest.approx(100.00) and p.flags == ["premium_needs_justification"]
    assert D.premium(30, 15, "justified", "growth engine + operating leverage").flags == []
    assert D.premium(30, 15, "unjustified", "no engine").flags == ["premium_unjustified"]
    assert D.gap_vs_sector((15, 14), (10, 12)) == ("narrowing", ["pricing_risk_gap_narrowing"])


def test_at_s07_4_target_from_multiple():
    t = D.target_price(True, 4.00, hist_multiples=[11, 12, 13])
    assert (t.price, t.method, t.multiple_source) == (48.00, "pe", "hist_3y")
    young = D.target_price(False, 2_000e6, hist_multiples=[8], sector_multiple=5, shares=100e6)
    assert (young.price, young.method, young.multiple_source) == (100.00, "ps", "sector")
    assert young.flags == ["no_3y_history_sector_multiple"]


def test_peer_regression_is_info_only():
    peers = [(10, 2.0), (20, 4.0), (30, 6.0)]
    t = D.target_price(True, 4.0, [12, 12, 12], peers=peers, growth=25)
    assert t.price == 48.00 and t.peer_multiple == pytest.approx(5.0)


def test_at_s07_5_value_trap_short_passes_to_watchlist():
    assert D.pct(220, 120) == pytest.approx(83.33, abs=0.01)            # not "80%"
    card = D.Card("BA", "short", answers={"sector_macro_supported": True, "stands_out_vs_sector": True,
                                          "growth_engines_identified": True, "catalyst_expected": True},
                  reasons=["guidance_vs_record_gap", "move_not_justified_fundamentally"], story="value_trap",
                  thesis_breakers=["deliveries resume"], flags=["negative_operating_leverage"])
    g = D.gate(card)
    assert g.outcome == "watchlist" and "negative_operating_leverage" not in g.lower_confidence


def test_at_s07_7_wide_guidance_math():
    lo, hi = 43.8, 45.2
    assert hi - lo == pytest.approx(1.4) and (hi - lo) / ((lo + hi) / 2) * 100 == pytest.approx(3.15, abs=0.01)
    assert (6.50 - 5.70) / 5.70 * 100 == pytest.approx(14.04, abs=0.01)    # not "19%"


def test_at_s07_9_growth_from_guidance():
    mid, lo, hi = D.guidance_growth(0.883, 1.16, 1.17)
    assert mid == pytest.approx(31.94, abs=0.01)
    assert (lo, hi) == (pytest.approx(31.37, abs=0.01), pytest.approx(32.50, abs=0.01))


def test_at_s07_10_penetration():
    assert 86_000 / 3e6 * 100 == pytest.approx(2.87, abs=0.01)
    assert 103_994 / 3e6 * 100 == pytest.approx(3.47, abs=0.01)


def test_at_s07_12_net_debt_trend():
    assert D.net_debt_to_ebitda(150, 30, 20) == pytest.approx(6.0)
    ford = [D.net_debt_to_ebitda(d, 30, oi) for d, oi in ((150, 20), (160, 18), (170, 15), (175, 12))]
    assert D.net_debt_trend(ford) == ("rising", ["net_debt_ebitda_rising"])
    assert D.net_debt_trend([0.5, 0.3, 0.2, 0.1])[0] == "falling"       # Tesla: almost no debt
    assert D.core_trend({"revenue": [10, 12, 15], "operating_income": [3, 2, 2.5]}) == {
        "revenue": "up", "operating_income": "mixed"}


def test_at_s07_13_cheap_is_not_a_reason():
    card = D.Card("X", "long", answers={q: True for q in D._d().questions}, reasons=["cheap_vs_sector"],
                  thesis_breakers=["x"])
    g = D.gate(card)
    assert g.outcome == "rejected" and "valuation_is_not_a_reason" in g.reasons


def test_d_s07_8_gate_questions_and_catalyst():
    base = dict(sector_macro_supported=True, stands_out_vs_sector=True, growth_engines_identified=True)
    no_cat = D.Card("AXON", "long", answers=base, reasons=["gov_contracts"], thesis_breakers=["x"])
    g = D.gate(no_cat)
    assert (g.outcome, g.heat) == ("watchlist_cold", "cold")             # AT-S07-6: no catalyst yet
    bad = D.Card("Y", "long", answers={**base, "stands_out_vs_sector": False}, reasons=["x"])
    assert D.gate(bad).reasons == ["question_failed:stands_out_vs_sector"]
    v = evaluate(TradeProposal("AXON", "long", catalyst_expected=False), _ctx())
    assert _sev(v, "S07-R13") == {Severity.NEEDS_OVERRIDE}


def test_other_statement_flags():
    assert D.profit_source(100, 110, 80, 75) == "both" and D.profit_source(100, 100, 80, 70) == "bottom"
    assert D.acceleration([28, 30], 20) == ["acceleration_find_the_factor"]
    assert D.fcf_from_operations(-5, 10, 50)[1] == ["cash_from_financing_not_operations"]
    assert D.capital_structure((100, 105)) == ["dilution"]
    assert D.liquidity(5, 50, 30) == ["weak_liquidity_long_waits"]
    assert D.extreme_growth(250) == ["verify_extreme_growth"]
    assert D.source_mismatch(1.35, 1.11) and D.source_mismatch(-0.1, 0.1) and not D.source_mismatch(0.12, 0.13 * 0.95)
    s = D.surprises([2, 3, -1, 4, 5, 6])
    assert s.usually_beats and s.trend == "improving"


# ---------------------------------------------------------------- stage 8

def _e(ticker="X", **kw):
    d = dict(direction="long", list="main", source="top_down", added=date(2023, 3, 1), entry_price=40.0)
    d.update(kw)
    return W.Entry(ticker, **d)


def test_at_s08_1_boiling_no_trade():
    e = _e("SEMI", heat="boiling")
    assert W.boiling_hint(16) is False                                 # +16% in two weeks: the agent's call
    ok, why = W.early_gate(e, news_adverse=False)
    assert not ok and why == ["boiling_wait_for_base_retest_or_cheaper_options"]
    v = evaluate(TradeProposal("SEMI", "long", heat="boiling"), _ctx())
    assert _sev(v, "S08-R28") == {Severity.BLOCK}


def test_at_s08_2_ba_from_watch_to_hot():
    e = _e("BA", direction="short", heat="cold")
    assert W.early_gate(e, False) == (False, ["heat_cold"])
    W.set_heat(e, "hot", "settled under $200 ($198); 5% underperformance; double top; trend break; level 190",
               date(2023, 5, 1))
    assert W.early_gate(e, False) == (True, [])
    ok, why = W.full_gate("orange", ("green", "orange"), stop=210, target=170, rr=3.2, horizon_months=3)
    assert ok and not why
    ok, why = W.full_gate("red", ("red",), stop=None, target=170, rr=2.0, horizon_months=3)
    assert set(why) == {"traffic_light_red", "against_the_trend", "stop_missing", "reward_risk_below_min"}


def test_at_s08_3_cold_with_mixed_news():
    e = _e("NVEI", heat="cold")
    assert W.early_gate(e, news_adverse=False)[0] is False
    with pytest.raises(W.WatchlistError):
        W.set_heat(e, "hot", "", date(2023, 5, 1))                     # a heat change needs a reason


def test_at_s08_4_frozen_never_flipped():
    e = _e("X")
    assert W.frozen_triggers("long", -2.0, True) == ["growth_below_sector", "price_fully_against_thesis"]
    assert W.frozen_triggers("short", 1.0, False) == ["growth_above_sector"]
    W.set_heat(e, "frozen", "growth below sector, price falling", date(2023, 5, 1))
    assert e.archived
    with pytest.raises(W.WatchlistError, match="S08-R27"):
        W.reverse(e, full_process=False)
    with pytest.raises(W.WatchlistError, match="S08-R27"):
        W.add(_e("X", direction="short"), process_complete=False, existing=[e])
    v = evaluate(TradeProposal("X", "short", reverses_frozen_idea=True), _ctx())
    assert _sev(v, "S08-R27") == {Severity.BLOCK}
    with pytest.raises(W.WatchlistError):
        W.revive(e, refreshed_6_7=False, reason="x", day=date(2023, 6, 1))
    assert W.revive(e, True, "fundamentals refreshed", date(2023, 6, 1)).heat == "cold"


def test_at_s08_5_day_61_review():
    c = W.computed(_e(added=date(2023, 3, 1)), 40, date(2023, 5, 1))
    assert c.days_on_list == 61 and c.review_flag
    assert not W.computed(_e(added=date(2023, 3, 2)), 40, date(2023, 5, 1)).review_flag


@pytest.mark.parametrize("kind,kw,cls,action", [
    ("debt_rating_upgrade", {}, "A", "trade_candidate"),
    ("debt_rating_downgrade", {}, "A", "short_or_exit_candidate"),
    ("dividend_suspension", {}, "A", "short_or_exit_candidate"),
    ("guidance_change", {"change_pct": 2.5}, "none", "nothing"),
    ("analyst_upgrades_no_new_target", {"on_list": True}, "C", "record"),
    ("analyst_upgrades_no_new_target", {"on_list": False}, "C", "nothing"),
])
def test_at_s08_6_catalyst_classes(kind, kw, cls, action):
    c = W.classify(kind, **kw)
    assert (c.cls, c.action) == (cls, action)


def test_at_s08_6_catalyst_against_prospect():
    c = W.classify("analyst_target", change_pct=10, prospect="negative")
    assert c.action == "nothing"                                     # +10% target: not even a catalyst
    c = W.classify("analyst_target", change_pct=50, prospect="negative")
    assert c.cls == "B" and c.action == "no_entry" and "catalyst_against_prospect" in c.flags


@pytest.mark.parametrize("rng,sideways,events,out", [
    (40, True, True, "not_tradable"),      # KO: growth sideways
    (21.4, False, True, "not_tradable"),   # CL: 70-85
    (60, False, True, "tradable"),         # CMG
    (80, False, False, "low_priority"),    # Bowlero: field without events
])
def test_at_s08_7_tradability(rng, sideways, events, out):
    assert W.tradability(rng, sideways, events) == out


def test_at_s08_8_event_list_only():
    with pytest.raises(W.WatchlistError, match="S08-R1"):
        W.add(_e("LEGN", source="news_flow"), process_complete=False)
    e = W.add(_e("LEGN", list="event", source="news_flow", heat="hot"), process_complete=False)
    assert W.early_gate(e, False) == (False, ["event_list_feeds_only_back_to_stages_6_7"])


def test_at_s08_9_computed_fields():
    c = W.computed(_e(entry_price=40, target=55, added=date(2023, 3, 1)), 44, date(2023, 3, 13))
    assert c.change_pct == pytest.approx(10.00) and c.potential_pct == pytest.approx(25.00)
    assert c.days_on_list == 12 and not c.review_flag


def test_spread_ratio():
    e = _e("A/B", direction="spread", leg_a="A", leg_b="B", target_a=120, target_b=60)
    assert W.spread_ratio(100, 50) == 2.0 and W.spread_target(e) == 2.0


def test_at_s08_10_composition():
    src = ["top_down"] * 6 + ["news_flow"] * 5 + ["theme"] * 3
    entries = [_e(f"T{i}", source=s) for i, s in enumerate(src)]
    assert W.composition(entries, "cold") == ["main_list_above_12", "top_down_share_42.86_below_50",
                                              "no_shorts_in_cold_stance"]


def test_at_s08_11_headline_only_off_list():
    v = evaluate(TradeProposal("ESTC", "long", on_watchlist=False), _ctx())
    assert _sev(v, "S08-R2") == {Severity.BLOCK} and v.blocks


def test_at_s08_12_analyst_target_materiality():
    assert [W.analyst_target((t / 50 - 1) * 100) for t in (75, 56, 52, 65)] == [
        "material", "not_renewed", "not_renewed", "judgment"]


def test_at_s08_13_guidance_vs_previous_midpoint():
    g = W.guidance_event(600, 500)
    assert g["change_pct"] == pytest.approx(20.00) and g["catalyst"].cls == "A" and g["catalyst"].sign == "positive"
    for actual, cons, gap in ((80, 76, 5.26), (3.33, 3.14, 6.05), (1.65, 1.24, 33.06), (2.1, 1.95, 7.69)):
        r = W.guidance_event(actual, None, consensus=cons)
        assert r["consensus_gap_pct"] == pytest.approx(gap, abs=0.01)
        assert r["guidance_change"] == "unknown" and r["catalyst"].cls == "none"


def test_missing_explanations():
    e = _e(explanation={"what_it_does": "x", "why_watch": "y"})
    assert "scenario" in W.missing_explanations(e) and "what_it_does" not in W.missing_explanations(e)


def test_watchlist_gate_check_passes_hot():
    assert check_watchlist_gate(TradeProposal("X", "long", heat="hot", price_behaviour_qm=("green",)), _ctx()) == []
