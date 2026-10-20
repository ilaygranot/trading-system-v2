"""Replays of the lecturer's own trades (architecture/research/audit-rerun.md) through the rules engine.

These are regression cases: a Playbook change that flips one of them must be deliberate.
Prices come from stored daily bars; ADR60 is measured on the 60 sessions before entry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Literal

from qt.calc import trade as T
from qt.calc.portfolio import Portfolio
from qt.calc.volatility import Bar, adr_before
from qt.rules.checks import Context, OptionLeg, TradeProposal, evaluate
from qt.rules.engine import Verdict


@dataclass
class StockCase:
    case: str                 # number in audit-rerun.md
    symbol: str
    direction: Literal["long", "short"]
    entry_day: date
    entry: float
    target: float
    target_source: str
    source: str               # transcript reference
    stage: Literal["initial", "full"] = "full"
    catalyst_chain: str = ""
    k3_reason: str = ""       # reason for k=3 (DC-19); empty => k=4 only
    extra: dict = field(default_factory=dict)


CASES = [
    StockCase("7", "SYM", "long", date(2023, 3, 17), 18.7, 32.0, "analyst", "098:267, 098:279",
              catalyst_chain="098:281 'two, three, six months' of catalysts",
              k3_reason="ADR60 is high (~7.5%), DC-19: 3x when the daily range is high"),
    StockCase("12", "ACMR", "long", date(2023, 5, 26), 10.7, 10.7 * 1.50, "own_multiple_target", "101:111-117",
              stage="initial", catalyst_chain="analyst coverage expected (101:113-119)",
              k3_reason="ADR60 is high (~5.4%), DC-19"),
    StockCase("9", "TSLA", "long", date(2023, 5, 18), 182.0, 200.0, "technical_level", "091:107, 091:115",
              k3_reason="test k=3 too"),
    StockCase("10", "META", "long", date(2023, 5, 25), 253.0, 300.0, "technical_level", "091:119",
              k3_reason="test k=3 too"),
    StockCase("11", "ZS", "long", date(2023, 4, 6), 110.0, 135.0, "technical_level", "091:59-61",
              k3_reason="test k=3 too"),
    StockCase("14", "BA", "short", date(2023, 5, 13), 198.0, 170.0, "technical_level", "101:71, 101:81"),
]


@dataclass
class ReplayResult:
    case: StockCase
    adr60: float
    k: float
    stop: float
    ratio: float
    verdict: Verdict


def run_stock_case(c: StockCase, bars: list[Bar], k: float) -> ReplayResult:
    a = adr_before(bars, c.entry_day)
    stop = T.stop_price(c.entry, a, k, c.direction)
    months = T.horizon_months(T.pct_move(c.entry, c.target), 2.6 * a)  # sigma_m ~= 2.6 x ADR (audit assumption)
    p = TradeProposal(c.symbol, c.direction, stage=c.stage, entry=c.entry, stop=stop, target=c.target,
                      target_source=c.target_source, horizon_months=months, catalyst_chain=c.catalyst_chain,
                      adr_pct=a, requested_pct=6 if c.stage == "initial" else 12, sector="XL?",
                      reasons={"DC-19": c.k3_reason} if k < 4 else {}, **c.extra)
    v = evaluate(p, Context(Portfolio(100_000)))
    return ReplayResult(c, a, k, stop, T.reward_risk(c.entry, stop, c.target, c.direction), v)


def spread_cases() -> list[tuple[str, T.VerticalSpread, float, Verdict]]:
    out = []
    for name, spread, target, months, cap in (
        ("BA puts 185/170 (101)", T.VerticalSpread("put", 185, 5.90, 170, 2.80), 170.0, 1.7, 120e9),
        ("BLDR puts 85/70 (100)", T.VerticalSpread("put", 85, 4.00, 70, 1.50), 70.0, 2.8, 15e9),
    ):
        ratio = spread.ratio_to_target(target)
        p = TradeProposal(name.split()[0], "short", instrument="option", target=target, target_source="technical_level",
                          horizon_months=months, market_cap=cap,
                          option=OptionLeg(spread.cost * 100, 3, 80, ratio))
        out.append((name, spread, ratio, evaluate(p, Context(Portfolio(100_000)))))
    return out


def report(load: Callable[[str], list[Bar]]) -> str:
    files = {"BA": "BA_prices_2023.json"}
    lines = ["| # | stock | ADR60 | k | stop | ratio | verdict |", "|---|---|---|---|---|---|---|"]
    for c in CASES:
        bars = load(files.get(c.symbol, f"{c.symbol}_daily_2022_2024.json"))
        for k in ((4.0, 3.0) if c.k3_reason else (4.0,)):
            r = run_stock_case(c, bars, k)
            lines.append(f"| {c.case} | {c.symbol} | {r.adr60:.2f}% | {k:g}x | {r.stop:.2f} | 1:{r.ratio:.2f} | "
                         f"{r.verdict.status.value} ({', '.join(sorted(r.verdict.rule_ids()))}) |")
    for name, _, ratio, v in spread_cases():
        lines.append(f"| spread | {name} | | | | 1:{ratio:.2f} | {v.status.value} |")
    return "\n".join(lines)
