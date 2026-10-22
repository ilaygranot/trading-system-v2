"""The daily run without agents (runtime plan 10, step 3): data, code numbers and rules only.

fetch -> fill -> calc -> rules -> watch -> news -> agents -> journal -> queue -> book -> learn -> notify. The agents step runs
A7 on positions with an event against them (from the portfolio or from today's news, qt.news) when an agent runner is configured; A5 and A6 join it as their inputs arrive. The portfolio
comes from a YAML file (cash, peak equity, open positions); nothing here sends an order: the queue holds
proposals and the standing orders approved with each trade, and approvals become paper orders (qt.approvals).
"""
from __future__ import annotations

from dataclasses import asdict, fields
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from qt.calc.portfolio import PortfolioStop
from qt.calc.volatility import Bar, atr_20_60_series, regime_transitions, vix_check
from qt.approvals import Queue
from qt.journal import Decision, Journal
from qt.manage import Held, daily_review
from qt.playbook import load_playbook
from qt.policy import load_policy
from qt.runs import RunContext, Step
from qt import book as B
from qt import watch as W

INDEX, VIX = "^GSPC", "^VIX"
HISTORY_DAYS = 400                     # enough for ATR60 and the VIX low window

def load_portfolio(path: str | Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _bars_json(bars: list[Bar]) -> list[list]:
    return [[b.day.isoformat(), b.open, b.high, b.low, b.close] for b in bars]


def _bars(rows: list[list]) -> list[Bar]:
    return [Bar(date.fromisoformat(r[0]), *r[1:5]) for r in rows]


# ---------------------------------------------------------------- steps

def fetch(ctx: RunContext, _: str) -> dict:
    """Rule 2: the only step that reads providers. Primary, then fallback; missing data marks the run stale."""
    pf = ctx.services.get("portfolio")
    if pf is None:
        raise RuntimeError("no portfolio: send portfolio.yaml to the bot (or set QT_PORTFOLIO)")
    symbols = sorted({p["symbol"] for p in pf.get("positions", []) if p.get("instrument", "stock") == "stock"}
                     | {o["symbol"] for o in Queue(ctx.db).open_orders()} | {INDEX, VIX}
                     | {e.symbol for e in W.load(ctx.services.get("watchlist"))})
    start = ctx.as_of - timedelta(days=HISTORY_DAYS)
    out: dict[str, Any] = {"bars": {}, "sources": {}, "stale": []}
    for s in symbols:
        for prov in (ctx.services.get("prices"), ctx.services.get("prices_fallback")):
            if prov is None:
                continue
            try:
                bars, src = prov.daily_bars(s, start, ctx.as_of)
            except Exception as e:                                   # noqa: BLE001 - next provider
                ctx.event(f"{s}: {type(e).__name__}: {e}", "warn", "fetch")
                continue
            bars = [b for b in bars if b.day <= ctx.as_of]
            if bars:
                out["bars"][s], out["sources"][s] = _bars_json(bars), src
                if bars[-1].day < ctx.as_of - timedelta(days=4):
                    out["stale"].append(s)
                break
        else:
            out["stale"].append(s)
    out["portfolio"] = pf
    return out


def fill(ctx: RunContext, _: str) -> dict:
    """Approved paper orders fill at the open of their session (runtime plan 7.2 rule 3), and each fill is
    applied to the portfolio (qt.book) before the day's review."""
    snap = ctx.out("fetch")
    opens = {(s, r[0]): r[1] for s, rows in snap["bars"].items() for r in rows}
    q = Queue(ctx.db)
    pf, log, filled = snap["portfolio"], [], []
    for o in q.fill(opens):
        item = q.get(o["item_id"]) or {}
        payload = dict(item.get("payload") or {}, as_of=ctx.as_of.isoformat(), item_id=item.get("id"),
                       decision_id=item.get("decision_id"))
        pf, what = B.apply_fill(pf, o["kind"], o["symbol"], o["qty"], o["fill_price"], payload)
        log.append(what)
        filled.append({k: o[k] for k in ("id", "symbol", "kind", "qty", "fill_on", "fill_price")})
    return {"filled": filled, "portfolio": pf, "log": log}


def _pf(ctx: RunContext) -> dict:
    """Today's portfolio: the loaded one with this morning's fills applied."""
    f = ctx.out("fill")
    return f["portfolio"] if f and f.get("portfolio") is not None else ctx.out("fetch")["portfolio"]


def calc(ctx: RunContext, _: str) -> dict:
    snap = ctx.out("fetch")
    out: dict[str, Any] = {"prices": {s: rows[-1][4] for s, rows in snap["bars"].items()}}
    if INDEX in snap["bars"]:
        series = atr_20_60_series(_bars(snap["bars"][INDEX]))
        events = regime_transitions(series)
        out["regime"] = events[-1].regime if events else "portfolio"
        out["regime_changed"] = bool(events) and events[-1].day == date.fromisoformat(snap["bars"][INDEX][-1][0])
        out["atr_20_60"] = series[-1][1] if series else None
        idx = snap["bars"][INDEX]
        out["market_ret_20d"] = (idx[-1][4] / idx[-21][4] - 1) * 100 if len(idx) > 21 else None
    else:
        out.update(regime="portfolio", regime_changed=False, atr_20_60=None, market_ret_20d=None)
    if VIX in snap["bars"]:
        v = vix_check([r[4] for r in snap["bars"][VIX]])
        out["vix"] = asdict(v)
    for s, rows in snap["bars"].items():
        if s not in (INDEX, VIX) and len(rows) > 21:
            out.setdefault("ret_20d", {})[s] = (rows[-1][4] / rows[-21][4] - 1) * 100
    return out


HELD_FIELDS = {f.name for f in fields(Held)}


def rules(ctx: RunContext, _: str) -> dict:
    from qt.calc.volatility import VixReading
    snap, c = ctx.out("fetch"), ctx.out("calc")
    pf = _pf(ctx)
    held = []
    for p in pf.get("positions", []):
        d = {k: v for k, v in p.items() if k in HELD_FIELDS}
        if d.get("instrument", "stock") == "stock":
            if p["symbol"] not in c["prices"]:
                continue                                              # stale: reported, not guessed
            d["price"] = c["prices"][p["symbol"]]
            d["stock_ret"] = c.get("ret_20d", {}).get(p["symbol"])
        held.append(Held(**d))
    value = sum((1 if h.direction == "long" or h.instrument == "option" else -1) * h.value for h in held)
    equity = pf["cash"] + value
    ps = PortfolioStop(peak=pf.get("peak_equity", equity), triggered=pf.get("portfolio_stop_triggered", False))
    review = daily_review(held, equity, ps, c["regime"], c["regime_changed"],
                          VixReading(**c["vix"]) if c.get("vix") else None,
                          market_ret=c.get("market_ret_20d"))
    acts = [asdict(a) for a in review.actions]
    caps = pf.get("caps") or dict(load_policy().exposure.caps)
    if caps and equity > 0:                                           # D-S11-7: Ilay's caps, a flag only
        lng = sum(h.value for h in held if h.instrument == "stock" and h.direction == "long") / equity
        sht = sum(h.value for h in held if h.instrument == "stock" and h.direction == "short") / equity
        for k, v in (("long", lng), ("short", sht), ("net", lng - sht)):
            cap = caps.get(f"{k}_max")
            if cap is not None and v > cap:
                acts.append(dict(kind="flag", rule_id="D-S11-7", symbol=None, qty=None, price=None, preapproved=False,
                                 detail=f"{k} exposure {v:.2f} > cap {cap:g}"))
    if snap["stale"]:
        acts.append(dict(kind="block_new_risk", rule_id="DATA-STALE", symbol=None, qty=None, price=None,
                         preapproved=False, detail="stale data: " + ", ".join(snap["stale"]) +
                         ": no new entries; risk and exits keep running"))
    return {"equity": equity, "peak_equity": ps.peak, "portfolio_stop_triggered": ps.triggered,
            "regime": review.regime, "metrics": review.metrics, "actions": acts}


def watch(ctx: RunContext, _: str) -> dict:
    """The watchlist pass (S08, S13-R23): one check per entry, and the opportunities as entry proposals."""
    snap, r = ctx.out("fetch"), ctx.out("rules")
    entries = W.load(ctx.services.get("watchlist"))
    held = {p["symbol"] for p in _pf(ctx).get("positions", [])} | {o["symbol"] for o in Queue(ctx.db).open_orders()}
    checks, actions = [], []
    for e in entries:
        rows = snap["bars"].get(e.symbol)
        if not rows or e.symbol in snap["stale"]:
            checks.append({"symbol": e.symbol, "status": "stale", "notes": ["אין נתונים עדכניים"]})
            continue
        c = W.check(e, _bars(rows), ctx.as_of)
        d = asdict(c)
        d["held"] = e.symbol in held
        checks.append(d)
        if c.status in ("opportunity", "ready") and not d["held"] and not snap["stale"]:
            seen = ctx.db.one("SELECT id FROM approval_queue WHERE symbol=? AND rule_id='S10-R4' AND as_of>=? "
                              "AND status IN ('approved','rejected')", (e.symbol, e.added))
            if not seen:
                actions.append(W.entry_action(e, c, r["equity"]))
    return {"checks": checks, "actions": actions, "n": len(entries)}


def agents(ctx: RunContext, _: str) -> dict:
    """A7 on each open position with an event against it (DC-21). Without an agent runner (no API key yet),
    the step records that agents are off and the code's proposals stand alone."""
    runner = ctx.services.get("agents")
    if runner is None:
        return {"enabled": False}
    from qt.facts import FactSheet
    snap, r = ctx.out("fetch"), ctx.out("rules")
    out = {}
    news_events = (ctx.out("news") or {}).get("adverse") or {}
    for p in _pf(ctx).get("positions", []):
        if not (p.get("adverse_catalyst") or p["symbol"] in news_events):
            continue
        fs = FactSheet()
        price = ctx.out("calc")["prices"].get(p["symbol"])
        if price is not None:
            fs.add(f"{p['symbol']}.close", price, snap["sources"].get(p["symbol"], ""), ctx.as_of)
        world = {"position": p, "regime": r["regime"], "metrics": r["metrics"],
                 "event": p.get("event") or news_events.get(p["symbol"], "")}
        res = runner.decide("A7", "DC-21", fs, world, run_id=ctx.run_id, step="agents", subject=p["symbol"])
        out[p["symbol"]] = {"agent": "A7", "card": "DC-21", "option": res.option, "status": res.status,
                            "decision_id": res.decision_id, "confidence": res.output.get("confidence"),
                            "reasoning": res.output.get("reasoning", ""), "deviations": res.output.get("deviations", []),
                            "critic": res.critic, "for_ilay": res.for_ilay}
    return {"enabled": True, "decisions": out}


def _actions(ctx: RunContext) -> list[dict]:
    return ctx.out("rules")["actions"] + ((ctx.out("watch") or {}).get("actions") or [])


def journal(ctx: RunContext, _: str) -> dict:
    """One journal entry per action, tagged with the run (re-running the step adds nothing twice)."""
    j = Journal(ctx.db)
    pb = load_playbook().version
    ids = []
    for a in _actions(ctx):
        done = ctx.db.one("SELECT id FROM decisions WHERE run_id=? AND card_id=? AND subject=? AND action=?",
                          (ctx.run_id, a["rule_id"], a["symbol"] or "portfolio", a["kind"]))
        if done:
            ids.append(done["id"])
            continue
        ids.append(j.record(Decision(decided_by="code", subject=a["symbol"] or "portfolio", choice=a["kind"],
                                     playbook_version=pb, card_id=a["rule_id"], action=a["kind"],
                                     reasoning=a["detail"], cited_rules=[a["rule_id"]], **idea_prediction(a, ctx.as_of)),
                            run_id=ctx.run_id))
    return {"decision_ids": ids}


AGENT_KIND = {"reduce": "reduce", "exit": "exit", "add": "add"}     # DC-21 options that move money


def queue(ctx: RunContext, _: str) -> dict:
    """Code actions and agent decisions into the approval queue (one item per day, symbol, rule and kind)."""
    q = Queue(ctx.db)
    prices = ctx.out("calc")["prices"]
    added = []
    for a, did in zip(_actions(ctx), ctx.out("journal")["decision_ids"]):
        iid = q.add(ctx.run_id, ctx.as_of, a, prices.get(a["symbol"]), did)
        if iid:
            added.append(iid)
    for sym, d in (ctx.out("agents").get("decisions") or {}).items():
        if not d["for_ilay"]:
            continue
        kind = AGENT_KIND.get(d["option"], "flag")
        a = dict(kind=kind, rule_id=d["card"], symbol=sym, qty=None, price=None, preapproved=False,
                 detail=f"{d['card']}: {d['option']}", agent=d)
        iid = q.add(ctx.run_id, ctx.as_of, a, prices.get(sym), d["decision_id"])
        if iid:
            added.append(iid)
    return {"added": added}


def book(ctx: RunContext, _: str) -> dict:
    """Apply the day's standing stop moves (60/40) to the portfolio, mark the peak, and save it as the newest
    portfolio upload, which the next run loads."""
    pf, log = _pf(ctx), list((ctx.out("fill") or {}).get("log") or [])
    for a in ctx.out("rules")["actions"]:
        if a["kind"] == "tighten_stop" and a.get("preapproved"):
            pf, what = B.apply_stop(pf, a["symbol"], a["price"])
            if what:
                log.append(what)
    B.mark_equity(pf, ctx.out("calc")["prices"])
    import yaml
    Queue(ctx.db).upload("portfolio", f"book-{ctx.as_of.isoformat()}.yaml",
                         yaml.safe_dump(pf, allow_unicode=True, sort_keys=False))
    return {"portfolio": pf, "log": log}


def notify(ctx: RunContext, _: str) -> dict:
    """The one-line daily summary (runtime plan 6), then one message per new item with buttons (7.2)."""
    r = ctx.out("rules")
    q = Queue(ctx.db)
    items = q.for_day(ctx.as_of)
    pending = [i for i in items if i["status"] in ("pending", "postponed", "changed")]
    stale = ctx.out("fetch")["stale"]
    filled = ctx.out("fill")["filled"] if ctx.out("fill") else []
    cost = ctx.db.one("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM agent_calls WHERE run_id = ?", (ctx.run_id,))
    text = (f"יומית {ctx.as_of.isoformat()} · ריצה תקינה · משטר {r['regime']} · {len(pending)} פריטים לאישור · "
            f"{len(items) - len(pending)} פקודות עומדות/דגלים · "
            + ("נתונים עדכניים" if not stale else f"נתונים ישנים: {', '.join(stale)}")
            + (f" · {len(filled)} עסקאות נייר בוצעו" if filled else "")
            + f" · {float(cost['c'] or 0):.2f}$")
    text += "\n\n" + day_summary(ctx)
    sent = 0
    bot = ctx.services.get("telegram")
    if bot is not None:
        from qt.telegram import send_item
        bot.send(text)
        for it in items:
            if it["status"] != "info" and not it.get("tg_message_id"):
                send_item(bot, q, it)
                sent += 1
    sender = ctx.services.get("notifier")
    if sender is not None:
        sender(text)
    return {"text": text, "items_sent": sent}


STATUS_HE = {"opportunity": "🟢 הזדמנות: הטריגר הופעל", "ready": "🟢 מוכנה לכניסה", "waiting": "⏳ ממתינה", "recheck": "🔎 היחס חזר מעל 3: לבדוק רמזור",
             "expired": "⌛ הטריגר פג: חוזרת למעקב", "invalidated": "✗ הטריגר בוטל (סגירה מעבר לרמת הביטול)",
             "stale": "⚠️ אין נתונים"}


def _f(v: float | None, d: int = 2, sign: bool = False) -> str:
    return "—" if v is None else (f"{v:+.{d}f}" if sign else f"{v:.{d}f}")


def day_summary(ctx: RunContext) -> str:
    """The end-of-day summary (S13-R22): market, positions, watchlist, what waits for Ilay."""
    snap, c, r = ctx.out("fetch"), ctx.out("calc"), ctx.out("rules")
    w = ctx.out("watch") or {}
    lines = ["סיכום יום"]
    idx = snap["bars"].get(INDEX)
    if idx and len(idx) > 1:
        lines.append(f"שוק: S&P 500 {idx[-1][4]:,.0f} ({_f((idx[-1][4] / idx[-2][4] - 1) * 100, 1, True)}%)"
                     + (f" · VIX {snap['bars'][VIX][-1][4]:.1f}" if snap["bars"].get(VIX) else "")
                     + f" · משטר {r['regime']}")
    pos = _pf(ctx).get("positions", [])
    if pos:
        lines.append("פוזיציות:")
        for p in pos:
            px = c["prices"].get(p["symbol"])
            if px is None or p.get("instrument", "stock") != "stock":
                continue
            sg = 1 if p["direction"] == "long" else -1
            pl = sg * (px / p["entry"] - 1) * 100
            to_stop = sg * (px / p["stop"] - 1) * 100 if p.get("stop") else None
            lines.append(f"• {p['symbol']} {px:.2f} · {_f(pl, 1, True)}% מהכניסה · {_f(to_stop, 1)}% מעל העצירה")
    else:
        lines.append("פוזיציות: אין")
    if w.get("checks"):
        lines.append("רשימת מעקב:")
        for x in w["checks"]:
            if x["status"] == "stale":
                lines.append(f"• {x['symbol']}: {STATUS_HE['stale']}")
                continue
            head = STATUS_HE.get(x["status"], x["status"]) if not x.get("held") else "בתיק"
            dist = (f" · {_f(x['dist_to_trigger_pct'], 1)}% לטריגר {x['trigger_level']:.2f}"
                    if x.get("dist_to_trigger_pct") is not None and not x["triggered"] else "")
            lines.append(f"• {x['symbol']} {x['close']:.2f} ({_f(x['change_1d_pct'], 1, True)}%) · {head}{dist}"
                         f" · יחס {_f(x['rr'], 1)} · עד היעד {_f(x['potential_pct'], 0)}%"
                         + (f" · {'; '.join(x['notes'])}" if x["notes"] else ""))
    elif ctx.services.get("watchlist") is None:
        lines.append("רשימת מעקב: לא נטענה (שלח watchlist.yaml לבוט)")
    lines += diary_lines(ctx.out("news") or {})
    bk = (ctx.out("book") or {}).get("log") or []
    if bk:
        lines.append("עדכוני תיק: " + " · ".join(bk))
    opp = [a["symbol"] for a in w.get("actions") or []]
    lines.append("לאישורך היום: " + (", ".join(opp) + " (כניסה)" if opp else "אין כניסות חדשות"))
    return "\n".join(lines)


from qt.news import diary_lines, news  # noqa: E402  (qt.news imports _pf from here)
from qt.learning import idea_prediction, learn_daily  # noqa: E402

DAILY = (Step("fetch", fetch), Step("fill", fill), Step("calc", calc), Step("rules", rules), Step("watch", watch),
         Step("news", news), Step("agents", agents), Step("journal", journal),
         Step("queue", queue), Step("book", book), Step("learn", learn_daily), Step("notify", notify))
PIPELINES = {"daily": DAILY}
