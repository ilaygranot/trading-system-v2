"""The daily run without agents (runtime plan 10, step 3): data, code numbers and rules only.

fetch -> calc -> rules -> journal -> queue -> notify. Agents (A5, A6, A7) and the Critic slot in between
rules and queue later. The portfolio comes from a YAML file (cash, peak equity, open positions); nothing
here sends an order: the queue holds proposals and the standing orders approved with each trade.
"""
from __future__ import annotations

from dataclasses import asdict, fields
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from qt.calc.portfolio import PortfolioStop
from qt.calc.volatility import Bar, atr_20_60_series, regime_transitions, vix_check
from qt.db import dumps
from qt.journal import Decision, Journal
from qt.manage import Held, daily_review
from qt.playbook import load_playbook
from qt.runs import RunContext, Step

INDEX, VIX = "^GSPC", "^VIX"
HISTORY_DAYS = 400                     # enough for ATR60 and the VIX low window

QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_queue (
  id {ID},
  run_id TEXT NOT NULL,
  as_of TEXT NOT NULL,
  item_key TEXT NOT NULL UNIQUE,         -- as_of + symbol + rule + kind: one item per day per action
  kind TEXT NOT NULL,                    -- exit | tighten_stop | sell | reduce | block_new_risk | flag
  rule_id TEXT NOT NULL,
  symbol TEXT,
  status TEXT NOT NULL,                  -- pending | standing_order | info | approved | rejected | changed
  payload {JSON} NOT NULL,
  created_at {TS} NOT NULL,
  decided_at {TS},
  decided_note TEXT
);
"""


def load_portfolio(path: str | Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _bars_json(bars: list[Bar]) -> list[list]:
    return [[b.day.isoformat(), b.open, b.high, b.low, b.close] for b in bars]


def _bars(rows: list[list]) -> list[Bar]:
    return [Bar(date.fromisoformat(r[0]), *r[1:5]) for r in rows]


# ---------------------------------------------------------------- steps

def fetch(ctx: RunContext, _: str) -> dict:
    """Rule 2: the only step that reads providers. Primary, then fallback; missing data marks the run stale."""
    pf = ctx.services["portfolio"]
    symbols = sorted({p["symbol"] for p in pf.get("positions", []) if p.get("instrument", "stock") == "stock"}
                     | {INDEX, VIX})
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


def queue(ctx: RunContext, _: str) -> dict:
    ctx.db.ddl(QUEUE_SCHEMA)
    added = 0
    for a in ctx.out("rules")["actions"]:
        status = "standing_order" if a["preapproved"] else "info" if a["kind"] == "flag" else "pending"
        key = f"{ctx.as_of.isoformat()}|{a['symbol'] or '-'}|{a['rule_id']}|{a['kind']}"
        added += ctx.db.insert_new("approval_queue", dict(
            run_id=ctx.run_id, as_of=ctx.as_of.isoformat(), item_key=key, kind=a["kind"], rule_id=a["rule_id"],
            symbol=a["symbol"], status=status, payload=dumps(a), created_at=ctx.as_of.isoformat()), ["item_key"])
    return {"added": added}


def notify(ctx: RunContext, _: str) -> dict:
    r = ctx.out("rules")
    pending = sum(1 for a in r["actions"] if not a["preapproved"] and a["kind"] != "flag")
    stale = ctx.out("fetch")["stale"]
    text = (f"daily {ctx.as_of.isoformat()} · regime {r['regime']} · {pending} for approval · "
            f"{len(r['actions']) - pending} standing/flags · " + ("data fresh" if not stale else f"stale: {', '.join(stale)}"))
    sender = ctx.services.get("notifier")
    if sender is not None:
        sender(text)
    return {"text": text}


DAILY = (Step("fetch", fetch), Step("calc", calc), Step("rules", rules), Step("journal", journal),
         Step("queue", queue), Step("notify", notify))
PIPELINES = {"daily": DAILY}
