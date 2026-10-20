"""Stage 6 screen and growth profiles (AT-S06-*, decision 12)."""
import pytest

from qt import screening as S
from qt.screening import Metrics, StockRow


@pytest.mark.parametrize("prev,nxt,exp,coded", [
    (0.35, 1.12, 220.00, False),      # ERJ EG1
    (1.12, 1.49, 33.04, False),       # ERJ EG2
    (-0.40, 0.12, 99, True),          # ESTC: loss -> profit
    (0.50, -0.20, -99, True),         # profit -> loss
    (-0.40, -0.10, -99, True),        # loss -> loss
])
def test_at_s06_1_growth_and_99_code(prev, nxt, exp, coded):
    g, c = S.growth(prev, nxt)
    assert g == pytest.approx(exp, abs=0.01) and c is coded


def test_at_s06_2_eps_zero_replaced():
    m = S.compute(StockRow("Z", price=10, market_cap=1e9, sales=4e8, sales_f1=5e8, f0=0.0, f1=0.30, f2=0.5))
    assert m.eg1 == 99 and "eps_zero_replaced" in m.flags and "coded_99" in m.flags
    assert m.track == "negative_pe"                     # not +300%


@pytest.mark.parametrize("avg,thr", [(11.8, 12.0), (22.19, 22.5), (12.0, 12.0)])
def test_at_s06_3_threshold_rounds_up(avg, thr):
    assert S.round_up_threshold(avg) == thr


@pytest.mark.parametrize("row,fails,priority", [
    (StockRow("a", "NASDAQ", 0.8e9, 12, 450e6, f2=0.40), [], False),
    (StockRow("b", "NYSE", 0.4e9, 9, 350e6, f2=0.20), ["S06-R6"], False),
    (StockRow("c", "NASDAQ", 3e9, 4.5, 900e6, f2=0.10), ["S06-R9"], True),
    (StockRow("d", "AMEX", 1.2e9, 20, 600e6, f2=0.80), ["S06-R5"], False),
    (StockRow("e", "NYSE", 1e9, 15, 100e6, f2=-0.30), ["S06-R10", "S06-R24"], False),
    (StockRow("f", "NASDAQ", 5e9, 40, 2e9, f2=-0.05), ["S06-R24"], True),
])
def test_at_s06_4_long_universe(row, fails, priority):
    assert S.universe_failures(row, "long") == fails
    assert S.priority_tag(row, "long") is priority


@pytest.mark.parametrize("sf,dtc,fails", [
    (14.9, 3.2, []), (14, 4.72, []), (25, 11, ["S06-R36", "S06-R37"]),   # Carvana fails twice
])
def test_at_s06_7_short_float_and_days_to_cover(sf, dtc, fails):
    assert S.universe_failures(StockRow("X", market_cap=50e9, short_float=sf, days_to_cover=dtc), "short") == fails


PROFILE_CASES = [
    ((20, 25), (10, 12), (1, 1, 1), "L1"),
    ((20, 20), (10, 12), (1, 1, 1), "L2"),
    ((20, 15), (10, 12), (1, 1, 1), "L3"),
    ((8, 20), (10, 12), (1, 1, 1), "L4"),
    ((10, 12), (10, 12), (1, 1, 1), "L5"),
    ((-20, 25), (10, 16), (1, 1, 1), "L6"),
    ((-99, 99), (10, 12), (-1, -0.5, 0.3), "L7"),
    ((-11, 9), (12, 16), (1, 1, 1), "L8"),
]


@pytest.mark.parametrize("e,a,f,exp", PROFILE_CASES)
def test_at_s06_8_profile_tree(e, a, f, exp):
    assert S.classify_long(*e, *a, *f)[0] == exp


def test_at_s06_9_rank():
    items = [(exp, *S.classify_long(*e, *a, *f)) for e, a, f, exp in PROFILE_CASES]
    assert S.rank_long(items) == ["L1", "L2", "L7", "L3", "L6", "L4", "L8"]
    # TDG-style L3 with y2_below_sector ranks after L4 and before L8
    items.append(("TDG", "L3", ["y2_below_sector"]))
    assert S.rank_long(items)[-2:] == ["TDG", "L8"]


AERO = [(19, 20), (13.28, 15.67)]   # lecturer vs 097 table


@pytest.mark.parametrize("a", AERO)
def test_at_s06_10_erj_l3(a):
    m = S.compute(StockRow("ERJ", f0=0.35, f1=1.12, f2=1.49, sales=1, sales_f1=1))
    assert (round(m.eg1, 2), round(m.eg2, 2)) == (220.00, 33.04)
    assert S.classify_long(m.eg1, m.eg2, *a, 0.35, 1.12, 1.49)[0] == "L3"


@pytest.mark.parametrize("a", AERO)
def test_at_s06_11_avav_l6_with_star(a):
    m = S.compute(StockRow("AVAV", f0=1.34, f1=1.26, f2=2.46, sales=1, sales_f1=1, second_source={"f2": 2.04}))
    assert (round(m.eg1, 2), round(m.eg2, 2)) == (-5.97, 95.24)
    assert "star_mismatch" in m.flags
    assert S.classify_long(m.eg1, m.eg2, *a, 1.34, 1.26, 2.46)[0] == "L6"
    assert S.classify_long(m.eg1, S.growth(1.26, 2.04)[0], *a, 1.34, 1.26, 2.04)[0] == "L6"


def test_at_s06_12_acmr_star():
    m = S.compute(StockRow("ACMR", f0=0.74, f1=0.82, f2=1.35, sales=1, sales_f1=1, second_source={"f2": 1.11}))
    assert (round(m.eg1, 2), round(m.eg2, 2)) == (10.81, 64.63)
    assert "star_mismatch" in m.flags
    # vs the broad tech average this would be L1, which is the documented error; the semis run is missing
    assert S.classify_long(m.eg1, m.eg2, 5.39, 21.47, 0.74, 0.82, 1.35)[0] == "L1"


def test_at_s06_13_estc_l1_not_l7():
    m = S.compute(StockRow("ESTC", f0=-0.40, f1=0.12, f2=0.92, sales=1, sales_f1=1,
                           second_source={"f1": 0.13, "f2": 0.90}))
    assert m.eg1 == 99 and round(m.eg2, 2) == 666.67
    assert "star_mismatch" not in m.flags
    prof, tags = S.classify_long(m.eg1, m.eg2, 6, 21.47, -0.40, 0.12, 0.92)
    assert prof == "L1" and "turns_profitable_this_year" in tags


def test_at_s06_14_dv_l1():
    assert S.classify_long(15, 36, 6, 21.47, 1, 1, 1)[0] == "L1"


@pytest.mark.parametrize("a", AERO)
def test_at_s06_15_tdg_l3_y2_below(a):
    prof, tags = S.classify_long(34, 15, *a, 1, 1, 1)
    assert prof == "L3" and "y2_below_sector" in tags
    assert S.classify_short(34, 15, *a) is None


@pytest.mark.parametrize("e,a,exp", [
    ((-2, -10), (5, 7), "S_acute"),
    ((6, 8), (10, 12), "S_relative"),
    ((14, 13), (10, 12), None),
])
def test_at_s06_16_short_profiles(e, a, exp):
    assert S.classify_short(*e, *a) == exp


def test_at_s06_17_guidance_change():
    pct = S.guidance_change_pct(750, 600)
    assert pct == pytest.approx(25.00, abs=0.01) and S.is_guidance_catalyst(pct)
    assert not S.is_guidance_catalyst(2.5)
    assert S.guidance_change_pct(105, 90, 110) == pytest.approx(5.0)   # vs the midpoint of a range


@pytest.mark.parametrize("eps,ok", [
    ((-0.16, 0.38, 0.52), True),    # NCNO
    ((-0.10, 0.30, 0.76), True),    # APPS
    ((-0.30, 0.40, 0.35), False),
])
def test_at_s06_18_loss_to_profit(eps, ok):
    assert S.loss_to_profit(*eps) is ok


def _m(t, eg1, base=1.0, pe=None, coded=False):
    row = StockRow(t, f0=base, f1=base, f2=base)
    return Metrics(row, eg1=eg1, eg2=eg1, pe=pe, coded={"eg1", "eg2"} if coded else set())


def test_at_s06_20_clean_sector_average():
    ms = [_m("A", 10, pe=20), _m("B", 14, pe=30), _m("C", 18, pe=40),
          _m("D", 2300, base=0.05, pe=650), _m("E", 99, coded=True, pe=-5)]
    avg = S.averages(ms)
    assert avg.eg1 == pytest.approx(14.00) and avg.pe == pytest.approx(30.00)
    assert set(avg.excluded["eg1"]) == {"D", "E"} and set(avg.excluded["pe"]) == {"D", "E"}
    assert avg.median_eg1 == 14


def test_at_s06_21_extreme_outlier():
    ms = [_m(f"S{i}", 25.56) for i in range(150)] + [_m("X", 2200)]
    assert S.averages(ms).eg1 == pytest.approx(25.56, abs=0.01)


def test_profit_loss_profit_rejected():
    prof, tags = S.classify_long(-99, 99, 10, 12, 0.5, -0.2, 0.4)    # ATRO pattern, S06-R41
    assert prof is None and "profit_loss_profit_rejected" in tags


def test_run_end_to_end():
    def row(t, f0, f1, f2, s0, s1, cap=5e9, price=50, ex="NASDAQ", **kw):
        return StockRow(t, ex, cap, price, s0, s1, f0, f1, f2, **kw)
    rows = [
        row("AAA", 1.0, 1.2, 1.6, 1e9, 1.3e9),     # 20 / 33: L1, strong sales
        row("BBB", 1.0, 1.1, 1.2, 1e9, 1.05e9),    # sales below the threshold
        row("CCC", 1.0, 0.95, 0.9, 1e9, 1.0e9, cap=30e9, short_float=5, days_to_cover=2),  # short
        row("DDD", 1.0, 1.05, 1.1, 1e9, 1.1e9),
        row("EEE", None, 1, 1, 1e9, 1e9),          # missing data track
        row("BAX", -3.0, 0.05, 3.0, 1e9, 1.0e9, cap=100e9),   # loss -> near zero -> jump (BA 2023)
    ]
    r = S.run(rows)
    assert [c.ticker for c in r.long] == ["AAA"] and r.long[0].profile == "L1"
    assert "S06-R22" in r.rejected["BBB"]
    assert "EEE" in r.tracks["missing_data"]
    shorts = {c.ticker: c for c in r.short}
    assert shorts["CCC"].profile in {"S_consistent", "S_relative", "S_acute"}
    assert shorts["BAX"].profile is None and "short_gate_override" in shorts["BAX"].tags
    assert len(shorts["BAX"].conflicts) == 2
