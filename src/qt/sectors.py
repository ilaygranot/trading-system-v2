"""Stages 4-5: the sector scorecard (097) and the industry map.

Score per sector = technical rank (RS vs SPY over 5 periods, 1..11) + fundamental rank (expected growth,
1..11) + macro points (0/3/6/9). Top 3 long, bottom 3 short, the rest neutral (D-S04-2 (a), D-S04-3).
Confirmations (RS vs its moving averages, cycle pairs, expectation change, three horizons) never enter the
score; they are flags that lower confidence (D-S04-8 (a)). The sector agent (A2) may change a macro score
with a reason and sub-industry type overrides (A&D defensive); both reach Ilay.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal, Mapping, Sequence

from qt.policy import load_policy
from qt.screening import Metrics, averages, compute, StockRow


def _s():
    return load_policy().sector_scorecard


Phase = Literal["recovery", "expansion", "slowdown", "recession"]


# ---------------------------------------------------------------- technical: relative strength

def rs_from_returns(r_sector_pct: float, r_bench_pct: float) -> float:
    """Relative performance over a period from absolute returns: (1 + r_s) / (1 + r_b) - 1, in percent."""
    return ((1 + r_sector_pct / 100) / (1 + r_bench_pct / 100) - 1) * 100


def rs_perf(sector: Sequence[float], bench: Sequence[float]) -> dict[str, float | None]:
    """S04-R10/R11: RS = sector / SPY; per period RS_now / RS_then - 1. Periods without history are None."""
    rs = [a / b for a, b in zip(sector, bench)]
    out: dict[str, float | None] = {}
    for name, n in _s().rs_periods.items():
        out[name] = (rs[-1] / rs[-1 - n] - 1) * 100 if len(rs) > n else None
    return out


def rs_avg(perf: Mapping[str, float | None]) -> float:
    """S04-R12: simple average of the periods that are filled (097 RS FLOW)."""
    vals = [v for v in perf.values() if v is not None]
    if not vals:
        raise ValueError("no RS period available")
    return sum(vals) / len(vals)


def rs_vs_mas(sector: Sequence[float], bench: Sequence[float]) -> dict[str, bool | None]:
    """S04-R14: RS above its quarterly / yearly moving average. Confirmation only."""
    rs = [a / b for a, b in zip(sector, bench)]
    out = {}
    for k, n in _s().rs_ma.items():
        out[f"above_{k}"] = rs[-1] > sum(rs[-n:]) / n if len(rs) >= n else None
    return out


def rank(values: Mapping[str, float], tiebreak: Mapping[str, float] | None = None) -> dict[str, int]:
    """Highest value gets n, lowest gets 1 (097: 11..1)."""
    tb = tiebreak or {}
    order = sorted(values, key=lambda k: (values[k], tb.get(k, 0.0)))
    return {k: i + 1 for i, k in enumerate(order)}


# ---------------------------------------------------------------- fundamental: expected growth

@dataclass
class GroupGrowth:
    sg1: float
    eg1: float
    eg2: float
    n: int
    excluded: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        """D-S04-4 (a): average of SG1 and EG1; EG2 breaks ties."""
        return (self.sg1 + self.eg1) / 2

    @property
    def worst(self) -> bool:
        """Both sales and earnings expected to fall: fundamental 'worst', short / avoid candidate (097)."""
        return self.sg1 < 0 and self.eg1 < 0


def group_growth(rows: Iterable[StockRow]) -> GroupGrowth:
    """Cleaned averages with the stage-6 formulas and exclusions (S04-R15/R16)."""
    ms: list[Metrics] = [compute(r) for r in rows]
    avg = averages(ms)
    ok = [m for m in ms if m.track != "missing_data"]
    return GroupGrowth(avg.sg1, avg.eg1, avg.eg2, len(ok), sorted(set(avg.excluded["eg1"])))


def fundamental_ranks(growth: Mapping[str, GroupGrowth]) -> dict[str, int]:
    return rank({k: g.score for k, g in growth.items()}, {k: g.eg2 for k, g in growth.items()})


def expectation_change(prev: GroupGrowth, now: GroupGrowth) -> str:
    """S04-R34: vs last month's pull. Any decline is flagged until a threshold is set (AT-S04-7)."""
    if now.score < prev.score:
        return "weakening"
    return "improving" if now.score > prev.score else "unchanged"


# ---------------------------------------------------------------- macro points

def cycle_phase(ism_quadrant: str, permits_quadrant: str | None = None) -> tuple[Phase, list[str]]:
    """S04-R6: phase from ISM (096 compass of the indicator) with building permits as a check."""
    flags = []
    if permits_quadrant and permits_quadrant != ism_quadrant:
        flags.append(f"permits_phase_{permits_quadrant}_differs")
    return ism_quadrant, flags          # type: ignore[return-value]


def macro_points(etf: str, phase: Phase, zone: str) -> tuple[int, str]:
    """D-S04-6 (c): cycle table sign (S04-R5) => 9/6/3/0, tilted by the stance (S04-R7): hot favours cyclicals,
    cold favours defensives; extreme zones no tilt (neutral / cautious). The agent may change it with a reason."""
    s = _s()
    sign = s.cycle_table[phase].get(etf, "")
    pts = s.macro_points[sign]
    t = s.type[etf]
    shift = s.stance_shift
    if zone == "hot":
        pts += shift if t == "cyclical" else -shift if t == "defensive" else 0
    elif zone == "cold":
        pts += shift if t == "defensive" else -shift if t == "cyclical" else 0
    return max(0, min(9, pts)), sign


# ---------------------------------------------------------------- scorecard

@dataclass
class SectorScore:
    etf: str
    type: str
    rs: dict[str, float | None]
    rs_avg: float
    tech_rank: int
    growth: GroupGrowth | None
    fund_rank: int
    macro: int
    cycle_sign: str
    total: int
    bucket: Literal["long", "neutral", "short"] = "neutral"
    flags: list[str] = field(default_factory=list)


@dataclass
class Scorecard:
    phase: str
    zone: str
    sectors: dict[str, SectorScore]
    long: list[str]
    short: list[str]
    neutral: list[str]
    flags: list[str]


def scorecard(rs: Mapping[str, Mapping[str, float | None]], growth: Mapping[str, GroupGrowth],
              phase: Phase, zone: str, macro_override: Mapping[str, tuple[int, str]] | None = None,
              confirmations: Mapping[str, list[str]] | None = None) -> Scorecard:
    """rs: per ETF the 5-period RS performance; growth: per ETF the mapped Zacks averages (D-S04-5)."""
    s = _s()
    etfs = list(rs)
    avgs = {e: rs_avg(rs[e]) for e in etfs}
    tech = rank(avgs)
    fund = fundamental_ranks({e: growth[e] for e in etfs})
    out: dict[str, SectorScore] = {}
    for e in etfs:
        pts, sign = macro_points(e, phase, zone)
        flags = list((confirmations or {}).get(e, []))
        if macro_override and e in macro_override:
            pts, why = macro_override[e]
            if pts not in (0, 3, 6, 9):
                raise ValueError("macro points are 0/3/6/9 (S04-R17)")
            flags.append(f"macro_override:{why}")
        g = growth[e]
        if g.worst:
            flags.append("fundamental_worst")
        if all(v is not None and v < 0 for v in rs[e].values()):
            flags.append("rs_down_all_periods_not_long")             # S04-R13
        out[e] = SectorScore(e, s.type[e], dict(rs[e]), avgs[e], tech[e], g, fund[e], pts, sign,
                             tech[e] + fund[e] + pts, flags=flags)
    order = sorted(etfs, key=lambda e: (out[e].total, out[e].tech_rank), reverse=True)
    nl, ns = s.buckets["long"], s.buckets["short"]
    longs = [e for e in order[:nl] if "rs_down_all_periods_not_long" not in out[e].flags]
    shorts = order[-ns:] if len(order) > nl + ns else []
    for e in order:
        out[e].bucket = "long" if e in longs else "short" if e in shorts else "neutral"
    picked = [out[e].type for e in longs + shorts]
    flags = []
    if "cyclical" not in picked:
        flags.append("no_cyclical_sector_selected")                  # S04-R21
    if "defensive" not in picked:
        flags.append("no_defensive_sector_selected")
    if zone == "cold" and sum(out[e].type == "cyclical" for e in longs) > len(longs) / 2:
        flags.append("mostly_cyclical_longs_in_cold_stance")          # AT-S04-5
    return Scorecard(phase, zone, out, longs, shorts, [e for e in order if out[e].bucket == "neutral"], flags)


# ---------------------------------------------------------------- confirmations

PAIR_GROWTH_DIRECTION = {                # S04-R33 / S04-R23: direction of the ratio in a growth phase
    ("XLP", "XLY"): "down", ("XLU", "XLI"): "down", ("SMH", "XLK"): "up", ("SPY", "TLT"): "up", ("IWM", "SPY"): "up",
}


def ratio_trend(a: Sequence[float], b: Sequence[float], months: int = 3) -> str:
    """Monthly prices (S04-R32): ratio A/B now vs `months` ago."""
    r_now, r_then = a[-1] / b[-1], a[-1 - months] / b[-1 - months]
    return "up" if r_now > r_then else "down" if r_now < r_then else "flat"


def cycle_pairs_check(trends: Mapping[tuple[str, str], str], phase: Phase) -> dict:
    """Pairs confirm or contradict the phase from stage 2 (S02-J20). Mismatch lowers confidence; not a score."""
    growth = phase in ("recovery", "expansion")
    agree, conflict = [], []
    for pair, d in trends.items():
        exp = PAIR_GROWTH_DIRECTION[pair] if growth else ("up" if PAIR_GROWTH_DIRECTION[pair] == "down" else "down")
        (agree if d == exp else conflict).append("/".join(pair))
    return dict(agree=agree, conflict=conflict, flag="cycle_pairs_disagree" if len(conflict) > len(agree) else None)


def sector_confirms(rel_3y: float | None, rel_3m: float, rel_1m: float, direction: str = "long") -> tuple[bool, list[str]]:
    """S04-R31: the sector's relative performance must agree with the stock idea over 3 years, 3 months and
    the last month. No lag threshold in the course: any opposite sign is reported (inferred)."""
    sgn = 1 if direction == "long" else -1
    against = [name for name, v in (("3y", rel_3y), ("3m", rel_3m), ("1m", rel_1m)) if v is not None and sgn * v < 0]
    return ("3m" not in against and "1m" not in against), [f"sector_against_{a}" for a in against]


# ---------------------------------------------------------------- stage 5: industries

@dataclass
class IndustryView:
    name: str
    growth: GroupGrowth
    vs_sector: Literal["above", "similar", "below"]
    use_own_average: bool
    tag: Literal["priority", "neutral", "avoid"]


def industry_map(rows: Sequence[StockRow], sector_growth: GroupGrowth) -> list[IndustryView]:
    """S05-R2/R3: industry averages vs the sector. Industries with >= 15 stocks can be screened against their own
    average (D-S05-2). The tag prioritizes; it doesn't narrow the screen (D-S05-4 (b))."""
    s = _s()
    groups: dict[str, list[StockRow]] = {}
    for r in rows:
        groups.setdefault(r.industry or "unknown", []).append(r)
    out = []
    for name, rs in sorted(groups.items()):
        g = group_growth(rs)
        d = g.score - sector_growth.score
        vs = "above" if d > s.similar_band_pp else "below" if d < -s.similar_band_pp else "similar"
        tag = "priority" if vs == "above" else "avoid" if vs == "below" and g.sg1 < 0 else "neutral"
        out.append(IndustryView(name, g, vs, g.n >= s.industry_min_stocks, tag))
    return sorted(out, key=lambda v: v.growth.score, reverse=True)


def months_in_row(history: Sequence[Iterable[str]], name: str) -> int:
    """Consecutive most recent months in which `name` appears (ISM growing list, S05-R6)."""
    n = 0
    for month in reversed(history):
        if name not in set(month):
            break
        n += 1
    return n


@dataclass
class IsmMention:
    name: str
    months_in_row: int
    trend: bool
    shortage_source: Literal["supply", "demand", "unknown"] = "unknown"


def ism_industries(history: Sequence[Iterable[str]]) -> list[IsmMention]:
    """Industries growing in the latest report with their streak; a trend needs 3 months in a row (D-S05-5).
    Candidates only, never an entry signal."""
    k = _s().ism_trend_months
    latest = list(history[-1]) if history else []
    return [IsmMention(n, m, m >= k) for n in latest for m in [months_in_row(history, n)]]


def ism_commodities(reported: Mapping[str, int], sources: Mapping[str, str] | None = None) -> list[IsmMention]:
    """Commodities up in price with the report's own month count (070). Shortage source must be classified
    before inferring a direction for the sector (D-S05-1 (a), S05-J11)."""
    k = _s().ism_trend_months
    src = sources or {}
    return [IsmMention(c, m, m >= k, src.get(c, "unknown")) for c, m in reported.items()]
