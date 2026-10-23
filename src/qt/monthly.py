"""The monthly preparation (S13-R25: the end of the first week of the month), after the macro stance (qt.pipelines):

a1:        A1 judges the stance on DC-01..DC-04 (grey band, compass, macro vs market, confidence), with the Critic.
mfetch:    monthly bars of SPY and the 11 sectors (15 years, for the seasonality) and the positions' last close.
candles:   the month's candle, and the quarter's at a quarter end, of the index and each sector (S04-R27).
sectors:   the sector growth expectations against the month before (S04-R34), from the weekly research's scorecard
           (S04-R29: the full scorecard is rebuilt by the weekly research, more often than the course asks).
ism:       the ISM industries table (S05-R6) and Short Supply (S05-R9) from the manual file, when it has them.
calendar:  the events of the coming month: FOMC, CPI, jobs, GDP, PCE, PPI, retail sales, JOLTS (FRED's release
           schedule), the ISM reports, earnings and time stops of positions and watchlist (S13-R25).
season:    the month's seasonality for SPY and each sector (S13-R25).
exposure:  long, short and net exposure against Ilay's caps (D-S11-7: policy.yaml exposure.caps, 22.10; the portfolio file can override).
sentiment: COT from the CFTC and NAAIM from the manual file (qt.sentiment).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from qt import watch as W
from qt.policy import load_policy
from qt.runs import RunContext
from qt.weekly import BENCH, SECTORS, _pf

SEASON_YEARS = 15                      # not from course: the course names seasonality without a method
CALENDAR_DAYS = 35                     # the coming month, with the first days of the next
# not from course: shape thresholds for a candle (the course names the shapes, not their proportions)
DOJI_BODY = 0.1                        # body under 10% of the range
WICK_RATIO = 2.0                       # a hammer's wick at least twice the body
TREND_BODY = 0.6                       # a trending candle: body at least 60% of the range
FRED_RELEASES = {10: "CPI", 50: "תעסוקה (NFP)", 53: "תוצר (GDP)", 54: "PCE", 46: "PPI", 9: "מכירות קמעונאיות",
                 192: "JOLTS"}
# FOMC decision days (the second day of each meeting). Source: the Fed's calendar as listed by the Chicago Fed and
# MNI (federalreserve.gov is not reachable from here); 2027 is tentative. Update once a year.
FOMC = ("2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28",
        "2026-12-09", "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09", "2027-07-28", "2027-09-15",
        "2027-10-27", "2027-12-08")
NET_REFERENCE = (0.8, 0.9)            # 087-R07: net 0.8-0.9 "completely fine", a reference, not a cap


# ---------------------------------------------------------------- A1

def a1(ctx: RunContext, _: str) -> dict:
    runner = ctx.services.get("agents")
    if runner is None:
        return {"enabled": False}
    from qt.facts import FactSheet
    from qt.research.judge import Judge, add_facts
    fs = FactSheet()
    st = {k: v for k, v in ctx.out("stance").items() if k != "cards"}
    add_facts(fs, "macro.stance", st, ctx.as_of, source="code:monthly", depth=3)
    try:
        return {"enabled": True, **Judge(runner, run_id=ctx.run_id, step="a1").macro(None, ctx.as_of, fs=fs)}
    except Exception as e:                                           # noqa: BLE001 - budget or API: the code's stance stands
        ctx.event(f"A1: {type(e).__name__}: {e}", "warn", "a1")
        return {"enabled": True, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- prices

def to_monthly(rows: list) -> list[list]:
    """Daily bars ([day, o, h, l, c] or Bar) to monthly [YYYY-MM, open, high, low, close]."""
    out: dict[str, list] = {}
    for r in rows:
        d, o, h, l, c = (r.day.isoformat(), r.open, r.high, r.low, r.close) if hasattr(r, "day") else r[:5]
        m = d[:7]
        if m not in out:
            out[m] = [m, o, h, l, c]
        else:
            b = out[m]
            b[2], b[3], b[4] = max(b[2], h), min(b[3], l), c
    return list(out.values())


def mfetch(ctx: RunContext, _: str) -> dict:
    pf = _pf(ctx)
    held = [p["symbol"] for p in pf.get("positions", []) if p.get("instrument", "stock") == "stock"]
    out: dict[str, Any] = {"monthly": {}, "last": {}, "missing": []}
    long_start = ctx.as_of - timedelta(days=365 * (SEASON_YEARS + 1))
    for s in (BENCH, *SECTORS, *held):
        start = long_start if s in (BENCH, *SECTORS) else ctx.as_of - timedelta(days=30)
        for prov in (ctx.services.get("prices"), ctx.services.get("prices_fallback")):
            if prov is None:
                continue
            try:
                bars, _src = prov.daily_bars(s, start, ctx.as_of)
            except Exception as e:                                   # noqa: BLE001 - next provider
                ctx.event(f"{s}: {type(e).__name__}: {e}", "warn", "mfetch")
                continue
            bars = [b for b in bars if b.day <= ctx.as_of]
            if bars:
                out["last"][s] = bars[-1].close
                if s in (BENCH, *SECTORS):
                    out["monthly"][s] = to_monthly(bars)
                break
        else:
            out["missing"].append(s)
    return out


def _closed(monthly: list[list], as_of: date) -> list[list]:
    """Months that ended before the run's month."""
    cut = f"{as_of.year:04d}-{as_of.month:02d}"
    return [m for m in monthly if m[0] < cut]


# ---------------------------------------------------------------- candles (S04-R27)

def candle(prev: list, cur: list) -> dict[str, Any]:
    """The shape of one candle against the one before: trending / hammer / inverted hammer / doji, the break of
    the previous low or high, inside / outside. The thresholds are not from the course."""
    from qt.macro import weekly_candle
    _, o, h, l, c = cur[:5]
    rng = max(h - l, 1e-9)
    body, upper, lower = abs(c - o), h - max(o, c), min(o, c) - l
    if lower >= WICK_RATIO * body and upper <= max(body, DOJI_BODY * rng):
        shape = "hammer"
    elif upper >= WICK_RATIO * body and lower <= max(body, DOJI_BODY * rng):
        shape = "inverted_hammer"
    elif body <= DOJI_BODY * rng:
        shape = "doji"
    elif body >= TREND_BODY * rng:
        shape = "trend_up" if c > o else "trend_down"
    else:
        shape = "plain_up" if c > o else "plain_down"
    breaks = "low" if c < prev[3] else "high" if c > prev[2] else None
    return dict(period=cur[0], shape=shape, closed_below_prev_low=c < prev[3], closed_above_prev_high=c > prev[2],
                breaks=breaks, relation=weekly_candle(prev[2], prev[3], h, l), change_pct=round((c / prev[4] - 1) * 100, 2))


def _quarter(months: list[list]) -> list:
    return [f"{months[0][0][:4]}-Q{(int(months[-1][0][5:7]) - 1) // 3 + 1}", months[0][1],
            max(m[2] for m in months), min(m[3] for m in months), months[-1][4]]


def candles(ctx: RunContext, _: str) -> dict:
    out: dict[str, Any] = {"month": {}, "quarter": {}}
    for s, rows in ctx.out("mfetch")["monthly"].items():
        ms = _closed(rows, ctx.as_of)
        if len(ms) >= 2:
            out["month"][s] = candle(ms[-2], ms[-1])
        if len(ms) >= 6 and ms[-1][0][5:7] in ("03", "06", "09", "12"):
            out["quarter"][s] = candle(_quarter(ms[-6:-3]), _quarter(ms[-3:]))
    return out


# ---------------------------------------------------------------- sectors (S04-R34)

def _research_growth(ctx: RunContext, before: date) -> tuple[str, dict] | None:
    row = ctx.db.one("SELECT r.as_of, s.output FROM run_steps s JOIN runs r ON r.id = s.run_id WHERE r.kind = 'weekly' "
                     "AND r.as_of <= ? AND s.step = 'research' AND s.status = 'done' ORDER BY r.as_of DESC LIMIT 1",
                     (before.isoformat(),), ["output"])
    g = ((row or {}).get("output") or {}).get("sector_growth")
    return (str(row["as_of"]), g) if g else None


def growth_change(prev: dict, now: dict) -> dict[str, dict]:
    from qt.sectors import GroupGrowth, expectation_change
    out = {}
    for e, n in now.items():
        p = prev.get(e)
        if not p:
            continue
        gp, gn = (GroupGrowth(x["sg1"], x["eg1"], x["eg2"], x["n"]) for x in (p, n))
        out[e] = dict(change=expectation_change(gp, gn), score_prev=round(gp.score, 2), score_now=round(gn.score, 2),
                      bucket=n.get("bucket"))
    return out


def sectors(ctx: RunContext, _: str) -> dict:
    now = _research_growth(ctx, ctx.as_of)
    if now is None:
        return {"available": False}
    prev = _research_growth(ctx, date.fromisoformat(now[0]) - timedelta(days=28))
    out = {"available": True, "as_of": now[0], "buckets": {e: v.get("bucket") for e, v in now[1].items()}}
    if prev:
        out.update(prev_as_of=prev[0], changes=growth_change(prev[1], now[1]))
    return out


# ---------------------------------------------------------------- ISM tables (S05-R6, S05-R9)

def ism_tables(raw: dict, as_of: date) -> dict:
    from qt.sectors import ism_industries
    cut = f"{as_of.year:04d}-{as_of.month:02d}"
    hist = [v for k, v in sorted((str(k), v) for k, v in (raw.get("ism_growing") or {}).items()) if k < cut]
    contracting = {str(k): v for k, v in (raw.get("ism_contracting") or {}).items() if str(k) < cut}
    supply = {str(k): v for k, v in (raw.get("ism_short_supply") or {}).items() if str(k) < cut}
    if not hist and not supply:
        return {"available": False}
    from dataclasses import asdict
    last_supply = supply[max(supply)] if supply else {}
    return {"available": True, "growing": [asdict(m) for m in ism_industries(hist)],
            "contracting": contracting[max(contracting)] if contracting else [],
            "short_supply": [dict(name=k, months=v) for k, v in sorted(last_supply.items(), key=lambda x: -x[1])]}


def ism(ctx: RunContext, _: str) -> dict:
    import yaml
    from qt.approvals import Queue
    up = Queue(ctx.db).latest_upload("manual_macro")
    return ism_tables(yaml.safe_load(up["content"]) or {} if up else {}, ctx.as_of)


# ---------------------------------------------------------------- calendar

def _business_day(year: int, month: int, n: int) -> date:
    """The n-th weekday of a month (holidays not counted: the ISM dates are approximate)."""
    d, k = date(year, month, 1), 0
    while True:
        if d.weekday() < 5:
            k += 1
            if k == n:
                return d
        d += timedelta(days=1)


def calendar(ctx: RunContext, _: str) -> dict:
    end = ctx.as_of + timedelta(days=CALENDAR_DAYS)
    ev: list[dict] = [dict(date=d, kind="macro", what="החלטת ריבית (FOMC)") for d in FOMC
                      if ctx.as_of.isoformat() < d <= end.isoformat()]
    fred = ctx.services.get("fred")
    missing = []
    if fred is not None and hasattr(fred, "release_dates"):
        for rid, name in FRED_RELEASES.items():
            try:
                days, _src = fred.release_dates(rid, ctx.as_of + timedelta(days=1), end)
            except Exception as e:                                   # noqa: BLE001 - the rest of the calendar stands
                ctx.event(f"FRED release {rid}: {type(e).__name__}: {e}", "warn", "calendar")
                missing.append(name)
                continue
            ev += [dict(date=d.isoformat(), kind="macro", what=name) for d in days]
    else:
        missing.append("FRED")
    for y, m in {(d.year, d.month) for d in (ctx.as_of, end)}:
        for n, name in ((1, "ISM תעשייה (בערך)"), (3, "ISM שירותים (בערך)")):
            d = _business_day(y, m, n)
            if ctx.as_of < d <= end:
                ev.append(dict(date=d.isoformat(), kind="macro", what=name))
    for e in W.load(ctx.services.get("watchlist")):
        for kind, d in (("דוח", e.next_earnings), ("עצירת זמן", e.time_stop)):
            if d and ctx.as_of.isoformat() < d <= end.isoformat():
                ev.append(dict(date=d, kind="symbol", what=f"{kind} {e.symbol}"))
    for p in _pf(ctx).get("positions", []):
        d = p.get("next_earnings")
        if d and ctx.as_of.isoformat() < str(d) <= end.isoformat():
            ev.append(dict(date=str(d), kind="symbol", what=f"דוח {p['symbol']} (פוזיציה)"))
    seen, uniq = set(), []
    for x in sorted(ev, key=lambda x: (x["date"], x["what"])):
        if (x["date"], x["what"]) not in seen:
            seen.add((x["date"], x["what"]))
            uniq.append(x)
    return {"events": uniq, "missing": missing}


# ---------------------------------------------------------------- seasonality

def seasonality(monthly: list[list], month: int, years: int = SEASON_YEARS) -> dict[str, Any] | None:
    """The calendar month's return in each of the last `years` years: the average and the share of up years."""
    rets = []
    for prev, cur in zip(monthly, monthly[1:]):
        if int(cur[0][5:7]) == month:
            rets.append((cur[4] / prev[4] - 1) * 100)
    rets = rets[-years:]
    if not rets:
        return None
    return dict(avg_pct=round(sum(rets) / len(rets), 2), up_pct=round(100 * sum(r > 0 for r in rets) / len(rets)),
                years=len(rets))


def season(ctx: RunContext, _: str) -> dict:
    out = {}
    for s, rows in ctx.out("mfetch")["monthly"].items():
        r = seasonality(_closed(rows, ctx.as_of), ctx.as_of.month)
        if r:
            out[s] = r
    return {"month": ctx.as_of.month, "by_symbol": out}


# ---------------------------------------------------------------- exposure (D-S11-7)

def exposure_now(pf: dict, last: dict[str, float]) -> dict[str, Any]:
    """Stock positions at the last close (the entry when there is none); options are left out (not from course)."""
    lng = sht = 0.0
    for p in pf.get("positions", []):
        if p.get("instrument", "stock") != "stock":
            continue
        v = p["qty"] * last.get(p["symbol"], p["entry"])
        if p["direction"] == "long":
            lng += v
        else:
            sht += v
    equity = pf.get("cash", 0.0) + lng - sht
    if equity <= 0:
        return {"equity": equity}
    caps = pf.get("caps") or dict(load_policy().exposure.caps)
    out = dict(equity=round(equity, 2), long=round(lng / equity, 3), short=round(sht / equity, 3),
               net=round((lng - sht) / equity, 3), caps=caps, over=[])
    for k in ("long", "short", "net"):
        cap = caps.get(f"{k}_max")
        if cap is not None and out[k] > cap:
            out["over"].append(k)
    return out


def sentiment(ctx: RunContext, _: str) -> dict:
    import yaml
    from qt import sentiment as SE
    from qt.approvals import Queue
    missing, rows = [], []
    cftc = ctx.services.get("cftc")
    if cftc is None:
        missing.append("COT")
    else:
        for y in (ctx.as_of.year - 1, ctx.as_of.year):
            try:
                text, _src = cftc.tff_year(y)
                rows += SE.parse_tff(text)
            except Exception as e:                                   # noqa: BLE001 - the rest of the preparation stands
                ctx.event(f"COT {y}: {type(e).__name__}: {e}", "warn", "sentiment")
        rows = [r for r in rows if r["date"] <= ctx.as_of.isoformat()]
        if not rows:
            missing.append("COT")
    up = Queue(ctx.db).latest_upload("manual_macro")
    naaim = SE.naaim_reading((yaml.safe_load(up["content"]) or {}).get("naaim") if up else None, ctx.as_of)
    if naaim is None:
        missing.append("NAAIM (לא נמצא אוטומטית ולא בקובץ: naaim)")
    return {"cot": SE.cot_reading(rows) if rows else None, "naaim": naaim, "missing": missing}


def exposure(ctx: RunContext, _: str) -> dict:
    return exposure_now(_pf(ctx), ctx.out("mfetch")["last"])


# ---------------------------------------------------------------- the message

SHAPE_HE = {"doji": "דוג'י", "hammer": "פטיש", "inverted_hammer": "פטיש הפוך", "trend_up": "מגמתי עולה",
            "trend_down": "מגמתי יורד", "plain_up": "עולה", "plain_down": "יורד"}
CHANGE_HE = {"improving": "משתפרות", "weakening": "נחלשות", "unchanged": "ללא שינוי"}
EXP_HE = {"long": "לונג", "short": "שורט", "net": "נטו"}


def _candle_line(s: str, c: dict) -> str:
    extra = " · שבר את הנמוך" if c["breaks"] == "low" else " · פרץ את הגבוה" if c["breaks"] == "high" else ""
    rel = {"inside": " · פנימי", "outside": " · עוטף"}.get(c["relation"], "")
    return f"{s} {SHAPE_HE[c['shape']]} ({c['change_pct']:+.1f}%){extra}{rel}"


def summary(ctx: RunContext) -> str:
    """The monthly preparation in the order of S13-R25 (the macro line goes in its own message)."""
    from qt.data.macro_auto import lines as macro_lines
    lines = [f"הכנה חודשית {ctx.as_of.isoformat()}"] + macro_lines((ctx.out("manual") or {}).get("auto"))
    a = ctx.out("a1") or {}
    if a.get("enabled") and not a.get("error"):
        lines.append("A1 על המקרו: " + " · ".join(f"{k} {a[k]['choice']}" for k in ("DC-01", "DC-02", "DC-03", "DC-04")
                                                    if k in a))
    elif a.get("error"):
        lines.append(f"A1 לא השלים: {a['error']}")
    else:
        lines.append("A1 לא רץ (אין חיבור ל-Claude): העמדה היא של הקוד בלבד")
    se = ctx.out("sectors") or {}
    if se.get("changes"):
        moves = [f"{e} {CHANGE_HE[v['change']]}" for e, v in se["changes"].items() if v["change"] != "unchanged"]
        lines.append(f"ציפיות צמיחה מול {se['prev_as_of']} (S04-R34): " + (" · ".join(moves) if moves else "ללא שינוי"))
    elif se.get("available"):
        lines.append("ציפיות צמיחה: אין עדיין מחקר מלפני חודש להשוואה")
    else:
        lines.append("ציפיות צמיחה: אין עדיין מחקר שבועי")
    cd = ctx.out("candles") or {}
    if cd.get("month"):
        lines.append("נרות החודש (S04-R27):")
        lines += [f"• {_candle_line(s, c)}" for s, c in cd["month"].items()]
    if cd.get("quarter"):
        lines.append("נרות הרבעון:")
        lines += [f"• {_candle_line(s, c)}" for s, c in cd["quarter"].items()]
    ism_out = ctx.out("ism") or {}
    if ism_out.get("available"):
        trend = [f"{m['name']} ({m['months_in_row']})" for m in ism_out["growing"] if m["trend"]]
        lines.append(f"ISM: {len(ism_out['growing'])} תעשיות צומחות" + (", ברצף: " + ", ".join(trend) if trend else ""))
        if ism_out["short_supply"]:
            lines.append("Short Supply: " + ", ".join(f"{x['name']} ({x['months']})" for x in ism_out["short_supply"]))
    else:
        lines.append("ISM: טבלת התעשיות לא בקובץ הידני (ism_growing, ism_contracting, ism_short_supply)")
    sn = (ctx.out("season") or {}).get("by_symbol") or {}
    if sn:
        b = sn.get(BENCH)
        best = sorted(((s, r) for s, r in sn.items() if s != BENCH), key=lambda x: -x[1]["avg_pct"])
        lines.append(f"עונתיות החודש ({SEASON_YEARS} שנה, לא מהקורס בשיטה): "
                     + (f"SPY {b['avg_pct']:+.1f}% בממוצע, עולה ב-{b['up_pct']}% מהשנים" if b else "")
                     + (f" · חזקים: {', '.join(s for s, _ in best[:3])} · חלשים: {', '.join(s for s, _ in best[-3:])}"
                        if len(best) >= 6 else ""))
    cal = ctx.out("calendar") or {}
    if cal.get("events"):
        lines.append("יומן אירועים:")
        lines += [f"• {e['date'][5:]} {e['what']}" for e in cal["events"]]
    if cal.get("missing"):
        lines.append("חסר ביומן: " + ", ".join(cal["missing"]))
    ex = ctx.out("exposure") or {}
    if "net" in ex:
        caps = ex["caps"]
        def _cap(k):
            c = caps.get(f"{k}_max")
            return f"{EXP_HE[k]} {ex[k]:.2f}" + (f"/{c:g}" if c is not None else "")
        lines.append("חשיפה: " + " · ".join(_cap(k) for k in ("long", "short", "net"))
                     + f" (נטו {NET_REFERENCE[0]}–{NET_REFERENCE[1]} תקין, 087-R07)")
        if not caps:
            lines.append("⚠️ אין תקרות לונג/שורט/נטו בקובץ התיק (caps: long_max, short_max, net_max). הן שלך לקבוע (D-S11-7)")
        for k in ex["over"]:
            lines.append(f"⚠️ {EXP_HE[k]} מעל התקרה")
    from qt import sentiment as SE
    se = ctx.out("sentiment") or {}
    lines += SE.lines(se.get("cot"), se.get("naaim"), se.get("missing", ["COT", "NAAIM"]))
    return "\n".join(lines)
