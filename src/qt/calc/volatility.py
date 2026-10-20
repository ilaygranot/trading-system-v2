"""Volatility numbers: daily range (ADR), ATR 20-60 regime, VIX flag, sigma conversions.

Definitions from lesson 084 (S11-R24, S12-R24, S12-R25). All percentages are in percent units (3.0 == 3%).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Sequence

from qt.policy import load_policy


@dataclass(frozen=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def daily_range_pct(bar: Bar) -> float:
    """(High - Low) / Close, in percent (084)."""
    return (bar.high - bar.low) / bar.close * 100.0


def adr(bars: Sequence[Bar], period: int | None = None) -> float:
    """Average daily range: simple mean of (H-L)/C over the last `period` bars (S11-R24, D-S11-8: 60)."""
    period = period or load_policy().stop.adr_period_days
    if len(bars) < period:
        raise ValueError(f"need {period} bars for ADR, got {len(bars)}")
    window = bars[-period:]
    return sum(daily_range_pct(b) for b in window) / period


def adr_before(bars: Sequence[Bar], day: date, period: int | None = None) -> float:
    """ADR over the `period` bars strictly before `day` (what was known at entry)."""
    return adr([b for b in bars if b.day < day], period)


def atr_20_60_series(bars: Sequence[Bar], short: int | None = None, long: int | None = None) -> list[tuple[date, float]]:
    """ATR20 - ATR60 of daily (H-L)/C, in percentage points (S12-R25). Subtraction, not division."""
    p = load_policy().regime
    short, long = short or p.atr_short, long or p.atr_long
    ranges = [daily_range_pct(b) for b in bars]
    out = []
    for i in range(long - 1, len(bars)):
        a_short = sum(ranges[i - short + 1 : i + 1]) / short
        a_long = sum(ranges[i - long + 1 : i + 1]) / long
        out.append((bars[i].day, a_short - a_long))
    return out


@dataclass(frozen=True)
class RegimeEvent:
    day: date
    regime: str  # "portfolio" | "tactical"
    value: float


def regime_transitions(series: Sequence[tuple[date, float]], start: str = "portfolio",
                       sigma: float | None = None) -> list[RegimeEvent]:
    """Crossing up through +sigma => tactical; crossing down through -sigma => portfolio.
    Between the lines the previous regime holds; crossing 0 is alert only, never a switch (S12-R25)."""
    sigma = sigma if sigma is not None else load_policy().regime.sigma_pct
    regime, events = start, []
    for (d0, v0), (d1, v1) in zip(series, series[1:]):
        if regime == "portfolio" and v0 <= sigma < v1:
            regime = "tactical"
            events.append(RegimeEvent(d1, regime, v1))
        elif regime == "tactical" and v0 >= -sigma > v1:
            regime = "portfolio"
            events.append(RegimeEvent(d1, regime, v1))
    return events


def current_regime(series: Sequence[tuple[date, float]], start: str = "portfolio") -> tuple[str, bool]:
    """(regime, alert). alert = ATR 20-60 above 0 (no action)."""
    events = regime_transitions(series, start)
    regime = events[-1].regime if events else start
    return regime, bool(series) and series[-1][1] > 0


@dataclass(frozen=True)
class VixReading:
    ratio_to_low: float
    jump: bool          # >= ~40% above the recent low => FLAG only (Ilay 20.10), never a regime switch
    above_mean: bool    # above historical mean 19.5 => expect stronger moves (alert)


def vix_check(closes: Sequence[float], window: int | None = None) -> VixReading:
    """closes: oldest..today. Low over the `window` sessions before today (window not from course, D-S12-2)."""
    p = load_policy().regime
    window = window or p.vix_low_window_days
    today = closes[-1]
    low = min(closes[-window - 1 : -1]) if len(closes) > 1 else today
    ratio = today / low
    return VixReading(ratio, ratio >= p.vix_jump_ratio - 1e-12, today > p.vix_hist_mean)


def monthly_from_annual(annual_pct: float) -> float:
    """IV or VIX (annual) -> one-sigma monthly move: divide by sqrt(12), never by 12 (084)."""
    return annual_pct / math.sqrt(12)


def annual_from_monthly(monthly_pct: float) -> float:
    return monthly_pct * math.sqrt(12)


def parse_yahoo_chart(payload: dict) -> list[Bar]:
    """Yahoo v8 chart JSON -> bars (skips rows with missing values)."""
    res = payload["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    bars = []
    for i, ts in enumerate(res["timestamp"]):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        bars.append(Bar(date.fromtimestamp(ts + res["meta"].get("gmtoffset", 0)), o, h, l, c, q["volume"][i] or 0))
    return bars


def parse_fmp_prices(rows: list[dict]) -> list[Bar]:
    """FMP historical rows (newest first) -> bars oldest first."""
    bars = [Bar(date.fromisoformat(r["date"]), r["open"], r["high"], r["low"], r["close"], r.get("volume", 0)) for r in rows]
    return sorted(bars, key=lambda b: b.day)
