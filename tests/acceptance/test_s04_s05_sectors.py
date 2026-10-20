"""Stages 4-5: sector scorecard and industries (AT-S04-*, AT-S05-*)."""
import re
from pathlib import Path

import pytest

from qt import sectors as S
from qt.calc.portfolio import Portfolio
from qt.rules.checks import Context, TradeProposal, check_sector_alignment, evaluate
from qt.rules.engine import Severity
from qt.screening import StockRow

ETFS = ["XLK", "XLY", "XLI", "XLE", "XLRE", "XLC", "XLF", "XLV", "XLU", "XLB", "XLP"]


def test_policy_has_no_duplicate_top_level_keys():
    keys = re.findall(r"^([a-z_]+):", (Path(S.__file__).parent / "policy.yaml").read_text(), re.M)
    assert len(keys) == len(set(keys))


def test_at_s04_1_rs_average_of_filled_periods():
    perf = {"1w": 0.44, "1m": 0.34, "3m": None, "6m": None, "1y": 4.26}
    assert S.rs_avg(perf) == pytest.approx(1.68, abs=0.01)


def test_at_s04_2_rs_from_absolute_returns_order():
    spy = [3.88, 1.67, 7.46, 15.05, -9.01]
    absolute = {
        "XLK": [4.59, 7.59, 21.16, 26.93, -5.13], "XLC": [4.19, 6.30, 21.30, 21.55, -15.37],
        "XLF": [2.55, -10.50, -5.82, 6.09, -15.94], "XLRE": [4.99, -4.44, 0.27, 2.83, -23.37],
        "XLE": [7.18, -0.78, -1.02, 20.22, 13.27],
    }
    avg = {k: sum(S.rs_from_returns(a, b) for a, b in zip(v, spy)) / 5 for k, v in absolute.items()}
    assert avg["XLK"] == pytest.approx(6.77, abs=0.05) and avg["XLF"] == pytest.approx(-8.20, abs=0.05)
    assert sorted(avg, key=avg.get, reverse=True) == ["XLK", "XLE", "XLC", "XLRE", "XLF"]
    assert S.rs_from_returns(-5.13, -9.01) == pytest.approx(4.26, abs=0.01)     # matches RS FLOW yearly


def test_rs_perf_from_prices():
    bench = [100.0] * 300
    sector = [100.0] * 47 + [100 + i * 0.1 for i in range(253)]
    p = S.rs_perf(sector, bench)
    assert set(p) == {"1w", "1m", "3m", "6m", "1y"} and all(v > 0 for v in p.values())
    assert S.rs_perf(sector[-30:], bench[-30:])["3m"] is None


def test_at_s04_3_fundamental_rank():
    data = {"Auto": (14.58, 15.81), "Aerospace": (10.10, 13.28), "Business Services": (10.26, 7.94),
            "Computer & Tech": (7.90, 5.39), "Finance": (0.41, 4.93), "Transportation": (3.56, -4.14),
            "Oils-Energy": (1.03, -4.37), "Basic Materials": (1.41, -8.28), "Construction": (-2.12, -9.34)}
    g = {k: S.GroupGrowth(a, b, 0, 10) for k, (a, b) in data.items()}
    assert g["Auto"].score == pytest.approx(15.20, abs=0.01) and g["Construction"].score == pytest.approx(-5.73, abs=0.01)
    r = S.fundamental_ranks(g)
    assert sorted(r, key=r.get, reverse=True) == list(data)
    market = (5.12 + 3.13) / 2
    assert [k for k in data if g[k].score > market] == ["Auto", "Aerospace", "Business Services", "Computer & Tech"]


def test_at_s04_4_construction_worst():
    assert S.GroupGrowth(-2.12, -9.34, 0, 10).worst


def _rs(v):
    return {"1w": v, "1m": v, "3m": v, "6m": v, "1y": v}


def test_at_s04_5_cold_stance_scorecard():
    # equal technical and fundamental ranks isolate the macro layer: cold + slowdown favours defensives
    rs = {e: _rs(0.1 * i) for i, e in enumerate(ETFS)}
    growth = {e: S.GroupGrowth(1.0 + 0.01 * i, 1.0, 0, 10) for i, e in enumerate(ETFS)}
    sc = S.scorecard(rs, growth, "slowdown", "cold")
    assert sc.sectors["XLU"].macro == 9 and sc.sectors["XLY"].macro == 0
    assert any(S.load_policy().sector_scorecard.type[e] == "defensive" for e in sc.long)
    assert len(sc.long) == 3 and len(sc.short) == 3
    assert "mostly_cyclical_longs_in_cold_stance" not in sc.flags


def test_macro_points_table():
    assert S.macro_points("XLI", "recovery", "extreme_cold") == (9, "++")
    assert S.macro_points("XLU", "recovery", "hot") == (0, "-")
    assert S.macro_points("XLK", "expansion", "hot") == (3, "")          # growth type: no tilt


def test_rs_down_everywhere_never_long():
    rs = {e: _rs(1.0) for e in ETFS}
    rs["XLE"] = _rs(-1.0)
    growth = {e: S.GroupGrowth(1, 1, 0, 10) for e in ETFS}
    growth["XLE"] = S.GroupGrowth(50, 50, 0, 10)
    sc = S.scorecard(rs, growth, "recovery", "hot", macro_override={"XLE": (9, "oil supply shock")})
    assert "XLE" not in sc.long and "rs_down_all_periods_not_long" in sc.sectors["XLE"].flags
    assert "macro_override:oil supply shock" in sc.sectors["XLE"].flags
    with pytest.raises(ValueError):
        S.scorecard(rs, growth, "recovery", "hot", macro_override={"XLE": (5, "x")})


def _ctx(**kw):
    return Context(Portfolio(equity=100_000, positions=[]), **kw)


def test_at_s04_6_long_against_short_bucket():
    p = TradeProposal("JPM", "long", sector="XLF")
    f = check_sector_alignment(p, _ctx(sector_buckets={"XLF": "short"}))
    assert [x.rule_id for x in f] == ["S04-R19"]
    v = evaluate(p, _ctx(sector_buckets={"XLF": "short"}))
    assert any(x.rule_id == "S04-R19" and x.severity is Severity.NEEDS_OVERRIDE for x in v.findings)


def test_at_s04_7_expectations_weakening():
    prev, now = S.GroupGrowth(10, 18, 0, 20), S.GroupGrowth(10, 12, 0, 20)
    assert S.expectation_change(prev, now) == "weakening"
    f = check_sector_alignment(TradeProposal("X", "long", sector="XLK"),
                               _ctx(sector_flags={"XLK": ["expectations_weakening"]}))
    assert [x.rule_id for x in f] == ["S04-R34"]


def test_at_s04_8_sector_not_confirming():
    ok, flags = S.sector_confirms(-20.0, -3.0, -1.5, "long")          # XLU early 2021
    assert not ok and flags == ["sector_against_3y", "sector_against_3m", "sector_against_1m"]
    ok, flags = S.sector_confirms(-30.0, 8.0, 3.0, "long")            # XLE catching up in an early cycle
    assert ok and flags == ["sector_against_3y"]


def test_cycle_pairs():
    trends = {("XLP", "XLY"): "down", ("XLU", "XLI"): "down", ("SMH", "XLK"): "up", ("IWM", "SPY"): "up"}
    assert S.cycle_pairs_check(trends, "recovery")["flag"] is None
    assert S.cycle_pairs_check(trends, "slowdown")["flag"] == "cycle_pairs_disagree"
    assert S.ratio_trend([10, 10, 10, 12], [10, 10, 10, 10]) == "up"


def test_at_s05_1_single_ism_report_is_not_a_trend():
    m = S.ism_industries([["Petroleum & Coal Products", "Machinery"]])
    assert [(x.name, x.months_in_row, x.trend) for x in m] == [
        ("Petroleum & Coal Products", 1, False), ("Machinery", 1, False)]
    hist = [["Machinery"], ["Machinery", "Chemicals"], ["Machinery", "Chemicals"]]
    assert {x.name: x.trend for x in S.ism_industries(hist)} == {"Machinery": True, "Chemicals": False}


def test_at_s05_4_commodity_streaks():
    m = {x.name: x for x in S.ism_commodities({"aluminum": 9, "steel": 7, "plastics & nylon": 2})}
    assert (m["aluminum"].trend, m["steel"].trend, m["plastics & nylon"].trend) == (True, True, False)
    assert all(x.shortage_source == "unknown" for x in m.values())


def test_at_s05_3_industry_vs_sector():
    def row(t, ind, f0, f1, s0, s1):
        return StockRow(t, f0=f0, f1=f1, f2=f1 * 1.1, sales=s0, sales_f1=s1, industry=ind, price=50, market_cap=5e9)
    rows = [row(f"A{i}", "Aerospace", 1.0, 1.2, 100, 112) for i in range(16)] + \
           [row(f"M{i}", "Machinery", 1.0, 0.9, 100, 97) for i in range(5)]
    sector = S.group_growth(rows)
    views = {v.name: v for v in S.industry_map(rows, sector)}
    assert views["Aerospace"].vs_sector == "above" and views["Aerospace"].tag == "priority"
    assert views["Aerospace"].use_own_average and not views["Machinery"].use_own_average
    assert views["Machinery"].tag == "avoid"
