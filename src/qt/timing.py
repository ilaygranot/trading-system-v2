"""Stage 9: the technical traffic light, computed from daily bars.

Seven sources (S09): levels, price behavior, SMA, RSI, MACD, PSAR, Volatility Stop. Six are computed here;
levels need judgment (which candle is "significant", S09-R15/R16), so the code proposes candidate levels
and the timing agent (A6) supplies the levels color. Until it does, levels count as orange and the
snapshot carries the flag `levels_pending`.

Colors are relative to the idea's direction: short ideas run the same logic on mirrored bars (080).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Sequence

from qt.calc import indicators as I
from qt.calc.traffic_light import Color, aggregate, price_behavior_color
from qt.calc.volatility import Bar, adr
from qt.policy import load_policy

Direction = Literal["long", "short"]


def count_color(above: Sequence[bool]) -> Color:
    """Interval-counted sources (PSAR, Volatility Stop): 4/4 green, 3/4 orange, 2/2 or worse red (S09-R22/R23)."""
    n = sum(above)
    return "green" if n == len(above) else "orange" if n == len(above) - 1 else "red"


def rsi_color(values: Sequence[float | None]) -> Color:
    """S09-R20 with D-S09-7 (b): below 30 and falling is red; orange only once it turns up."""
    t = load_policy().timing
    vals = [v for v in values if v is not None]
    if len(vals) <= t.rsi_slope_days:
        raise ValueError("not enough RSI values")
    r, prev = vals[-1], vals[-1 - t.rsi_slope_days]
    rising, falling = r > prev, r < prev
    if r > 70 and falling:
        return "red"
    if r < 30:
        return "orange" if rising else "red"
    if falling and r < 50:
        return "red"
    if rising and all(v > 50 for v in vals[-t.rsi_settled_days:]):
        return "green"
    return "orange"


def macd_color(line: Sequence[float], signal: Sequence[float], hist: Sequence[float]) -> Color:
    """S09-R21: green = above 0, above signal, gap widening; red = negative/below signal and widening against."""
    m, s, h, hp = line[-1], signal[-1], hist[-1], hist[-2]
    if m > 0 and m > s and h > hp:
        return "green"
    if (m < 0 or m < s) and h < 0 and h < hp:
        return "red"
    return "orange"


def extended_days(lows: Sequence[float], ma21: Sequence[float | None]) -> int:
    """Consecutive most recent sessions whose low did not touch MA21 (D-S09-4 (a), 082-R15)."""
    n = 0
    for lo, m in zip(reversed(lows), reversed(ma21)):
        if m is None or lo <= m:
            break
        n += 1
    return n


def sma_color(close: float, mas: Sequence[float], extended: bool) -> Color:
    """S09-R18. mas = (short, mid, long). Extended price can't be green and counts red."""
    s, m, l = mas
    if extended:
        return "red"
    if close > max(mas) and s > m > l:
        return "green"
    if close < min(mas) or (close < max(mas) and s < m):
        return "red"
    return "orange"


@dataclass
class TimingSnapshot:
    direction: Direction
    sources: dict[str, Color]
    overall: Color
    high_interval_veto: str | None
    details: dict = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)


def high_interval_veto(bars: Sequence[Bar]) -> str | None:
    """S09-R7 (long logic; pass mirrored bars for shorts).
    (a) quarterly/yearly candle broke the previous candle's low and is red; (b) yearly broke the previous
    high and is back below it while red; (c) year, quarter, month and week all against the idea."""
    for kind in ("quarter", "year"):
        c = I.resample(bars, kind)
        if len(c) >= 2 and c[-1].low < c[-2].low and c[-1].close < c[-1].open:
            return f"(a) {kind} candle broke the previous low and is red"
    y = I.resample(bars, "year")
    if len(y) >= 2 and y[-1].high > y[-2].high and y[-1].close < y[-2].high and y[-1].close < y[-1].open:
        return "(b) yearly candle broke the previous high and fell back below it, red"
    against = [I.resample(bars, k)[-1].close < I.resample(bars, k)[-1].open
               for k in load_policy().timing.price_behavior_intervals]
    if all(against):
        return "(c) year, quarter, month and week all against the idea"
    return None


def candidate_levels(bars: Sequence[Bar], price: float, kinds: Sequence[str] = ("month", "quarter")) -> dict:
    """Highs/lows of closed monthly and quarterly candles as level candidates (S09-R15). The agent decides
    which are significant; the code gives nearest support/resistance and the room to them."""
    pts = sorted({round(x, 4) for k in kinds for c in I.resample(bars, k)[:-1] for x in (c.high, c.low)})
    sup = max((p for p in pts if p < price), default=None)
    res = min((p for p in pts if p > price), default=None)
    return dict(nearest_support=sup, nearest_resistance=res,
                room_up_pct=None if res is None else round((res / price - 1) * 100, 2),
                room_down_pct=None if sup is None else round((1 - sup / price) * 100, 2))


def snapshot(daily: Sequence[Bar], direction: Direction = "long", levels_color: Color | None = None) -> TimingSnapshot:
    t = load_policy().timing
    bars = I.mirror(daily) if direction == "short" else list(daily)
    closes = [b.close for b in bars]
    price = closes[-1]
    flags: list[str] = []
    details: dict = {}

    # price behavior: current candle vs its open, per interval (S09-R5, S09-R11)
    pb = {k: I.resample(bars, k)[-1].close > I.resample(bars, k)[-1].open for k in t.price_behavior_intervals}
    details["price_behavior"] = pb

    # moving averages (S09-R17/R18, D-S09-3/4)
    mas_series = [I.sma(closes, n) for n in t.sma_periods]
    mas = [s[-1] for s in mas_series]
    if None in mas:
        raise ValueError(f"need {max(t.sma_periods)} daily bars for the moving averages")
    ext = extended_days([b.low for b in bars], mas_series[0])
    if ext > t.extended_days:
        flags.append("extended")
    details["sma"] = dict(values=[round(m, 4) for m in mas], extended_days=ext)

    rsi_vals = I.rsi(closes, t.rsi_period)
    details["rsi"] = round(rsi_vals[-1], 2)
    line, sig, hist = I.macd(closes, *t.macd)
    details["macd"] = dict(line=round(line[-1], 4), signal=round(sig[-1], 4), hist=round(hist[-1], 4))

    psar_above, vs_above = [], []
    for k in t.counted_intervals:
        c = I.resample(bars, k)
        psar_above.append(c[-1].close > I.psar(c, *t.psar)[-1])
        n, mult = t.vol_stop[k]
        vs = I.volatility_stop(c, n, mult)[-1]
        vs_above.append(bool(vs[0] is not None and vs[1]))
        if vs[0] is None:
            flags.append(f"vol_stop_{k}_insufficient_history")
    details["psar_above"] = dict(zip(t.counted_intervals, psar_above))
    details["vol_stop_above"] = dict(zip(t.counted_intervals, vs_above))
    flags.append("vol_stop_quarter_multiplier_unverified")      # D-S09-5

    if levels_color is None:
        flags.append("levels_pending")
    details["levels"] = candidate_levels(daily, daily[-1].close)

    sources: dict[str, Color] = dict(
        levels=levels_color or "orange",
        price_behavior=price_behavior_color(pb),
        sma=sma_color(price, mas, ext > t.extended_days),
        rsi=rsi_color(rsi_vals),
        macd=macd_color(line, sig, hist),
        psar=count_color(psar_above),
        volatility_stop=count_color(vs_above),
    )
    veto = high_interval_veto(bars)
    if len(daily) >= 60 and adr(daily, 20) / adr(daily, 60) >= t.atr_ratio_wait:
        flags.append("atr20_over_atr60_wait")                    # S09-R52, D-S09-10
    return TimingSnapshot(direction, sources, aggregate(sources, veto is not None), veto, details, flags)
