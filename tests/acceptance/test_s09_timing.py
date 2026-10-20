"""Stage 9 traffic light computed from bars (AT-S09-*, indicators)."""
from datetime import date, timedelta

import pytest

from qt import timing as T
from qt.calc import indicators as I
from qt.calc.volatility import Bar


@pytest.mark.parametrize("vals,exp", [
    ([40, 35, 31, 29, 27], "red"),        # 27, down from 31
    ([30, 27, 25, 26, 28], "orange"),     # 28, up from 25
    ([75, 76, 78, 74, 72], "red"),        # above 70 and falling
    ([52, 54, 56, 58, 60], "green"),      # rising, settled above 50
    ([60, 58, 55, 50, 45], "red"),        # falling below 50
    ([45, 47, 49, 51, 53], "orange"),     # rising but not settled above 50
])
def test_at_s09_5_rsi(vals, exp):
    assert T.rsi_color(vals) == exp


def test_at_s09_6_extended():
    lows = [10.0] * 30
    ma21 = [9.0] * 30
    assert T.extended_days(lows, ma21) == 30
    lows[-23] = 8.0                        # touched MA21 23 sessions ago -> 22 untouched
    assert T.extended_days(lows, ma21) == 22
    assert T.sma_color(12, (11, 10, 9), extended=True) == "red"
    assert T.sma_color(12, (11, 10, 9), extended=False) == "green"


def test_macd_cross_while_negative_is_orange():
    # StoneCo (AT-S09-2): MACD crosses above signal while still negative -> orange, not green
    assert T.macd_color([-1.0, -0.8], [-0.9, -0.85], [-0.1, 0.05]) == "orange"
    assert T.macd_color([0.5, 0.7], [0.4, 0.5], [0.1, 0.2]) == "green"
    assert T.macd_color([-0.5, -0.7], [-0.4, -0.5], [-0.1, -0.2]) == "red"


@pytest.mark.parametrize("above,exp", [
    ([True] * 4, "green"), ([True, True, True, False], "orange"), ([False, False, True, True], "red"),
])
def test_interval_count(above, exp):
    assert T.count_color(above) == exp            # PSAR 2/2 = red (StoneCo)


def _bars(closes, start=date(2023, 1, 2)):
    out, d = [], start
    for c in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append(Bar(d, c * 0.995, c * 1.01, c * 0.99, c, 1000))
        d += timedelta(days=1)
    return out


def test_resample_and_mirror():
    b = _bars([float(i) for i in range(1, 61)])
    w = I.resample(b, "week")
    assert w[0].open == b[0].open and w[0].close == b[4].close and w[0].high == max(x.high for x in b[:5])
    assert sum(x.volume for x in w) == sum(x.volume for x in b)
    assert len(I.resample(b, "month")) == 3
    m = I.mirror(b)
    assert m[0].high == pytest.approx(1 / b[0].low) and m[0].low < m[0].high


def test_indicators_on_trend():
    up = _bars([100 * 1.003 ** i for i in range(300)])
    closes = [x.close for x in up]
    assert I.rsi(closes, 20)[-1] > 70
    line, sig, hist = I.macd(closes, 20, 60, 10)
    assert line[-1] > 0
    assert I.psar(up, 0.02, 0.2)[-1] < up[-1].low
    stop, uptrend = I.volatility_stop(up, 20, 2.5)[-1]
    assert uptrend and stop < up[-1].close


def test_snapshot_uptrend_long_vs_short():
    up = _bars([100 * 1.002 ** i for i in range(800)])   # >2 years: monthly ATR24 and quarterly ATR8 need it
    lg = T.snapshot(up, "long", levels_color="green")
    assert lg.sources["psar"] == lg.sources["volatility_stop"] == lg.sources["price_behavior"] == "green"
    assert lg.high_interval_veto is None and "extended" in lg.flags and lg.sources["sma"] == "red"
    sh = T.snapshot(up, "short")
    assert sh.overall == "red" and "levels_pending" in sh.flags


@pytest.mark.parametrize("name", ["SYM_daily_2022_2024.json", "META_daily_2022_2024.json"])
def test_snapshot_on_real_bars(bars, name):
    b = bars(name)
    for d in ("long", "short"):
        s = T.snapshot(b, d)
        assert set(s.sources) == {"levels", "price_behavior", "sma", "rsi", "macd", "psar", "volatility_stop"}
        assert s.overall in {"green", "orange", "red"}
        assert "levels_pending" in s.flags
