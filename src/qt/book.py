"""The paper book: the portfolio file kept up to date by the daily run (runtime plan 7.2 rule 3).

An approved entry, a stop exit, a partial take or a cut fills at the next open as a paper order; this module
applies each fill to the portfolio (cash, quantity, average entry), and applies the standing stop moves of the
day (60/40, S12-R11) so tomorrow's review starts from the stop that was approved. Stops only tighten
(S12-R2). The result is saved as the newest portfolio upload, which the next run loads. Nothing here talks
to a broker.
"""
from __future__ import annotations

import copy
from typing import Any

FLAG_ON_FILL = {"S12-R13": "partial_taken", "S12-R11": "profit_taken"}      # sells that close a one-time step


def _find(pf: dict, symbol: str) -> dict | None:
    return next((p for p in pf.get("positions", []) if p["symbol"] == symbol), None)


def apply_fill(pf: dict, kind: str, symbol: str, qty: float | None, price: float, payload: dict[str, Any]) -> tuple[dict, str]:
    """One paper fill -> (new portfolio, what happened). Unknown or unsized orders change nothing and say so."""
    pf = copy.deepcopy(pf)
    pf.setdefault("positions", [])
    pos = _find(pf, symbol)
    if qty is None or qty <= 0:
        return pf, f"{symbol}: {kind} without a quantity, not applied"
    if kind in ("enter", "add"):
        direction = payload.get("direction") or (pos or {}).get("direction") or "long"
        sg = 1 if direction == "long" else -1
        pf["cash"] = pf.get("cash", 0.0) - sg * qty * price
        if pos is None:
            stop = payload.get("stop")
            pf["positions"].append({k: v for k, v in dict(
                symbol=symbol, direction=direction, sector=payload.get("sector") or "?", qty=qty, entry=price,
                entry_stop=stop, stop=stop, target=payload.get("target"), opened=payload.get("as_of")).items()
                if v is not None})
            return pf, f"{symbol}: opened {qty:g} @ {price:.2f}"
        total = pos["qty"] + qty
        pos["entry"] = (pos["entry"] * pos["qty"] + price * qty) / total
        pos["qty"] = total
        return pf, f"{symbol}: added {qty:g} @ {price:.2f}, average {pos['entry']:.2f}"
    if pos is None:
        return pf, f"{symbol}: {kind} but no open position, not applied"
    sg = 1 if pos["direction"] == "long" else -1
    q = min(qty, pos["qty"]) if kind != "exit" else pos["qty"]
    pf["cash"] = pf.get("cash", 0.0) + sg * q * price
    pnl = sg * (price - pos["entry"]) * q
    if q >= pos["qty"]:
        pf["positions"].remove(pos)
        pf.setdefault("closed", []).append(dict(symbol=symbol, qty=q, entry=pos["entry"], exit=price,
                                                pnl=round(pnl, 2), rule=payload.get("rule_id")))
        return pf, f"{symbol}: closed {q:g} @ {price:.2f}, P&L {pnl:+,.0f}$"
    pos["qty"] = pos["qty"] - q
    flag = FLAG_ON_FILL.get(payload.get("rule_id", ""))
    if flag:
        pos[flag] = True
    return pf, f"{symbol}: sold {q:g} @ {price:.2f}, P&L {pnl:+,.0f}$, {pos['qty']:g} left"


def apply_stop(pf: dict, symbol: str, new_stop: float) -> tuple[dict, str | None]:
    """A standing stop move; only toward profit (S12-R2: never move a stop away)."""
    pf = copy.deepcopy(pf)
    pos = _find(pf, symbol)
    if pos is None or new_stop is None:
        return pf, None
    old = pos.get("stop")
    tighter = old is None or (new_stop > old if pos["direction"] == "long" else new_stop < old)
    if not tighter:
        return pf, None
    pos["stop"] = new_stop
    return pf, f"{symbol}: stop {old if old is None else f'{old:.2f}'} -> {new_stop:.2f}"


def mark_equity(pf: dict, prices: dict[str, float]) -> None:
    """Peak equity for the portfolio stop (S12-R1): daily close including open profit (decision 20.10)."""
    value = sum((1 if p["direction"] == "long" else -1) * p["qty"] * prices.get(p["symbol"], p["entry"])
                for p in pf.get("positions", []))
    eq = pf.get("cash", 0.0) + value
    pf["peak_equity"] = max(pf.get("peak_equity", eq), eq)
