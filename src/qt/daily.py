"""The daily run without agents (runtime plan 10, step 3): data, code numbers and rules only.

fetch -> fill -> calc -> rules -> agents -> journal -> queue -> notify. The agents step runs A7 on positions with an
event against them when an agent runner is configured; A5 and A6 join it as their inputs arrive. The portfolio
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
from qt.runs import RunContext, Step

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
                     | {o["symbol"] for o in Queue(ctx.db).open_orders()} | {INDEX, VIX})
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
    """Approved paper orders fill at the open of their session (runtime plan 7.2 rule 3)."""
    bars = ctx.out("fetch")["bars"]
    opens = {(s, r[0]): r[1] for s, rows in bars.items() for r in rows}
    return {"filled": [{k: o[k] for k in ("id", "symbol", "kind", "qty", "fill_on", "fill_price")}
                       for o in Queue(ctx.db).fill(opens)]}


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
    pf = snap["portfolio"]
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
    if snap["stale"]:
        acts.append(dict(kind="block_new_risk", rule_id="DATA-STALE", symbol=None, qty=None, price=None,
                         preapproved=False, detail="stale data: " + ", ".join(snap["stale"]) +
                         ": no new entries; risk and exits keep running"))
    return {"equity": equity, "peak_equity": ps.peak, "portfolio_stop_triggered": ps.triggered,
            "regime": review.regime, "metrics": review.metrics, "actions": acts}


def agents(ctx: RunContext, _: str) -> dict:
    """A7 on each open position with an event against it (DC-21). Without an agent runner (no API key yet),
    the step records that agents are off and the code's proposals stand alone."""
    runner = ctx.services.get("agents")
    if runner is None:
        return {"enabled": False}
    from qt.facts import FactSheet
    snap, r = ctx.out("fetch"), ctx.out("rules")
    out = {}
    for p in snap["portfolio"].get("positions", []):
        if not p.get("adverse_catalyst"):
            continue
        fs = FactSheet()
        price = ctx.out("calc")["prices"].get(p["symbol"])
        if price is not None:
            fs.add(f"{p['symbol']}.close", price, snap["sources"].get(p["symbol"], ""), ctx.as_of)
        world = {"position": p, "regime": r["regime"], "metrics": r["metrics"], "event": p.get("event", "")}
        res = runner.decide("A7", "DC-21", fs, world, run_id=ctx.run_id, step="agents", subject=p["symbol"])
        out[p["symbol"]] = {"agent": "A7", "card": "DC-21", "option": res.option, "status": res.status,
                            "decision_id": res.decision_id, "confidence": res.output.get("confidence"),
                            "reasoning": res.output.get("reasoning", ""), "deviations": res.output.get("deviations", []),
                            "critic": res.critic, "for_ilay": res.for_ilay}
    return {"enabled": True, "decisions": out}


def journal(ctx: RunContext, _: str) -> dict:
    """One journal entry per action, tagged with the run (re-running the step adds nothing twice)."""
    j = Journal(ctx.db)
    pb = load_playbook().version
    ids = []
    for a in ctx.out("rules")["actions"]:
        done = ctx.db.one("SELECT id FROM decisions WHERE run_id=? AND card_id=? AND subject=? AND action=?",
                          (ctx.run_id, a["rule_id"], a["symbol"] or "portfolio", a["kind"]))
        if done:
            ids.append(done["id"])
            continue
        ids.append(j.record(Decision(decided_by="code", subject=a["symbol"] or "portfolio", choice=a["kind"],
                                     playbook_version=pb, card_id=a["rule_id"], action=a["kind"],
                                     reasoning=a["detail"], cited_rules=[a["rule_id"]]), run_id=ctx.run_id))
    return {"decision_ids": ids}


AGENT_KIND = {"reduce": "reduce", "exit": "exit", "add": "add"}     # DC-21 options that move money


def queue(ctx: RunContext, _: str) -> dict:
    """Code actions and agent decisions into the approval queue (one item per day, symbol, rule and kind)."""
    q = Queue(ctx.db)
    prices = ctx.out("calc")["prices"]
    added = []
    for a, did in zip(ctx.out("rules")["actions"], ctx.out("journal")["decision_ids"]):
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


DAILY = (Step("fetch", fetch), Step("fill", fill), Step("calc", calc), Step("rules", rules), Step("agents", agents), Step("journal", journal),
         Step("queue", queue), Step("notify", notify))
PIPELINES = {"daily": DAILY}
