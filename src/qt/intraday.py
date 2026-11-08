"""The intraday watch (Ilay 08.11, stage 2 of the cadence proposal: reaction). Not from course: the course has the
trader look at the screens when something happens (the 20% of the time, 084); the AI looks three times a session.

fetch -> filings -> news -> checks -> agents -> queue -> notify, at 10:30, 12:30 and 14:30 New York on trading days.

fetch:    a fresh price for every open stock position and watchlist stock (Yahoo's chart, ~15 minutes delayed) and
          its ADR60 from the daily bars before today.
filings:  the SEC current reports of the day (qt.news.filings, the SEC list read fresh).
news:     the daily run's news pass (qt.news.news): new headlines and filings classified by A5 (DC-14). A link the
          watch already handled earlier today does not raise an event again.
checks:   code only. On a position: price through the stop during the session (an alert: the stop is on the close,
          S12-R2, and the exit stays the standing order of the evening run), within one ADR of the stop, a move against
          it of at least 2 x ADR from the previous close (a shock, an event for A7), the target reached. On a watchlist
          stock: its trigger crossed during the session (information: the entry is decided on the close, as the course
          and the daily run do). Each alert once a day.
agents:   A7 on DC-21 for a position with a strong headline or filing against it, or a shock, at most once a day per
          position. The calc and rules of the last daily run are the background (regime, exposure), with today's price.
queue:    A7's money-moving answers into the approval queue, as in the daily run (paper fills at the next open).
notify:   one Telegram message, only when there is something: a quiet session sends nothing.

Nothing here changes a stop, a size or a rule: the watch sees sooner, it does not trade faster.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from qt.approvals import Queue
from qt.runs import RunContext, Step

SHOCK_ADR = 2.0                       # a move against a position this many ADRs from the previous close (not from course)
NEAR_STOP_ADR = 1.0                   # within one ADR of the stop (not from course)
ADR_DAYS = 120                        # daily bars read for ADR60 (the policy's period, with margin)
KIND_HE = {"stop_breach": "🔴 מעבר לסטופ במהלך היום", "near_stop": "🟠 קרובה לסטופ", "shock": "⚡ תנועה חדה נגד הפוזיציה",
           "target": "🎯 הגיעה ליעד", "trigger": "🔔 חצתה את הטריגר במהלך היום"}


# ---------------------------------------------------------------- the background: the last daily run

def _last_daily(ctx: RunContext) -> dict[str, Any]:
    row = ctx.db.one("SELECT id FROM runs WHERE kind = 'daily' AND status = 'done' AND as_of <= ? "
                     "ORDER BY as_of DESC, started_at DESC LIMIT 1", (ctx.as_of.isoformat(),))
    if row is None:
        return {}
    return {r["step"]: r["output"] for r in ctx.db.all(
        "SELECT step, output FROM run_steps WHERE run_id = ? AND step IN ('calc', 'rules') AND status = 'done'",
        (row["id"],), ["output"])}


def prime(ctx: RunContext) -> bool:
    """The last daily run's calc and rules, in memory, with today's prices: what event_card and the news pass read."""
    if "calc" not in ctx.outputs:
        base = _last_daily(ctx)
        if "calc" not in base or "rules" not in base:
            return False
        live = ctx.out("fetch")["prices"]
        ctx.outputs["calc"] = {"": {**base["calc"], "prices": {**(base["calc"].get("prices") or {}), **live}}}
        ctx.outputs["rules"] = {"": base["rules"]}
    return True


# ---------------------------------------------------------------- steps

def fetch(ctx: RunContext, _: str) -> dict:
    from qt import watch as W
    from qt.calc.volatility import adr
    pf = ctx.services.get("portfolio") or {"positions": []}
    held = [p["symbol"] for p in pf.get("positions", []) if p.get("instrument", "stock") == "stock"]
    syms = list(dict.fromkeys(held + [e.symbol for e in W.load(ctx.services.get("watchlist"))]))
    prov = ctx.services.get("prices")
    out: dict[str, Any] = {"portfolio": pf, "live": {}, "prices": {}, "sources": {}, "stale": [], "missing": []}
    for s in syms:
        try:
            q = (ctx.services.get("live") or prov.live)(s)
            bars, _src = prov.daily_bars(s, ctx.as_of - timedelta(days=ADR_DAYS), ctx.as_of)
            q["adr"] = adr([b for b in bars if b.day < ctx.as_of])
        except Exception as e:                                       # noqa: BLE001 - one stock, never the run
            ctx.event(f"{s}: live {type(e).__name__}: {e}", "warn", "fetch")
            out["missing"].append(s)
            continue
        if not q.get("price"):
            out["missing"].append(s)
            continue
        out["live"][s], out["prices"][s], out["sources"][s] = q, q["price"], q.get("source", "live")
    return out


def filings(ctx: RunContext, _: str) -> dict:
    from qt.news import filings as F
    sec = ctx.services.get("sec")
    if sec is not None and hasattr(sec, "submissions_max_age"):
        sec.submissions_max_age = 15 * 60                            # today's filings, not this morning's list
    return F(ctx, _)


def news(ctx: RunContext, _: str) -> dict:
    from qt.news import news as N
    prime(ctx)                                                       # ret_20d and the regime, when a daily run exists
    return N(ctx, _)


def _sent_today(ctx: RunContext) -> set[tuple[str, str]]:
    rows = ctx.db.all("SELECT s.output FROM run_steps s JOIN runs r ON r.id = s.run_id WHERE r.kind = 'intraday' "
                      "AND r.as_of = ? AND s.step = 'checks' AND s.status = 'done' AND r.id <> ?",
                      (ctx.as_of.isoformat(), ctx.run_id), ["output"])
    return {(a["symbol"], a["kind"]) for r in rows for a in (r["output"] or {}).get("alerts") or []}


def position_alerts(p: dict, q: dict) -> list[dict]:
    """Code's intraday checks on one stock position (see the module notes)."""
    px, prev, a = q["price"], q.get("prev_close"), q.get("adr")
    sg = 1 if p["direction"] == "long" else -1
    out = []
    stop, target = p.get("stop"), p.get("target")
    low = q.get("low") if sg == 1 else q.get("high")
    if stop and sg * ((low if low else px) - stop) <= 0:
        out.append(dict(kind="stop_breach", detail=f"{px:.2f} (שפל היום {low or px:.2f}) מול סטופ {stop:.2f}: הסטופ נבדק "
                                                   "בסגירה; אם תיסגר מעברו, היציאה בפתיחה הבאה היא פקודה עומדת"))
    elif stop and a and abs(px / stop - 1) * 100 < NEAR_STOP_ADR * a:
        out.append(dict(kind="near_stop", detail=f"{px:.2f}, {abs(px / stop - 1) * 100:.1f}% מהסטופ {stop:.2f} "
                                                 f"(פחות מ-ADR אחד, {a:.1f}%)"))
    if prev and a:
        against = -sg * (px / prev - 1) * 100
        if against >= SHOCK_ADR * a:
            out.append(dict(kind="shock", detail=f"{-sg * against:+.1f}% מהסגירה הקודמת, {against / a:.1f} ADR"))
    if target and sg * (px - target) >= 0:
        out.append(dict(kind="target", detail=f"{px:.2f} מול יעד {target:.2f}: מימוש חצי מוצע בסגירה (כמו בריצה היומית)"))
    return out


def watch_alerts(e, q: dict) -> list[dict]:
    if e.trigger_kind == "now" or e.trigger_level is None or not q.get("prev_close"):
        return []
    sg = 1 if e.direction == "long" else -1
    if sg * (q["price"] - e.trigger_level) > 0 and not sg * (q["prev_close"] - e.trigger_level) > 0:
        return [dict(kind="trigger", detail=f"{q['price']:.2f} {'מעל' if sg == 1 else 'מתחת ל'}-{e.trigger_level:.2f}: "
                                            "ההחלטה על כניסה בסגירה, כמו בקורס")]
    return []


def checks(ctx: RunContext, _: str) -> dict:
    from qt import watch as W
    f = ctx.out("fetch")
    live, done = f["live"], _sent_today(ctx)
    alerts = []
    for p in f["portfolio"].get("positions", []):
        q = live.get(p["symbol"])
        if p.get("instrument", "stock") == "stock" and q:
            alerts += [{"symbol": p["symbol"], "held": True, **a} for a in position_alerts(p, q)]
    held = {p["symbol"] for p in f["portfolio"].get("positions", [])}
    for e in W.load(ctx.services.get("watchlist")):
        if e.symbol not in held and live.get(e.symbol):
            alerts += [{"symbol": e.symbol, "held": False, **a} for a in watch_alerts(e, live[e.symbol])]
    new = [a for a in alerts if (a["symbol"], a["kind"]) not in done]
    return {"alerts": new, "repeated": len(alerts) - len(new)}


def _decided_today(ctx: RunContext, sym: str) -> bool:
    return ctx.db.one("SELECT id FROM decisions WHERE card_id = 'DC-21' AND subject = ? AND created_at >= ? LIMIT 1",
                      (sym, ctx.as_of.isoformat())) is not None


def agents(ctx: RunContext, _: str) -> dict:
    runner = ctx.services.get("agents")
    if runner is None:
        return {"enabled": False}
    if not prime(ctx):
        return {"enabled": False, "why": "no daily run yet: A7 has no background"}
    from qt.daily import event_card
    events = dict((ctx.out("news") or {}).get("adverse") or {})
    for a in ctx.out("checks")["alerts"]:
        if a["held"] and a["kind"] == "shock":
            events[a["symbol"]] = (events.get(a["symbol"], "") + " · " if a["symbol"] in events else "") + \
                f"תנועה חדה במהלך היום: {a['detail']}"
    out = {}
    for p in ctx.out("fetch")["portfolio"].get("positions", []):
        sym = p["symbol"]
        if sym not in events or _decided_today(ctx, sym):
            continue
        try:
            out[sym] = event_card(ctx, runner, p, events[sym], "intraday", step="agents")
        except Exception as ex:                                      # noqa: BLE001 - the alert still goes out
            ctx.event(f"{sym}: DC-21 {type(ex).__name__}: {ex}", "warn", "agents")
    return {"enabled": True, "decisions": out}


def queue(ctx: RunContext, _: str) -> dict:
    from qt.daily import AGENT_KIND
    q, added = Queue(ctx.db), []
    prices = ctx.out("fetch")["prices"]
    for sym, d in ((ctx.out("agents") or {}).get("decisions") or {}).items():
        if not d["for_ilay"]:
            continue
        a = dict(kind=AGENT_KIND.get(d["option"], "flag"), rule_id="DC-21", symbol=sym, qty=None, price=None,
                 preapproved=False, detail=f"DC-21 (תוך-יומי): {d['option']}. {d['reasoning']}"[:600], agent=d)
        iid = q.add(ctx.run_id, ctx.as_of, a, prices.get(sym), d["decision_id"])
        if iid:
            added.append(iid)
    return {"added": added}


def message(ctx: RunContext) -> str | None:
    alerts = ctx.out("checks")["alerts"]
    nw = ctx.out("news") or {}
    dec = (ctx.out("agents") or {}).get("decisions") or {}
    strong = [it for it in nw.get("items") or [] if not it.get("known") and it.get("klass") in ("A", "B", "macro_lead")]
    flags = nw.get("watch_flags") or {}
    if not (alerts or dec or strong or flags):
        return None
    now = ctx.services.get("now", lambda: datetime.now(timezone.utc))()
    from zoneinfo import ZoneInfo
    lines = [f"⚡ ניטור תוך-יומי {now.astimezone(ZoneInfo('America/New_York')):%H:%M} ניו יורק · {ctx.as_of.isoformat()}"]
    for a in alerts:
        lines.append(f"{KIND_HE[a['kind']]} · {a['symbol']}{' (בתיק)' if a['held'] else ''}: {a['detail']}")
    for sym, d in dec.items():
        lines.append(f"A7 על {sym}: {d['option']}. {d['reasoning'][:300]}")
    for it in strong:
        side = {"against_idea": "נגד", "for_idea": "בעד"}.get(it.get("direction") or "", "")
        lines.append(f"📰 {it['symbol']} [{it['klass']}{', ' + side if side else ''}] {it['title'][:200]}")
    for sym, t in flags.items():
        if not any(it["symbol"] == sym for it in strong):
            lines.append(f"🚩 {sym} ברשימת המעקב: {t[:200]}")
    if any(a["kind"] == "stop_breach" for a in alerts):
        lines.append("שום פקודה לא נשלחת עכשיו: הסטופ על הסגירה, והיציאה בפתיחה הבאה (בכסף אמיתי הסטופ יושב אצל הברוקר).")
    return "\n".join(lines)


def notify(ctx: RunContext, _: str) -> dict:
    text = message(ctx)
    if text is None:
        return {"sent": False}
    bot = ctx.services.get("telegram")
    if bot is not None:
        bot.send(text)
        from qt.telegram import send_item
        q = Queue(ctx.db)
        for iid in ctx.out("queue")["added"]:
            send_item(bot, q, q.get(iid))
    if ctx.services.get("notifier") is not None:
        ctx.services["notifier"](text)
    return {"sent": True, "text": text}


INTRADAY = (Step("fetch", fetch), Step("filings", filings), Step("news", news), Step("checks", checks),
            Step("agents", agents), Step("queue", queue), Step("notify", notify))
