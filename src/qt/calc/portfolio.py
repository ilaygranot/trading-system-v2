"""Portfolio state and the numbers S11/S12 enforce on it."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from qt.policy import load_policy


@dataclass
class Position:
    symbol: str
    direction: Literal["long", "short"]
    sector: str                          # one of the 11 XL* sectors (term #12)
    market_value: float                  # absolute value; for options = current value
    instrument: Literal["stock", "option"] = "stock"
    entry_cost: float = 0.0              # options: premium paid at entry (counts against caps, term #13)
    days_to_expiry: int | None = None
    full_size: bool = True               # a short counts for the sector exception only at full size
    beta: float = 1.0

    @property
    def short_dated(self) -> bool:
        return self.instrument == "option" and self.days_to_expiry is not None \
            and self.days_to_expiry <= load_policy().options.short_dated_days


@dataclass
class Portfolio:
    equity: float
    positions: list[Position] = field(default_factory=list)

    def long_value(self) -> float:
        return sum(p.market_value for p in self.positions if p.direction == "long" and p.instrument == "stock")

    def short_value(self) -> float:
        return sum(p.market_value for p in self.positions if p.direction == "short" and p.instrument == "stock")

    def gross(self) -> float:
        return (self.long_value() + self.short_value()) / self.equity

    def net(self) -> float:
        return (self.long_value() - self.short_value()) / self.equity

    def options_cost(self, short_only: bool = False) -> float:
        return sum(p.entry_cost for p in self.positions
                   if p.instrument == "option" and (p.short_dated or not short_only))

    def sectors(self) -> set[str]:
        return {p.sector for p in self.positions}

    def has_full_short(self) -> bool:
        return any(p.direction == "short" and p.full_size for p in self.positions)

    def weight_pct(self, symbol: str) -> float:
        return sum(p.market_value for p in self.positions if p.symbol == symbol) / self.equity * 100.0

    def beta(self) -> float:
        """Beta-weighted net exposure (S11-R13)."""
        return sum((1 if p.direction == "long" else -1) * p.beta * p.market_value for p in self.positions) / self.equity


def leverage_loss_pct(exposure: float, asset_drop_pct: float) -> float:
    return exposure * asset_drop_pct


@dataclass
class PortfolioStop:
    """15% drawdown from peak equity (S12-R1). Latched once triggered; released only by Ilay
    after a documented reassessment, which resets the reference to current equity (part ד decision 4)."""
    peak: float
    triggered: bool = False

    def update(self, equity: float) -> bool:
        """Feed daily closing equity (incl. unrealized). Returns True while new risk is blocked."""
        if not self.triggered:
            self.peak = max(self.peak, equity)
            if self.drawdown_pct(equity) >= load_policy().portfolio_stop.drawdown_pct:
                self.triggered = True
        return self.triggered

    def drawdown_pct(self, equity: float) -> float:
        return (self.peak - equity) / self.peak * 100.0

    def trigger_level(self) -> float:
        return self.peak * (1 - load_policy().portfolio_stop.drawdown_pct / 100.0)

    def release(self, equity: float, approved_by_ilay: bool, reassessment_note: str) -> None:
        if not (approved_by_ilay and reassessment_note.strip()):
            raise PermissionError("portfolio stop releases only with Ilay's approval and a documented reassessment")
        self.triggered = False
        self.peak = equity


def sixty_forty_stock_stop(entry: float, entry_stop: float, price: float,
                           direction: Literal["long", "short"] = "long") -> float | None:
    """Trailing stop that keeps 60% of open profit once profit >= 3R (S12-R11, D-S12-4).
    Not bound by the k>=3 minimum (fix 10). Returns None before the trigger."""
    p = load_policy().sixty_forty
    s = 1 if direction == "long" else -1
    r = s * (entry - entry_stop)
    profit = s * (price - entry)
    if profit < p.stock_trigger_r * r - 1e-9:
        return None
    return entry + s * p.stock_keep_share * profit


def sixty_forty_option_sell_fraction(cost: float, value: float) -> float | None:
    """At +150%, sell contracts worth 60% of the profit (S12-R11). Returns fraction of contracts to sell."""
    p = load_policy().options
    if (value - cost) / cost * 100.0 < p.profit_take_gain_pct - 1e-9:
        return None
    return p.profit_take_share * (value - cost) / value


def tighten_only(old_stop: float, new_stop: float, direction: Literal["long", "short"] = "long") -> float:
    """A stop may only move toward profit, never farther away (S12-R2)."""
    return max(old_stop, new_stop) if direction == "long" else min(old_stop, new_stop)


def winner_action(weight_pct: float) -> Literal["none", "rebalance_flag", "rebalance_mandatory"]:
    """Open position that grew (S12-R12). The 12% cap applies only at entry/add."""
    p = load_policy().winner
    if weight_pct > p.rebalance_mandatory_pct:
        return "rebalance_mandatory"
    if weight_pct >= p.rebalance_flag_pct:
        return "rebalance_flag"
    return "none"
