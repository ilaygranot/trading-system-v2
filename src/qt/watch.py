"""The daily pass over the watchlist (S08 maintenance, S13-R22, S13-R23): did an opportunity open?

Code only, no agents. Each entry comes from a research run (`from_dossier`): its trigger, stop width, target,
time stop, next earnings and the tell-a-friend line. Every close the pass recomputes price, distance to the
trigger, the stop from today's range, reward:risk, potential to the target (S08-J1), days on the list (S08-R29),
the trigger's expiry (not from course), earnings within a week, and a run-up that makes it "boiling"
(S08-R25). A trigger that fired with reward:risk still >= the minimum, not expired and not invalidated is an
opportunity: an `enter` item in the approval queue, sized at the first step (S11-R5). Nothing is sent to a
broker; the full gate is Ilay's approval.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Literal, Sequence

from qt.calc import trade as T
from qt.calc.volatility import Bar, adr
from qt.policy import load_policy
from qt.watchlist import boiling_hint

EARNINGS_SOON_DAYS = 7                      # not from course: how early the summary warns
TriggerKind = Literal["now", "close_above", "close_below"]


@dataclass
class WatchEntry:
    symbol: str
    direction: Literal["long", "short"]
    etf: str
    bucket: str                             # enter | conditional | watch
    added: str                              # the research run's as-of date
    trigger_kind: TriggerKind
    trigger_level: float | None
    k: float
    target: float | None
    target_source: str | None
    invalidate_level: float | None = None   # a close beyond it cancels a conditional trigger
    time_stop: str | None = None
    next_earnings: str | None = None
    first_size_pct: float = 6.0
    price_at_add: float | None = None
    tldr: str = ""
    breakers: list[str] = field(default_factory=list)


@dataclass
class WatchCheck:
    symbol: str
    close: float
    change_1d_pct: float | None
    trigger_kind: str
    trigger_level: float | None
    dist_to_trigger_pct: float | None       # how far the close still has to go (0 or less = there)
    triggered: bool
    invalidated: bool
    expired: bool
    stop: float
    target: float | None
    rr: float | None
    rr_ok: bool
    potential_pct: float | None
    potential_at_add_pct: float | None
    days_on_list: int
    review_day60: bool
    earnings_in_days: int | None
    boiling: bool
    status: str                             # opportunity | ready | recheck | waiting | expired | invalidated | stale
    notes: list[str] = field(default_factory=list)


def _pct(a: float, b: float) -> float:
    return (a / b - 1) * 100


def from_dossier(d: dict) -> dict[str, Any]:
    """The watchlist file from a research dossier: every idea that is not rejected."""
    as_of = d["meta"]["as_of"]
    out = []
    for sym, i in d["ideas"].items():
        if i["bucket"] == "reject":
            continue
        j, p, x = i["judgment"], i["plan"], i.get("exit") or {}
        trig = j.get("trigger") or {}
        level = trig.get("level")
        kind: TriggerKind = "now" if i["bucket"] == "enter" or level is None else (
            "close_above" if i["direction"] == "long" else "close_below")
        inval = None
        if i["bucket"] == "conditional":
            sup = (i.get("levels") or {}).get("nearest_support" if i["direction"] == "long" else "nearest_resistance")
            inval = round(sup, 2) if sup else None
        out.append(asdict(WatchEntry(
            symbol=sym, direction=i["direction"], etf=i["etf"], bucket=i["bucket"], added=as_of,
            trigger_kind=kind, trigger_level=round(level, 2) if level else None, k=p["k"],
            target=round(p["target"], 2) if p.get("target") else None, target_source=p.get("target_source"),
            invalidate_level=inval, time_stop=x.get("time_stop_date"), next_earnings=x.get("next_earnings") or
            (i.get("catalysts") or {}).get("next_earnings"),
            first_size_pct=float((j.get("sizing") or {}).get("first_size_pct") or p["sizing"]["allowed_pct"]),
            price_at_add=round(p["entry"], 2), tldr=(j.get("tldr") or {}).get("line", "") or j.get("why", ""),
            breakers=[b["text"] for b in j.get("breakers", [])])))
    return {"as_of": as_of, "source": "research " + as_of, "entries": out}


def check(e: WatchEntry, bars: Sequence[Bar], as_of: date) -> WatchCheck:
    close = bars[-1].close
    sg = 1 if e.direction == "long" else -1
    prev = bars[-2].close if len(bars) > 1 else None
    if e.trigger_kind == "now" or e.trigger_level is None:
        dist, triggered = None, True
    else:
        dist = sg * _pct(e.trigger_level, close)
        triggered = sg * (close - e.trigger_level) > 0
    invalidated = bool(e.invalidate_level and not triggered and sg * (close - e.invalidate_level) < 0)
    expired = bool(e.time_stop and as_of > date.fromisoformat(e.time_stop) and not triggered)
    stop = T.stop_price(close, adr(bars), e.k, e.direction)                     # type: ignore[arg-type]
    rr = T.reward_risk(close, stop, e.target, e.direction) if e.target else None  # type: ignore[arg-type]
    rr_ok = rr is not None and rr >= load_policy().reward_risk.min_ratio
    pot = sg * _pct(e.target, close) if e.target else None
    pot0 = sg * _pct(e.target, e.price_at_add) if e.target and e.price_at_add else None
    days = (as_of - date.fromisoformat(e.added)).days
    ne = (date.fromisoformat(e.next_earnings) - as_of).days if e.next_earnings else None
    two_w = [b for b in bars if b.day >= as_of - timedelta(days=14)]
    boiling = bool(two_w) and boiling_hint(sg * _pct(close, two_w[0].close))
    notes = []
    if ne is not None and 0 <= ne <= EARNINGS_SOON_DAYS:
        notes.append(f"דוח בעוד {ne} ימים")
    if days > 60:
        notes.append("מעל 60 יום ברשימה: לבדוק אם להוציא (S08-R29)")
    if pot is not None and pot0 is not None and pot < pot0 / 2:
        notes.append("הפוטנציאל ליעד ירד ביותר מחצי מאז ההוספה (S08-J1)")
    if boiling:
        notes.append("רותח: זזה חזק בשבועיים, לא רודפים (S08-R25)")
    if triggered and e.trigger_kind != "now" and not rr_ok:
        notes.append("הטריגר הופעל אבל היחס ירד מתחת למינימום")
    if invalidated:
        status = "invalidated"
    elif expired:
        status = "expired"
    elif e.bucket == "watch":                # no trigger was set: a reward:risk back above the minimum asks for
        status = "recheck" if rr_ok and not boiling else "waiting"      # a fresh traffic light, not an entry
    elif triggered and rr_ok and not boiling:
        status = "opportunity" if e.trigger_kind != "now" else "ready"
    else:
        status = "waiting"
    return WatchCheck(symbol=e.symbol, close=close, change_1d_pct=_pct(close, prev) if prev else None,
                      trigger_kind=e.trigger_kind, trigger_level=e.trigger_level, dist_to_trigger_pct=dist,
                      triggered=triggered, invalidated=invalidated, expired=expired, stop=stop, target=e.target,
                      rr=rr, rr_ok=rr_ok, potential_pct=pot, potential_at_add_pct=pot0, days_on_list=days,
                      review_day60=days > 60, earnings_in_days=ne, boiling=boiling, status=status, notes=notes)


def entry_action(e: WatchEntry, c: WatchCheck, equity: float) -> dict[str, Any]:
    """An opportunity as an approval-queue item: first step of the size, the stop and target from today."""
    qty = int(equity * e.first_size_pct / 100 // c.close)
    trig = (f"סגירה {c.close:.2f} {'מעל' if e.direction == 'long' else 'מתחת ל'}-{c.trigger_level:.2f}"
            if c.trigger_level else "מוכנה לכניסה")
    detail = (f"{trig}. עצירה {c.stop:.2f}, יעד {c.target:.2f}, יחס {c.rr:.1f}. "
              f"{e.tldr}" + (f" ⚠️ {'; '.join(c.notes)}" if c.notes else ""))
    return dict(kind="enter", rule_id="S10-R4", symbol=e.symbol, qty=qty, price=c.close, preapproved=False,
                detail=detail, direction=e.direction, sector=e.etf, stop=round(c.stop, 2), target=c.target,
                size_pct=e.first_size_pct, equity=round(equity, 2))


def load(raw: dict | None) -> list[WatchEntry]:
    names = set(WatchEntry.__dataclass_fields__)
    return [WatchEntry(**{k: v for k, v in x.items() if k in names}) for x in (raw or {}).get("entries", [])]
