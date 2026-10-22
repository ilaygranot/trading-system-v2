"""The trade journal (S13-R29, S08-R32, S08-J23): one row per trade, in the columns of the course and of Ilay's
paper sheet ("Quantum Trader: יומן עסקאות ודוח שבועי"), with the numbers code measures and the review fields.

Pure functions over the book's closed-trade record (qt.book) and daily bars [day, open, high, low, close]:
- the plan as it was approved (thesis and trigger, planned entry, stop, target, size, risk);
- the execution (fill, slippage, exit and its reason, days, P&L in $ and in R);
- the excursions while in the trade (MFE / MAE in R), whether the target was reached, and after a stop exit
  whether the target came within 20 sessions anyway (stopped by noise before the thesis, architecture 7.2);
- did the trade follow the plan, and the code's draft of the review: idea quality and execution quality apart
  (S08-J23), a loss or a mistake (S13-J4). The failure stage (S13-R29) is left to agent L and Ilay.
"""
from __future__ import annotations

import csv
import io
from datetime import date
from typing import Any

AFTER_EXIT_SESSIONS = 20         # not from course: how long after a stop exit we look for the target (about a month)
SLIPPAGE_PCT = 1.0               # not from course: a fill this far from the planned price counts as off plan
SIZE_TOLERANCE_PCT = 1.0         # not from course: entry value vs the planned % of equity, in points

# exits by a rule of the system (standing orders and approved proposals): the plan was followed
EXIT_HE = {"S12-R2": "סטופ", "S12-R1": "סטופ תיק", "S12-R11": "60/40 שימור רווח", "S12-R13": "חצי ביעד",
           "S12-R12": "איזון מנצחת", "S12-R26": "חיתוך משטר", "S12-R7": "נטו גבוה בזעזוע",
           "S12-R27": "קטליסט נגדי", "DC-21": "קצין סיכונים", "DC-23": "מימוש טכני"}

STAGES_HE = {"1": "מקרו גלובלי", "2": "מקרו מדינה", "3": "שוק ונכסים", "4": "סקטורים", "5": "תעשיות",
             "6": "סינון מניות", "7": "ניתוח עומק", "8": "רשימת מעקב", "9": "תזמון טכני", "10": "בניית עסקה",
             "11": "סיכון מקדים", "12": "סיכון תגובתי", "13": "התנהלות"}
QUALITY_HE = {"good": "טוב", "bad": "רע", "unclear": "לא ברור"}
LOSS_HE = {"loss": "הפסד (לא טעות)", "mistake": "טעות", "none": ""}


def trade_key(c: dict) -> str:
    return f"{c['symbol']}|{c.get('opened') or '?'}"


def _sg(c: dict) -> int:
    return 1 if c.get("direction", "long") == "long" else -1


def risk_per_share(c: dict) -> float | None:
    if c.get("entry_stop") is None:
        return None
    r = abs(c["entry"] - c["entry_stop"])
    return r or None


def r_multiple(c: dict) -> float | None:
    """The whole trade's P&L over the initial risk (average entry to the stop at entry, on the largest quantity)."""
    rps = risk_per_share(c)
    if rps is None:
        return None
    pnl = c["trade_pnl"] if c.get("trade_pnl") is not None else c["pnl"]
    return round(pnl / (rps * (c.get("max_qty") or c["qty"])), 2)


def _window(rows: list[list], start: str | None, end: str | None) -> list[list]:
    return [r for r in rows if (start is None or r[0] >= start) and (end is None or r[0] <= end)]


def excursions(c: dict, rows: list[list]) -> dict[str, Any]:
    """MFE / MAE in R between the entry and the exit days, and whether the target was touched."""
    inside = _window(rows, c.get("opened"), c.get("closed"))
    rps = risk_per_share(c)
    if not inside:
        return {"mfe_r": None, "mae_r": None, "target_hit": None}
    sg, e = _sg(c), c["entry"]
    best = max(r[2] for r in inside) if sg == 1 else min(r[3] for r in inside)
    worst = min(r[3] for r in inside) if sg == 1 else max(r[2] for r in inside)
    t = c.get("target")
    return {"mfe_r": round(sg * (best - e) / rps, 2) if rps else None,
            "mae_r": round(sg * (worst - e) / rps, 2) if rps else None,
            "target_hit": None if t is None else bool(sg * (best - t) >= 0)}


def after_exit(c: dict, rows: list[list], sessions: int = AFTER_EXIT_SESSIONS) -> dict[str, Any]:
    """After the exit: did the price reach the target within `sessions` (complete once that many sessions passed)."""
    t, closed = c.get("target"), c.get("closed")
    if t is None or closed is None:
        return {"target_after_exit": None, "complete": True}
    later = [r for r in rows if r[0] > closed][:sessions]
    sg = _sg(c)
    hit = any(sg * ((r[2] if sg == 1 else r[3]) - t) >= 0 for r in later)
    return {"target_after_exit": hit if hit or len(later) >= sessions else None,
            "complete": hit or len(later) >= sessions, "sessions_seen": len(later)}


def plan_check(c: dict) -> list[str]:
    """What departed from the approved plan. Empty = the trade followed the plan."""
    off, plan = [], c.get("plan") or {}
    if plan.get("price"):
        slip = abs(c["entry"] / plan["price"] - 1) * 100
        if slip > SLIPPAGE_PCT:
            off.append(f"כניסה {c['entry']:.2f} רחוקה {slip:.1f}% מהמתוכנן {plan['price']:.2f}")
    if plan.get("size_pct") and plan.get("equity"):
        size = c["entry"] * (c.get("max_qty") or c["qty"]) / plan["equity"] * 100
        if size - plan["size_pct"] > SIZE_TOLERANCE_PCT:
            off.append(f"גודל {size:.1f}% מעל המתוכנן {plan['size_pct']:g}%")
    if c.get("rule") not in EXIT_HE:
        off.append("יציאה בלי כלל של המערכת (ידנית)")
    if c.get("entry_stop") is None:
        off.append("אין סטופ בכניסה")
    return off


def code_review(c: dict, ex: dict, ax: dict, off: list[str]) -> dict[str, Any]:
    """The code's draft: idea apart from execution (S08-J23), loss apart from mistake (S13-J4). An idea is good
    when its assumptions proved right: the target was reached, in the trade or within the window after it."""
    r = r_multiple(c)
    reached = ex.get("target_hit") or ax.get("target_after_exit")
    if reached:
        idea = "good"
    elif c.get("rule") == "S12-R2" and ax.get("target_after_exit") is False:
        idea = "bad"
    else:
        idea = "unclear"
    execution = "bad" if off else "good"
    loss = "none" if r is None or r >= 0 else ("loss" if not off else "mistake")
    noise = c.get("rule") == "S12-R2" and bool(ax.get("target_after_exit"))
    return {"idea_quality": idea, "execution_quality": execution, "loss_or_mistake": loss,
            "stopped_by_noise": noise, "off_plan": off, "failure_class": None, "stage": None,
            "worked": "", "not_worked": "", "lesson": "", "proposal": ""}


def days_held(c: dict) -> int | None:
    if not c.get("opened") or not c.get("closed"):
        return None
    return (date.fromisoformat(c["closed"]) - date.fromisoformat(c["opened"])).days


# ---------------------------------------------------------------- the journal row

COLUMNS = [  # (key, header): Ilay's paper sheet first, then the course's review fields
    ("opened", "תאריך כניסה"), ("symbol", "סימול"), ("direction", "כיוון"), ("sector", "סקטור"),
    ("thesis", "תזה, קטליסט ולמה עכשיו"), ("planned_entry", "כניסה מתוכננת"), ("entry_stop", "סטופ"),
    ("target", "יעד"), ("risk_usd", "סיכון $"), ("risk_pct", "סיכון % מההון"), ("max_qty", "כמות"),
    ("value", "שווי פוזיציה $"), ("size_pct", "% מהתיק"), ("by_formula", "גודל לפי נוסחה?"),
    ("entry", "כניסה בפועל"), ("closed", "תאריך יציאה"), ("exit", "יציאה בפועל"), ("exit_reason", "סיבת יציאה"),
    ("days", "ימים"), ("pnl", "PnL $"), ("r", "PnL ב-R"), ("mfe_r", "MFE ב-R"), ("mae_r", "MAE ב-R"),
    ("target_hit", "הגיע ליעד בעסקה"), ("target_after_exit", "הגיע ליעד אחרי היציאה"),
    ("followed_plan", "עמדתי בתכנית?"), ("off_plan", "חריגות מהתכנית"),
    ("idea_quality", "איכות הרעיון"), ("execution_quality", "איכות הביצוע"), ("loss_or_mistake", "הפסד או טעות"),
    ("stage", "שלב הכשל (1-13)"), ("failure_class", "סוג הכשל"), ("worked", "מה עבד"), ("not_worked", "מה לא עבד"),
    ("lesson", "מה למדתי"), ("proposal", "הצעת שינוי"), ("review_by", "תחקיר: מי"), ("review_status", "אישור אילי"),
]


def _yn(v: bool | None) -> str:
    return "" if v is None else ("כן" if v else "לא")


def row(c: dict, review: dict | None = None, measured: dict | None = None) -> dict[str, Any]:
    """One journal row. `review` is the latest review (code, L or Ilay's), `measured` the code's measurements."""
    plan, m, rv = c.get("plan") or {}, measured or {}, review or {}
    rps, qty = risk_per_share(c), c.get("max_qty") or c["qty"]
    risk = round(rps * qty, 2) if rps else None
    eq = plan.get("equity")
    size = round(c["entry"] * qty / eq * 100, 2) if eq else None
    pnl = c["trade_pnl"] if c.get("trade_pnl") is not None else c["pnl"]
    off = rv.get("off_plan", m.get("off_plan"))
    stage = rv.get("stage")
    return {
        "opened": c.get("opened"), "symbol": c["symbol"], "direction": "לונג" if _sg(c) == 1 else "שורט",
        "sector": c.get("sector"), "thesis": plan.get("detail", ""), "planned_entry": plan.get("price"),
        "entry_stop": c.get("entry_stop"), "target": c.get("target"), "risk_usd": risk,
        "risk_pct": round(risk / eq * 100, 2) if risk and eq else None, "max_qty": qty,
        "value": round(c["entry"] * qty, 2), "size_pct": size,
        "by_formula": _yn(None if size is None or not plan.get("size_pct")
                          else size - plan["size_pct"] <= SIZE_TOLERANCE_PCT),
        "entry": round(c["entry"], 4), "closed": c.get("closed"), "exit": c.get("exit"),
        "exit_reason": EXIT_HE.get(c.get("rule"), c.get("rule") or "ידני"), "days": days_held(c),
        "pnl": pnl, "r": r_multiple(c), "mfe_r": m.get("mfe_r"), "mae_r": m.get("mae_r"),
        "target_hit": _yn(m.get("target_hit")), "target_after_exit": _yn(m.get("target_after_exit")),
        "followed_plan": _yn(None if off is None else not off), "off_plan": "; ".join(off or []),
        "idea_quality": QUALITY_HE.get(rv.get("idea_quality"), ""),
        "execution_quality": QUALITY_HE.get(rv.get("execution_quality"), ""),
        "loss_or_mistake": LOSS_HE.get(rv.get("loss_or_mistake"), ""),
        "stage": f"{stage} {STAGES_HE.get(str(stage), '')}".strip() if stage not in (None, "none") else "",
        "failure_class": rv.get("failure_class") or "", "worked": rv.get("worked", ""),
        "not_worked": rv.get("not_worked", ""), "lesson": rv.get("lesson", ""), "proposal": rv.get("proposal", ""),
        "review_by": rv.get("by", ""), "review_status": rv.get("status", ""),
    }


def open_row(p: dict, price: float | None) -> dict[str, Any]:
    """An open position in the same columns (no exit yet): the plan and the open result."""
    c = dict(p, exit=None, pnl=0.0, closed=None, rule=None)
    if price is not None:
        c["pnl"] = round(_sg(p) * (price - p["entry"]) * p["qty"], 2)
    out = row(c)
    out.update(exit_reason="פתוחה", followed_plan="", off_plan="")
    return out


def to_csv(rows: list[dict[str, Any]], columns: list[tuple[str, str]] | None = None) -> str:
    """UTF-8 with a BOM so Excel and Google Sheets read the Hebrew headers."""
    cols = columns or [(k, k) for k in (rows[0] if rows else {})]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([h for _, h in cols])
    for r in rows:
        w.writerow(["" if r.get(k) is None else r.get(k) for k, _ in cols])
    return "﻿" + buf.getvalue()
