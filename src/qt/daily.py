"""The daily run without agents (runtime plan 10, step 3): data, code numbers and rules only.

fetch -> fill -> calc -> rules -> watch -> news -> agents -> journal -> queue -> book -> learn -> notify. The agents step runs
A7 on positions with an event against them (from the portfolio or from today's news, qt.news; DC-21), on the regime's grey band
(DC-22) and on technical profit-taking flags (DC-23) when an agent runner is configured, and A5's
heat cards on the watchlist (DC-16 on an against move, DC-15 on a heat trigger; proposals only, approved by Ilay). The portfolio
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
MAX_HEAT = 8                           # DC-15/DC-16 calls a day (not from course: cost)
MAX_TECH = 4                           # DC-23 calls a day (not from course: cost)
TECH_REPEAT_DAYS = 5                   # DC-23 asks again about the same position after 5 days (not from course)
HEAT_RULES = ("S08-R26", "DC-16")      # approval-queue rule ids of a heat proposal

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
        out["atr_20_60_recent"] = [round(v, 4) for _, v in series[-10:]]   # DC-22: today and the two weeks before
        idx = snap["bars"][INDEX]
        out["market_ret_20d"] = (idx[-1][4] / idx[-21][4] - 1) * 100 if len(idx) > 21 else None
    else:
        out.update(regime="portfolio", regime_changed=False, atr_20_60=None, market_ret_20d=None)
    if VIX in snap["bars"]:
        vc = [r[4] for r in snap["bars"][VIX]]
        out["vix"] = asdict(vix_check(vc))
        out["vix_close"] = vc[-1]
        out["vix_jump_yesterday"] = len(vc) > 2 and vix_check(vc[:-1]).jump
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
        c = W.check(e, _bars(rows), ctx.as_of, approved_heat(ctx, e.symbol, e.added),
                    reviewed=day61_reviewed(ctx, e.symbol, e.added))
        d = asdict(c)
        d["held"] = e.symbol in held
        checks.append(d)
        if c.status in ("opportunity", "ready") and c.heat == "hot" and not d["held"] and not snap["stale"]:
            seen = ctx.db.one("SELECT id FROM approval_queue WHERE symbol=? AND rule_id='S10-R4' AND as_of>=? "
                              "AND status IN ('approved','rejected')", (e.symbol, e.added))
            if not seen:
                actions.append(W.entry_action(e, c, r["equity"]))
    return {"checks": checks, "actions": actions, "n": len(entries)}


def approved_heat(ctx: RunContext, symbol: str, since: str) -> str | None:
    """The latest heat Ilay approved for a watchlist entry since it was added (S08-R26, D-S08-3); None = the research's."""
    Queue(ctx.db)                                                    # the table exists
    row = ctx.db.one("SELECT payload FROM approval_queue WHERE symbol=? AND rule_id IN (?, ?) AND status='approved' "
                     "AND as_of>=? ORDER BY id DESC LIMIT 1", (symbol, *HEAT_RULES, since), ["payload"])
    return (row["payload"] or {}).get("heat") if row else None


REVIEW_EVENT = "{sym}: DC-15 day-61 review (S08-R29)"           # run_events marker: the review ran once since added


def day61_reviewed(ctx: RunContext, symbol: str, since: str) -> bool:
    """S08-R29 / D-S08-1: the review after 60 days runs once, even when the daily run missed day 61 itself."""
    row = ctx.db.one("SELECT e.id FROM run_events e JOIN runs r ON r.id = e.run_id WHERE e.message=? AND r.as_of>=? "
                     "AND e.run_id<>? LIMIT 1", (REVIEW_EVENT.format(sym=symbol), since, ctx.run_id))
    return row is not None


DC16_HEAT = {"frozen_exit": "frozen"}                                # noise_continue keeps the heat


def heat_cards(ctx: RunContext, runner) -> dict:
    """A5 on the watchlist (S08-R25, S08-R26, DC-15, DC-16): DC-16 when code saw an against move and the stock is not
    held; otherwise DC-15 on a code reason or today's A/B news for the idea not priced in. A changed heat is a proposal
    in the approval queue; the heat in force changes only once Ilay approves it (watch.check reads it back)."""
    from qt.facts import FactSheet
    w, nw = ctx.out("watch") or {}, ctx.out("news") or {}
    entries = {e.symbol: e for e in W.load(ctx.services.get("watchlist"))}
    news_for = {}
    for it in nw.get("items") or []:
        if it.get("klass") in ("A", "B") and it.get("direction") == "for_idea" and it.get("priced_in") is False:
            news_for.setdefault(it["symbol"], []).append(f"{it['klass']}: {it['title']}")
    q, out, n = Queue(ctx.db), {}, 0
    waiting = {i["symbol"] for i in q.open_items() if i["kind"] == "heat"}   # one open proposal per symbol is enough
    for c in w.get("checks") or []:
        e = entries.get(c["symbol"])
        if e is None or c.get("status") == "stale" or e.symbol in waiting:
            continue
        frozen = (c.get("heat") or e.heat) == "frozen"                # DC-16 can only say frozen again: no call
        if c.get("dc16") and not c.get("held") and not frozen:
            card = "DC-16"
        elif c.get("dc15") or news_for.get(e.symbol):
            card = "DC-15"
        else:
            continue
        if n >= MAX_HEAT:
            ctx.event(f"{e.symbol}: {card} skipped, {MAX_HEAT} heat calls today", "warn", "agents")
            continue
        n += 1
        fs = FactSheet()
        for k in ("close", "since_add_pct", "potential_pct", "rr", "days_on_list", "trend_3m_pct"):
            if c.get(k) is not None:
                fs.add(f"{e.symbol}.{k}", c[k], "code:watch", ctx.as_of)
        world = {"symbol": e.symbol, "direction": e.direction, "current_state": c.get("heat") or e.heat,
                 "since": e.added, "catalysts": e.catalysts, "news": news_for.get(e.symbol, []),
                 "days_on_list": c.get("days_on_list"), "potential": c.get("potential_pct"),
                 "reasons": (["against_move"] if card == "DC-16" else c.get("dc15") or ["news"]), "write_in": "Hebrew"}
        try:
            r = runner.decide("A5", card, fs, world, run_id=ctx.run_id, step="agents", subject=e.symbol)
        except Exception as ex:                                      # noqa: BLE001 - budget or API: no change
            ctx.event(f"{e.symbol}: {card} {type(ex).__name__}: {ex}", "warn", "agents")
            continue
        if card == "DC-15" and "day_61" in world["reasons"]:
            ctx.event(REVIEW_EVENT.format(sym=e.symbol), "info", "agents")
        new = DC16_HEAT.get(r.option) if card == "DC-16" else (r.option if r.option in W.HEATS else None)
        rec = {"card": card, "option": r.option, "status": r.status, "from": world["current_state"], "to": new,
               "reasoning": r.output.get("reasoning", ""), "item_id": None}
        if new and new != world["current_state"]:
            rule = "DC-16" if card == "DC-16" else "S08-R26"
            item = {"kind": "heat", "rule_id": rule, "symbol": e.symbol, "qty": None, "price": None,
                    "preapproved": False, "heat": new,
                    "detail": f"{card}: {world['current_state']} → {new}. {rec['reasoning']}"[:600]}
            # no ref_price: a heat has no price, the premarket recheck must not undo an approval
            rec["item_id"] = q.add(ctx.run_id, ctx.as_of, item, None, getattr(r, "decision_id", None))
        out[e.symbol] = rec
    return out


def _portfolio_facts(ctx: RunContext, fs) -> None:
    """The market and the book as facts (DC-21, DC-22, DC-23): regime, ATR 20-60, VIX, exposure, drawdown."""
    c, r = ctx.out("calc"), ctx.out("rules")
    m = r["metrics"]
    rows = [("portfolio.regime", r["regime"], "code:volatility"), ("market.atr_20_60_pct", c.get("atr_20_60"), "code:volatility"),
            ("market.atr_20_60_last_10", c.get("atr_20_60_recent"), "code:volatility"),
            ("market.sp500_ret_20d_pct", c.get("market_ret_20d"), "code:calc"), ("market.vix", c.get("vix_close"), "code:calc"),
            ("market.vix_vs_recent_low", (c.get("vix") or {}).get("ratio_to_low"), "code:volatility"),
            ("market.vix_above_hist_mean_19_5", (c.get("vix") or {}).get("above_mean"), "code:volatility"),
            ("portfolio.equity", r["equity"], "code:rules"), ("portfolio.gross", m.get("gross"), "code:manage"),
            ("portfolio.net", m.get("net"), "code:manage"), ("portfolio.beta", m.get("beta"), "code:manage"),
            ("portfolio.drawdown_pct", m.get("drawdown_pct"), "code:manage"),
            ("portfolio.to_portfolio_stop_pct", m.get("to_portfolio_stop_pct"), "code:manage")]
    for key, value, src in rows:
        if value is not None:
            fs.add(key, round(value, 4) if isinstance(value, float) else value, src, ctx.as_of)


def _position_facts(ctx: RunContext, p: dict, fs) -> float | None:
    """One open position as facts: structure, P&L, distance to the stop and the target, weight, open R, and the
    thesis breakers written at entry when the portfolio file has them. Returns today's price."""
    c, r = ctx.out("calc"), ctx.out("rules")
    sym, stock = p["symbol"], p.get("instrument", "stock") == "stock"
    price = c["prices"].get(sym) if stock else p.get("price")
    src = (ctx.out("fetch")["sources"].get(sym) or "provider") if stock else "portfolio"
    sg = 1 if p["direction"] == "long" or not stock else -1
    rows = [("instrument", p.get("instrument", "stock")), ("direction", p["direction"]), ("qty", p.get("qty")),
            ("entry", p.get("entry")), ("stop", p.get("stop")), ("entry_stop", p.get("entry_stop")),
            ("target", p.get("target")), ("expiry", p.get("expiry")), ("strikes", p.get("strikes")),
            ("defined_risk", p.get("defined_risk", None if stock else True)), ("thesis", p.get("thesis")),
            ("thesis_breakers", p.get("thesis_breakers"))]
    if price is not None:
        fs.add(f"{sym}.{'close' if stock else 'value'}", price, src, ctx.as_of)
        e = p.get("entry")
        if e:
            rows.append(("pnl_pct", round(sg * (price / e - 1) * 100, 2)))
        if stock and p.get("stop"):
            rows.append(("to_stop_pct", round(abs(price / p["stop"] - 1) * 100, 2)))
        if stock and p.get("target"):
            rows.append(("to_target_pct", round(sg * (p["target"] / price - 1) * 100, 2)))
        if stock and e and p.get("entry_stop") and e != p["entry_stop"]:
            rows.append(("open_r", round(sg * (price - e) / abs(e - p["entry_stop"]), 2)))
    w = (r["metrics"].get("weights") or {}).get(sym)
    rows += [("weight_pct", round(w, 2) if w is not None else None), ("ret_20d_pct", (c.get("ret_20d") or {}).get(sym)),
             ("beta", p.get("beta")), ("speculative", p.get("speculative")), ("below_mas", p.get("below_mas"))]
    for k, v in rows:
        if v not in (None, "", []):
            fs.add(f"{sym}.{k}", round(v, 4) if isinstance(v, float) else v, "portfolio" if k not in
                   ("pnl_pct", "to_stop_pct", "to_target_pct", "open_r", "weight_pct", "ret_20d_pct") else "code:daily",
                   ctx.as_of)
    return price


def regime_card(ctx: RunContext, runner) -> dict | None:
    """A7 on the regime's grey band (DC-22, S12-R24/R25, D-S12-2): (b) the code switched to tactical today: which
    cutting method and order; (a) alert without a switch, on the day it starts (ATR 20-60 crossed above 0, or a VIX
    jump that was not there yesterday): switch now or stay. Asked once per start, not every alert day (not from
    course: cost). Positions only: without one there is nothing to cut."""
    from qt.facts import FactSheet
    c, r = ctx.out("calc"), ctx.out("rules")
    pos = _pf(ctx).get("positions", [])
    if not pos:
        return None
    recent = c.get("atr_20_60_recent") or []
    if r["regime"] == "tactical" and c.get("regime_changed"):
        state = "b"
    elif r["regime"] == "portfolio" and ((len(recent) > 1 and recent[-2] <= 0 < recent[-1])
                                         or ((c.get("vix") or {}).get("jump") and not c.get("vix_jump_yesterday"))):
        state = "a"
    else:
        return None
    fs = FactSheet()
    _portfolio_facts(ctx, fs)
    for p in pos:
        _position_facts(ctx, p, fs)
    code_cuts = [a for a in r["actions"] if a["rule_id"] == "S12-R26"]
    world = {"state": state, "regime": r["regime"], "regime_changed": c.get("regime_changed"),
             "code_cut_order": [a["symbol"] for a in code_cuts if a["symbol"]], "positions": [p["symbol"] for p in pos],
             "write_in": "Hebrew"}
    res = runner.decide("A7", "DC-22", fs, world, run_id=ctx.run_id, step="agents", subject="portfolio")
    o = res.output
    method, order = o.get("cut_method"), o.get("cut_order") or []
    if state == "b" or res.option == "switch":
        plan = f"DC-22 ({'מעבר שזוהה' if state == 'b' else 'כוננות'}): {res.option}; שיטת חיתוך {method or '-'}; סדר: {', '.join(order) or '-'}"
        kind = "block_new_risk" if state == "a" else "flag"           # (b): the code's S12-R26 cuts are already queued
    else:
        plan, kind = None, None
    return {"agent": "A7", "card": "DC-22", "state": state, "option": res.option, "status": res.status,
            "cut_method": method, "cut_order": order, "decision_id": res.decision_id,
            "reasoning": o.get("reasoning", ""), "for_ilay": res.for_ilay,
            "item": dict(kind=kind, rule_id="DC-22", symbol=None, qty=None, price=None, preapproved=False,
                         detail=f"{plan}. {o.get('reasoning', '')}"[:600]) if kind else None}


DC23_KIND = {"half": "sell", "all": "exit"}


def technical_cards(ctx: RunContext, runner) -> dict:
    """A7 on technical profit-taking flags (DC-23): an open stock position in profit whose stop is not hit, with at
    least one flag from qt.timing.profit_flags. Not on options (60/40 is code, S12-R11). half / all go to Ilay as a
    sell or an exit; none is journalled and the flags stay open for the next close."""
    from qt.facts import FactSheet
    from qt.timing import profit_flags
    snap, c, r = ctx.out("fetch"), ctx.out("calc"), ctx.out("rules")
    exits = {a["symbol"] for a in r["actions"] if a["kind"] == "exit"}
    since = (ctx.as_of - timedelta(days=TECH_REPEAT_DAYS)).isoformat()
    out, n = {}, 0
    for p in _pf(ctx).get("positions", []):
        sym = p["symbol"]
        price, rows = c["prices"].get(sym), snap["bars"].get(sym)
        if p.get("instrument", "stock") != "stock" or price is None or not rows or sym in exits or sym in snap["stale"]:
            continue
        sg = 1 if p["direction"] == "long" else -1
        if sg * (price - p["entry"]) <= 0:
            continue
        flags = profit_flags(_bars(rows), p["direction"])
        if not flags:
            continue
        if ctx.db.one("SELECT id FROM decisions WHERE card_id='DC-23' AND subject=? AND created_at>=? LIMIT 1",
                      (sym, since)):
            continue
        if n >= MAX_TECH:
            ctx.event(f"{sym}: DC-23 skipped, {MAX_TECH} calls today", "warn", "agents")
            continue
        n += 1
        fs = FactSheet()
        _portfolio_facts(ctx, fs)
        _position_facts(ctx, p, fs)
        for k, v in flags.items():
            fs.add(f"{sym}.flag.{k}", v, "code:timing.profit_flags", ctx.as_of)
        world = {"symbol": sym, "direction": p["direction"], "flags": sorted(flags), "regime": r["regime"],
                 "partial_taken": p.get("partial_taken", False), "write_in": "Hebrew"}
        try:
            res = runner.decide("A7", "DC-23", fs, world, run_id=ctx.run_id, step="agents", subject=sym)
        except Exception as ex:                                      # noqa: BLE001 - budget or API: no proposal
            ctx.event(f"{sym}: DC-23 {type(ex).__name__}: {ex}", "warn", "agents")
            continue
        kind = DC23_KIND.get(res.option)
        qty = None if kind is None else (float(int(abs(p["qty"]) // 2)) if kind == "sell" else p["qty"])
        out[sym] = {"agent": "A7", "card": "DC-23", "option": res.option, "status": res.status, "flags": flags,
                    "decision_id": res.decision_id, "reasoning": res.output.get("reasoning", ""), "for_ilay": res.for_ilay,
                    "item": dict(kind=kind, rule_id="DC-23", symbol=sym, qty=qty, price=price, preapproved=False,
                                 detail=f"DC-23: {res.option} ({', '.join(sorted(flags))}). "
                                        f"{res.output.get('reasoning', '')}"[:600]) if kind and qty else None}
    return out


def agents(ctx: RunContext, _: str) -> dict:
    """A7 on each open position with an event against it (DC-21), on the regime's grey band (DC-22) and on technical
    profit-taking flags (DC-23), and A5's heat cards on the watchlist (DC-15, DC-16). Without an agent runner (no API
    key yet), the step records that agents are off and the code's proposals stand alone."""
    runner = ctx.services.get("agents")
    if runner is None:
        return {"enabled": False}
    from qt.facts import FactSheet
    r = ctx.out("rules")
    out = {}
    news_events = (ctx.out("news") or {}).get("adverse") or {}
    for p in _pf(ctx).get("positions", []):
        if not (p.get("adverse_catalyst") or p["symbol"] in news_events):
            continue
        fs = FactSheet()
        _position_facts(ctx, p, fs)
        _portfolio_facts(ctx, fs)
        event = p.get("event") or news_events.get(p["symbol"], "")
        fs.add(f"{p['symbol']}.event", event, "news" if p["symbol"] in news_events else "portfolio", ctx.as_of)
        world = {"position": p, "regime": r["regime"], "metrics": r["metrics"], "event": event}
        res = runner.decide("A7", "DC-21", fs, world, run_id=ctx.run_id, step="agents", subject=p["symbol"])
        out[p["symbol"]] = {"agent": "A7", "card": "DC-21", "option": res.option, "status": res.status,
                            "decision_id": res.decision_id, "confidence": res.output.get("confidence"),
                            "reasoning": res.output.get("reasoning", ""), "deviations": res.output.get("deviations", []),
                            "critic": res.critic, "for_ilay": res.for_ilay}
    try:
        regime = regime_card(ctx, runner)
    except Exception as ex:                                          # noqa: BLE001 - budget or API: the code's cuts stand
        ctx.event(f"DC-22 {type(ex).__name__}: {ex}", "warn", "agents")
        regime = None
    return {"enabled": True, "decisions": out, "heat": heat_cards(ctx, runner), "regime": regime,
            "technical": technical_cards(ctx, runner)}


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
    ag = ctx.out("agents") or {}
    for d in [ag.get("regime")] + list((ag.get("technical") or {}).values()):   # DC-22, DC-23: every action to Ilay
        if d and d.get("item"):
            iid = q.add(ctx.run_id, ctx.as_of, {**d["item"], "agent": {k: v for k, v in d.items() if k != "item"}},
                        prices.get(d["item"]["symbol"]), d["decision_id"])
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
    digest = chunks(digest_lines(ctx.out("news") or {}))
    sent = 0
    bot = ctx.services.get("telegram")
    if bot is not None:
        from qt.telegram import send_item
        bot.send(text)
        for part in digest:
            bot.send(part, html=True)
        for it in items:
            if it["status"] != "info" and not it.get("tg_message_id"):
                send_item(bot, q, it)
                sent += 1
    sender = ctx.services.get("notifier")
    if sender is not None:
        sender(text)
        for part in digest:
            sender(part)
    return {"text": text, "digest": digest, "items_sent": sent}


STATUS_HE = {"opportunity": "🟢 הזדמנות: הטריגר הופעל", "ready": "🟢 מוכנה לכניסה", "waiting": "⏳ ממתינה", "recheck": "🔎 היחס חזר מעל 3: לבדוק רמזור",
             "expired": "⌛ הטריגר פג: חוזרת למעקב", "invalidated": "✗ הטריגר בוטל (סגירה מעבר לרמת הביטול)",
             "stale": "⚠️ אין נתונים", "frozen": "🧊 קפואה: אין כניסה (S08-R27)"}


def _trend(x: dict) -> str:
    """The 078 trend columns (S08-R4): price change over a year, 3 months and since the idea was added."""
    parts = [(lbl, x.get(k)) for lbl, k in (("שנה", "trend_1y_pct"), ("3ח׳", "trend_3m_pct"),
                                            ("מאז ההוספה", "since_add_pct"))]
    shown = [f"{lbl} {_f(v, 0, True)}%" for lbl, v in parts if v is not None]
    return " · מגמה: " + ", ".join(shown) if shown else ""


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
                         + _trend(x)
                         + (f" · {'; '.join(x['notes'])}" if x["notes"] else ""))
    elif ctx.services.get("watchlist") is None:
        lines.append("רשימת מעקב: לא נטענה (שלח watchlist.yaml לבוט)")
    lines += diary_lines(ctx.out("news") or {})
    for sym, h in ((ctx.out("agents") or {}).get("heat") or {}).items():
        if h.get("item_id"):
            lines.append(f"🌡️ {sym}: {h['card']} מציע חום {h['from']} → {h['to']} (ממתין לאישורך, S08-R26)")
    rg = (ctx.out("agents") or {}).get("regime")
    if rg:
        lines.append(f"🧭 משטר (DC-22, {'מעבר שזוהה' if rg['state'] == 'b' else 'כוננות'}): {rg['option']}"
                     + (f" · סדר חיתוך {', '.join(rg['cut_order'])}" if rg.get("cut_order") else "")
                     + (" (ממתין לאישורך)" if rg.get("item") else ""))
    for sym, t in ((ctx.out("agents") or {}).get("technical") or {}).items():
        lines.append(f"📈 {sym}: דגלי מימוש {', '.join(sorted(t['flags']))} · DC-23: {t['option']}"
                     + (" (ממתין לאישורך)" if t.get("item") else ""))
    bk = (ctx.out("book") or {}).get("log") or []
    if bk:
        lines.append("עדכוני תיק: " + " · ".join(bk))
    opp = [a["symbol"] for a in w.get("actions") or []]
    lines.append("לאישורך היום: " + (", ".join(opp) + " (כניסה)" if opp else "אין כניסות חדשות"))
    return "\n".join(lines)


from qt.news import chunks, diary_lines, digest_lines, news  # noqa: E402  (qt.news imports _pf from here)
from qt.learning import idea_prediction, learn_daily  # noqa: E402

DAILY = (Step("fetch", fetch), Step("fill", fill), Step("calc", calc), Step("rules", rules), Step("watch", watch),
         Step("news", news), Step("agents", agents), Step("journal", journal),
         Step("queue", queue), Step("book", book), Step("learn", learn_daily), Step("notify", notify))
PIPELINES = {"daily": DAILY}
