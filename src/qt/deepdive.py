"""Stage 7: the company deep dive (074-076, 078, 098-099, 103, 107).

The code computes the numbers and the flags; the story, the KPIs, the thesis breakers and the ranking to
hot/cold are the analyst agent's judgment written on the card (S07-R27, R29, R30, R32, R35).
Red flags lower confidence; only unhealthy finances (S07-R7) rule out a long, and for a short the same
flags are support (D-S07-3 (b)). The gate to the watchlist is questions 1-3 plus a catalyst (D-S07-8 (a)).
A multiple is used only for the target price, never as "cheap" or "expensive" (D-S07-4, D-S07-7).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean
from typing import Literal, Mapping, Sequence

from qt.policy import load_policy
from qt.screening import mismatch

Direction = Literal["long", "short"]


def _d():
    return load_policy().deepdive


def pct(new: float, old: float) -> float:
    return (new / old - 1) * 100.0


# ---------------------------------------------------------------- the statements (S07-R5..R12, R24, R37)

def core_trend(lines: Mapping[str, Sequence[float]]) -> dict[str, str]:
    """S07-R5: each core line (revenue, gross profit, operating income, EPS, operating cash flow) compared
    YoY, oldest first. up = every step up; down = every step down; else mixed."""
    out = {}
    for name, v in lines.items():
        steps = [b - a for a, b in zip(v, v[1:])]
        out[name] = "up" if all(s > 0 for s in steps) else "down" if all(s < 0 for s in steps) else "mixed"
    return out


@dataclass
class OperatingLeverage:
    revenue_growth: float
    cogs_growth: float
    gross_before: float
    gross_after: float
    direction: Literal["positive", "negative", "neutral"]
    flags: list[str]


def operating_leverage(rev0: float, rev1: float, cogs0: float, cogs1: float) -> OperatingLeverage:
    """S07-R9: revenue growing faster than cost of goods = margins up (positive); the reverse is a red flag."""
    rg, cg = pct(rev1, rev0), pct(cogs1, cogs0)
    d = "positive" if rg > cg else "negative" if cg > rg else "neutral"
    return OperatingLeverage(rg, cg, rev0 - cogs0, rev1 - cogs1, d,
                             ["negative_operating_leverage"] if d == "negative" else [])


def profit_source(rev0: float, rev1: float, costs0: float, costs1: float) -> str:
    """S07-R10: earnings growth from the top (revenue), the bottom (cost cuts) or both."""
    top, bottom = rev1 > rev0, costs1 < costs0
    return "both" if top and bottom else "top" if top else "bottom" if bottom else "none"


def acceleration(recent_growth: Sequence[float], avg_5y: float) -> list[str]:
    """S07-R11: the last 1-2 quarters clearly above the 5-year average => find the factor."""
    return ["acceleration_find_the_factor"] if mean(recent_growth) - avg_5y >= _d().acceleration_pp else []


def gap_vs_sector(stock: tuple[float, float], sector: tuple[float, float]) -> tuple[str, list[str]]:
    """S07-R12: growth gap vs the sector over two years. A narrowing gap = pricing risk."""
    g0, g1 = stock[0] - sector[0], stock[1] - sector[1]
    band = _d().gap_stable_pp
    if g1 < g0 - band:
        return "narrowing", ["pricing_risk_gap_narrowing"]
    return ("widening" if g1 > g0 + band else "stable"), []


@dataclass
class Health:
    state: Literal["healthy", "unhealthy", "neutral"]
    ok: bool                     # fits the direction: an unhealthy long is not ok, an unhealthy short is
    note: str


def health(gross_profit_yoy: Sequence[float], debt: tuple[float, float],
           interest: tuple[float, float] | None = None, direction: Direction = "long") -> Health:
    """S07-R7: gross profit falling (YoY, the last two quarters) with debt or interest rising = "really not
    healthy". Rules out a long (D-S07-3 (b)); for a short it supports the idea."""
    gp_down = len(gross_profit_yoy) >= 2 and all(x < 0 for x in gross_profit_yoy[-2:])
    gp_up = bool(gross_profit_yoy) and all(x > 0 for x in gross_profit_yoy[-2:])
    debt_up = debt[1] > debt[0] or (interest is not None and interest[1] > interest[0])
    if gp_down and debt_up:
        if direction == "long":
            return Health("unhealthy", False, "S07-R7: gross profit falling while debt/interest rise: long ruled out")
        return Health("unhealthy", True, "S07-R7: unhealthy finances support the short")
    if gp_up and debt[1] <= debt[0]:
        return Health("healthy", True, "gross profit rising, debt stable")
    return Health("neutral", True, "")


def fcf_from_operations(cfo: float, capex: float, financing_inflow: float = 0.0) -> tuple[float, list[str]]:
    """S07-R8: FCF from the core business only; cash from issuing debt or shares is kept apart."""
    fcf = cfo - capex
    return fcf, (["cash_from_financing_not_operations"] if fcf <= 0 < financing_inflow else [])


def capital_structure(shares: tuple[float, float], buyback: bool = False, debt_issued: bool = False) -> list[str]:
    """S07-R6 (1): share issuance = dilution (negative); buyback; debt issuance."""
    out = []
    if shares[1] > shares[0]:
        out.append("dilution")
    if buyback:
        out.append("buyback")
    if debt_issued:
        out.append("debt_issued")
    return out


def net_debt_to_ebitda(debt_and_liabilities: float, cash_and_liquid: float, operating_income: float) -> float:
    """S07-R24 (074): (all debt and liabilities - cash and liquid assets) / operating income."""
    return (debt_and_liabilities - cash_and_liquid) / operating_income


def net_debt_trend(values: Sequence[float]) -> tuple[str, list[str]]:
    """S07-R24: a notable rise over the years is a red flag (Ford)."""
    steps = [b > a for a, b in zip(values, values[1:])]
    if steps and values[-1] > values[0] and sum(steps) / len(steps) >= _d().net_debt_rising_share:
        return "rising", ["net_debt_ebitda_rising"]
    falling = steps and values[-1] < values[0] and sum(not s for s in steps) / len(steps) >= _d().net_debt_rising_share
    return ("falling" if falling else "flat"), []


def liquidity(cash: float, near_liabilities: float, current_assets: float, direction: Direction = "long") -> list[str]:
    """S07-R37: low cash and near-term liabilities above the assets => raising money and dilution expected.
    A long waits; a short may be examined."""
    if near_liabilities > current_assets and cash < near_liabilities:
        return ["weak_liquidity_long_waits"] if direction == "long" else ["weak_liquidity_supports_short"]
    return []


# ---------------------------------------------------------------- growth vs sector and guidance (S07-R12, R19, R25, R33, R34)

def guidance_growth(prev_year_revenue: float, guide_low: float, guide_high: float | None = None) -> tuple[float, float, float]:
    """S07-R34: expected growth = guidance midpoint / last year's revenue - 1; also the range ends."""
    hi = guide_low if guide_high is None else guide_high
    mid = (guide_low + hi) / 2
    return pct(mid, prev_year_revenue), pct(guide_low, prev_year_revenue), pct(hi, prev_year_revenue)


def extreme_growth(growth_pct: float) -> list[str]:
    """S07-R25: very high expected growth => check what it's built of; never a rejection."""
    return ["verify_extreme_growth"] if abs(growth_pct) >= _d().extreme_growth_pct else []


def source_mismatch(a: float, b: float) -> bool:
    """D-S07-6 (b) + (a): relative gap above 10%, or a different sign (profit vs loss)."""
    if (a < 0) != (b < 0):
        return True
    return mismatch(a, b) if b else a != b


@dataclass
class Premium:
    pct: float
    justified: Literal["justified", "unjustified", "unclear"] | None
    reason: str
    flags: list[str]


def premium(pe_stock: float, pe_sector: float, justified: str | None = None, reason: str = "") -> Premium:
    """S07-R19: premium = forward P/E stock / sector - 1. A premium must be justified from the statements
    (growth engine, operating leverage, health); the agent writes justified / unjustified / unclear + reason."""
    p = pct(pe_stock, pe_sector)
    flags = []
    if p > 0:
        if justified not in ("justified", "unjustified", "unclear") or not reason.strip():
            flags.append("premium_needs_justification")
        elif justified == "unjustified":
            flags.append("premium_unjustified")
        elif justified == "unclear":
            flags.append("premium_unclear")
    return Premium(p, justified, reason, flags)


# ---------------------------------------------------------------- target price (S07-R26, D-S07-4)

@dataclass
class Target:
    price: float
    method: Literal["pe", "ps"]
    multiple: float
    multiple_source: Literal["hist_3y", "sector"]
    flags: list[str] = field(default_factory=list)
    peer_multiple: float | None = None        # regression on peers: info only (calculator-hunt)


def target_price(profitable: bool, forward_value: float, hist_multiples: Sequence[float] = (),
                 sector_multiple: float | None = None, shares: float | None = None,
                 peers: Sequence[tuple[float, float]] = (), growth: float | None = None) -> Target:
    """D-S07-4 (a): reasonable multiple = the 3-year average P/E (profitable; forward_value = next-year EPS, F2)
    or P/S (not profitable; forward_value = expected sales, divided by the shares). Without 3 years of history:
    the sector multiple, flagged. Peers (growth, multiple) give a regression multiple, shown as info only."""
    n = _d().target_history_years
    flags = []
    if len(hist_multiples) >= n:
        mult, src = mean(hist_multiples[-n:]), "hist_3y"
    elif sector_multiple is not None:
        mult, src = sector_multiple, "sector"
        flags.append("no_3y_history_sector_multiple")
    else:
        raise ValueError("no multiple: need 3 years of history or the sector multiple (D-S07-4)")
    if profitable:
        price, method = mult * forward_value, "pe"
    else:
        if not shares:
            raise ValueError("P/S target needs the share count (target market cap / shares)")
        price, method = mult * forward_value / shares, "ps"
    peer = peer_regression(peers, growth) if peers and growth is not None else None
    return Target(round(price, 2), method, mult, src, flags, peer)


def peer_regression(peers: Sequence[tuple[float, float]], growth: float) -> float | None:
    """Least squares multiple ~ growth among peers (P/S ~ SG1 or P/E ~ EG1), evaluated at the stock's growth."""
    if len(peers) < 3:
        return None
    xs, ys = [p[0] for p in peers], [p[1] for p in peers]
    mx, my = mean(xs), mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return my + b * (growth - mx)


# ---------------------------------------------------------------- surprises (S07-R31)

@dataclass
class Surprises:
    beat_share: float
    avg_pct: float
    trend: Literal["improving", "worsening", "flat"]
    usually_beats: bool


def surprises(history_pct: Sequence[float]) -> Surprises:
    """S07-R31: EPS/revenue surprises in percent over quarters, oldest first: frequency, size, direction."""
    beats = sum(1 for x in history_pct if x > 0) / len(history_pct)
    half = len(history_pct) // 2
    a, b = mean(history_pct[:half] or history_pct), mean(history_pct[half:])
    trend = "improving" if b > a else "worsening" if b < a else "flat"
    return Surprises(beats, mean(history_pct), trend, beats >= _d().surprise_usually_beats)


# ---------------------------------------------------------------- the card and the gate (S07-R3, R13, D-S07-8)

@dataclass
class Card:
    ticker: str
    direction: Direction
    answers: dict[str, bool | None] = field(default_factory=dict)   # the ten questions (policy deepdive.questions)
    reasons: list[str] = field(default_factory=list)                # tags of why the idea works
    story: str = ""                                                 # e.g. value_trap, turnaround, momentum
    kpis: list[str] = field(default_factory=list)                   # S07-R27
    thesis_breakers: list[str] = field(default_factory=list)        # S07-R29
    health: Health | None = None
    flags: list[str] = field(default_factory=list)


@dataclass
class GateResult:
    outcome: Literal["watchlist", "watchlist_cold", "rejected"]
    heat: Literal["cold"] | None
    reasons: list[str]
    lower_confidence: list[str]


def gate(card: Card) -> GateResult:
    """D-S07-8 (a): questions 1-3 must be yes; no expected catalyst => the watchlist as cold (S08-R33);
    unhealthy finances rule out a long (D-S07-3 (b)); valuation alone is never a reason (D-S07-7 (a)).
    The other questions and the red flags lower confidence only. A pass goes to the watchlist, never to a
    trade (S07-R13)."""
    d = _d()
    reasons = [f"question_failed:{q}" for q in d.gate_questions if card.answers.get(q) is not True]
    if card.health is not None and not card.health.ok and card.direction == "long":
        reasons.append("S07-R7_unhealthy_long")
    real = [r for r in card.reasons if r not in d.valuation_only_reasons]
    if not real:
        reasons.append("valuation_is_not_a_reason" if card.reasons else "no_reason_written")
    lower = [f"question_open:{q}" for q in d.questions
             if q not in d.gate_questions and q != "catalyst_expected" and card.answers.get(q) is not True]
    lower += [f for f in card.flags if not (card.direction == "short" and f in SHORT_SUPPORT)]
    if not card.thesis_breakers:
        lower.append("thesis_breakers_missing")
    if reasons:
        return GateResult("rejected", None, reasons, lower)
    if card.answers.get("catalyst_expected") is not True:
        return GateResult("watchlist_cold", "cold", ["no_expected_catalyst"], lower)
    return GateResult("watchlist", None, [], lower)


# red flags that support a short instead of lowering confidence (D-S07-3 (b))
SHORT_SUPPORT = {"negative_operating_leverage", "pricing_risk_gap_narrowing", "dilution", "net_debt_ebitda_rising",
                 "cash_from_financing_not_operations", "premium_unjustified", "weak_liquidity_supports_short"}
