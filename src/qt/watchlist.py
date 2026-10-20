"""Stage 8: the watchlist (078, 091, 098-101, 107).

Three lists (S08-R8, fix 12 of 20.10): the main list (only after stages 1-7, S08-R1), the guidance-change
list and the event list. The event list only feeds: an idea from it goes back through stages 6-7 before any
trade. Heat is the agent's judgment with a written reason (D-S08-3 (a)); frozen has proposed hard
triggers. Day 60 raises a review flag, never an automatic removal (D-S08-1 (a)). Size and the top-down
share are weekly warnings (D-S08-5, D-S08-8). The idea-to-trade gate is two steps (S08-R28, fix 7):
an early gate here (hot + news not adverse) and the full AND after stage 11.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Sequence

from qt.calc.traffic_light import Color
from qt.policy import load_policy
from qt.screening import guidance_change_pct, is_guidance_catalyst

Heat = Literal["cold", "hot", "boiling", "frozen"]
ListName = Literal["main", "guidance", "event"]
Source = Literal["top_down", "news_flow", "theme", "technical"]


def _w():
    return load_policy().watchlist


class WatchlistError(ValueError):
    """A step the Playbook doesn't allow; carries the rule id."""

    def __init__(self, rule_id: str, msg: str):
        super().__init__(f"{rule_id}: {msg}")
        self.rule_id = rule_id


@dataclass
class HeatChange:
    day: date
    old: Heat
    new: Heat
    reason: str
    by: str


@dataclass
class Entry:
    ticker: str
    direction: Literal["long", "short", "spread"]
    list: ListName
    source: Source
    added: date
    entry_price: float
    target: float | None = None                 # from stage 7 (D-S07-4)
    heat: Heat = "cold"
    explanation: dict[str, str] = field(default_factory=dict)   # S08-R3
    # spread (S08-R5): long A / short B traded as one ratio
    leg_a: str | None = None
    leg_b: str | None = None
    target_a: float | None = None
    target_b: float | None = None
    history: list[HeatChange] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)           # C-list records (D-S08-2 (a))
    archived: bool = False


def add(entry: Entry, process_complete: bool, existing: Sequence[Entry] = ()) -> Entry:
    """S08-R1: the main list only after the full process (stages 1-7). The event list takes a short review
    (D-S08-6 (b)) and only feeds. A reversed direction on a frozen ticker needs the full process (S08-R27)."""
    for e in existing:
        if e.ticker == entry.ticker and e.heat == "frozen" and e.direction != entry.direction and not process_complete:
            raise WatchlistError("S08-R27", f"{entry.ticker}: frozen {e.direction} idea can't come back as {entry.direction}")
    if entry.list == "main" and not process_complete:
        raise WatchlistError("S08-R1", f"{entry.ticker}: main list only after stages 1-7; use the event list")
    return entry


def missing_explanations(e: Entry) -> list[str]:
    """S08-R3: no ticker without an explanation."""
    return [f for f in _w().explanation_fields if not e.explanation.get(f, "").strip()]


# ---------------------------------------------------------------- computed fields (S08-R4, R5, R29)

@dataclass
class Computed:
    change_pct: float
    potential_pct: float | None
    days_on_list: int
    review_flag: bool


def computed(e: Entry, last: float, today: date) -> Computed:
    """S08-R4 (formulas inferred): change = last / entry - 1; potential = target / last - 1;
    days = today - added. Over 60 days => review flag (S08-R29)."""
    days = (today - e.added).days
    pot = (e.target / last - 1) * 100 if e.target is not None else None
    return Computed((last / e.entry_price - 1) * 100, pot, days, days > _w().review_days)


def spread_ratio(price_a: float, price_b: float) -> float:
    """S08-R5: the ratio A / B is traded as if it were a stock."""
    return price_a / price_b


def spread_target(e: Entry) -> float:
    """S08-R5: target ratio = target A / target B."""
    return e.target_a / e.target_b


# ---------------------------------------------------------------- heat (S08-R25..R27, R30, R33, D-S08-3, D-S08-7)

def set_heat(e: Entry, new: Heat, reason: str, day: date, by: str = "agent") -> Entry:
    """The agent sets heat with a written reason (D-S08-3 (a)); Ilay sees every change. Frozen = archived."""
    if not reason.strip():
        raise WatchlistError("S08-R26", "a heat change needs a written reason")
    if e.archived and new != "frozen":
        raise WatchlistError("S08-R30", "archived idea: use revive() after refreshing stages 6-7")
    e.history.append(HeatChange(day, e.heat, new, reason, by))
    e.heat = new
    e.archived = new == "frozen"
    return e


def frozen_triggers(direction: str, growth_vs_sector_pp: float | None, price_against_thesis: bool) -> list[str]:
    """D-S08-3 proposed hard triggers (101-R5); short is the mirror (20.10 decision 13).
    growth_vs_sector_pp = the idea's expected growth minus the sector average, in points."""
    out = []
    if growth_vs_sector_pp is not None:
        if direction == "long" and growth_vs_sector_pp < 0:
            out.append("growth_below_sector")
        if direction == "short" and growth_vs_sector_pp > 0:
            out.append("growth_above_sector")
    if price_against_thesis:
        out.append("price_fully_against_thesis")
    return out


def revive(e: Entry, refreshed_6_7: bool, reason: str, day: date) -> Entry:
    """D-S08-7 (a): a frozen idea comes back in the same direction after stages 6-7 are refreshed."""
    if not refreshed_6_7:
        raise WatchlistError("S08-R30", f"{e.ticker}: refresh stages 6-7 before reviving")
    e.archived = False
    e.history.append(HeatChange(day, e.heat, "cold", reason, "agent"))
    e.heat = "cold"
    return e


def reverse(e: Entry, full_process: bool) -> None:
    """S08-R27 (hard): a frozen long never becomes a short because of price or a report (or the reverse;
    for a spread, swapping the legs). The reverse idea goes through the whole process as a new entry."""
    raise WatchlistError("S08-R27", f"{e.ticker}: no direction flip; "
                         + ("add it as a new entry" if full_process else "run the full process first"))


def boiling_hint(move_2w_pct: float) -> bool:
    """S08-R25 / S13-R5: the stock already ran; propose 'boiling' and wait for a base, a flag, a retest or
    cheaper options (threshold from conduct.fomo_move_pct)."""
    return move_2w_pct >= load_policy().conduct.fomo_move_pct


# ---------------------------------------------------------------- composition (S08-R7, R9, R10, D-S08-5, D-S08-8)

def composition(entries: Sequence[Entry], macro_bias: str | None = None) -> list[str]:
    """Weekly warnings, never blocks."""
    w = _w()
    active = [e for e in entries if not e.archived]
    main = [e for e in active if e.list == "main"]
    out = []
    lo, hi = w.main_size
    if len(main) > hi:
        out.append(f"main_list_above_{hi}")
    elif len(main) < lo:
        out.append(f"main_list_below_{lo}")
    if sum(e.heat == "hot" for e in active) > w.hot_max:
        out.append(f"hot_above_{w.hot_max}")
    if len(active) > w.soft_cap_total:
        out.append(f"total_above_{w.soft_cap_total}")
    if main:
        share = sum(e.source == "top_down" for e in main) / len(main)
        if share < w.top_down_min_share:
            out.append(f"top_down_share_{share * 100:.2f}_below_50")
    dirs = {e.direction for e in main}
    if macro_bias in ("bearish", "cold") and "short" not in dirs:
        out.append("no_shorts_in_cold_stance")
    if macro_bias in ("bullish", "hot") and "long" not in dirs:
        out.append("no_longs_in_hot_stance")
    return out


# ---------------------------------------------------------------- tradability (S08-R11, R12)

def tradability(range_2y_pct: float, growth_sideways: bool, event_field: bool) -> str:
    """S08-R11/R12: a narrow band or sideways growth => not tradable (low priority or out); a field that
    doesn't produce events => low priority, kept; else tradable. The agent writes the criteria."""
    if range_2y_pct <= _w().narrow_range_pct or growth_sideways:
        return "not_tradable"
    return "tradable" if event_field else "low_priority"


# ---------------------------------------------------------------- catalysts (S08-R15..R23, decision 13)

A_POSITIVE = {"fda_approval", "late_trial_success", "annual_guidance_raise", "debt_rating_upgrade",
              "buyback_increase", "major_partnership"}
A_NEGATIVE = {"annual_guidance_cut", "dividend_suspension", "dividend_cut", "debt_rating_downgrade",
              "acquisition_cancelled", "trial_failure", "fda_rejection"}
B_POSITIVE = {"insider_buy", "activist_stake", "analyst_major_upgrade"}
B_NEGATIVE = {"short_report"}
C_EVENTS = {"analyst_upgrades_no_new_target", "competitor_results", "technical_breakout"}


@dataclass
class Catalyst:
    cls: Literal["A", "B", "C", "D", "none"]
    sign: Literal["positive", "negative", None]
    action: str
    flags: list[str] = field(default_factory=list)


def classify(kind: str, on_list: bool = True, prospect: Literal["positive", "negative", None] = None,
             change_pct: float | None = None) -> Catalyst:
    """S08-R15..R18 with D-S08-2 (a). change_pct: for guidance, the change vs the company's previous
    guidance midpoint (decision 13); for an analyst, the new target vs the price (S08-R17).
    prospect: the stage 7 view of the fundamentals; a catalyst against it => no entry (S08-R23)."""
    if kind in ("annual_guidance_raise", "annual_guidance_cut", "guidance_change"):
        if change_pct is None:
            return Catalyst("none", None, "guidance_change_unknown", ["previous_guidance_missing"])
        if not is_guidance_catalyst(change_pct):
            return Catalyst("none", None, "nothing")
        kind = "annual_guidance_raise" if change_pct > 0 else "annual_guidance_cut"
    if kind == "analyst_target":
        m = analyst_target(change_pct)
        if m != "material":
            return Catalyst("none", None, "nothing" if m == "not_renewed" else "judgment_with_reason")
        kind = "analyst_major_upgrade" if change_pct > 0 else "analyst_major_downgrade"
    if kind in A_POSITIVE or kind in A_NEGATIVE:
        c = Catalyst("A", "positive" if kind in A_POSITIVE else "negative",
                     "trade_candidate" if kind in A_POSITIVE else "short_or_exit_candidate", ["check_priced_in"])
    elif kind in B_POSITIVE or kind in B_NEGATIVE or kind == "analyst_major_downgrade":
        c = Catalyst("B", "positive" if kind in B_POSITIVE else "negative", "needs_technical_and_market_support")
    elif kind in C_EVENTS:
        return Catalyst("C", None, "record" if on_list else "nothing")
    else:
        return Catalyst("D", None, "nothing")
    if prospect is not None and c.sign != prospect:
        c.action, c.flags = "no_entry", c.flags + ["catalyst_against_prospect"]
    return c


def analyst_target(change_pct: float) -> str:
    """S08-R17 / AT-S08-12: +50% and above = material; +12% and below = didn't renew; between = judgment."""
    w = _w()
    a = abs(change_pct)
    if a >= w.analyst_target_material_pct - 1e-9:
        return "material"
    return "not_renewed" if a <= w.analyst_target_stale_pct + 1e-9 else "judgment"


def guidance_event(new: float, prev_low: float | None, prev_high: float | None = None,
                   consensus: float | None = None) -> dict:
    """Decision 13: the change is vs the company's previous guidance midpoint; the consensus gap is a
    secondary field. Without the previous guidance: unknown, never an automatic A list."""
    out = {"consensus_gap_pct": (new / consensus - 1) * 100 if consensus else None}
    if prev_low is None:
        out.update(guidance_change="unknown", change_pct=None, catalyst=classify("guidance_change"))
        return out
    ch = guidance_change_pct(new, prev_low, prev_high)
    out.update(guidance_change="raise" if ch > 0 else "cut", change_pct=ch, catalyst=classify("guidance_change", change_pct=ch))
    return out


# ---------------------------------------------------------------- the idea-to-trade gate (S08-R28, fix 7)

def early_gate(e: Entry, news_adverse: bool) -> tuple[bool, list[str]]:
    """Stage 8: hot + news flow not adverse => run stages 9-11. Boiling never passes."""
    why = []
    if e.archived:
        why.append("archived")
    if e.list == "event":
        why.append("event_list_feeds_only_back_to_stages_6_7")
    if e.heat == "boiling":
        why.append("boiling_wait_for_base_retest_or_cheaper_options")
    elif e.heat != "hot":
        why.append(f"heat_{e.heat}")
    if news_adverse:
        why.append("news_adverse")
    return not why, why


def full_gate(traffic_light: Color | None, price_behaviour_qm: Sequence[Color],
              stop: float | None, target: float | None, rr: float | None, horizon_months: float | None,
              news_adverse: bool = False) -> tuple[bool, list[str]]:
    """After stage 11, before approval: news not adverse; overall light orange or green; the quarterly and
    monthly price behaviour not red vs the direction (colours already relative to it); stop, target, R:R >= 3 and horizon defined."""
    why = []
    if news_adverse:
        why.append("news_adverse")
    if traffic_light not in ("green", "orange"):
        why.append(f"traffic_light_{traffic_light}")
    if any(c == "red" for c in price_behaviour_qm):
        why.append("against_the_trend")
    for name, v in (("stop", stop), ("target", target), ("horizon", horizon_months)):
        if v is None:
            why.append(f"{name}_missing")
    if rr is None or rr < load_policy().reward_risk.min_ratio:
        why.append("reward_risk_below_min")
    return not why, why
