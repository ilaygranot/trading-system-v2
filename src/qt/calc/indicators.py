"""Technical indicators used by the traffic light (S09), on lists of bars (oldest first).

Parameters come from lesson 080/082 via policy.yaml. Where the course gives no formula, the standard
definition is used (Wilder RSI/ATR/PSAR, TradingView-style Volatility Stop) and marked as such.
"""
from __future__ import annotations

from datetime import date
from typing import Callable, Sequence

from qt.calc.volatility import Bar


# ---------- resampling ----------

def _period_key(kind: str) -> Callable[[date], tuple]:
    return {
        "week": lambda d: tuple(d.isocalendar()[:2]),
        "month": lambda d: (d.year, d.month),
        "quarter": lambda d: (d.year, (d.month - 1) // 3),
        "year": lambda d: (d.year,),
    }[kind]


def resample(bars: Sequence[Bar], kind: str) -> list[Bar]:
    """Daily bars -> week / month / quarter / year candles. The last candle may still be forming."""
    if kind == "day":
        return list(bars)
    key = _period_key(kind)
    out: list[Bar] = []
    cur_key = None
    for b in bars:
        k = key(b.day)
        if k != cur_key:
            out.append(Bar(b.day, b.open, b.high, b.low, b.close, b.volume))
            cur_key = k
        else:
            last = out[-1]
            out[-1] = Bar(last.day, last.open, max(last.high, b.high), min(last.low, b.low), b.close,
                          last.volume + b.volume)
    return out


def mirror(bars: Sequence[Bar]) -> list[Bar]:
    """Short ideas: indicators are defined for longs and mirrored for shorts (080). Mirroring by the
    reciprocal price keeps OHLC ordering (not from course: the transform itself)."""
    return [Bar(b.day, 1 / b.open, 1 / b.low, 1 / b.high, 1 / b.close, b.volume) for b in bars]


# ---------- moving averages ----------

def sma(values: Sequence[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(values: Sequence[float], n: int) -> list[float]:
    k = 2 / (n + 1)
    out = []
    for i, v in enumerate(values):
        out.append(v if i == 0 else v * k + out[-1] * (1 - k))
    return out


def rsi(closes: Sequence[float], n: int) -> list[float | None]:
    """Wilder RSI."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for i in range(n, len(closes)):
        if i > n:
            ag = (ag * (n - 1) + gains[i - 1]) / n
            al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def macd(closes: Sequence[float], fast: int, slow: int, signal: int) -> tuple[list[float], list[float], list[float]]:
    line = [a - b for a, b in zip(ema(closes, fast), ema(closes, slow))]
    sig = ema(line, signal)
    return line, sig, [a - b for a, b in zip(line, sig)]


def true_range(bars: Sequence[Bar]) -> list[float]:
    out = []
    for i, b in enumerate(bars):
        if i == 0:
            out.append(b.high - b.low)
        else:
            pc = bars[i - 1].close
            out.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
    return out


def atr_wilder(bars: Sequence[Bar], n: int) -> list[float | None]:
    tr = true_range(bars)
    out: list[float | None] = [None] * len(bars)
    if len(bars) < n:
        return out
    a = sum(tr[:n]) / n
    out[n - 1] = a
    for i in range(n, len(bars)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def psar(bars: Sequence[Bar], step: float, max_af: float) -> list[float]:
    """Wilder Parabolic SAR (080: factor 0.02, step 0.02, max 0.2)."""
    if len(bars) < 2:
        return [bars[0].low] if bars else []
    up = bars[1].close >= bars[0].close
    sar = bars[0].low if up else bars[0].high
    ep = bars[0].high if up else bars[0].low
    af = step
    out = [sar]
    for i in range(1, len(bars)):
        b, prev = bars[i], bars[i - 1]
        sar = sar + af * (ep - sar)
        if up:
            sar = min(sar, prev.low, bars[i - 2].low if i >= 2 else prev.low)
            if b.low < sar:
                up, sar, ep, af = False, ep, b.low, step
            elif b.high > ep:
                ep, af = b.high, min(af + step, max_af)
        else:
            sar = max(sar, prev.high, bars[i - 2].high if i >= 2 else prev.high)
            if b.high > sar:
                up, sar, ep, af = True, ep, b.high, step
            elif b.low < ep:
                ep, af = b.low, min(af + step, max_af)
        out.append(sar)
    return out


def volatility_stop(bars: Sequence[Bar], n: int, mult: float) -> list[tuple[float | None, bool]]:
    """Volatility Stop: extreme close -/+ mult x ATR(n) (080). Returns (stop, uptrend) per bar.
    Standard TradingView definition (not from course: the use of closing extremes)."""
    atr = atr_wilder(bars, n)
    out: list[tuple[float | None, bool]] = []
    up, ext, stop = True, None, None
    for i, b in enumerate(bars):
        a = atr[i]
        if a is None:
            out.append((None, True))
            continue
        if ext is None:
            ext, up = b.close, True
            stop = b.close - mult * a
        if up:
            ext = max(ext, b.close)
            stop = max(stop, ext - mult * a)
            if b.close < stop:
                up, ext, stop = False, b.close, b.close + mult * a
        else:
            ext = min(ext, b.close)
            stop = min(stop, ext + mult * a)
            if b.close > stop:
                up, ext, stop = True, b.close, b.close - mult * a
        out.append((stop, up))
    return out
