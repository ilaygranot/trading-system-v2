"""Trade arithmetic: stops, reward:risk, option spreads, sizing, horizon.

Code computes every number; agents receive them ready (architecture principle 3).
Direction is "long" or "short". Percentages are in percent units.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from qt.policy import load_policy

Direction = Literal["long", "short"]


def _sign(direction: Direction) -> int:
    return 1 if direction == "long" else -1


def stop_price(entry: float, adr_pct: float, k: float, direction: Direction = "long") -> float:
    """Entry stop = k x ADR60 away from entry (S11-R11, S10-R24). Not from the chart."""
    return entry * (1 - _sign(direction) * k * adr_pct / 100.0)


def stop_distance_pct(entry: float, stop: float) -> float:
    return abs(entry - stop) / entry * 100.0


def k_of(entry: float, stop: float, adr_pct: float) -> float:
    return stop_distance_pct(entry, stop) / adr_pct


def reward_risk(entry: float, stop: float, target: float, direction: Direction = "long") -> float:
    """Reward / risk with R = |entry - entry stop| (part ד decision 1)."""
    s = _sign(direction)
    risk = s * (entry - stop)
    reward = s * (target - entry)
    if risk <= 0:
        raise ValueError("stop must be on the losing side of entry")
    return reward / risk


@dataclass(frozen=True)
class VerticalSpread:
    """Debit vertical spread: buy one strike, sell a further OTM strike (S10-R19)."""
    kind: Literal["put", "call"]
    long_strike: float
    long_premium: float
    short_strike: float
    short_premium: float

    @property
    def cost(self) -> float:
        return self.long_premium - self.short_premium

    @property
    def width(self) -> float:
        return abs(self.long_strike - self.short_strike)

    @property
    def max_profit(self) -> float:
        return self.width - self.cost

    @property
    def ratio(self) -> float:
        """Max profit / cost (S10-R19, S10-R17: from the premium)."""
        return self.max_profit / self.cost

    def ratio_to_target(self, target: float) -> float:
        """Profit at target / cost; the short leg caps the payoff."""
        if self.kind == "put":
            intrinsic = max(0.0, self.long_strike - max(target, self.short_strike))
        else:
            intrinsic = max(0.0, min(target, self.short_strike) - self.long_strike)
        return (intrinsic - self.cost) / self.cost


def single_option_ratio(kind: Literal["put", "call"], strike: float, premium: float, target: float) -> float:
    """(target - strike - premium) / premium for a call; mirror for a put (S10-R17, 091 ZS example)."""
    intrinsic = (target - strike) if kind == "call" else (strike - target)
    return (max(0.0, intrinsic) - premium) / premium


@dataclass(frozen=True)
class Sizing:
    cap_pct: float          # 6% initial / 12% full
    risk_cap_pct: float     # max risk per trade / stop distance
    allowed_pct: float      # min of the two
    quantity: int
    risk_pct_of_equity: float
    reduced_by_risk_cap: bool
    unbearable: bool        # even one unit risks > 3%: "סיכון שלא ניתן לשאת" (term #4)


def size_position(equity: float, price: float, stop_pct: float, stage: Literal["initial", "full"] = "full",
                  requested_pct: float | None = None) -> Sizing:
    p = load_policy().sizing
    cap = p.initial_position_pct if stage == "initial" else p.full_position_pct
    risk_cap = p.max_risk_per_trade_pct / stop_pct * 100.0
    allowed = min(cap, risk_cap)
    if requested_pct is not None:
        allowed = min(allowed, requested_pct)
    qty = math.floor(equity * allowed / 100.0 / price + 1e-9)
    risk = qty * price * stop_pct / 100.0 / equity * 100.0
    one_unit_risk = price * stop_pct / 100.0 / equity * 100.0
    return Sizing(cap, risk_cap, allowed, qty, risk, risk_cap < cap, one_unit_risk > p.max_risk_per_trade_pct)


def risk_pct(position_pct: float, stop_pct: float) -> float:
    return position_pct * stop_pct / 100.0


def horizon_months(target_pct: float, monthly_sigma_pct: float) -> float:
    """Months ~= move / monthly sigma; how the lecturer judges realism (084:61). Sets horizon, doesn't block."""
    return abs(target_pct) / monthly_sigma_pct


def pct_move(entry: float, target: float) -> float:
    return (target - entry) / entry * 100.0


def cagr_pct(start: float, end: float, years: float) -> float:
    return ((end / start) ** (1 / years) - 1) * 100.0
