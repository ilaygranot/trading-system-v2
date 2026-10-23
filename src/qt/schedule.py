"""When each run is due, in New York time (runtime plan 4). Render cron runs in UTC and does not follow
daylight saving, so each cron fires at both candidate UTC hours and this guard lets exactly one through."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from qt.policy import load_policy


def _r():
    return load_policy().runtime


def ny(now_utc: datetime | None = None) -> datetime:
    return (now_utc or datetime.now(timezone.utc)).astimezone(ZoneInfo(_r().timezone))


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in {str(h) for h in _r().nyse_holidays}


def previous_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def next_open(d: date) -> datetime:
    """09:30 New York on the next trading day after `d`: when an entry proposal expires (runtime plan 7.2)."""
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return datetime.combine(d, time(9, 30), ZoneInfo(_r().timezone))


def _in_window(t: datetime, hhmm: str) -> bool:
    h, m = map(int, hhmm.split(":"))
    start = t.replace(hour=h, minute=m, second=0, microsecond=0)
    return start <= t < start + timedelta(minutes=_r().window_minutes)


def due(kind: str, now_utc: datetime | None = None) -> tuple[bool, date]:
    """(is it time, the as-of date). Daily/premarket/open: trading days only; weekly/monthly: Saturday,
    as of the last trading day; monthly only in the second week (after the first week, S13)."""
    t = ny(now_utc)
    r = _r()
    if kind == "daily":
        return is_trading_day(t.date()) and _in_window(t, r.daily_at), t.date()
    if kind == "premarket":
        return is_trading_day(t.date()) and _in_window(t, r.premarket_at), t.date()
    if kind == "open":
        return is_trading_day(t.date()) and _in_window(t, r.open_at), t.date()
    as_of = previous_trading_day(t.date() + timedelta(days=1))
    if kind == "weekly":
        return t.weekday() == 5 and _in_window(t, r.weekly_at), as_of
    if kind == "monthly":
        lo, hi = r.monthly_days
        return t.weekday() == 5 and lo <= t.day <= hi and _in_window(t, r.weekly_at), as_of
    raise ValueError(kind)


def next_run(kind: str, now_utc: datetime | None = None) -> datetime | None:
    """When `kind` is next due (New York time), for the dashboard's schedule. Walks the days ahead with due()."""
    r = _r()
    at = {"daily": r.daily_at, "premarket": r.premarket_at, "open": r.open_at, "weekly": r.weekly_at, "monthly": r.weekly_at}[kind]
    h, m = map(int, at.split(":"))
    t = ny(now_utc)
    for i in range(45):
        cand = (t + timedelta(days=i)).replace(hour=h, minute=m, second=0, microsecond=0)
        if cand > t and due(kind, cand.astimezone(timezone.utc))[0]:
            return cand
    return None
