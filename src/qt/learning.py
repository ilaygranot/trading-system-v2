"""The learning loop (architecture 7): outcomes measured by code, the post-trade review, and the weekly and
monthly learning reports. Learning without leaving the course and without reacting to noise.

The course gives the frame: write down what worked, what didn't, what we waited for and it ran away, what was
missed and why, and at which stage a failed trade failed (S13-R29, S08-R32); judge the idea apart from its
execution (S08-J23); a loss is not a mistake (S13-J4); never change the method on a short streak (S13-J6);
targets: a win rate above 50%, 60-65% with an average around 1:3 (S13-R18); measure risk-adjusted, not only
the P&L (S13-J17).

daily  (step learn, after book): the day's equity; every trade the book closed today gets its journal row,
       its measurements and the code's draft review; agent L classifies it on DC-24 (when agents are on) and the
       review goes to Ilay as a queue item (approve = confirmed; reject with a reason = his correction).
weekly (step learn): every entry proposal measured (target or stop first, within 60 days, S08-R29) together with
       what Ilay answered: approved, rejected or not reviewed; ideas that ran away without us (S08-R32, S13-J8);
       stopped trades whose target came after the exit; re-entries after stops (S13-R6, D-S13-1); reviews
       still waiting for Ilay.
monthly (step learn): the metrics of 7.3: win rate and R against the course, Sharpe / Sortino / drawdown,
       Ilay's agreement per card, deviations' hit rate per rule, calibration by confidence, the Critic's returns,
       failure stages, streak warnings, and candidates for a Playbook change (7.4: at least 10 cases; L drafts,
       Ilay decides; nothing changes by itself).
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

from qt import tradelog as T
from qt.approvals import Queue
from qt.db import DB, dumps
from qt.journal import Decision, Journal
from qt.runs import RunContext

SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_reviews (
  id {ID},
  trade_key TEXT NOT NULL,               -- symbol|opened (qt.tradelog.trade_key)
  created_at {TS} NOT NULL,
  reviewer TEXT NOT NULL,                -- code | agent:L | ilay | code:after_exit
  trade {JSON},                          -- the book's closed-trade record
  review {JSON} NOT NULL,                -- measurements and the review fields
  decision_id BIGINT,                    -- the journal entry Ilay is asked about
  item_id BIGINT                         -- the approval-queue item carrying it
);
CREATE TABLE IF NOT EXISTS equity_daily (
  day TEXT PRIMARY KEY,
  equity {REAL} NOT NULL,
  peak {REAL}
);
"""

IDEA_HORIZON_DAYS = 60           # S08-R29: day 60 on the list is the review point; an idea is measured up to it
IDEA_RULE = "S10-R4"             # the entry proposal (qt.watch.entry_action)
REVIEW_RULE = "S13-R29"
MIN_CASES = 10                   # architecture 7.4: cases before a rule change is proposed (not from course)
WIN_RATE_MIN, WIN_RATE_AIM = 50.0, (60.0, 65.0)      # S13-R18
RR_AIM = 3.0                                         # S13-R18: average around 1:3
LOSS_STREAK, WIN_STREAK = 3, 4   # not from course: when the streak warnings (S13-J6, S13-J5) show
REENTRY_WINDOW_DAYS, MAX_REENTRIES = 30, 2           # D-S13-1 as recommended: a third re-entry after stops is flagged
FAILURE_HE = {"no_failure": "אין כשל", "idea": "רעיון", "execution": "ביצוע", "playbook": "Playbook",
              "data": "נתונים", "unclear": "לא ברור"}


def ensure(db: DB) -> None:
    db.ddl(SCHEMA)


def idea_prediction(action: dict, as_of: date) -> dict[str, Any]:
    """The measurable prediction of an entry proposal (architecture 7.1): the target before the stop."""
    if action.get("kind") != "enter" or action.get("target") is None or action.get("stop") is None:
        return {}
    return {"prediction": f"{action['symbol']} reaches the target {action['target']:g} before the stop "
                          f"{action['stop']:g} within {IDEA_HORIZON_DAYS} days",
            "check_on": as_of + timedelta(days=IDEA_HORIZON_DAYS)}


# ---------------------------------------------------------------- bars

def _bars(ctx: RunContext, symbol: str, start: date) -> list[list]:
    """Bars from this run's snapshot when it has them, else from the providers (primary, then fallback)."""
    for step in ("fetch", "wfetch", "mfetch"):
        rows = ((ctx.out(step) or {}).get("bars") or {}).get(symbol)
        if rows and rows[0][0] <= start.isoformat():
            return rows
    for prov in (ctx.services.get("prices"), ctx.services.get("prices_fallback")):
        if prov is None:
            continue
        try:
            bars, _src = prov.daily_bars(symbol, start - timedelta(days=5), ctx.as_of)
        except Exception as e:                                       # noqa: BLE001 - next provider
            ctx.event(f"{symbol}: {type(e).__name__}: {e}", "warn", "learn")
            continue
        if bars:
            return [[b.day.isoformat(), b.open, b.high, b.low, b.close] for b in bars if b.day <= ctx.as_of]
    return []


# ---------------------------------------------------------------- reviews

def reviews(db: DB) -> dict[str, dict]:
    """The latest review per trade, merged: measurements from code, the classification from L when it ran,
    Ilay's verdict from the queue and his own lesson last (his word wins)."""
    ensure(db)
    rows = db.all("SELECT r.*, q.status AS item_status, q.decided_note FROM trade_reviews r "
                  "LEFT JOIN approval_queue q ON q.id = r.item_id ORDER BY r.id", (), ["trade", "review"])
    out: dict[str, dict] = {}
    for r in rows:
        cur = out.setdefault(r["trade_key"], {"trade": r["trade"], "by": ""})
        if r["trade"]:
            cur["trade"] = r["trade"]
        rv = {k: v for k, v in (r["review"] or {}).items() if v not in (None, "") or k not in cur}
        cur.update(rv)
        if r["reviewer"] in ("code", "agent:L"):
            cur["by"] = r["reviewer"]
        if r["item_id"]:
            cur["item_id"] = r["item_id"]
            st = r["item_status"]
            cur["status"] = {"approved": "אושר", "rejected": f"תוקן: {r['decided_note'] or ''}".strip(),
                             "expired": "לא נבדק"}.get(st, "ממתין")
    return out


def _l_review(ctx: RunContext, c: dict, measured: dict, draft: dict) -> dict | None:
    """Agent L on DC-24. Without an agent runner the code's draft stands alone."""
    runner = ctx.services.get("agents")
    if runner is None:
        return None
    from qt.facts import FactSheet
    fs = FactSheet()
    src = f"book:{T.trade_key(c)}"
    for k in ("entry", "exit", "entry_stop", "target", "pnl", "trade_pnl", "max_qty", "opened", "closed", "rule"):
        if c.get(k) is not None:
            fs.add(f"{c['symbol']}.trade.{k}", c[k], src, ctx.as_of)
    for k, v in measured.items():
        if v is not None and not isinstance(v, (list, dict)):
            fs.add(f"{c['symbol']}.measured.{k}", v, "code:qt.tradelog", ctx.as_of)
    world = {"trade": c, "plan": c.get("plan") or {}, "measured": measured, "code_draft": draft,
             "regime": (ctx.out("rules") or {}).get("regime")}
    try:
        res = runner.decide("L", "DC-24", fs, world, run_id=ctx.run_id, step="learn", subject=c["symbol"])
    except Exception as e:                                           # noqa: BLE001 - budget or API: the draft stands
        ctx.event(f"L on {c['symbol']}: {type(e).__name__}: {e}", "warn", "learn")
        return None
    o = res.output
    return {"decision_id": res.decision_id, "status": res.status, "failure_class": o.get("option"),
            **{k: o[k] for k in ("stage", "idea_quality", "execution_quality", "loss_or_mistake", "worked",
                                 "not_worked", "lesson", "proposal", "reasoning", "confidence") if k in o}}


def _review_text(c: dict, measured: dict, rv: dict) -> str:
    r = T.r_multiple(c)
    lines = [f"תחקיר {c['symbol']} · {c.get('opened') or '?'} → {c.get('closed') or '?'} · "
             f"{T.EXIT_HE.get(c.get('rule'), c.get('rule') or 'ידני')} · "
             + (f"{r:+.2f}R" if r is not None else f"{c.get('trade_pnl', c['pnl']):+,.0f}$"),
             f"רעיון {T.QUALITY_HE[rv['idea_quality']]} · ביצוע {T.QUALITY_HE[rv['execution_quality']]}"
             + (f" · {T.LOSS_HE[rv['loss_or_mistake']]}" if rv.get("loss_or_mistake") not in (None, "none") else "")]
    if measured.get("mfe_r") is not None:
        lines.append(f"MFE {measured['mfe_r']:+.2f}R · MAE {measured['mae_r']:+.2f}R · "
                     f"הגיע ליעד: {T._yn(measured.get('target_hit'))}")
    if rv.get("off_plan"):
        lines.append("חריגות: " + "; ".join(rv["off_plan"]))
    if rv.get("failure_class"):
        stage = rv.get("stage")
        lines.append(f"L: כשל {FAILURE_HE.get(rv['failure_class'], rv['failure_class'])}"
                     + (f" · שלב {stage} {T.STAGES_HE.get(str(stage), '')}" if stage not in (None, "none") else ""))
    for k, he in (("worked", "עבד"), ("not_worked", "לא עבד"), ("lesson", "לקח"), ("proposal", "הצעת שינוי")):
        if rv.get(k):
            lines.append(f"{he}: {rv[k]}")
    lines.append("אשר אם התחקיר נכון; דחה עם סיבה כדי לתקן (S13-R29)")
    return "\n".join(lines)


def review_trade(ctx: RunContext, c: dict, rows: list[list]) -> dict:
    """Measure, draft, ask L, and send the review to Ilay. Safe to run again (one review per trade)."""
    db = ctx.db
    ensure(db)
    key = T.trade_key(c)
    if db.one("SELECT id FROM trade_reviews WHERE trade_key = ? AND reviewer IN ('code', 'agent:L')", (key,)):
        return {"trade": key, "skipped": "already reviewed"}
    measured = {**T.excursions(c, rows), **T.after_exit(c, rows)}
    off = T.plan_check(c)
    draft = T.code_review(c, measured, measured, off)
    measured["off_plan"] = off
    rv = dict(draft)
    l = _l_review(ctx, c, measured, draft)
    did = None
    if l:
        did = l.pop("decision_id")
        l.pop("status")
        rv.update({k: v for k, v in l.items() if v not in (None, "")})
    else:
        from qt.playbook import load_playbook
        did = Journal(db).record(Decision(
            decided_by="code", subject=c["symbol"], choice=f"idea:{draft['idea_quality']}/execution:"
            f"{draft['execution_quality']}", playbook_version=load_playbook().version, card_id=REVIEW_RULE,
            action="review", reasoning="; ".join(off) or "followed the plan", cited_rules=[REVIEW_RULE, "S08-J23"]),
            run_id=ctx.run_id)
    text = _review_text(c, measured, rv)
    iid = Queue(db).add(ctx.run_id, ctx.as_of, dict(kind="review", rule_id=REVIEW_RULE, symbol=c["symbol"], qty=None,
                                                    price=None, preapproved=False, detail=text, trade_key=key),
                        c.get("exit"), did)
    db.insert("trade_reviews", dict(trade_key=key, created_at=ctx.as_of.isoformat(), reviewer="agent:L" if l else "code",
                                    trade=dumps(c), review=dumps({**measured, **rv}), decision_id=did, item_id=iid))
    return {"trade": key, "item": iid, "by": "agent:L" if l else "code", "r": T.r_multiple(c),
            "idea": rv["idea_quality"], "execution": rv["execution_quality"]}


def ilay_lesson(db: DB, item_id: int, text: str) -> str | None:
    """Ilay's own lesson for a reviewed trade (the course: write it down yourself, S13-R29)."""
    ensure(db)
    row = db.one("SELECT trade_key FROM trade_reviews WHERE item_id = ? ORDER BY id LIMIT 1", (item_id,))
    if row is None:
        return None
    db.insert("trade_reviews", dict(trade_key=row["trade_key"], created_at=date.today().isoformat(), reviewer="ilay",
                                    review=dumps({"lesson": text[:1000]}), item_id=item_id))
    return row["trade_key"]


# ---------------------------------------------------------------- daily

def learn_daily(ctx: RunContext, _: str) -> dict:
    db = ctx.db
    ensure(db)
    r = ctx.out("rules") or {}
    if r.get("equity") is not None:
        db.upsert("equity_daily", dict(day=ctx.as_of.isoformat(), equity=round(r["equity"], 2),
                                       peak=r.get("peak_equity")), ["day"])
    pf = (ctx.out("book") or {}).get("portfolio") or {}
    today = [c for c in pf.get("closed", []) if c.get("closed") == ctx.as_of.isoformat()]
    snap = (ctx.out("fetch") or {}).get("bars") or {}
    done = []
    for c in today:
        rows = snap.get(c["symbol"]) or _bars(ctx, c["symbol"], date.fromisoformat(c.get("opened") or ctx.as_of.isoformat()))
        done.append(review_trade(ctx, c, rows))
    return {"reviews": done}


# ---------------------------------------------------------------- weekly

def first_touch(rows: list[list], direction: str, stop: float, target: float, after: str,
                horizon_end: str) -> tuple[str | None, str | None]:
    """Which came first after `after`: the target or the stop (same day both: the stop, conservative)."""
    sg = 1 if direction == "long" else -1
    for d, _o, h, low, _c in (r for r in rows if after < r[0] <= horizon_end):
        hit_stop = (low <= stop) if sg == 1 else (h >= stop)
        hit_target = (h >= target) if sg == 1 else (low <= target)
        if hit_stop:
            return "stop", d
        if hit_target:
            return "target", d
    return None, None


def measure_ideas(ctx: RunContext) -> list[dict]:
    """Every entry proposal without an outcome: target or stop first, or neither by day 60."""
    db, j = ctx.db, Journal(ctx.db)
    items = db.all("SELECT q.id, q.as_of, q.symbol, q.status, q.payload, q.decision_id FROM approval_queue q "
                   "WHERE q.kind = 'enter' AND q.rule_id = ? AND q.decision_id IS NOT NULL AND NOT EXISTS "
                   "(SELECT 1 FROM outcomes o WHERE o.decision_id = q.decision_id) ORDER BY q.id", (IDEA_RULE,),
                   ["payload"])
    out = []
    for it in items:
        p = it["payload"]
        if p.get("stop") is None or p.get("target") is None:
            continue
        start = date.fromisoformat(it["as_of"])
        end = start + timedelta(days=IDEA_HORIZON_DAYS)
        rows = _bars(ctx, it["symbol"], start)
        first, day = first_touch(rows, p.get("direction", "long"), p["stop"], p["target"], it["as_of"],
                                 min(end, ctx.as_of).isoformat())
        if first is None and ctx.as_of < end:
            continue                                             # still open: measured again next week
        res = {"first": first or "neither", "on": day, "ilay": it["status"], "item": it["id"],
               "symbol": it["symbol"], "proposed": it["as_of"]}
        j.record_outcome(it["decision_id"], res, None if first is None else first == "target")
        out.append(res)
    return out


def escaped(entries: list, bars: dict[str, list[list]], held: set[str]) -> list[str]:
    """S08-R32: ideas on the list that reached their target while we were not in."""
    out = []
    for e in entries:
        rows = bars.get(e.symbol)
        if e.symbol in held or not rows or e.target is None:
            continue
        sg = 1 if e.direction == "long" else -1
        since = [r for r in rows if r[0] >= e.added]
        if since and any(sg * ((r[2] if sg == 1 else r[3]) - e.target) >= 0 for r in since):
            out.append(e.symbol)
    return out


def reentries(pf: dict, as_of: date) -> list[dict]:
    """S13-R6 with D-S13-1 (b): more than 2 re-entries into the same stock after stops within 30 days."""
    since = (as_of - timedelta(days=REENTRY_WINDOW_DAYS)).isoformat()
    by: dict[str, list[dict]] = {}
    for c in pf.get("closed", []):
        if (c.get("opened") or "") >= since:
            by.setdefault(c["symbol"], []).append(c)
    flags = []
    for s, cs in by.items():
        stops = sum(c.get("rule") == "S12-R2" for c in cs)
        entries = len(cs) + any(p["symbol"] == s for p in pf.get("positions", []))
        if stops >= 2 and entries - 1 > MAX_REENTRIES:
            flags.append({"symbol": s, "entries": entries, "stops": stops})
    return flags


def learn_weekly(ctx: RunContext, _: str) -> dict:
    from qt import watch as W
    db = ctx.db
    ensure(db)
    pf = ctx.services.get("portfolio") or {}
    ideas = measure_ideas(ctx)
    # stopped trades: did the target come after the exit (architecture 7.2: stopped by noise before the thesis)
    late = []
    for key, rv in reviews(db).items():
        c = rv.get("trade")
        if not c or (rv.get("complete") and rv.get("target_after_exit") is not None):
            continue                                             # measured in full at the close
        if db.one("SELECT id FROM trade_reviews WHERE trade_key = ? AND reviewer = 'code:after_exit'", (key,)):
            continue
        ax = T.after_exit(c, _bars(ctx, c["symbol"], date.fromisoformat(c.get("closed") or ctx.as_of.isoformat())))
        if ax["complete"]:
            ax["stopped_by_noise"] = c.get("rule") == "S12-R2" and bool(ax["target_after_exit"])
            db.insert("trade_reviews", dict(trade_key=key, created_at=ctx.as_of.isoformat(),
                                            reviewer="code:after_exit",
                                            review=dumps(ax)))
            late.append({"trade": key, **ax})
    entries = W.load(ctx.services.get("watchlist"))
    held = {p["symbol"] for p in pf.get("positions", [])}
    bars = (ctx.out("wfetch") or {}).get("bars") or {}
    since = (ctx.as_of - timedelta(days=6)).isoformat()
    closed = [c for c in pf.get("closed", []) if (c.get("closed") or "") >= since]
    rv = reviews(db)
    waiting = [k for k, v in rv.items() if v.get("status") == "ממתין"]
    return {"ideas": ideas, "after_exit": late, "escaped": escaped(entries, bars, held),
            "reentries": reentries(pf, ctx.as_of), "waiting_reviews": waiting,
            "closed": [{"key": T.trade_key(c), "symbol": c["symbol"], "r": T.r_multiple(c),
                        "idea": (rv.get(T.trade_key(c)) or {}).get("idea_quality"),
                        "execution": (rv.get(T.trade_key(c)) or {}).get("execution_quality")} for c in closed]}


VERDICT_HE = {"approved": "אישרת", "rejected": "דחית", "expired": "לא נבדק", "changed": "השתנה",
              "pending": "ממתין", "postponed": "נדחה"}


def weekly_lines(out: dict | None) -> list[str]:
    if not out:
        return []
    lines = ["למידה (S13-R29, S08-R32)"]
    for c in out["closed"]:
        q = (f" · רעיון {T.QUALITY_HE.get(c['idea'], '?')} · ביצוע {T.QUALITY_HE.get(c['execution'], '?')}"
             if c.get("idea") else "")
        lines.append(f"• נסגרה {c['symbol']} " + (f"{c['r']:+.2f}R" if c["r"] is not None else "") + q)
    hits = [i for i in out["ideas"] if i["first"] == "target"]
    if out["ideas"]:
        lines.append("רעיונות שהתבררו: " + " · ".join(
            f"{i['symbol']} {'✓ יעד' if i['first'] == 'target' else '✗ סטופ' if i['first'] == 'stop' else '– אף אחד'}"
            f" ({VERDICT_HE.get(i['ilay'], i['ilay'])})" for i in out["ideas"]))
        good_no = [i["symbol"] for i in hits if i["ilay"] in ("rejected", "expired")]
        if good_no:
            lines.append("⚠️ רעיון שהגיע ליעד ולא נכנסנו (S13-J8, 'הדבר הכי גרוע'): " + ", ".join(good_no)
                         + ": למה? (פחד, ערנות, ארגון)")
    if out["escaped"]:
        lines.append("ברחו מהרשימה בלי כניסה (S08-R32): " + ", ".join(out["escaped"]))
    noise = [a["trade"].split("|")[0] for a in out["after_exit"] if a.get("stopped_by_noise")]
    if noise:
        lines.append("נעצרו ואז הגיעו ליעד תוך 20 מסחרים (רעש לפני התזה?): " + ", ".join(noise))
    for f in out["reentries"]:
        lines.append(f"⚠️ {f['symbol']}: {f['entries']} כניסות ו-{f['stops']} סטופים ב-30 יום: סימן לנקמה (S13-R6)")
    if out["waiting_reviews"]:
        lines.append(f"תחקירים שמחכים לאישורך: {len(out['waiting_reviews'])} (/queue)")
    return lines if len(lines) > 1 else lines + ["אין עסקאות או רעיונות שהתבררו השבוע"]


# ---------------------------------------------------------------- monthly

def _std(xs: list[float]) -> float:
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) if len(xs) > 1 else 0.0


def risk_metrics(equity: list[tuple[str, float]]) -> dict[str, Any]:
    """S13-J17: Sharpe, Sortino, volatility and the max drawdown from the daily equity (annualised with 252
    sessions; risk-free rate 0: not from course)."""
    eq = [e for _, e in equity if e]
    if len(eq) < 3:
        return {"days": len(eq)}
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))]
    sd = _std(rets)
    down = [min(r, 0.0) for r in rets]
    dsd = math.sqrt(sum(d * d for d in down) / len(down))
    mean = sum(rets) / len(rets)
    peak, mdd = eq[0], 0.0
    for e in eq:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1)
    return {"days": len(eq), "return_pct": round((eq[-1] / eq[0] - 1) * 100, 2),
            "vol_pct": round(sd * math.sqrt(252) * 100, 2),
            "sharpe": round(mean / sd * math.sqrt(252), 2) if sd else None,
            "sortino": round(mean / dsd * math.sqrt(252), 2) if dsd else None,
            "max_dd_pct": round(mdd * 100, 2)}


def trade_stats(trades: list[dict]) -> dict[str, Any]:
    """S13-R18: win rate and R; process compliance (Ilay's sheet: 90%+, not from course)."""
    rs = [T.r_multiple(c) for c in trades]
    pnl = [c["trade_pnl"] if c.get("trade_pnl") is not None else c["pnl"] for c in trades]
    n = len(trades)
    if not n:
        return {"n": 0}
    wins = [p for p in pnl if p > 0]
    rw = [r for r, p in zip(rs, pnl) if r is not None and p > 0]
    rl = [r for r, p in zip(rs, pnl) if r is not None and p <= 0]
    followed = [not T.plan_check(c) for c in trades]
    streak, last = 0, None
    for p in reversed(pnl):
        w = p > 0
        if last is None or w == last:
            streak, last = streak + 1, w
        else:
            break
    return {"n": n, "win_rate": round(len(wins) / n * 100, 1), "pnl": round(sum(pnl), 2),
            "avg_win_r": round(sum(rw) / len(rw), 2) if rw else None,
            "avg_loss_r": round(sum(rl) / len(rl), 2) if rl else None,
            "rr": round(sum(rw) / len(rw) / abs(sum(rl) / len(rl)), 2) if rw and rl and sum(rl) else None,
            "expectancy_r": round(sum(r for r in rs if r is not None) / max(1, sum(r is not None for r in rs)), 2),
            "followed_pct": round(sum(followed) / n * 100, 1), "streak": streak,
            "streak_kind": "win" if last else "loss"}


def learn_monthly(ctx: RunContext, _: str) -> dict:
    db, j = ctx.db, Journal(ctx.db)
    ensure(db)
    pf = ctx.services.get("portfolio") or {}
    since = (ctx.as_of - timedelta(days=31)).isoformat()
    closed = pf.get("closed", [])
    month = [c for c in closed if (c.get("closed") or "") >= since]
    eq = [(r["day"], float(r["equity"])) for r in db.all("SELECT day, equity FROM equity_daily WHERE day >= ? "
                                                          "ORDER BY day", (since,))]
    eq_all = [(r["day"], float(r["equity"])) for r in db.all("SELECT day, equity FROM equity_daily ORDER BY day")]
    agree = db.all("SELECT d.card_id, r.verdict, COUNT(*) AS n FROM ilay_reviews r JOIN decisions d "
                   "ON d.id = r.decision_id WHERE r.created_at >= ? GROUP BY d.card_id, r.verdict", (since,))
    critic = db.all("SELECT card_id, status, COUNT(*) AS n FROM decisions WHERE decided_by LIKE ? "
                    "AND created_at >= ? GROUP BY card_id, status", ("agent:%", since))
    calib = db.all("SELECT d.confidence, COUNT(*) AS n, SUM(o.correct) AS correct FROM decisions d JOIN outcomes o "
                   "ON o.decision_id = d.id WHERE d.confidence IS NOT NULL AND o.correct IS NOT NULL "
                   "GROUP BY d.confidence")
    ideas = db.all("SELECT o.result, o.correct FROM outcomes o JOIN decisions d ON d.id = o.decision_id "
                   "WHERE d.card_id = ?", (IDEA_RULE,), ["result"])
    by_verdict: dict[str, list[int]] = {}
    for i in ideas:
        if i["correct"] is not None:
            by_verdict.setdefault(i["result"].get("ilay", "?"), []).append(int(i["correct"]))
    devs = [dict(r, n=int(r["n"]), correct=int(r["correct"] or 0)) for r in j.deviation_success()]
    stages: dict[str, int] = {}
    classes: dict[str, int] = {}
    for rv in reviews(db).values():
        if rv.get("stage") not in (None, "none", ""):
            stages[str(rv["stage"])] = stages.get(str(rv["stage"]), 0) + 1
        if rv.get("failure_class") not in (None, "no_failure", "unclear"):
            classes[rv["failure_class"]] = classes.get(rv["failure_class"], 0) + 1
    proposals = [d for d in devs if d["n"] >= MIN_CASES]
    proposals += [{"stage": s, "n": n} for s, n in stages.items() if n >= 3]   # not from course: a repeated failure
    return {"month": trade_stats(month), "all": trade_stats(closed), "risk_month": risk_metrics(eq),
            "risk_all": risk_metrics(eq_all),
            "agreement": [dict(r, n=int(r["n"])) for r in agree], "critic": [dict(r, n=int(r["n"])) for r in critic],
            "calibration": [dict(r, n=int(r["n"]), correct=int(r["correct"] or 0)) for r in calib],
            "ideas": {k: {"n": len(v), "hit_pct": round(sum(v) / len(v) * 100, 1)} for k, v in by_verdict.items()},
            "deviations": devs, "stages": stages, "classes": classes, "proposals": proposals}


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f}%"


def monthly_lines(out: dict | None) -> list[str]:
    if not out:
        return []
    lines = ["למידה חודשית (7.3)"]
    for name, s in (("החודש", out["month"]), ("מההתחלה", out["all"])):
        if not s.get("n"):
            lines.append(f"{name}: אין עסקאות סגורות")
            continue
        aim = ("✓" if s["win_rate"] >= WIN_RATE_AIM[0] else "≈" if s["win_rate"] > WIN_RATE_MIN else "✗")
        lines.append(f"{name}: {s['n']} עסקאות · הצלחה {s['win_rate']:.0f}% {aim} (יעד >50%, שאיפה 60-65%) · "
                     f"יחס {s['rr'] if s['rr'] is not None else '—'} (יעד ~3) · תוחלת {s['expectancy_r']:+.2f}R · "
                     f"P&L {s['pnl']:+,.0f}$ · לפי התכנית {s['followed_pct']:.0f}%")
    rk = out["risk_month"]
    if rk.get("days", 0) >= 3:
        lines.append(f"סיכון (S13-J17): תשואה {_pct(rk['return_pct'])} · תנודתיות {_pct(rk['vol_pct'])} · "
                     f"שארפ {rk['sharpe'] if rk['sharpe'] is not None else '—'} · "
                     f"סורטינו {rk['sortino'] if rk['sortino'] is not None else '—'} · ירידה מקסימלית {_pct(rk['max_dd_pct'])}")
    s = out["all"]
    if s.get("n") and s["streak"] >= LOSS_STREAK and s["streak_kind"] == "loss":
        lines.append(f"רצף של {s['streak']} הפסדים: לא משנים שיטה בגלל רצף קצר; תחקיר תהליך, לא תירוצים (S13-J6)")
    if s.get("n") and s["streak"] >= WIN_STREAK and s["streak_kind"] == "win":
        lines.append(f"רצף של {s['streak']} רווחים: לבדוק כמות עסקאות, חשיפה ודילוג על שלבים; לשקול חופשה (S13-J5, S13-R17)")
    if out["ideas"]:
        lines.append("רעיונות כניסה שהתבררו, לפי מה שענית: " + " · ".join(
            f"{VERDICT_HE.get(k, k)} {v['n']} ({v['hit_pct']:.0f}% ליעד)" for k, v in sorted(out["ideas"].items())))
    if out["agreement"]:
        ag: dict[str, dict[str, int]] = {}
        for r in out["agreement"]:
            ag.setdefault(r["card_id"] or "?", {})[r["verdict"]] = r["n"]
        lines.append("ההחלטות שלך לפי כרטיס/כלל: " + " · ".join(
            f"{k} " + "/".join(f"{VERDICT_HE.get(v, v)} {n}" for v, n in sorted(d.items())) for k, d in sorted(ag.items())))
    ret = [r for r in out["critic"] if r["status"] != "decided"]
    if ret:
        lines.append("הבודק החזיר / לא הוכרע: " + " · ".join(f"{r['card_id']} {r['status']} {r['n']}" for r in ret))
    if out["calibration"]:
        lines.append("כיול: " + " · ".join(f"{r['confidence']} {r['correct']}/{r['n']}" for r in out["calibration"]))
    if out["deviations"]:
        lines.append("חריגות מברירת מחדל: " + " · ".join(f"{d['rule_id']} {d['correct']}/{d['n']}" for d in out["deviations"]))
    if out["stages"]:
        lines.append("שלבי כשל: " + " · ".join(f"{k} {T.STAGES_HE.get(k, '')} ×{n}" for k, n in sorted(out["stages"].items())))
    if out["proposals"]:
        lines.append("מועמדים להצעת שינוי (7.4, להחלטתך): " + " · ".join(
            d.get("rule_id") or f"שלב {d['stage']}" for d in out["proposals"]))
    else:
        lines.append(f"הצעות שינוי ל-Playbook: אין עדיין מספיק מקרים (סף {MIN_CASES} לכלל)")
    return lines


# ---------------------------------------------------------------- export (qt.web /journal)

IDEA_COLUMNS = [("proposed", "תאריך הצעה"), ("symbol", "סימול"), ("ilay", "מה ענית"), ("first", "מה קרה קודם"),
                ("on", "מתי"), ("item", "פריט")]
EQUITY_COLUMNS = [("day", "תאריך"), ("equity", "Equity בסוף יום $"), ("peak", "שיא")]


def tables(db: DB, pf: dict, prices: dict[str, float] | None = None) -> dict[str, tuple[list[dict], list[tuple[str, str]]]]:
    """The journal as tables: trades (closed and open), entry ideas and how they ended, the daily equity."""
    ensure(db)
    Queue(db)
    rv = reviews(db)
    trades = [T.row(c, rv.get(T.trade_key(c)), rv.get(T.trade_key(c))) for c in pf.get("closed", [])]
    trades += [T.open_row(p, (prices or {}).get(p["symbol"])) for p in pf.get("positions", [])]
    Journal(db)
    ideas = [dict(o["result"], measured=o["measured_at"][:10]) for o in db.all(
        "SELECT o.result, o.measured_at FROM outcomes o JOIN decisions d ON d.id = o.decision_id WHERE d.card_id = ? "
        "ORDER BY o.id", (IDEA_RULE,), ["result"])]
    for i in ideas:
        i["ilay"] = VERDICT_HE.get(i.get("ilay"), i.get("ilay"))
        i["first"] = {"target": "יעד", "stop": "סטופ", "neither": "אף אחד (60 יום)"}.get(i.get("first"), i.get("first"))
    equity = db.all("SELECT day, equity, peak FROM equity_daily ORDER BY day")
    return {"trades": (trades, T.COLUMNS), "ideas": (ideas, IDEA_COLUMNS), "equity": (equity, EQUITY_COLUMNS)}
