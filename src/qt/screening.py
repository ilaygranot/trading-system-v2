"""Stage 6: quantitative screen and growth profiles.

Input is one group (a sector, or an industry run separately, as the lecturer does with Semiconductors):
rows from the monthly Zacks export (qt.data.zacks) or another source with the same fields.
Profiles compare a stock to the averages of the group it was screened in (decision 12).
Every exclusion carries the rule id that caused it; default-tier rules become flags in the rules engine.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Iterable, Literal

from qt.policy import load_policy


@dataclass
class StockRow:
    ticker: str
    exchange: str = "NASDAQ"
    market_cap: float | None = None        # dollars
    price: float | None = None
    sales: float | None = None             # annual sales (last reported year), dollars
    sales_f1: float | None = None          # current-year sales consensus
    f0: float | None = None                # EPS last reported year
    f1: float | None = None                # EPS consensus current year
    f2: float | None = None                # EPS consensus next year
    short_float: float | None = None       # percent
    days_to_cover: float | None = None
    industry: str = ""
    second_source: dict[str, float] = field(default_factory=dict)  # e.g. {"f2": 2.04} from Koyfin/Yahoo


@dataclass
class Metrics:
    row: StockRow
    eg1: float | None = None
    eg2: float | None = None
    sg1: float | None = None
    pe: float | None = None
    pe1: float | None = None
    pe2: float | None = None
    ps: float | None = None
    ps1: float | None = None
    peg: float | None = None
    flags: list[str] = field(default_factory=list)
    coded: set[str] = field(default_factory=set)   # which of eg1/eg2 got the ±99 code
    track: str | None = None               # missing_data | negative_pe


def growth(prev: float, nxt: float) -> tuple[float, bool]:
    """EPS growth in percent with the ±99% code for a negative base (S06-R14, 097/098):
    prev < 0 -> +99 if next > 0 else -99; prev >= 0 and next < 0 -> -99; else next/prev - 1."""
    code = load_policy().screening.coded_growth_pct
    if prev < 0:
        return (code if nxt > 0 else -code), True
    if nxt < 0:
        return -code, True
    return (nxt / prev - 1) * 100.0, False


def round_up_threshold(avg_pct: float) -> float:
    """'Above average' threshold rounded up to the next half point (11.8 -> 12.0; 22.19 -> 22.5). Inferred from 098/097."""
    return math.ceil(avg_pct * 2 - 1e-9) / 2


def mismatch(a: float, b: float) -> bool:
    """Two sources differ by more than 10% => star, don't rely on the number (S06-R43)."""
    return abs(a / b - 1) * 100.0 > load_policy().screening.mismatch_pct


def compute(row: StockRow) -> Metrics:
    s = load_policy().screening
    m = Metrics(row)
    core = (row.f0, row.f1, row.f2, row.sales, row.sales_f1)
    if any(v is None for v in core):
        m.track = "missing_data"                                  # S06-R17
        return m
    f0, f1, f2 = row.f0, row.f1, row.f2
    if 0 in (f0, f1, f2):
        f0, f1, f2 = (s.eps_zero_replacement if v == 0 else v for v in (f0, f1, f2))
        m.flags.append("eps_zero_replaced")                       # S06-R15, D-S06-4
    m.eg1, c1 = growth(f0, f1)
    m.eg2, c2 = growth(f1, f2)
    m.coded = {k for k, c in (("eg1", c1), ("eg2", c2)) if c}
    if m.coded:
        m.flags.append("coded_99")
    if (not c1 and abs(f0) <= s.base_effect_eps) or (not c2 and abs(f1) <= s.base_effect_eps):
        m.flags.append("base_effect")
    if (not c1 and m.eg1 >= s.verify_growth_pct) or (not c2 and m.eg2 >= s.verify_growth_pct):
        m.flags.append("growth_over_200_verify")                  # S06-R21
    m.sg1 = (row.sales_f1 / row.sales - 1) * 100.0 if row.sales else None
    if row.price:
        m.pe, m.pe1, m.pe2 = row.price / f0, row.price / f1, row.price / f2
    if row.market_cap:
        m.ps = row.market_cap / row.sales if row.sales else 0.0
        m.ps1 = row.market_cap / row.sales_f1 if row.sales_f1 else 0.0
    if m.pe is not None and m.pe > 0 and m.eg1 and m.eg1 > 0:
        m.peg = m.pe / m.eg1                                       # S06-R16: growth as a whole number
    if m.pe is not None and m.pe < 0:
        m.track = "negative_pe"                                   # S06-R18
    for k, alt in row.second_source.items():
        v = getattr(row, k, None)
        if v and alt and mismatch(v, alt):
            m.flags.append("star_mismatch")
            break
    return m


@dataclass
class GroupAverages:
    sg1: float
    eg1: float
    eg2: float
    pe: float | None
    ps: float | None
    median_eg1: float
    median_eg2: float
    excluded: dict[str, list[str]]

    @property
    def sg1_threshold(self) -> float:
        return round_up_threshold(self.sg1)


def _growth_ok(m: Metrics, which: str) -> bool:
    s = load_policy().screening
    v = getattr(m, which)
    if v is None or which in m.coded:
        return False
    base = m.row.f0 if which == "eg1" else m.row.f1
    if base is not None and abs(base) <= s.base_effect_eps:
        return False
    return abs(v) <= s.base_effect_growth_pct and abs(v) <= s.outlier_growth_pct


def averages(metrics: Iterable[Metrics]) -> GroupAverages:
    """Group averages for screening (D-S06-5 (c): mean after exclusions; median for display).
    Excluded from growth averages: coded ±99, |base EPS| <= 0.10, |growth| > 200% (D-S06-6 (c)).
    Excluded from P/E average: P/E > 500 or negative (S06-R20). Excluded stocks stay in the sheet."""
    s = load_policy().screening
    ms = [m for m in metrics if m.track != "missing_data"]
    excluded: dict[str, list[str]] = {"eg1": [], "eg2": [], "pe": []}
    vals: dict[str, list[float]] = {"eg1": [], "eg2": []}
    for m in ms:
        for k in ("eg1", "eg2"):
            if _growth_ok(m, k):
                vals[k].append(getattr(m, k))
            else:
                excluded[k].append(m.row.ticker)
    pes = []
    for m in ms:
        if m.pe is not None and 0 < m.pe <= s.pe_outlier:
            pes.append(m.pe)
        else:
            excluded["pe"].append(m.row.ticker)
    sg = [m.sg1 for m in ms if m.sg1 is not None]
    ps = [m.ps for m in ms if m.ps]
    mean = lambda xs: sum(xs) / len(xs) if xs else 0.0
    return GroupAverages(mean(sg), mean(vals["eg1"]), mean(vals["eg2"]), mean(pes) if pes else None,
                         mean(ps) if ps else None,
                         statistics.median(vals["eg1"]) if vals["eg1"] else 0.0,
                         statistics.median(vals["eg2"]) if vals["eg2"] else 0.0, excluded)


def universe_failures(row: StockRow, side: Literal["long", "short"]) -> list[str]:
    """Universe gates with the rule that fails each (decision 11, S06-R5/R6/R8/R9/R10/R24)."""
    f = load_policy().floors
    s = load_policy().screening
    out = []
    if row.exchange.upper() not in s.exchanges:
        out.append("S06-R5")
    if side == "long":
        if row.market_cap is not None and row.market_cap < f.long_min_market_cap:
            out.append("S06-R6")
        if row.price is not None and row.price <= f.long_min_price:
            out.append("S06-R9")
        if row.sales is not None and row.sales <= f.long_min_revenue:
            out.append("S06-R10")
        if row.f2 is not None and row.f2 <= 0:
            out.append("S06-R24")
    else:
        if row.market_cap is not None and row.market_cap < f.short_min_market_cap:
            out.append("S06-R8")
        if row.short_float is not None and row.short_float > s.short_float_max_pct:
            out.append("S06-R36")
        if row.days_to_cover is not None and row.days_to_cover > s.days_to_cover_max:
            out.append("S06-R37")
    return out


def priority_tag(row: StockRow, side: Literal["long", "short"]) -> bool:
    """Priority band by market cap (decision 11): long $2-30B, short >= $20B. A tag, not a gate."""
    f = load_policy().floors
    if row.market_cap is None:
        return False
    if side == "long":
        lo, hi = f.long_priority_cap_range
        return lo <= row.market_cap <= hi
    return row.market_cap >= f.short_priority_min_cap


def classify_long(e1: float, e2: float, a1: float, a2: float, f0: float, f1: float, f2: float) -> tuple[str | None, list[str]]:
    """Long profile tree (S06 integration logic, decision 12). Order: L7 -> L6/L8 -> L5 -> L1..L4."""
    band = load_policy().screening.similar_band_pp
    tags = []
    if f0 > 0 and f1 < 0 and f2 > 0:
        return None, ["profit_loss_profit_rejected"]               # S06-R41 (ATRO)
    if f1 <= 0 < f2 and f0 <= 0:
        return "L7", tags
    if f0 < 0 < f1:
        tags.append("turns_profitable_this_year")                  # ESTC: L1 with a star
    if e1 < 0 < e2:
        return ("L6" if e2 > a2 else "L8"), tags
    if abs(e1 - a1) <= band and abs(e2 - a2) <= band:
        return "L5", tags
    if e1 > a1 and e2 > a2 and e2 - e1 > band:
        return "L1", tags
    if e1 > a1 and e2 > a2 and abs(e2 - e1) <= band:
        return "L2", tags
    if e1 > a1 and e2 < e1 - band:
        if e2 <= a2:
            tags.append("y2_below_sector")                         # TDG
        return "L3", tags
    if e1 <= a1 and e2 > a2:
        return "L4", tags
    return None, tags


def classify_short(e1: float, e2: float, a1: float, a2: float) -> str | None:
    """Short gate D-S06-11 (a): both years below the group average; then acute > consistent > relative."""
    if not (e1 < a1 and e2 < a2):
        return None
    if e1 < 0 and e2 < e1 and a1 > 0 and a2 > 0:
        return "S_acute"
    if e1 < 0 and e2 < 0:
        return "S_consistent"
    return "S_relative"


def rank_key(profile: str, tags: list[str]) -> int:
    order = load_policy().screening.long_rank
    key = "L3*" if profile == "L3" and "y2_below_sector" in tags else profile
    return order.index(key)


def rank_long(items: list[tuple[str, str, list[str]]]) -> list[str]:
    """items: (ticker, profile, tags) -> tickers in rank order; L5 and unclassified are dropped."""
    keep = [(t, p, g) for t, p, g in items if p in {"L1", "L2", "L3", "L4", "L6", "L7", "L8"}]
    return [t for t, p, g in sorted(keep, key=lambda x: rank_key(x[1], x[2]))]


def short_gate_conflict(m: Metrics) -> bool:
    """S06-J6 vs S06-R34: loss -> near-zero profit -> huge jump (BA 2023). Fails the short gate on growth,
    but a course principle says consider it as a short: pass it on flagged, never drop silently (step 5א)."""
    r = m.row
    if None in (r.f0, r.f1, r.f2):
        return False
    near_zero = 0 < r.f1 <= max(0.1, 0.1 * abs(r.f0))
    return r.f0 < 0 and near_zero and (m.eg2 or 0) >= load_policy().screening.verify_growth_pct


def guidance_change_pct(new: float, prev_low: float, prev_high: float | None = None) -> float:
    """Change vs the company's own previous guidance (midpoint of a range), decision 13."""
    base = prev_low if prev_high is None else (prev_low + prev_high) / 2
    return (new / base - 1) * 100.0


def is_guidance_catalyst(pct: float) -> bool:
    return abs(pct) >= load_policy().screening.guidance_change_min_pct


def loss_to_profit(this_year: float, next_year: float, year_after: float) -> bool:
    """S06-R40: loss now, next year profit >= 2x the loss (inferred), and the year after higher."""
    k = load_policy().screening.loss_to_profit_multiple
    return this_year < 0 and next_year >= k * abs(this_year) - 1e-9 and year_after > next_year


# DC-08: the patterns in code that may turn a default failure around, and the J rule each one opens (decision cards 6).
# S06-J14 (news that changes the prospect) needs headlines the screen does not have: it is not detected here.
PATTERNS = {"near_zero_jump": "S06-J6", "extreme_expectations": "S06-R42", "no_profile": "S06-J15"}


def rescue_patterns(m: Metrics, failed: list[str], avg: "GroupAverages") -> list[str]:
    """The DC-08 patterns on a stock that failed a default rule: loss -> profit "on zero" -> huge jump (S06-J6),
    expectations above the verification threshold (S06-R42, the course gives no number: S06-R21's 200%), and a
    stock that fits no profile (S06-J15): the profit -> loss -> profit rejection (S06-R41), or no profile while one
    year grows above the group (the "flying" stock the screen missed; the growth condition is not from course, so
    every stock below the group does not reach the card)."""
    out = []
    if short_gate_conflict(m):
        out.append("near_zero_jump")
    v = load_policy().screening.verify_growth_pct
    if any(x is not None and abs(x) >= v and k not in m.coded for k, x in (("eg1", m.eg1), ("eg2", m.eg2))):
        out.append("extreme_expectations")
    above = any(x is not None and x > a for x, a in ((m.eg1, avg.eg1), (m.eg2, avg.eg2)))
    if failed and (failed[0] == "S06-R41" or (failed[0] == "no_profile" and above)):
        out.append("no_profile")
    return out


@dataclass
class Rescue:
    """A DC-08 candidate: failed a default rule, passed every hard floor, and a pattern applies."""
    ticker: str
    failed: list[str]
    patterns: list[str]
    metrics: Metrics


@dataclass
class Candidate:
    ticker: str
    side: str
    profile: str | None
    tags: list[str]
    metrics: Metrics
    triple_filter: bool = False
    conflicts: list[str] = field(default_factory=list)


@dataclass
class ScreenResult:
    averages: GroupAverages
    long: list[Candidate]
    short: list[Candidate]
    tracks: dict[str, list[str]]
    rejected: dict[str, list[str]]
    rescue: list[Rescue] = field(default_factory=list)


def run(rows: list[StockRow]) -> ScreenResult:
    """Screen one group. Averages are taken over the whole group that was screened (decision 12)."""
    metrics = [compute(r) for r in rows]
    avg = averages(metrics)
    tracks: dict[str, list[str]] = {"missing_data": [], "negative_pe": []}
    rejected: dict[str, list[str]] = {}
    longs, shorts = [], []
    for m in metrics:
        t = m.row.ticker
        if m.track == "missing_data":
            tracks["missing_data"].append(t)
            continue
        if m.track == "negative_pe":
            tracks["negative_pe"].append(t)
        lf = universe_failures(m.row, "long")
        sg_ok = m.sg1 is not None and m.sg1 > avg.sg1_threshold          # 098 gate (D-S06-7 (c))
        if not lf and sg_ok:
            prof, tags = classify_long(m.eg1, m.eg2, avg.eg1, avg.eg2, *(m.row.f0, m.row.f1, m.row.f2))
            triple = m.eg1 > avg.eg1 and m.eg2 > avg.eg2
            extra = (["triple_filter_A"] if triple else []) + (["priority_cap"] if priority_tag(m.row, "long") else [])
            if prof and prof != "L5":
                longs.append(Candidate(t, "long", prof, tags + extra + m.flags, m, triple))
            else:
                rejected[t] = ["S06-R31" if prof == "L5" else "S06-R41" if "profit_loss_profit_rejected" in tags else "no_profile"] + tags
        else:
            rejected.setdefault(t, []).extend(lf or ["S06-R22"])
        sf = universe_failures(m.row, "short")
        sprof = classify_short(m.eg1, m.eg2, avg.eg1, avg.eg2)
        if sprof and not sf:
            shorts.append(Candidate(t, "short", sprof, (["priority_cap"] if priority_tag(m.row, "short") else []) + m.flags, m))
        elif not sf and short_gate_conflict(m):
            shorts.append(Candidate(t, "short", None, ["short_gate_override"], m,
                                    conflicts=["S06-R34: growth above the group average, not a short",
                                               "S06-J6: loss -> near-zero profit -> huge jump, consider as a short"]))
    hard = {r for r, x in _tiers().items() if x == "hard"}
    passed = {c.ticker for c in longs + shorts}
    by_t = {m.row.ticker: m for m in metrics}
    rescue = []
    for t, failed in rejected.items():
        if t in passed or hard & set(failed):
            continue
        pats = rescue_patterns(by_t[t], failed, avg)
        if pats:
            rescue.append(Rescue(t, failed, pats, by_t[t]))
    longs.sort(key=lambda c: rank_key(c.profile, c.tags))
    shorts.sort(key=lambda c: load_policy().screening.short_rank.index(c.profile) if c.profile else 99)
    return ScreenResult(avg, longs, shorts, tracks, rejected, rescue)


def _tiers() -> dict[str, str]:
    """rule id -> tier, for the hard floors a DC-08 candidate must have passed."""
    from qt.playbook import load_playbook
    return {rid: r.tier.value for rid, r in load_playbook().rules.items() if rid.startswith("S06-")}
