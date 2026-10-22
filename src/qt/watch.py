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
    risk_pct_at_add: float | None = None    # the stop's width when the decision was made (k x ADR, % of price)
    green_at_add: bool = False              # Ilay 22.10: every green stock stays on the daily pass
    why_not: str = ""                       # why it was not entered then (the research's reason)


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
    trend_1y_pct: float | None = None       # the 078 watchlist's three trend columns: price change over the last
    trend_3m_pct: float | None = None       # year, the last 3 months and since the idea was added (S08-R4)
    since_add_pct: float | None = None
    hindsight: str | None = None            # missed (moved 3R our way without us) | saved (would have hit the stop)


def _change_since(bars: Sequence[Bar], start: date) -> float | None:
    """Close now vs the first close on or after `start`; None when the bars don't reach back that far."""
    if not bars or bars[0].day > start:
        return None
    first = next(b for b in bars if b.day >= start)
    return _pct(bars[-1].close, first.close)


def _pct(a: float, b: float) -> float:
    return (a / b - 1) * 100


def from_dossier(d: dict) -> dict[str, Any]:
    """The watchlist file from a research dossier: every idea that is not rejected, and every green stock even when
    it was rejected or skipped (Ilay 22.10: each green stock is followed daily, to learn whether the decision was
    right and to catch the day it meets the criteria)."""
    as_of = d["meta"]["as_of"]
    color = {t["sym"]: t.get("combined") for t in d.get("timing") or []}
    out = []
    for sym, i in d["ideas"].items():
        green = color.get(sym) == "green"
        if i["bucket"] == "reject" and not green:
            continue
        j, p, x = i["judgment"], i["plan"], i.get("exit") or {}
        bucket = "watch" if i["bucket"] == "reject" else i["bucket"]
        trig = j.get("trigger") or {}
        level = trig.get("level")
        kind: TriggerKind = "now" if bucket == "enter" or level is None else (
            "close_above" if i["direction"] == "long" else "close_below")
        inval = None
        if bucket == "conditional":
            sup = (i.get("levels") or {}).get("nearest_support" if i["direction"] == "long" else "nearest_resistance")
            inval = round(sup, 2) if sup else None
        out.append(asdict(WatchEntry(
            symbol=sym, direction=i["direction"], etf=i["etf"], bucket=bucket, added=as_of,
            trigger_kind=kind, trigger_level=round(level, 2) if level else None, k=p["k"],
            target=round(p["target"], 2) if p.get("target") else None, target_source=p.get("target_source"),
            invalidate_level=inval, time_stop=x.get("time_stop_date"), next_earnings=x.get("next_earnings") or
            (i.get("catalysts") or {}).get("next_earnings"),
            first_size_pct=float((j.get("sizing") or {}).get("first_size_pct") or p["sizing"]["allowed_pct"]),
            price_at_add=round(p["entry"], 2), tldr=(j.get("tldr") or {}).get("line", "") or j.get("why", ""),
            breakers=[b["text"] for b in j.get("breakers", [])],
            risk_pct_at_add=round(T.stop_distance_pct(p["entry"], p["stop"]), 2) if p.get("stop") else None,
            green_at_add=green, why_not="" if bucket == "enter" else (i.get("bucket_reason") or j.get("why", "")))))
    skipped = ((d.get("judgments") or {}).get("A3") or {}).get("skipped")
    k = load_policy().stop.k_default
    for t in d.get("timing") or []:                     # a green stock skipped before the deep dive, with its reason
        if t.get("combined") != "green" or t["sym"] in d["ideas"] or t.get("error"):
            continue
        why = skipped.get(t["sym"], "") if isinstance(skipped, dict) else ""
        out.append(asdict(WatchEntry(
            symbol=t["sym"], direction=t["dir"], etf=t["etf"], bucket="watch", added=as_of, trigger_kind="now",
            trigger_level=None, k=k, target=round(t["extreme"], 2) if t.get("extreme") else None,
            target_source="all_time_high_target", price_at_add=round(t["price"], 2),
            risk_pct_at_add=round(k * t["adr"], 2) if t.get("adr") else None, green_at_add=True,
            why_not=why or "ירוקה שלא נותחה לעומק", tldr=why)))
    return {"as_of": as_of, "source": "research " + as_of, "entries": out}


def hindsight(e: WatchEntry, close: float) -> str | None:
    """Was not entering right, so far (not from course)? 'missed': since the decision the price moved our way by 3
    times the stop width it had then (a 1:3 trade that happened without us); 'saved': it moved against us by the
    whole stop width (the trade would have been stopped out)."""
    if e.bucket == "enter" or not e.price_at_add or not e.risk_pct_at_add:
        return None
    move = (1 if e.direction == "long" else -1) * _pct(close, e.price_at_add)
    if move >= 3 * e.risk_pct_at_add:
        return "missed"
    if move <= -e.risk_pct_at_add:
        return "saved"
    return None


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
    hs = hindsight(e, close)
    if hs == "missed":
        notes.append(f"זזה {sg * _pct(close, e.price_at_add):.0f}% מאז ההחלטה לא להיכנס, פי 3 מהסיכון: לבדוק אם טעינו")
    elif hs == "saved":
        notes.append("ההחלטה לא להיכנס הייתה נכונה עד עכשיו: המחיר עבר את העצירה שהייתה אז")
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
                      review_day60=days > 60, earnings_in_days=ne, boiling=boiling, status=status, notes=notes,
                      trend_1y_pct=_change_since(bars, as_of - timedelta(days=365)),
                      trend_3m_pct=_change_since(bars, as_of - timedelta(days=91)),
                      since_add_pct=_pct(close, e.price_at_add) if e.price_at_add else None, hindsight=hs)


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
