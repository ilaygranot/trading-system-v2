"""The morning digest (not from course: Ilay 05.11): one readable page every morning, in Hebrew, with
  1. the run(s) since the last digest: daily, weekly or monthly, whichever ran, and how each went;
  2. the system's health in short: the status dot of the dashboard, the last run of each kind, what runs next;
  3. the stocks the daily run scanned: the book and the watchlist with their status and the headlines that run saw;
  4. the macro that moves the market: S&P 500, VIX and the regime, the monthly stance, the week's macro calendar,
     and the last day's macro headlines (Google News, Bing standing in, as in qt.research.macro_story).

Everything is read from what the runs already saved (runs, run_steps, approval_queue, uploads); the only fetch is
the macro headlines, and a failed fetch is a line in the digest, never a missing digest. No agent writes any of it.
The digest is saved as an upload (kind 'digest', JSON) and goes to Telegram as a short message with a signed link to
the full page (qt.web /digest/<id>). The cron qt-digest fires at both UTC candidates of 07:00 Israel time; the guard
here lets one through and a second one the same day is skipped.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from qt.db import DB

TZ = ZoneInfo("Asia/Jerusalem")          # Ilay's morning (not from course)
AT = time(7, 0)
WINDOW = timedelta(minutes=30)
MAIN = ("daily", "weekly", "monthly")
KIND_HE = {"daily": "יומית", "weekly": "שבועית", "monthly": "חודשית", "premarket": "לפני פתיחה", "open": "בפתיחה"}
LOOKBACK = timedelta(hours=36)           # with no digest before: the runs of the last day and a half
MAX_LOOKBACK = timedelta(days=4)         # a long gap still shows only the last few days
CALENDAR_DAYS = 7
MACRO_QUERIES = ("Federal Reserve", "inflation CPI", "jobs report", "Treasury yields", "oil prices",
                 "stock market today")
MACRO_PER_QUERY, MACRO_MAX = 2, 8
INDEX, VIX = "^GSPC", "^VIX"

STATUS_HE = {"opportunity": "🟢 הזדמנות", "ready": "🟢 מוכנה לכניסה", "waiting": "⏳ ממתינה",
             "recheck": "🔎 לבדוק רמזור", "expired": "⌛ פג", "invalidated": "✗ בוטל", "stale": "⚠️ אין נתונים",
             "frozen": "🧊 קפואה"}
DIR_HE = {"for_idea": "בעד", "against_idea": "נגד"}


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _ts(s: Any) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _local(s: Any, fmt: str = "%d.%m %H:%M") -> str:
    t = _ts(s)
    return t.astimezone(TZ).strftime(fmt) if t else ""


# ---------------------------------------------------------------- reading the runs

def _steps(db: DB, run_id: str) -> dict[str, Any]:
    rows = db.all("SELECT step, subject, status, output, error FROM run_steps WHERE run_id = ? ORDER BY seq, subject",
                  (run_id,), ["output"])
    return {r["step"]: r for r in rows if r["subject"] == ""}


def _out(steps: dict, name: str) -> dict:
    r = steps.get(name)
    return (r or {}).get("output") or {} if r and r["status"] == "done" else {}


def run_summary(db: DB, run: dict) -> dict:
    """How one run went, and what it said: the numbers per kind and its own Telegram summary."""
    steps = _steps(db, run["id"])
    cost = db.one("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM agent_calls WHERE run_id = ?", (run["id"],))
    warns = db.all("SELECT level, step, message FROM run_events WHERE run_id = ? AND level IN ('warn', 'error') "
                   "ORDER BY id", (run["id"],))
    a, b = _ts(run["started_at"]), _ts(run.get("finished_at"))
    out: dict[str, Any] = {
        "id": run["id"], "kind": run["kind"], "as_of": run["as_of"], "status": run["status"],
        "error": run.get("error"), "resumes": run.get("resumes") or 0,
        "started": run["started_at"], "minutes": round((b - a).total_seconds() / 60, 1) if a and b else None,
        "cost_usd": round(float(cost["c"] or 0), 2), "warnings": sum(w["level"] == "warn" for w in warns),
        "errors": sum(w["level"] == "error" for w in warns),
        "top_warnings": [f"{w['step'] or '-'}: {w['message']}"[:200] for w in warns[-5:]],
        "failed_step": next((s for s, r in steps.items() if r["status"] == "failed"), None),
        "text": _out(steps, "notify").get("text") or "",
    }
    if run["kind"] == "daily":
        out.update(daily_numbers(db, run, steps))
    elif run["kind"] == "weekly":
        rs = _out(steps, "research")
        out["ideas"] = [dict(symbol=i["symbol"], bucket=i["bucket"], direction=i["direction"], rr=i.get("rr"),
                             line=i.get("line") or "") for i in rs.get("ideas") or []]
        out["watchlist_n"] = rs.get("watchlist_n")
        out["report_id"] = rs.get("report_id")
    elif run["kind"] == "monthly":
        out["stance"] = stance_of(_out(steps, "stance"))
    return out


def daily_numbers(db: DB, run: dict, steps: dict) -> dict:
    fetch, calc, rules = _out(steps, "fetch"), _out(steps, "calc"), _out(steps, "rules")
    bars = fetch.get("bars") or {}
    idx = bars.get(INDEX) or []
    market = {"regime": rules.get("regime") or calc.get("regime"), "regime_changed": calc.get("regime_changed"),
              "atr_20_60": calc.get("atr_20_60"), "ret_20d": calc.get("market_ret_20d")}
    if len(idx) > 1:
        market.update(spx=idx[-1][4], spx_1d=(idx[-1][4] / idx[-2][4] - 1) * 100)
    if bars.get(VIX):
        market["vix"] = bars[VIX][-1][4]
        market["vix_jump"] = bool((calc.get("vix") or {}).get("jump"))
    items = db.all("SELECT kind, status, symbol, rule_id FROM approval_queue WHERE run_id = ?", (run["id"],))
    cc = fetch.get("crosscheck") or {}
    return {"market": market,
            "pending": sum(i["status"] in ("pending", "postponed", "changed") for i in items),
            "standing": sum(i["status"] == "standing_order" for i in items),
            "flags": sum(i["status"] == "info" for i in items),
            "entries": [i["symbol"] for i in items if i["kind"] in ("enter", "add")],
            "stale": fetch.get("stale") or [], "mismatch": fetch.get("mismatch") or [],
            "crosscheck": f"{sum(v.get('status') == 'ok' for v in cc.values())}/{len(cc)}" if cc else None,
            "filled": len((_out(steps, "fill") or {}).get("filled") or []),
            "book_log": (_out(steps, "book") or {}).get("log") or []}


def stance_of(st: dict) -> dict | None:
    if not st:
        return None
    return {"zone": st.get("zone"), "bias": st.get("bias"), "compass": st.get("compass_now"),
            "activity_pct": (st.get("activity") or {}).get("pct"), "fed": (st.get("fed") or {}).get("period"),
            "inflation": (st.get("inflation") or {}).get("direction") if isinstance(st.get("inflation"), dict) else None,
            "flags": st.get("flags") or []}


def stocks(db: DB, run: dict | None) -> dict:
    """The book and the watchlist as the daily run saw them, each with the headlines that run found new."""
    if run is None:
        return {"run": None, "rows": [], "quiet": []}
    steps = _steps(db, run["id"])
    calc, watch, news = _out(steps, "calc"), _out(steps, "watch"), _out(steps, "news")
    by: dict[str, list[dict]] = {}
    for it in news.get("items") or []:
        by.setdefault(it["symbol"], []).append({k: it.get(k) for k in ("title", "publisher", "published", "link",
                                                                         "klass", "direction", "priced_in", "why")})
    from qt.news import RANK
    for v in by.values():
        v.sort(key=lambda x: (RANK.get(x.get("klass"), 6), "" if x.get("published") is None else x["published"]))
    rows = []
    pf = (_out(steps, "book") or {}).get("portfolio") or (_out(steps, "fetch") or {}).get("portfolio") or {}
    prices = calc.get("prices") or {}
    for p in pf.get("positions") or []:
        px, sg = prices.get(p["symbol"]), 1 if p.get("direction", "long") == "long" else -1
        rows.append(dict(symbol=p["symbol"], held=True, status="held", close=px, direction=p.get("direction"),
                         pl_pct=sg * (px / p["entry"] - 1) * 100 if px and p.get("entry") else None,
                         to_stop_pct=sg * (px / p["stop"] - 1) * 100 if px and p.get("stop") else None,
                         news=by.get(p["symbol"], [])))
    held = {r["symbol"] for r in rows}
    for c in watch.get("checks") or []:
        if c["symbol"] in held:
            continue
        rows.append(dict(symbol=c["symbol"], held=False, status=c.get("status"), close=c.get("close"),
                         change_1d_pct=c.get("change_1d_pct"), dist_to_trigger_pct=None if c.get("triggered")
                         else c.get("dist_to_trigger_pct"), trigger_level=c.get("trigger_level"), rr=c.get("rr"),
                         potential_pct=c.get("potential_pct"), notes=c.get("notes") or [],
                         earnings_in_days=c.get("earnings_in_days"), news=by.get(c["symbol"], [])))
    return {"run": run["id"], "as_of": run["as_of"], "rows": rows, "news_enabled": bool(news.get("enabled")),
            "quiet": [r["symbol"] for r in rows if not r["news"]],
            "watch_flags": news.get("watch_flags") or {}, "adverse": news.get("adverse") or {}}


# ---------------------------------------------------------------- health

def health(db: DB, now: datetime) -> dict:
    """The dashboard's status dot (qt.web.App.health), the last run of each kind and when it runs next."""
    from qt.schedule import next_run
    from qt.web import App
    out: dict[str, Any] = {"runs": []}
    try:
        app = App(db.url)
        app._db = db
        h = app.health()
        out.update(state=h["state"] or "ok", text=h["text"], issues=[t for _, t in h["issues"]])
    except Exception as e:                                           # noqa: BLE001 - the digest still goes out
        out.update(state="warn", text="לא הצלחתי לבדוק", issues=[f"{type(e).__name__}: {e}"[:200]])
    for k, he in KIND_HE.items():
        r = db.one("SELECT id, as_of, status, started_at, error FROM runs WHERE kind = ? ORDER BY started_at DESC LIMIT 1",
                   (k,))
        nxt = next_run(k, now)
        out["runs"].append(dict(kind=k, name=he, last=r and dict(as_of=r["as_of"], status=r["status"],
                                                                  started=r["started_at"]),
                                next=nxt.isoformat() if nxt else None))
    since = (now - timedelta(days=1)).isoformat(timespec="seconds")
    c = db.one("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM agent_calls WHERE created_at >= ?", (since,))
    out["cost_24h"] = round(float(c["c"] or 0), 2)
    out["open_items"] = (db.one("SELECT COUNT(*) AS n FROM approval_queue WHERE status IN "
                                "('pending', 'postponed', 'changed')") or {}).get("n", 0)
    return out


# ---------------------------------------------------------------- macro

def macro_headlines(as_of: date, get: Callable[[str], str] | None = None) -> dict:
    """The last day's macro headlines: Google News (when:1d), Bing standing in for a query Google did not answer."""
    from qt.research.macro_story import BING, GNEWS, parse_rss
    if get is None:
        from qt.data.macro_auto import Http
        get = Http().text
    since = (as_of - timedelta(days=1)).isoformat()
    items, errors = [], []
    for q in MACRO_QUERIES:
        got: list[dict] = []
        for name, url in (("google", GNEWS.format(q=quote_plus(f"{q} when:1d"))), ("bing", BING.format(q=quote_plus(q)))):
            try:
                got = [x for x in parse_rss(get(url), f"{name.title()} News") if x["date"] >= since]
            except Exception as e:                                   # noqa: BLE001 - the other source, then a line
                errors.append(f"{q} ({name}): {type(e).__name__}")
                continue
            if got:
                break
        items += [dict(x, query=q) for x in got[:MACRO_PER_QUERY]]
    seen, out = set(), []
    for x in sorted(items, key=lambda x: x["date"], reverse=True):
        k = x["title"].lower()
        if k not in seen:
            seen.add(k)
            out.append({k2: x.get(k2) for k2 in ("title", "publisher", "date", "link", "query")})
    return {"items": out[:MACRO_MAX], "errors": errors if not out else []}


def macro(db: DB, today: date, daily: dict | None, get: Callable[[str], str] | None) -> dict:
    mrun = db.one("SELECT id, as_of FROM runs WHERE kind = 'monthly' AND status = 'done' ORDER BY started_at DESC LIMIT 1")
    stance, events = None, []
    if mrun:
        steps = _steps(db, mrun["id"])
        stance = stance_of(_out(steps, "stance"))
        if stance:
            stance["as_of"] = mrun["as_of"]
        end = (today + timedelta(days=CALENDAR_DAYS)).isoformat()
        events = [e for e in (_out(steps, "calendar").get("events") or []) if today.isoformat() <= e["date"] <= end]
    try:
        news = macro_headlines(today, get)
    except Exception as e:                                           # noqa: BLE001
        news = {"items": [], "errors": [f"{type(e).__name__}: {e}"[:200]]}
    return {"market": (daily or {}).get("market"), "market_as_of": (daily or {}).get("as_of"),
            "stance": stance, "calendar": events, "headlines": news["items"], "headline_errors": news["errors"]}


# ---------------------------------------------------------------- the digest

def build(db: DB, now: datetime | None = None, get: Callable[[str], str] | None = None) -> dict:
    now = _now(now)
    from qt.approvals import Queue
    from qt.runs import SCHEMA
    db.ddl(SCHEMA)
    Queue(db)                                                        # the queue and uploads tables
    last = db.one("SELECT created_at FROM uploads WHERE kind = 'digest' ORDER BY id DESC LIMIT 1")
    since = _ts(last["created_at"]) if last else None
    since = max(since or now - LOOKBACK, now - MAX_LOOKBACK)
    marks = ",".join("?" * len(MAIN))
    rows = db.all(f"SELECT * FROM runs WHERE kind IN ({marks}) AND started_at >= ? ORDER BY started_at",
                  (*MAIN, since.isoformat(timespec="seconds")))
    fresh = bool(rows)
    if not rows:                                                     # nothing new: the last one, marked as such
        rows = db.all(f"SELECT * FROM runs WHERE kind IN ({marks}) ORDER BY started_at DESC LIMIT 1", MAIN)
    runs = [run_summary(db, r) for r in rows]
    drow = next((r for r in reversed(rows) if r["kind"] == "daily" and r["status"] == "done"), None) or \
        db.one("SELECT * FROM runs WHERE kind = 'daily' AND status = 'done' ORDER BY started_at DESC LIMIT 1")
    daily = next((s for s in runs if drow and s["id"] == drow["id"]), None) or \
        (run_summary(db, drow) if drow else None)
    today = now.astimezone(TZ).date()
    return {"date": today.isoformat(), "generated_at": now.isoformat(timespec="seconds"),
            "since": since.isoformat(timespec="seconds"), "fresh": fresh, "runs": runs,
            "health": health(db, now), "stocks": stocks(db, drow), "macro": macro(db, today, daily, get)}


# ---------------------------------------------------------------- words

def _f(v: Any, d: int = 1, sign: bool = False) -> str:
    if v is None:
        return "—"
    return f"{v:+,.{d}f}" if sign else f"{v:,.{d}f}"


def run_line(r: dict) -> str:
    ok = {"done": "✓", "failed": "✗ נכשלה", "running": "… רצה"}.get(r["status"], r["status"])
    parts = [f"{KIND_HE.get(r['kind'], r['kind'])} {r['as_of'][8:10]}.{r['as_of'][5:7]} {ok}"]
    if r["kind"] == "daily" and r["status"] == "done":
        parts.append(f"{r.get('pending', 0)} לאישורך")
        if r.get("entries"):
            parts.append("כניסה: " + ", ".join(r["entries"]))
        if r.get("stale"):
            parts.append("נתונים ישנים: " + ", ".join(r["stale"]))
    if r["kind"] == "weekly" and r.get("ideas") is not None:
        parts.append(f"{len(r['ideas'])} רעיונות · {r.get('watchlist_n') or 0} ברשימה")
    if r["kind"] == "monthly" and r.get("stance"):
        parts.append(f"אזור {r['stance']['zone']} · מצפן {r['stance']['compass']}")
    if r["status"] == "failed" and r.get("failed_step"):
        parts.append(f"בצעד {r['failed_step']}")
    if r["warnings"]:
        parts.append(f"{r['warnings']} אזהרות")
    parts.append(f"{r['cost_usd']:.2f}$")
    return " · ".join(parts)


def market_line(m: dict | None) -> str:
    if not m:
        return "שוק: אין נתונים מהריצה היומית"
    s = f"S&P 500 {_f(m.get('spx'), 0)} ({_f(m.get('spx_1d'), 1, True)}%)" if m.get("spx") else "S&P 500 —"
    if m.get("vix") is not None:
        s += f" · VIX {m['vix']:.1f}" + (" ⚠️ קפיצה" if m.get("vix_jump") else "")
    s += f" · משטר {'טקטי' if m.get('regime') == 'tactical' else 'תיק'}" + (" (מעבר היום)" if m.get("regime_changed") else "")
    return s


def stance_line(st: dict | None) -> str:
    if not st:
        return "עמדת מקרו: עוד אין ריצה חודשית שהסתיימה"
    return (f"עמדת מקרו ({st.get('as_of', '')}): אזור {st['zone']} · הטיה {st['bias'] or 'אפור'} · מצפן {st['compass']}"
            + (f" · פד {st['fed']}" if st.get("fed") else "") + (f" · דגלים: {', '.join(st['flags'])}" if st["flags"] else ""))


def telegram_lines(d: dict, link: str | None) -> list[str]:
    """The short version: one look tells whether anything needs Ilay; the page holds the rest."""
    h, s, m = d["health"], d["stocks"], d["macro"]
    dot = {"ok": "🟢", "warn": "🟠", "bad": "🔴"}.get(h.get("state"), "⚪")
    lines = [f"☀️ בוקר טוב · digest {d['date'][8:10]}.{d['date'][5:7]}", "", f"{dot} מערכת: {h['text']}"]
    lines += [f"   • {t}" for t in h.get("issues", [])[:3]]
    lines.append("")
    if d["runs"]:
        lines.append("🏃 " + ("מאז ה-digest הקודם:" if d["fresh"] else "אין ריצה חדשה. האחרונה:"))
        lines += [f"   • {run_line(r)}" for r in d["runs"]]
    else:
        lines.append("🏃 עוד לא הייתה ריצה")
    lines += ["", "📈 " + market_line(m["market"]), "🧭 " + stance_line(m["stance"])]
    if m["calendar"]:
        lines.append("📅 השבוע: " + " · ".join(f"{e['what']} {e['date'][8:10]}.{e['date'][5:7]}" for e in m["calendar"][:5]))
    if s["rows"]:
        counts: dict[str, int] = {}
        for r in s["rows"]:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        hot = [r["symbol"] for r in s["rows"] if r["status"] in ("opportunity", "ready", "recheck")]
        lines.append("")
        lines.append(f"🔍 נסרקו {len(s['rows'])} מניות" + (f" · {counts['held']} בתיק" if counts.get("held") else "")
                     + (f" · קרובות לפעולה: {', '.join(hot)}" if hot else "")
                     + f" · {len(s['rows']) - len(s['quiet'])} עם חדשות")
        for sym, what in list(s.get("adverse", {}).items()) + list(s.get("watch_flags", {}).items()):
            lines.append(f"   ⚠️ {sym}: ידיעה נגד ({what})"[:200])
    if m["headlines"]:
        lines += ["", "🌍 מקרו ב-24 השעות האחרונות:"]
        lines += [f"   • {x['title']} ({x['publisher']})" for x in m["headlines"][:4]]
    if link:
        lines += ["", f"📄 ה-digest המלא: {link}"]
    return lines


# ---------------------------------------------------------------- the page

_NUM = re.compile(r"[+\-]?\d[\d,]*(?:\.\d+)?[%$]?")


def _ltr(text: Any) -> str:
    """Escaped, with each number kept left to right (+37.1% read as 37.1%+ inside Hebrew)."""
    from qt.webui import e
    return _NUM.sub(lambda m: f"<span dir=ltr>{m.group(0)}</span>", e(text))

def page(d: dict) -> str:
    from qt import webui as ui
    e = ui.e
    h, s, m = d["health"], d["stocks"], d["macro"]
    body = []
    dot = h.get("state") if h.get("state") in ("ok", "warn", "bad") else ""
    status = {"done": "הושלמה", "failed": "נכשלה", "running": "רצה עכשיו"}
    rl = "".join(f"<li><b>{e(r['name'])}</b>: "
                 + (f"{e(r['last']['as_of'])} {e(status.get(r['last']['status'], r['last']['status']))}" if r["last"]
                    else "עוד לא רצה")
                 + (f" <span class=note>· הבאה {e(_local(r['next']))}</span>" if r["next"] else "") + "</li>"
                 for r in h["runs"])
    health_html = ui.panel("סטטוס מערכת",
                           f"<p><span class='dot {dot}'></span> <b>{e(h['text'])}</b> · {h['open_items']} פריטים פתוחים "
                           f"בתור · עלות Agents ב-24 שעות {h['cost_24h']:.2f}$</p>"
                           + ("<ul>" + "".join(f"<li>{_ltr(t)}</li>" for t in h.get("issues", [])) + "</ul>"
                              if h.get("issues") else "")
                           + f"<ul class=note>{rl}</ul><p class=note>השעות בשעון ישראל</p>", id="health")
    runs = []
    for r in d["runs"]:
        extra = ""
        if r["top_warnings"]:
            extra += ui.fold(f"אזהרות ושגיאות ({r['warnings'] + r['errors']})",
                             "<ul>" + "".join(f"<li>{e(w)}</li>" for w in r["top_warnings"]) + "</ul>")
        if r.get("ideas"):
            extra += ui.table(("מניה", "סוג", "כיוון", "יחס", "בקצרה"),
                              ([ui.sym(i["symbol"]), e(i["bucket"]), e(i["direction"]), e(_f(i.get("rr"))), e(i["line"])]
                               for i in r["ideas"]))
        if r.get("error"):
            extra += f"<p class=note>שגיאה: {e(r['error'])}</p>"
        if r.get("text"):
            extra += ui.fold("ההודעה שהריצה שלחה", f"<pre style='white-space:pre-wrap;direction:rtl;text-align:right;"
                                                   f"font-family:var(--body)'>{e(r['text'])}</pre>")
        runs.append(f"<div class=card><h3>{_ltr(run_line(r))}</h3><p class=note>התחילה {e(_local(r['started']))}"
                    + (f" · {r['minutes']} דק'" if r.get("minutes") is not None else "")
                    + (f" · הומשכה {r['resumes']} פעמים" if r["resumes"] else "") + f"</p>{extra}</div>")
    title = "הריצה מאז ה-digest הקודם" if d["fresh"] else "אין ריצה חדשה מאז ה-digest הקודם (האחרונה)"
    body.append(ui.panel(title, "".join(runs) or ui.empty("עוד לא הייתה ריצה"), id="runs"))
    body.append(health_html)
    mk = [f"<p><b>{e(market_line(m['market']))}</b>" + (f" <span class=note>(סגירת {e(m['market_as_of'])})</span>"
                                                         if m.get("market_as_of") else "") + "</p>",
          f"<p>{e(stance_line(m['stance']))}</p>"]
    if m["calendar"]:
        mk.append(ui.table(("תאריך", "אירוע"), ([e(x["date"]), e(x["what"])] for x in m["calendar"])))
    if m["headlines"]:
        mk.append("<ul>" + "".join(f"<li><a href='{e(x['link'])}' target=_blank rel=noopener>{e(x['title'])}</a> "
                                   f"<span class=note>{e(x['publisher'])} · {e(x['date'])}</span></li>"
                                   for x in m["headlines"]) + "</ul>")
    elif m["headline_errors"]:
        mk.append(f"<p class=note>כותרות המקרו לא נטענו: {e('; '.join(m['headline_errors'][:3]))}</p>")
    else:
        mk.append("<p class=note>אין כותרות מקרו מה-24 שעות האחרונות</p>")
    macro_html = ui.panel("מקרו ושוק", "".join(mk), id="macro")
    cards = []
    for r in s["rows"]:
        if r["held"]:
            head = (f"בתיק · {_f(r['close'], 2)} · {_f(r.get('pl_pct'), 1, True)}% מהכניסה · "
                    f"{_f(r.get('to_stop_pct'), 1)}% מהעצירה")
        else:
            head = (f"{STATUS_HE.get(r['status'], r['status'])} · {_f(r['close'], 2)} ({_f(r.get('change_1d_pct'), 1, True)}%)"
                    + (f" · {_f(r['dist_to_trigger_pct'], 1)}% לטריגר {_f(r['trigger_level'], 2)}"
                       if r.get("dist_to_trigger_pct") is not None else "")
                    + f" · יחס {_f(r.get('rr'))} · עד היעד {_f(r.get('potential_pct'), 0)}%")
        news = "".join(f"<li>{'<b>[' + e(n['klass']) + ', ' + e(DIR_HE.get(n.get('direction'), '?')) + ']</b> ' if n.get('klass') else ''}"
                       f"<a href='{e(n['link'])}' target=_blank rel=noopener>{e(n['title'])}</a> "
                       f"<span class=note>{e(n['publisher'])} · {e((n.get('published') or '')[5:])}</span>"
                       + (f"<br><span class=note>{e(n['why'])}</span>" if n.get("why") else "") + "</li>"
                       for n in r["news"])
        notes = f"<p class=note>{e('; '.join(r['notes']))}</p>" if r.get("notes") else ""
        cards.append(f"<div class=card><h3>{ui.sym(r['symbol'])} <span class=note>{_ltr(head)}</span></h3>{notes}"
                     + (f"<ul>{news}</ul>" if news else "") + "</div>")
    st = (f"<p class=note>מהריצה היומית של {e(s.get('as_of'))}. החדשות הן כותרות של 3 הימים האחרונים שהריצה ראתה לראשונה"
          + ("" if s.get("news_enabled") else " (החדשות כבויות בריצה הזו)") + ".</p>" if s["rows"] else "")
    if s["quiet"]:
        st += f"<p class=note>בלי כותרות חדשות: {e(', '.join(s['quiet']))}</p>"
    body.append(ui.panel(f"מניות שנסרקו ({len(s['rows'])})", st + ("".join(cards) or ui.empty("אין ריצה יומית")),
                         id="stocks"))
    body.append(macro_html)
    return ui.page(f"Digest בוקר {d['date'][8:10]}.{d['date'][5:7]}", "".join(body),
                   sub=f"נוצר {e(_local(d['generated_at']))} שעון ישראל · ריצות מאז {e(_local(d['since']))}",
                   rail=(("runs", "ריצה"), ("health", "מערכת"), ("stocks", "מניות"), ("macro", "מקרו")),
                   health=(dot, h["text"]))


# ---------------------------------------------------------------- the link, the schedule, the send

def sign(upload_id: int, secret: str) -> str:
    return hmac.new(secret.encode(), f"digest:{upload_id}".encode(), hashlib.sha256).hexdigest()[:24]


def verify(upload_id: int, sig: str, secret: str) -> bool:
    return bool(secret) and hmac.compare_digest(sign(upload_id, secret), sig or "")


def link(upload_id: int) -> str | None:
    base = os.environ.get("QT_PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    secret = os.environ.get("DASHBOARD_PASSWORD")
    if not base or not secret:
        return None
    if not base.startswith("http"):
        base = "https://" + base
    return f"{base.rstrip('/')}/digest/{upload_id}?sig={sign(upload_id, secret)}"


def due(now: datetime | None = None) -> tuple[bool, date]:
    t = _now(now).astimezone(TZ)
    start = datetime.combine(t.date(), AT, TZ)
    return start <= t < start + WINDOW, t.date()


def send(db: DB, bot: Any = None, now: datetime | None = None, get: Callable[[str], str] | None = None) -> dict:
    """Build, save, and send. Returns {"id", "text"}; without a bot only saved."""
    from qt.approvals import Queue
    d = build(db, now, get)
    uid = Queue(db).upload("digest", f"digest-{d['date']}.json", json.dumps(d, ensure_ascii=False, default=str))
    url = link(uid)
    from qt.news import chunks
    parts = chunks(telegram_lines(d, url))
    if bot is not None:
        for i, p in enumerate(parts):
            last = i == len(parts) - 1
            bot.send(p, [[{"text": "📄 ה-digest המלא", "url": url}]] if last and url else None)
    return {"id": uid, "text": "\n\n".join(parts), "link": url}


def scheduled(db: DB, bot: Any = None, now: datetime | None = None, get: Callable[[str], str] | None = None) -> str:
    """The cron entry point: once a morning, at 07:00 Israel time."""
    ok, today = due(now)
    if not ok:
        return "not due: digest"
    from qt.approvals import Queue
    Queue(db)
    if db.one("SELECT id FROM uploads WHERE kind = 'digest' AND name = ?", (f"digest-{today.isoformat()}.json",)):
        return f"skip: digest {today} already sent"
    r = send(db, bot, now, get)
    return f"digest {today} #{r['id']}"
