"""Stage 12: the daily review of open positions (087, 084, 077, 093).

Inputs are the closing prices and the day's context; the output is a list of actions. Standing orders
approved with the trade (stop exits, the 60/40 trailing stop, the option profit take; decision 3) are
marked preapproved. Everything else is a proposal for Ilay: nothing here sends an order. Exits and risk
reduction are never blocked (S10-R29); new risk is blocked by the portfolio stop (S12-R1) and the tactical
regime (S12-R3). A VIX jump is a flag only (Ilay 20.10).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Sequence

from qt.calc.portfolio import (Portfolio, PortfolioStop, Position, sixty_forty_option_sell_fraction,
                               sixty_forty_stock_stop, tighten_only, winner_action)
from qt.calc.volatility import VixReading
from qt.policy import load_policy


def _m():
    return load_policy().manage


@dataclass
class Held:
    """An open position as the daily review sees it."""
    symbol: str
    direction: Literal["long", "short"]
    sector: str
    qty: float                                   # shares, or contracts for options
    entry: float                                 # price per share, or premium per contract for options
    entry_stop: float | None                     # the stop approved with the trade (stocks)
    stop: float | None                           # the standing stop now (stocks)
    price: float                                 # today's close, or the option's value per contract
    instrument: Literal["stock", "option"] = "stock"
    beta: float = 1.0
    multiplier: float = 1.0                      # 100 for an option contract
    unwanted: bool = False                       # thesis weakened (agent)
    speculative: bool = False
    below_mas: bool = False                      # breaking or below its moving averages (stage 9)
    adverse_catalyst: bool = False               # S12-R27: event against us on this position (agent)
    stock_ret: float | None = None               # recent return, for the beta disconnect (S12-R28)
    profit_taken: bool = False                   # options 60/40 already done

    @property
    def value(self) -> float:
        return abs(self.qty) * self.price * self.multiplier

    @property
    def open_profit(self) -> float:
        s = 1 if self.direction == "long" or self.instrument == "option" else -1
        return s * (self.price - self.entry) * abs(self.qty) * self.multiplier

    def as_position(self) -> Position:
        return Position(self.symbol, self.direction, self.sector, self.value, self.instrument,
                        entry_cost=abs(self.qty) * self.entry * self.multiplier if self.instrument == "option" else 0.0,
                        beta=self.beta)


@dataclass
class Action:
    kind: Literal["exit", "tighten_stop", "sell", "reduce", "block_new_risk", "flag"]
    rule_id: str
    detail: str
    symbol: str | None = None
    qty: float | None = None
    price: float | None = None
    preapproved: bool = False                    # a standing order approved with the trade (decision 3)


@dataclass
class Review:
    actions: list[Action] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    regime: str = "portfolio"

    def of(self, kind: str) -> list[Action]:
        return [a for a in self.actions if a.kind == kind]

    def rules(self) -> list[str]:
        return [a.rule_id for a in self.actions]


def stop_hit(h: Held) -> bool:
    if h.instrument != "stock" or h.stop is None:
        return False
    return h.price <= h.stop if h.direction == "long" else h.price >= h.stop


def position_actions(h: Held) -> list[Action]:
    """Per position: stop exit (S12-R2), 60/40 (S12-R11), adverse catalyst (S12-R27)."""
    out = []
    if stop_hit(h):
        return [Action("exit", "S12-R2", f"close {h.price:.2f} through the stop {h.stop:.2f}", h.symbol, h.qty,
                       h.stop, preapproved=True)]
    if h.instrument == "stock" and h.entry_stop is not None and h.stop is not None:
        trail = sixty_forty_stock_stop(h.entry, h.entry_stop, h.price, h.direction)
        if trail is not None:
            new = tighten_only(h.stop, trail, h.direction)
            if new != h.stop:
                out.append(Action("tighten_stop", "S12-R11", f"60/40: stop {h.stop:.2f} -> {new:.2f} on the full quantity",
                                  h.symbol, h.qty, round(new, 2), preapproved=True))
    if h.instrument == "option" and not h.profit_taken:
        frac = sixty_forty_option_sell_fraction(h.entry, h.price)
        if frac is not None:
            out.append(Action("sell", "S12-R11", f"+150%: sell {frac * 100:.0f}% of the contracts (60% of the profit)",
                              h.symbol, round(abs(h.qty) * frac, 2), h.price, preapproved=True))
    if h.adverse_catalyst:
        out.append(Action("reduce", "S12-R27", "event against the position: at minimum reduce risk; never add "
                          "because it 'fell enough'", h.symbol))
    return out


def tactical_cuts(held: Sequence[Held], equity: float) -> list[Action]:
    """S12-R26 / D-S12-6 (a): cut in the 084 order until medium-long exposure is below 50% of equity:
    unwanted => very high beta => speculative => below the moving averages. The rest keeps momentum."""
    m = _m()
    exposure = sum(h.value for h in held)
    target = m.tactical_exposure_target * equity
    order = [("unwanted", lambda h: h.unwanted), ("high_beta", lambda h: h.beta >= m.high_beta),
             ("speculative", lambda h: h.speculative), ("below_mas", lambda h: h.below_mas)]
    out, done = [], set()
    for name, pred in order:
        for h in sorted(held, key=lambda x: -x.value):
            if exposure < target:
                return out
            if h.symbol in done or not pred(h):
                continue
            done.add(h.symbol)
            exposure -= h.value
            out.append(Action("reduce", "S12-R26", f"tactical cut #{len(out) + 1} ({name})", h.symbol, h.qty, h.price))
    if exposure >= target:
        out.append(Action("flag", "S12-R26", f"exposure still {exposure / equity * 100:.0f}% after the ordered cuts; "
                          f"alternative: cut {m.uniform_cut_share * 100:.0f}% of every position"))
    return out


def metrics(pf: Portfolio, ps: PortfolioStop | None, equity: float) -> dict:
    """S12-R23: what the daily summary shows."""
    m = _m()
    out = {"gross": pf.gross(), "net": pf.net(), "beta": pf.beta(),
           "weights": {p.symbol: pf.weight_pct(p.symbol) for p in pf.positions},
           "stress_loss_pct": {d: pf.beta() * d for d in m.stress_drops_pct}}
    if ps is not None:
        out["drawdown_pct"] = ps.drawdown_pct(equity)
        out["to_portfolio_stop_pct"] = (equity - ps.trigger_level()) / equity * 100
    return out


def daily_review(held: Sequence[Held], equity: float, portfolio_stop: PortfolioStop | None = None,
                 regime: str = "portfolio", regime_changed: bool = False, vix: VixReading | None = None,
                 market_shock: bool = False, market_ret: float | None = None) -> Review:
    """The whole stage 12 pass for one day. `regime` comes from qt.calc.volatility.current_regime."""
    m = _m()
    r = Review(regime=regime)
    for h in held:
        r.actions += position_actions(h)
    pf = Portfolio(equity, [h.as_position() for h in held])
    # S12-R12: a winner that grew; trim to 24% (part ד), mandatory above 30%
    for h in held:
        w = pf.weight_pct(h.symbol)
        act = winner_action(w)
        if act != "none":
            keep = load_policy().winner.rebalance_flag_pct
            qty = abs(h.qty) * (w - keep) / w
            r.actions.append(Action("sell" if act == "rebalance_mandatory" else "flag", "S12-R12",
                                    f"{h.symbol} is {w:.1f}% of the portfolio: trim to {keep:.0f}% and move the stop up"
                                    + (" (mandatory above 30%)" if act == "rebalance_mandatory" else ""),
                                    h.symbol, round(qty, 2), h.price))
    # S12-R1: the portfolio stop
    if portfolio_stop is not None and portfolio_stop.update(equity):
        r.actions.append(Action("block_new_risk", "S12-R1",
                                f"portfolio stop: drawdown {portfolio_stop.drawdown_pct(equity):.2f}% from the peak; "
                                "new risk blocked until Ilay releases it after a documented reassessment"))
        r.actions.append(Action("reduce", "S12-R1", "propose cuts for Ilay (no automatic selling)"))
    # S12-R3 / R25 / R26: the tactical regime
    if regime == "tactical":
        r.actions.append(Action("block_new_risk", "S12-R3", "tactical regime (ATR 20-60 crossed +sigma): no new risk"))
        if regime_changed:
            r.actions += tactical_cuts(held, equity)
    # S12-R24: VIX is a flag only
    if vix is not None and vix.jump:
        r.actions.append(Action("flag", "S12-R24", f"VIX {vix.ratio_to_low:.2f}x its recent low: flag only, no regime change"))
    # S12-R7: high net into a shock
    if market_shock and pf.net() >= m.shock_net_flag:
        weak = sorted((h for h in held if h.direction == "long"), key=lambda h: h.open_profit)
        r.actions.append(Action("reduce", "S12-R7", f"net {pf.net():.2f} into a market shock: cut net now, weakest / "
                                "no-profit first: " + ", ".join(h.symbol for h in weak[:3])))
    # S12-R28: beta disconnect
    if market_ret is not None and market_ret > 0:
        for h in held:
            if h.beta > 0 and h.stock_ret is not None and h.stock_ret < 0 and h.direction == "long":
                r.actions.append(Action("flag", "S12-R28", "falls while the market rises: check the thesis", h.symbol))
    r.metrics = metrics(pf, portfolio_stop, equity)
    if r.metrics["beta"] > m.beta_flag:
        r.actions.append(Action("flag", "S12-R23", f"portfolio beta {r.metrics['beta']:.2f} above 1"))
    return r
