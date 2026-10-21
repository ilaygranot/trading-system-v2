"""The end-of-week review (S13-R21, S13-R25), code only: what a person reads on Saturday before the new ideas.

wfetch -> arena -> revisit -> week, then the weekly notify joins it to the operations report (qt.pipelines).
arena:   the weekly sector arena (S04-R29): RS of the 11 SPDR sectors against SPY over a week, a month, 3 and 6
         months and a year (S04-R11), their average and rank (S04-R12), and the move against last week's rank.
revisit: every watchlist entry checked again (S13-R25 "old ideas", S08-R29, S08-R30): invalidated, expired, more
         than 60 days on the list, potential to the target halved. These are proposals to archive, never a removal:
         the list is Ilay's (S08-R29 is a trigger for review, not an automatic exit).
week:    the week in the book: entries filled, sells with their P&L, open positions with the week's move and the
         distance to the stop, the ones that hurt; earnings and time stops in the next 14 days; the idea quota
         (S13-R26: at least 2 new ideas a week).
News of the week, the economic calendar and sentiment (NAAIM / COT) have no source yet; the summary says so.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from qt import watch as W
from qt.daily import _bars, _bars_json
from qt.runs import RunContext
from qt.sectors import rank, rs_avg, rs_perf

SECTORS = ("XLK", "XLY", "XLI", "XLE", "XLF", "XLV", "XLP", "XLU", "XLB", "XLRE", "XLC")
BENCH, VIX = "SPY", "^VIX"
HISTORY_DAYS = 400                     # a year of sessions (252) for the 1y RS period, with margin
AHEAD_DAYS = 14                        # S13-R21: the calendar for the next week or two
MIN_NEW_IDEAS = 2                      # S13-R26
ARCHIVE = {"invalidated": "הטריגר בוטל", "expired": "הטריגר פג"}


def _pf(ctx: RunContext) -> dict:
    return ctx.services.get("portfolio") or {}


def wfetch(ctx: RunContext, _: str) -> dict:
    """The week's prices: SPY, VIX, the 11 sectors, the positions and the watchlist."""
    pf = _pf(ctx)
    entries = W.load(ctx.services.get("watchlist"))
    symbols = sorted(set(SECTORS) | {BENCH, VIX} | {e.symbol for e in entries}
                     | {p["symbol"] for p in pf.get("positions", []) if p.get("instrument", "stock") == "stock"})
    start = ctx.as_of - timedelta(days=HISTORY_DAYS)
    out: dict[str, Any] = {"bars": {}, "missing": []}
    for s in symbols:
        for prov in (ctx.services.get("prices"), ctx.services.get("prices_fallback")):
            if prov is None:
                continue
            try:
                bars, _src = prov.daily_bars(s, start, ctx.as_of)
            except Exception as e:                                   # noqa: BLE001 - next provider
                ctx.event(f"{s}: {type(e).__name__}: {e}", "warn", "wfetch")
                continue
            bars = [b for b in bars if b.day <= ctx.as_of]
            if bars:
                out["bars"][s] = _bars_json(bars)
                break
        else:
            out["missing"].append(s)
    return out


def _closes_by_day(rows: list[list]) -> dict[str, float]:
    return {r[0]: r[4] for r in rows}


def _last_arena(ctx: RunContext) -> dict[str, int]:
    """Ranks from the previous finished weekly run, to show the move."""
    row = ctx.db.one("SELECT s.output FROM run_steps s JOIN runs r ON r.id = s.run_id WHERE r.kind = 'weekly' "
                     "AND r.as_of < ? AND s.step = 'arena' AND s.status = 'done' ORDER BY r.as_of DESC LIMIT 1",
                     (ctx.as_of.isoformat(),), ["output"])
    return {x["etf"]: x["rank"] for x in ((row or {}).get("output") or {}).get("rows", [])}


def sector_arena(bars: dict[str, list[list]], prev: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """RS per period on the sessions both series have, the average, the rank (11 = strongest)."""
    if BENCH not in bars:
        return []
    spy = _closes_by_day(bars[BENCH])
    perf, avg = {}, {}
    for etf in SECTORS:
        if etf not in bars:
            continue
        c = _closes_by_day(bars[etf])
        days = sorted(set(c) & set(spy))
        p = rs_perf([c[d] for d in days], [spy[d] for d in days])
        try:
            avg[etf] = rs_avg(p)
        except ValueError:
            continue
        perf[etf] = p
    ranks = rank(avg)
    rows = [dict(etf=e, rank=ranks[e], rs_avg=round(avg[e], 2), **{k: None if v is None else round(v, 2)
                                                                   for k, v in perf[e].items()},
                 rank_change=(ranks[e] - prev[e]) if prev and e in prev else None) for e in avg]
    return sorted(rows, key=lambda r: -r["rank"])


def arena(ctx: RunContext, _: str) -> dict:
    rows = sector_arena(ctx.out("wfetch")["bars"], _last_arena(ctx))
    return {"rows": rows, "top": [r["etf"] for r in rows[:3]], "bottom": [r["etf"] for r in rows[-3:]]}


def revisit_entries(entries: list[W.WatchEntry], bars: dict[str, list[list]], as_of: date) -> list[dict[str, Any]]:
    out = []
    for e in entries:
        rows = bars.get(e.symbol)
        if not rows:
            out.append(dict(symbol=e.symbol, status="stale", propose="", reasons=["אין נתונים"]))
            continue
        c = W.check(e, _bars(rows), as_of)
        reasons = [ARCHIVE[c.status]] if c.status in ARCHIVE else []
        if c.review_day60:
            reasons.append(f"{c.days_on_list} ימים ברשימה (S08-R29)")
        if c.potential_pct is not None and c.potential_at_add_pct and c.potential_pct < c.potential_at_add_pct / 2:
            reasons.append(f"הפוטנציאל ליעד ירד ל-{c.potential_pct:.0f}% מ-{c.potential_at_add_pct:.0f}% (S08-J1)")
        propose = "archive" if c.status in ARCHIVE else "review" if reasons else "keep"
        out.append(dict(symbol=e.symbol, direction=e.direction, bucket=e.bucket, added=e.added, status=c.status,
                        close=c.close, rr=c.rr, potential_pct=c.potential_pct, days_on_list=c.days_on_list,
                        propose=propose, reasons=reasons))
    return out


def revisit(ctx: RunContext, _: str) -> dict:
    entries = W.load(ctx.services.get("watchlist"))
    rows = revisit_entries(entries, ctx.out("wfetch")["bars"], ctx.as_of)
    since = ctx.as_of - timedelta(days=6)
    new = [e.symbol for e in entries if date.fromisoformat(e.added) >= since]
    return {"rows": rows, "n": len(entries), "new_this_week": new,
            "longs": sum(e.direction == "long" for e in entries), "shorts": sum(e.direction == "short" for e in entries),
            "archive": [r["symbol"] for r in rows if r["propose"] == "archive"],
            "review": [r["symbol"] for r in rows if r["propose"] == "review"]}


def _week_ago(rows: list[list], as_of: date) -> float | None:
    """The last close on or before the same weekday a week earlier."""
    cut = (as_of - timedelta(days=7)).isoformat()
    prior = [r for r in rows if r[0] <= cut]
    return prior[-1][4] if prior else None


def week_book(pf: dict, fills: list[dict], bars: dict[str, list[list]], entries: list[W.WatchEntry],
              as_of: date) -> dict[str, Any]:
    since = (as_of - timedelta(days=6)).isoformat()
    realized = [r for r in pf.get("realized", []) if (r.get("on") or "") >= since]
    positions = []
    for p in pf.get("positions", []):
        rows = bars.get(p["symbol"])
        if not rows or p.get("instrument", "stock") != "stock":
            continue
        sg = 1 if p["direction"] == "long" else -1
        px, w0 = rows[-1][4], _week_ago(rows, as_of)
        pl = sg * (px / p["entry"] - 1) * 100
        wk = sg * (px / w0 - 1) * 100 if w0 else None
        to_stop = sg * (px / p["stop"] - 1) * 100 if p.get("stop") else None
        positions.append(dict(symbol=p["symbol"], direction=p["direction"], close=px, pl_pct=round(pl, 2),
                              week_pct=None if wk is None else round(wk, 2),
                              to_stop_pct=None if to_stop is None else round(to_stop, 2),
                              hurting=pl < 0 or (wk is not None and wk < 0)))
    end = as_of + timedelta(days=AHEAD_DAYS)
    ahead = []
    for e in entries:
        for kind, d in (("earnings", e.next_earnings), ("time_stop", e.time_stop)):
            if d and as_of < date.fromisoformat(d) <= end:
                ahead.append(dict(date=d, symbol=e.symbol, kind=kind))
    return {"entries": [f for f in fills if f["kind"] in ("enter", "add")],
            "exits": [f for f in fills if f["kind"] not in ("enter", "add")],
            "realized": realized, "realized_pnl": round(sum(r["pnl"] for r in realized), 2),
            "positions": positions, "hurting": [p["symbol"] for p in positions if p["hurting"]],
            "ahead": sorted(ahead, key=lambda a: a["date"])}


def week(ctx: RunContext, _: str) -> dict:
    since = (ctx.as_of - timedelta(days=6)).isoformat()
    fills = ctx.db.all("SELECT symbol, kind, qty, fill_on, fill_price FROM paper_orders WHERE status = 'filled' "
                       "AND fill_on >= ? AND fill_on <= ? ORDER BY fill_on, id", (since, ctx.as_of.isoformat()))
    bars = ctx.out("wfetch")["bars"]
    out = week_book(_pf(ctx), fills, bars, W.load(ctx.services.get("watchlist")), ctx.as_of)
    for s in (BENCH, VIX):
        rows = bars.get(s)
        if rows:
            w0 = _week_ago(rows, ctx.as_of)
            out[s] = dict(close=rows[-1][4], week_pct=round((rows[-1][4] / w0 - 1) * 100, 2) if w0 else None)
    return out


# ---------------------------------------------------------------- the message

def _f(v: float | None, d: int = 1, sign: bool = True) -> str:
    return "—" if v is None else (f"{v:+.{d}f}" if sign else f"{v:.{d}f}")


KIND_HE = {"enter": "כניסה", "add": "הגדלה", "exit": "יציאה", "reduce": "הקטנה", "sell": "מכירה",
           "earnings": "דוח", "time_stop": "עצירת זמן"}


def summary(ctx: RunContext) -> str:
    """The weekly summary in the order of S13-R21."""
    wk, ar, rv = ctx.out("week") or {}, ctx.out("arena") or {}, ctx.out("revisit") or {}
    lines = [f"סיכום שבוע עד {ctx.as_of.isoformat()}"]
    if wk.get(BENCH):
        lines.append(f"שוק: SPY {wk[BENCH]['close']:.2f} ({_f(wk[BENCH]['week_pct'])}% בשבוע)"
                     + (f" · VIX {wk[VIX]['close']:.1f}" if wk.get(VIX) else ""))
    trades = [f"{KIND_HE.get(f['kind'], f['kind'])} {f['symbol']} {f['qty']:g} @ {f['fill_price']:.2f}"
              for f in wk.get("entries", []) + wk.get("exits", [])]
    lines.append("עסקאות השבוע: " + (" · ".join(trades) if trades else "אין"))
    if wk.get("realized"):
        lines.append(f"רווח/הפסד ממומש: {wk['realized_pnl']:+,.0f}$")
    if wk.get("positions"):
        lines.append("פוזיציות:")
        for p in wk["positions"]:
            lines.append(f"• {p['symbol']} {p['close']:.2f} · {_f(p['week_pct'])}% בשבוע · {_f(p['pl_pct'])}% מהכניסה"
                         f" · {_f(p['to_stop_pct'], sign=False)}% מהעצירה" + (" · ⚠️ מזיקה" if p["hurting"] else ""))
    else:
        lines.append("פוזיציות: אין")
    if ar.get("rows"):
        def _arena(r):
            mv = r["rank_change"]
            return f"{r['etf']} {r['rank']}" + (f" ({mv:+d})" if mv else "")
        lines.append("זירת סקטורים (11 = החזק): " + " · ".join(_arena(r) for r in ar["rows"]))
    if rv:
        lines.append(f"רשימת מעקב: {rv['n']} ({rv['longs']} לונג, {rv['shorts']} שורט) · חדשים השבוע: "
                     + (", ".join(rv["new_this_week"]) if rv["new_this_week"] else "אין"))
        if len(rv["new_this_week"]) < MIN_NEW_IDEAS:
            lines.append(f"⚠️ פחות מ-{MIN_NEW_IDEAS} רעיונות חדשים השבוע (S13-R26)")
        for r in rv["rows"]:
            if r["propose"] != "keep":
                head = "להעביר לארכיון" if r["propose"] == "archive" else "לבדוק"
                lines.append(f"• {r['symbol']}: {head} · " + "; ".join(r["reasons"]))
    if wk.get("ahead"):
        lines.append("שבועיים קדימה: " + " · ".join(f"{a['date'][5:]} {KIND_HE[a['kind']]} {a['symbol']}"
                                                      for a in wk["ahead"]))
    lines.append("עוד לא בסיכום (אין מקור): חדשות השבוע מול עמדת המקרו, יומן כלכלי, סנטימנט NAAIM/COT")
    return "\n".join(lines)
