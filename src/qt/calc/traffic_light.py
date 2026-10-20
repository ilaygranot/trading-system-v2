"""Technical traffic light (S09): combine the seven sources into one color.

Colors are relative to the idea's direction. The indicators themselves are computed elsewhere;
this module only applies the aggregation rules:
- decision 16: red price behavior (long horizon) or the high-interval veto (S09-R7) => red;
  green only if price behavior, PSAR and Volatility Stop are green and >= 2 of SMA/RSI/MACD are green
  (the 2-of-3 is not from the course); otherwise orange.
- fix 9 / S09-R13: 4 or more of the 7 sources red => red.
- S09-R10 term #8: stock color decides; market or sector red => at most orange; both red => red.
"""
from __future__ import annotations

from typing import Literal, Mapping

from qt.policy import load_policy

Color = Literal["green", "orange", "red"]
SOURCES = ("levels", "price_behavior", "sma", "rsi", "macd", "psar", "volatility_stop")
_RANK = {"green": 0, "orange": 1, "red": 2}


def price_behavior_color(intervals: Mapping[str, bool]) -> Color:
    """intervals: year/quarter/month/week -> True if with the idea (S09-R11).
    4/4 green; 3/1 orange; 2/2 or worse red."""
    with_idea = sum(intervals.values())
    return "green" if with_idea == 4 else "orange" if with_idea == 3 else "red"


def aggregate(sources: Mapping[str, Color], high_interval_veto: bool = False) -> Color:
    missing = set(SOURCES) - set(sources)
    if missing:
        raise ValueError(f"missing sources: {sorted(missing)}")
    reds = sum(1 for s in SOURCES if sources[s] == "red")
    if high_interval_veto or sources["price_behavior"] == "red" or reds >= load_policy().traffic_light.red_sources_for_red:
        return "red"
    core_green = all(sources[s] == "green" for s in ("price_behavior", "psar", "volatility_stop"))
    daily_green = sum(1 for s in ("sma", "rsi", "macd") if sources[s] == "green")
    return "green" if core_green and daily_green >= 2 else "orange"


def combine(stock: Color, sector: Color, market: Color) -> Color:
    if sector == "red" and market == "red":
        return "red"
    if sector == "red" or market == "red":
        return max(stock, "orange", key=_RANK.get)  # type: ignore[return-value]
    return stock
