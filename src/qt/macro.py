"""Stages 1-3: global macro, the macro stance (activity and Fed scales), and market state.

Code computes every number and every mapping the course defines. What needs judgment goes to the macro
agent (A1) as a decision card: a score in a grey band (DC-01), the compass in six months (DC-02),
market vs macro (DC-03) and confidence (DC-04). Where the course is silent, policy.yaml holds the
chapter recommendation and marks it "not from course".

Monthly series are lists of values, oldest first, one per month with no gaps; daily series the same per
trading day. Fetching them (FRED with ALFRED vintages, the ISM report, NFIB) is qt.data.macro_feed.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Literal, Sequence

from qt.policy import load_policy

Zone = Literal["extreme_cold", "grey_low", "cold", "hot", "grey_high", "extreme_heat"]
FED_TOOLS = {"ffr", "fed_funds", "fed_funds_rate", "interest_rate", "m2", "fed_balance_sheet"}


def _m():
    return load_policy().macro


def linear_trend_at_end(values: Sequence[float]) -> float:
    """Least-squares line over the whole history, evaluated at the last point (068's Excel trend line)."""
    n = len(values)
    xs = range(n)
    mx, my = (n - 1) / 2, sum(values) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, values)) / sxx if sxx else 0.0
    return my + slope * (n - 1 - mx)


def moving_average(values: Sequence[float], n: int) -> float:
    w = values[-n:]
    return sum(w) / len(w)


# ======================================================================= stage 1: global macro

def spread(risky_yield: float, govt_yield: float) -> float:
    """S01-R1: credit spread = risky (HY) yield - government yield."""
    return risky_yield - govt_yield


@dataclass
class CreditStress:
    value: float
    ma250: float
    dist_ma250: float
    dist_trend: Literal["growing", "shrinking", "flat"]
    yoy_change: float | None
    chg_60d: float | None
    lt_avg: float
    state: Literal["calm", "rising_stress", "panic", "easing"]
    flags: list[str] = field(default_factory=list)


def credit_stress(daily_spread: Sequence[float], dist_lookback: int = 20) -> CreditStress:
    """S01-R2..R6 with D-S01-1 (c) and D-S01-3 (a).
    rising_stress: above MA250 or the yearly change turned positive. panic: above MA250, above the long-term
    average and the distance from the MA is growing. easing: above MA250 and the distance is shrinking."""
    m = _m()
    s = list(daily_spread)
    n = m.hy_ma_days
    if len(s) < n + dist_lookback:
        raise ValueError(f"need at least {n + dist_lookback} daily spread values")
    ma = moving_average(s, n)
    dist = s[-1] - ma
    prev_dist = s[-1 - dist_lookback] - moving_average(s[:-dist_lookback], n)
    trend = "growing" if dist > prev_dist else "shrinking" if dist < prev_dist else "flat"
    yoy = s[-1] - s[-1 - n] if len(s) > n else None
    c60 = s[-1] - s[-1 - m.hy_chg_short_days] if len(s) > m.hy_chg_short_days else None
    lt = sum(s) / len(s)            # D-S01-1 (c): computed from the data, not a fixed 5%
    flags = []
    if len(s) < 252 * 10:
        flags.append("lt_avg_short_history")           # HY history on FRED is ~3 years
    if dist > 0:
        if s[-1] > lt and trend == "growing":
            state = "panic"
        elif trend == "shrinking":
            state = "easing"
        else:
            state = "rising_stress"
    elif yoy is not None and yoy > 0:
        state = "rising_stress"                          # S01-J2: stress starting before the crossing
    else:
        state = "calm"
    if yoy is not None and yoy > 0:
        flags.append("yoy_change_positive")             # S01-R5 red flag
    return CreditStress(s[-1], ma, dist, trend, yoy, c60, lt, state, flags)


@dataclass
class Curve:
    pair: tuple[str, str]
    value: float
    shape: Literal["normal", "inverted"]
    recession_flag: bool


def yield_curve(long_yields: Sequence[float], short_yields: Sequence[float]) -> Curve:
    """D-S01-2: 10Y - 3M, monthly average of daily values; < 0 = inverted (S01-R10, no time horizon)."""
    diffs = [a - b for a, b in zip(long_yields, short_yields)]
    v = sum(diffs) / len(diffs)
    inv = v < 0
    return Curve(tuple(_m().curve_pair), v, "inverted" if inv else "normal", inv)


def real_rate(nominal: float, inflation: float) -> tuple[float, bool]:
    """S01-R12: real = nominal - inflation; returns (value, negative)."""
    r = nominal - inflation
    return r, r < 0


@dataclass
class CommoditySignal:
    basket_yoy: float
    direction: Literal["up", "down", "flat"]
    extreme: bool
    shocks: list[str]


def commodities(yoy_now: dict[str, float], yoy_3m_ago: dict[str, float]) -> CommoditySignal:
    """D-S01-4 (b): basket yearly change positive and rising => up; negative and falling => down;
    extreme flag at +100% (S01-R17). Single commodities at +100% go to stages 4/5 (S01-J7)."""
    now = sum(yoy_now.values()) / len(yoy_now)
    before = sum(yoy_3m_ago[k] for k in yoy_now) / len(yoy_now)
    ext = _m().commodity_extreme_yoy_pct
    d = "up" if now > 0 and now > before else "down" if now < 0 and now < before else "flat"
    return CommoditySignal(now, d, now >= ext, sorted(k for k, v in yoy_now.items() if v >= ext))


def direction_vote(*dirs: str) -> str:
    """Majority of up/down/flat; ties are flat. Used to combine the leading inflation signals (S01 step 4)."""
    up, down = dirs.count("up"), dirs.count("down")
    return "up" if up > down else "down" if down > up else "flat"


def stage1_consistency(stress: CreditStress, curve: Curve, growth_sign: str) -> str:
    """S01-J1 (step 6): normal curve + easing/calm spreads support growth+; inverted + stress support growth-.
    Returns agree / conflict / mixed. A conflict lowers confidence; it never flips the stance alone."""
    supports_up = curve.shape == "normal" and stress.state in ("calm", "easing")
    supports_down = curve.shape == "inverted" and stress.state in ("rising_stress", "panic")
    if not (supports_up or supports_down):
        return "mixed"
    return "agree" if (supports_up and growth_sign == "+") or (supports_down and growth_sign == "-") else "conflict"


# ======================================================================= stage 2: activity scale

class ScaleError(ValueError):
    """An indicator that does not belong in the activity scale (S02-R42, S02-R43)."""


@dataclass
class IndicatorScore:
    name: str
    now: float
    m3: float
    m6: float
    m12: float
    ref: float
    level_ok: bool
    yoy_pos: bool
    half_pos: bool
    quarter_pos: bool
    score: int
    cycle_quadrant: str                  # 096 per-indicator compass (D-S02-9): yearly x quarterly change


def score_indicator(name: str, now: float, m3: float, m6: float, m12: float, ref: float) -> IndicatorScore:
    """S02-R41 (096): level above reference = 3, yearly change > 0 = 3, half-year > 0 = 2, quarter > 0 = 2."""
    if name in FED_TOOLS:
        raise ScaleError(f"{name}: direction error, Fed tools belong in the Fed scale (S02-R43, S02-R44)")
    p = _m().points
    lv, y, h, q = now > ref, now > m12, now > m6, now > m3
    score = p.level * lv + p.yoy * y + p.half * h + p.quarter * q
    quad = {(True, True): "expansion", (False, True): "recovery", (True, False): "slowdown",
            (False, False): "recession"}[(y, q)]
    return IndicatorScore(name, now, m3, m6, m12, ref, lv, y, h, q, score, quad)


def reference_level(name: str, history: Sequence[float]) -> float:
    """D-S02-6: ISM = 50; Michigan = linear trend over all history; Permits = MA24; small business = MA60."""
    spec = _m().level_ref[name]
    if spec["kind"] == "fixed":
        return float(spec["value"])
    if spec["kind"] == "linear_trend":
        return linear_trend_at_end(history)
    return moving_average(history, spec["months"])


def score_series(name: str, monthly: Sequence[float]) -> IndicatorScore:
    if len(monthly) < 13:
        raise ValueError(f"{name}: need 13 monthly values (now and 12 months back)")
    v = list(monthly)
    return score_indicator(name, v[-1], v[-4], v[-7], v[-13], reference_level(name, v))


@dataclass
class ActivityScale:
    total: int
    max: int
    pct: float
    zone: Zone
    candidates: tuple[str, ...]          # the two zones in a grey band (DC-01), else one
    bias: str | None                     # None while a grey band waits for the agent
    spread: dict[str, int]               # per-indicator scores: report average and dispersion (S02-J2)


def zone_of(pct: float) -> tuple[Zone, tuple[str, ...]]:
    """Decision 14 (096): <=10% extreme cold, >=90% extreme heat, 50% is the equator; 10-20% and 80-90% are
    grey bands where the agent chooses between the adjacent zones. Exact 10/50/90 assignment is inferred."""
    z = _m().zones
    if pct <= z.extreme_cold_max:
        return "extreme_cold", ("extreme_cold",)
    if pct <= z.grey_low[1]:
        return "grey_low", ("extreme_cold", "cold")
    if pct < z.hot_min:
        return "cold", ("cold",)
    if pct < z.grey_high[0]:
        return "hot", ("hot",)
    if pct < z.extreme_heat_min:
        return "grey_high", ("hot", "extreme_heat")
    return "extreme_heat", ("extreme_heat",)


def bias_of(zone: str) -> str:
    """S02-R47: extreme heat neutral, hot bullish, cold bearish, extreme cold cautious optimism."""
    return _m().bias[zone]


def activity_scale(scores: Sequence[IndicatorScore]) -> ActivityScale:
    allowed = set(_m().activity_indicators)
    names = [s.name for s in scores]
    bad = [n for n in names if n not in allowed]
    if bad:
        raise ScaleError(f"not in the activity scale (D-S02-7, leading indicators only): {bad}")
    if len(set(names)) != len(names):
        raise ScaleError("an indicator is counted twice (096: 'ISM zero, PMI zero' is the same series)")
    total = sum(s.score for s in scores)
    mx = 10 * len(scores)
    pct = 100.0 * total / mx
    zone, cand = zone_of(pct)
    return ActivityScale(total, mx, pct, zone, cand, bias_of(zone) if len(cand) == 1 else None,
                         {s.name: s.score for s in scores})


def resolve_grey(scale: ActivityScale, agent_choice: str | None, previous_zone: str | None) -> tuple[str, str]:
    """DC-01. The agent picks one of the two adjacent zones. Without a decision: last month's zone if it is
    one of the two, else the non-extreme zone (both not from course). Returns (zone, bias)."""
    if len(scale.candidates) == 1:
        return scale.zone, scale.bias
    if agent_choice is not None:
        if agent_choice not in scale.candidates:
            raise ValueError(f"grey band {scale.zone}: choose one of {scale.candidates}")
        z = agent_choice
    elif previous_zone in scale.candidates:
        z = previous_zone
    else:
        z = "cold" if scale.zone == "grey_low" else "hot"
    return z, bias_of(z)


# ----------------------------------------------------------------------- ISM detail

@dataclass
class IsmState:
    value: float
    change: float
    state: Literal["bullish", "weak_positive", "strong_negative", "weak_negative_accumulate", "positive_cycle"]
    alternatives: tuple[str, ...] = ()   # ambiguous "≈40" case: both readings go to the agent / Ilay
    events: list[str] = field(default_factory=list)


def ism_state(values: Sequence[float]) -> IsmState:
    """S02-R10/R11/R12 with D-S02-3 (c) (3-month change) and D-S02-4 (c): below 50 and rising is
    'weak negative, accumulate' unless the rise starts from the lower extreme (≈40, 068)."""
    m = _m()
    v = list(values)
    k = min(m.ism_trend_months, len(v) - 1)
    now, chg = v[-1], v[-1] - v[-1 - k]
    events, alts = [], ()
    if now > 50:
        state = "bullish" if chg > 0 else "weak_positive"
    elif chg < 0:
        state = "strong_negative"
    else:
        trough = min(v[-7:])
        if trough < m.ism_extreme_low:
            state = "positive_cycle"
        elif trough <= m.ism_extreme_low + m.ism_extreme_band:
            state, alts = "weak_negative_accumulate", ("weak_negative_accumulate", "positive_cycle")
        else:
            state = "weak_negative_accumulate"
    if len(v) >= 2 and v[-2] >= 50 > now:
        events.append("crossed_below_50_reduce_risk")             # S02-R12
    w = v[-7:-1]
    if w and min(w) < 50 and now > min(w) and w.index(min(w)) < len(w) - 1 and v[-2] > min(w):
        events.append("trough_turning_up")                          # S02-R12: be in before the official bull
    return IsmState(now, chg, state, alts, events)


# ----------------------------------------------------------------------- inflation, labor, Fed

@dataclass
class Inflation:
    cpi_yoy: float
    direction_3m: Literal["up", "down", "flat"]
    vs_range: Literal["below", "in", "above"]
    fed_expected: Literal["heat", "cool", "wait"]
    flags: list[str] = field(default_factory=list)


def inflation(cpi_yoy: Sequence[float]) -> Inflation:
    """S02-R28..R31 with decision 15 (target 2%, normal range 2-3%). Below range => the Fed heats;
    above range and rising => cools; in range => 'won't hurry'. A FOMC statement can override (S02-J3)."""
    m = _m()
    now, before = cpi_yoy[-1], cpi_yoy[-4] if len(cpi_yoy) >= 4 else cpi_yoy[0]
    d = "up" if now > before else "down" if now < before else "flat"
    lo, hi = m.inflation_range
    flags = []
    if now < lo:
        vs, exp = "below", "heat"
    elif now <= hi:
        vs, exp = "in", "wait"
    else:
        vs = "above"
        exp = "cool" if d == "up" else "wait"
        if d != "up":
            flags.append("above_range_not_rising")
    if now < 0:
        flags.append("deflation_recession_flag")               # 067-R12
    return Inflation(now, d, vs, exp, flags)


@dataclass
class Labor:
    rate: float
    lt_avg: float
    ma5y: float
    above_lt_avg: bool
    above_ma5y: bool
    crossed_lt_up: bool
    fed_comfortable_tightening: bool


def labor(unemployment: Sequence[float]) -> Labor:
    """S02-R23..R27: vs the long-term average (Fed direction) and the 5-year average (trend)."""
    u = list(unemployment)
    lt = sum(u) / len(u)
    ma5 = moving_average(u, _m().unemployment_ma_months)
    crossed = len(u) >= 2 and u[-2] <= lt < u[-1]
    falling = len(u) >= 4 and u[-1] < u[-4]
    return Labor(u[-1], lt, ma5, u[-1] > lt, u[-1] > ma5, crossed, u[-1] < lt and falling)


@dataclass
class FedScale:
    points: int
    max: int
    period: Literal["tightening", "easing", "neutral"]
    state: Literal["not_acting", "acting_continuing", "acting_reversing"]
    components: dict[str, bool]


def fed_scale(ffr: Sequence[float], m2_yoy: Sequence[float], balance_sheet: Sequence[float]) -> FedScale:
    """Separate policy scale (S02-R43..R45, D-S02-8 (b)): FFR falling earns points (rising never does),
    M2 yearly change above its linear trend earns points, a growing balance sheet earns points.
    The categorical state (S02-R45) is kept beside the score. Weights: not from course."""
    p = _m().fed_points
    f = list(ffr)
    yoy = f[-1] - f[-13] if len(f) >= 13 else f[-1] - f[0]
    q = f[-1] - f[-4] if len(f) >= 4 else f[-1] - f[0]
    comp = dict(ffr_down=yoy < 0, m2_above_trend=m2_yoy[-1] > linear_trend_at_end(m2_yoy),
                balance_sheet_up=balance_sheet[-1] > balance_sheet[-13 if len(balance_sheet) >= 13 else 0])
    pts = sum(p[k] for k, v in comp.items() if v)
    period = "tightening" if yoy > 0 else "easing" if yoy < 0 else "neutral"
    if abs(q) < 1e-9:
        state = "not_acting"
    elif (q > 0) == (yoy > 0) and yoy != 0:
        state = "acting_continuing"
    else:
        state = "acting_reversing"
    return FedScale(pts, sum(p.values()), period, state, comp)


# ----------------------------------------------------------------------- compass and stance

def quadrant(growth_sign: str, inflation_sign: str) -> str:
    return f"growth{growth_sign}_inflation{inflation_sign}"


def compass_now(scale: ActivityScale, infl: Inflation, lead_direction: str | None = None) -> tuple[str, list[str]]:
    """D-S02-9 (approved 20.10, not from course): growth = activity scale >= 50%; inflation = 3-month
    direction of CPI YoY, together with the leading direction from stage 1. Disagreement is flagged."""
    g = "+" if scale.pct >= _m().zones.hot_min else "-"
    i = "+" if infl.direction_3m == "up" else "-"
    flags = []
    if lead_direction in ("up", "down") and (lead_direction == "up") != (i == "+"):
        flags.append("inflation_lead_disagrees_with_cpi")
    return quadrant(g, i), flags


@dataclass
class MacroStance:
    activity: ActivityScale
    zone: str
    bias: str | None
    fed: FedScale | None
    inflation: Inflation | None
    labor: Labor | None
    ism: IsmState | None
    compass_now: str | None
    stage1: str | None
    fed_against_bias: bool
    flags: list[str]
    cards: list[str]                      # decision cards the agent must answer


def stance(scores: Sequence[IndicatorScore], *, fed: FedScale | None = None, infl: Inflation | None = None,
           lab: Labor | None = None, ism: IsmState | None = None, stress: CreditStress | None = None,
           curve: Curve | None = None, lead_direction: str | None = None,
           grey_choice: str | None = None, previous_zone: str | None = None) -> MacroStance:
    """The regime sets the bias; the Fed only changes confidence (decision 14, D-S02-10 (a))."""
    sc = activity_scale(scores)
    flags: list[str] = []
    cards = ["DC-02", "DC-03", "DC-04"]
    if len(sc.candidates) > 1:
        cards.insert(0, "DC-01")
    zone, bias = resolve_grey(sc, grey_choice, previous_zone) if (grey_choice or len(sc.candidates) == 1) \
        else (sc.zone, None)
    comp, cflags = (compass_now(sc, infl, lead_direction) if infl else (None, []))
    flags += cflags
    s1 = None
    if stress and curve:
        s1 = stage1_consistency(stress, curve, "+" if sc.pct >= _m().zones.hot_min else "-")
        if s1 == "conflict":
            flags.append("stage1_conflict_lower_confidence")      # S01-J1
    fed_against = False
    if fed and bias:
        fed_against = (bias == "bullish" and fed.period == "tightening") or (bias == "bearish" and fed.period == "easing")
        if fed_against:
            flags.append("fed_against_bias_lower_confidence")
    if ism and ism.alternatives:
        flags.append("ism_reading_ambiguous")
    if lab and lab.crossed_lt_up:
        flags.append("unemployment_crossed_lt_avg_up")             # S02-R25 red flag
    if infl and infl.vs_range == "above" and lab and lab.above_lt_avg and sc.pct < 50:
        flags.append("nightmare_scenario")                         # S02-R34
    return MacroStance(sc, zone, bias, fed, infl, lab, ism, comp, s1, fed_against, flags, cards)


# ======================================================================= stage 3: market state

@dataclass
class MarketState:
    peak: float
    bear_level: float
    drawdown_pct: float
    regime: Literal["bull", "correction", "bear"]
    sessions_from_peak: int
    history: list[str] = field(default_factory=list)


def market_regime(closes: Sequence[float]) -> MarketState:
    """S03-R6 with D-S03-4: daily close of the S&P 500 vs the highest close. Bear below peak x 0.8; back above
    the line is the official return to a bull market (until the next bear). 'correction' is a descriptive label
    for a 10%+ drawdown with no bear since the peak (not from course). Description only, never a trigger (S03-R7)."""
    m = _m()
    peak, since, bear_since_peak = float("-inf"), 0, False
    hist = []
    for i, c in enumerate(closes):
        if c >= peak:
            peak, since, bear_since_peak = c, i, False
        level = peak * (1 - m.bear_drawdown_pct / 100)
        dd = (1 - c / peak) * 100
        if c < level:
            r, bear_since_peak = "bear", True
        elif bear_since_peak or dd < m.correction_drawdown_pct:
            r = "bull"
        else:
            r = "correction"
        hist.append(r)
    level = peak * (1 - m.bear_drawdown_pct / 100)
    return MarketState(peak, level, (1 - closes[-1] / peak) * 100, hist[-1], len(closes) - 1 - since, hist)


@dataclass
class Breadth:
    pct_above_ma200: float | None
    ma200_zone: Literal["low", "mid", "high"] | None
    pct_closed_below_prior_week_low: float | None
    index_candle_w: Literal["inside", "directional", "outside"] | None
    flags: list[str]


def weekly_candle(prev_high: float, prev_low: float, high: float, low: float) -> str:
    if high <= prev_high and low >= prev_low:
        return "inside"
    if high > prev_high and low < prev_low:
        return "outside"
    return "directional"


def breadth(pct_above_ma200: float | None = None, closed_below: int | None = None, members: int | None = None,
            index_prev_week: tuple[float, float] | None = None, index_week: tuple[float, float] | None = None) -> Breadth:
    """S03-R10/R11 with D-S03-3: 80/20 extremes on % above MA200; >= 40% of members closing below their prior
    weekly low while the index did not break = internal weakness. Flags lower confidence only (D-S03-5)."""
    m = _m()
    flags = []
    zone = None
    if pct_above_ma200 is not None:
        zone = "high" if pct_above_ma200 >= m.breadth_ma200.high else "low" if pct_above_ma200 <= m.breadth_ma200.low else "mid"
        if zone != "mid":
            flags.append(f"breadth_extreme_{zone}_possible_reversal")    # S03-J11
    pct_below = 100.0 * closed_below / members if closed_below is not None and members else None
    candle = weekly_candle(*index_prev_week, *index_week) if index_prev_week and index_week else None
    index_broke = index_week is not None and index_prev_week is not None and index_week[1] < index_prev_week[1]
    if pct_below is not None and pct_below >= m.weekly_breaks_flag_pct and not index_broke:
        flags.append("internal_weakness")                                # S03-R10, S03-J10
    return Breadth(pct_above_ma200, zone, pct_below, candle, flags)


V1_ASSETS = {"equities"}


def preferred_assets(quad: str) -> dict:
    """S03-R4 (066 slide) and decision 1: v1 trades only US stocks and sector ETFs, so other asset types are
    shown as context and expressed through sectors in stage 4 (D-S03-7), never traded directly."""
    assets = list(_m().quadrant_assets[quad])
    return dict(preferred=assets, tradable_in_v1=[a for a in assets if a in V1_ASSETS],
                context_only=[a for a in assets if a not in V1_ASSETS])
