"""Rule checks for a proposed action. Each check returns findings tagged with the Playbook rule id;
the engine decides block / flag / override from the rule's tier, not the check."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from typing import Literal

from qt.calc import trade as T
from qt.calc.portfolio import Portfolio, PortfolioStop, Position
from qt.calc.traffic_light import Color
from qt.calc.volatility import VixReading
from qt.policy import load_policy
from qt.rules.engine import (Finding, Override, Verdict, approvable, decide, flag, info, needs_reason,
                             violation)


@dataclass
class OptionLeg:
    """Options trade: cost per contract (premium) and the ratio inputs."""
    cost_per_contract: float            # in dollars (premium x 100)
    contracts: int
    days_to_expiry: int
    ratio: float                        # from the premium (S10-R17), computed by qt.calc.trade


@dataclass
class TradeProposal:
    symbol: str
    direction: Literal["long", "short"]
    action: Literal["enter", "add", "exit", "reduce"] = "enter"
    stage: Literal["initial", "full"] = "full"
    instrument: Literal["stock", "option"] = "stock"
    sector: str = ""
    entry: float | None = None
    stop: float | None = None
    stop_basis: Literal["volatility", "chart"] = "volatility"
    target: float | None = None
    target_source: str | None = None    # technical_level | own_multiple_target | all_time_high_target | analyst
    horizon_months: float | None = None
    catalyst_chain: str = ""            # required for horizon > 3 months
    adr_pct: float | None = None        # ADR60 measured by code
    monthly_sigma_pct: float | None = None
    requested_pct: float | None = None  # position size as % of equity
    option: OptionLeg | None = None
    # process and conduct
    on_watchlist: bool = True
    process_complete: bool = True
    reverses_frozen_idea: bool = False
    traffic_light: Color | None = None
    market_cap: float | None = None
    price: float | None = None
    revenue: float | None = None
    planned_time_ny: time | None = None
    news_event: bool = False
    move_pct_2w: float | None = None
    idea_listed_before_move: bool = True
    stops_last_30d: int = 0
    journaled: bool = True
    reasons: dict[str, str] = field(default_factory=dict)  # e.g. {"DC-19": "ADR is high (7.5%)"}


@dataclass
class Readiness:
    slept: bool = True
    prepared: bool = True
    personal_event_today: bool = False
    personal_crisis: bool = False


@dataclass
class Context:
    portfolio: Portfolio
    portfolio_stop: PortfolioStop | None = None
    regime: Literal["portfolio", "tactical"] = "portfolio"
    vix: VixReading | None = None
    readiness: Readiness = field(default_factory=Readiness)
    gross_limit: float | None = None     # raised only by Ilay (1.5 / 1.8)


# ---------- individual checks ----------

def check_readiness(p: TradeProposal, c: Context) -> list[Finding]:
    r = c.readiness
    out = []
    if r.personal_crisis:
        out.append(violation("S13-R14", "personal crisis: pause trading (a week to a month); Ilay decides when to resume"))
    if not (r.slept and r.prepared) or r.personal_event_today:
        out.append(violation("S13-R13", "readiness check failed: no new risk today; exits remain allowed",
                             slept=r.slept, prepared=r.prepared, personal_event_today=r.personal_event_today))
    return out


def check_portfolio_state(p: TradeProposal, c: Context) -> list[Finding]:
    out = []
    if c.portfolio_stop and c.portfolio_stop.triggered:
        out.append(violation("S12-R1", "portfolio stop (15% from peak) is latched: new risk blocked until Ilay releases it"))
    if c.regime == "tactical":
        out.append(violation("S12-R3", "tactical regime (ATR 20-60 crossed +0.325%): adding risk is blocked"))
    if c.vix and c.vix.jump:
        out.append(flag("S12-R24", f"VIX jumped {c.vix.ratio_to_low:.2f}x its recent low: flag only, regime unchanged",
                        ratio=round(c.vix.ratio_to_low, 3)))
    return out


def check_process(p: TradeProposal, c: Context) -> list[Finding]:
    out = []
    if not p.on_watchlist:
        out.append(violation("S08-R2", "trades come only from the watchlist"))
    if not p.process_complete:
        out.append(violation("S13-R1", "not every process stage was run and documented"))
    if p.reverses_frozen_idea:
        out.append(violation("S12-R17", "reversing a frozen idea needs the full process again"))
    if not p.journaled:
        out.append(violation("S13-R29", "entry/add must be recorded in the decision journal first"))
    return out


def check_definition(p: TradeProposal, c: Context) -> list[Finding]:
    missing = [n for n in ("stop", "target", "horizon_months") if getattr(p, n) is None]
    if p.instrument == "option" and p.option is not None:
        missing = [m for m in missing if m != "stop"]  # premium is the risk (S10-R15)
    if missing:
        return [violation("S10-R2", f"trade must define stop, target and horizon; missing: {', '.join(missing)}",
                          missing=missing)]
    return []


def check_traffic_light(p: TradeProposal, c: Context) -> list[Finding]:
    if p.traffic_light == "red":
        return [violation("S09-R13", "traffic light is red: no new position")]
    if p.traffic_light == "orange" and (p.stage == "full" or p.action == "add"):
        return [violation("S09-R10", "orange light: initial position only")]
    return []


def check_floors(p: TradeProposal, c: Context) -> list[Finding]:
    f = load_policy().floors
    out = []
    if p.direction == "long":
        if p.market_cap is not None and p.market_cap < f.long_min_market_cap:
            out.append(violation("S06-R6", f"long market cap ${p.market_cap/1e9:.2f}B < $0.5B"))
        if p.price is not None and p.price <= f.long_min_price:
            out.append(violation("S06-R9", f"long price ${p.price:.2f} not above $5"))
        if p.revenue is not None and p.revenue <= f.long_min_revenue:
            out.append(violation("S06-R10", f"revenue ${p.revenue/1e6:.0f}M not above $300M"))
    elif p.market_cap is not None and p.market_cap < f.short_min_market_cap:
        out.append(violation("S06-R8", f"short market cap ${p.market_cap/1e9:.2f}B < $10B"))
    return out


def check_stop(p: TradeProposal, c: Context) -> list[Finding]:
    if p.instrument != "stock" or p.stop is None or p.entry is None:
        return []
    s = load_policy().stop
    out = []
    if p.stop_basis == "chart":
        out.append(violation("S11-R10", "stop is set from the chart; it must come from volatility (k x ADR60)"))
    if p.adr_pct is None:
        return out + [violation("S11-R24", "ADR60 missing: the stop width cannot be checked")]
    k = T.k_of(p.entry, p.stop, p.adr_pct)
    dist = T.stop_distance_pct(p.entry, p.stop)
    data = dict(k=round(k, 2), stop_pct=round(dist, 2), adr_pct=round(p.adr_pct, 2),
                min_stop=round(T.stop_price(p.entry, p.adr_pct, s.k_hard_min, p.direction), 2),
                default_stop=round(T.stop_price(p.entry, p.adr_pct, s.k_default, p.direction), 2))
    if k < s.k_hard_min - 1e-9:
        out.append(violation("S11-R11", f"stop is {k:.2f}x ADR60, below the 3x minimum", **data))
    elif k < s.k_needs_reason_below - 1e-9 and not p.reasons.get("DC-19", "").strip():
        out.append(needs_reason("DC-19", f"stop {k:.2f}x ADR60 (between 3x and 4x) needs a written reason", **data))
    if dist > s.huge_stop_pct:
        out.append(violation("S11-R11", f"huge stop {dist:.1f}% (> 30%): check as an option instead", **data))
    if p.adr_pct > s.adr_route_to_options_pct:
        out.append(flag("S11-R11", f"ADR60 {p.adr_pct:.1f}% > 10%: check this as an option", **data))
    return out


def check_reward_risk(p: TradeProposal, c: Context) -> list[Finding]:
    rr = load_policy().reward_risk
    out = []
    if p.target is not None and p.target_source not in rr.target_sources:
        out.append(flag("S10-R3", "target has no valid source tag (significant level / our multiple target / ATH => target); "
                        "analyst targets are for comparison only", target_source=p.target_source))
    if p.instrument == "option" and p.option is not None:
        ratio = p.option.ratio
    elif None not in (p.entry, p.stop, p.target):
        ratio = T.reward_risk(p.entry, p.stop, p.target, p.direction)
    else:
        return out
    if ratio < rr.min_ratio - 1e-9:
        hint = " Check it as an option; otherwise keep it waiting." if p.instrument == "stock" else ""
        out.append(violation("S10-R3", f"reward:risk 1:{ratio:.2f} below 1:3.{hint}", ratio=round(ratio, 2)))
    return out


def check_horizon(p: TradeProposal, c: Context) -> list[Finding]:
    h = load_policy().horizon
    out = []
    months = p.horizon_months
    if p.monthly_sigma_pct and p.entry and p.target:
        implied = T.horizon_months(T.pct_move(p.entry, p.target), p.monthly_sigma_pct)
        if months is None or implied > months + 1e-9:
            out.append(flag("S10-R35", f"realism: target needs ~{implied:.1f} months at monthly sigma "
                            f"{p.monthly_sigma_pct:.1f}%; horizon set to that", implied_months=round(implied, 2)))
            months = implied
    if months is None:
        return out
    if months > h.extended_max_months:
        out.append(flag("D-S10-5", f"horizon_beyond_6m: {months:.1f} months", horizon_beyond_6m=True))
    elif months > h.default_max_months and not p.catalyst_chain.strip():
        out.append(needs_reason("D-S10-5", f"horizon {months:.1f} months > 3 needs a documented catalyst chain"))
    elif months > h.default_max_months:
        out.append(flag("D-S10-5", f"horizon {months:.1f} months: extended on a catalyst chain", catalyst_chain=p.catalyst_chain))
    return out


def check_sizing(p: TradeProposal, c: Context) -> list[Finding]:
    if p.instrument != "stock" or p.requested_pct is None:
        return []
    pol = load_policy().sizing
    cap = pol.initial_position_pct if p.stage == "initial" else pol.full_position_pct
    out = []
    rule = "S11-R5" if p.stage == "initial" else "S11-R6"
    if p.requested_pct > cap + 1e-9:
        qty = None
        if p.entry:
            qty = int(c.portfolio.equity * cap / 100 / p.entry + 1e-9)
        out.append(violation(rule, f"position {p.requested_pct:.2f}% > {cap:.0f}% cap", max_pct=cap, max_qty=qty))
    if p.entry and p.stop:
        dist = T.stop_distance_pct(p.entry, p.stop)
        sz = T.size_position(c.portfolio.equity, p.entry, dist, p.stage, p.requested_pct)
        if sz.unbearable:
            out.append(violation("S10-R2", "even the minimum quantity risks more than 3% of the account"))
        elif sz.reduced_by_risk_cap and p.requested_pct > sz.risk_cap_pct + 1e-9:
            out.append(info("decision-5", f"size reduced to {sz.risk_cap_pct:.2f}% so the loss to the stop stays <= 3%",
                            max_pct=round(sz.risk_cap_pct, 2), quantity=sz.quantity))
    return out


def check_exposure(p: TradeProposal, c: Context) -> list[Finding]:
    if p.instrument != "stock" or p.requested_pct is None:
        return []
    limit = c.gross_limit or load_policy().exposure.gross_max
    gross_after = c.portfolio.gross() + p.requested_pct / 100.0
    if gross_after > limit + 1e-9:
        return [violation("S11-R15", f"gross exposure would be {gross_after:.2f} > {limit:.1f}",
                          gross_after=round(gross_after, 2))]
    return []


def check_options(p: TradeProposal, c: Context) -> list[Finding]:
    if p.instrument != "option" or p.option is None:
        return []
    o = load_policy().options
    eq = c.portfolio.equity
    new_cost = p.option.cost_per_contract * p.option.contracts
    short = p.option.days_to_expiry <= o.short_dated_days
    total = (c.portfolio.options_cost() + new_cost) / eq * 100
    short_total = (c.portfolio.options_cost(short_only=True) + (new_cost if short else 0)) / eq * 100
    out = []
    key = f"small_account_options_override:{p.symbol}"
    if total > o.total_cap_pct + 1e-9:
        max_contracts = int((o.total_cap_pct / 100 * eq - c.portfolio.options_cost()) // p.option.cost_per_contract)
        out.append(approvable("S11-R22", key, f"options would be {total:.2f}% of the account (> 20%)",
                              total_pct=round(total, 2), max_contracts=max(0, max_contracts)))
    if short and short_total > o.short_dated_cap_pct + 1e-9:
        out.append(approvable("S11-R22", key, f"short-dated options would be {short_total:.2f}% (> 10%)",
                              short_pct=round(short_total, 2)))
    if p.option.cost_per_contract / eq * 100 > o.contract_too_costly_pct:
        out.append(flag("S10-R15", "one contract costs more than 3% of the account"))
    return out


def check_sectors(p: TradeProposal, c: Context) -> list[Finding]:
    s = load_policy().sectors
    held = c.portfolio.sectors()
    if not held or len(held) >= s.min_sectors or p.sector not in held:
        return []
    if len(held) >= s.min_sectors_with_shorts and c.portfolio.has_full_short():
        return [approvable("S11-R2", "three_sector_exception",
                           f"{len(held)} sectors with shorts: documented exception, Ilay approves")]
    return [violation("S11-R2", f"only {len(held)} sectors held: adding to an already-held sector is blocked",
                      sectors=sorted(held))]


def check_conduct(p: TradeProposal, c: Context) -> list[Finding]:
    cd = load_policy().conduct
    out = []
    if p.planned_time_ny is not None and not p.news_event:
        hh, mm = map(int, cd.no_entry_before.split(":"))
        if p.planned_time_ny < time(hh, mm):
            out.append(violation("S10-R25", f"no entry in the first half hour: postpone to {cd.no_entry_before} New York"))
    if p.move_pct_2w is not None and p.move_pct_2w >= cd.fomo_move_pct and not p.idea_listed_before_move:
        out.append(violation("S13-R5", "chasing a move that already ran (FOMO): put it on the watchlist as 'boiling'"))
    if p.stops_last_30d >= cd.reentries_after_stops:
        out.append(violation("S13-R6", f"{p.stops_last_30d} stop-outs on this stock in 30 days: revenge risk, "
                             "idea goes back for a full review"))
    return out


ENTRY_CHECKS = (check_readiness, check_portfolio_state, check_process, check_definition, check_traffic_light,
                check_floors, check_stop, check_reward_risk, check_horizon, check_sizing, check_exposure,
                check_options, check_sectors, check_conduct)


def evaluate(p: TradeProposal, c: Context, overrides: tuple[Override, ...] = (),
             approvals: tuple[str, ...] = ()) -> Verdict:
    """Run every check; exits and reductions are evaluated too, but nothing can block them."""
    findings: list[Finding] = []
    if p.action in ("enter", "add"):
        for check in ENTRY_CHECKS:
            findings.extend(check(p, c))
    else:
        findings.append(info("S10-R29", "exits and risk reduction are always allowed"))
    return decide(p.action, findings, overrides, approvals)


def review_position(pos: Position, c: Context) -> list[Finding]:
    """Daily review of an open position (no blocking; proposals go to Ilay)."""
    from qt.calc.portfolio import winner_action
    w = c.portfolio.weight_pct(pos.symbol)
    act = winner_action(w)
    out = []
    if act == "rebalance_mandatory":
        out.append(violation("S12-R12", f"{pos.symbol} is {w:.1f}% of the portfolio (> 30%): rebalance proposal required"))
    elif act == "rebalance_flag":
        out.append(flag("S12-R12", f"{pos.symbol} is {w:.1f}%: consider rebalancing (target <= 24%)"))
    elif w > load_policy().sizing.full_position_pct:
        out.append(flag("S11-R6", f"{pos.symbol} is {w:.1f}% (> 12%): no adds; trim is a proposal for Ilay"))
    return out
