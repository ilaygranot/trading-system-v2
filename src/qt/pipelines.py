"""The scheduled runs besides the daily one (runtime plan 4): premarket recheck, weekly report, monthly macro.

premarket: expire -> quotes -> recheck -> notify. Items still open or approved are checked against the latest
           price; a move past the threshold, or a hard block now in force, sends the item back as "changed".
weekly:    expire -> report -> wfetch -> arena -> revisit -> week -> research -> learn -> notify. Runs that failed or resumed, agent cost
           per agent, queue discipline (approved / rejected / not reviewed / changed), agent decisions that were
           undecided or returned; then the end-of-week review (qt.weekly, S13-R21).
monthly:   manual -> fred -> stance -> ... -> queue -> learn -> notify. The learn steps are qt.learning (architecture 7.3). Waits for the manual ISM / NFIB file: without it the run
           fails with a reminder, and resumes when the file arrives (the bot's upload calls resume).
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

from qt.approvals import Queue
from qt.daily import DAILY, load_portfolio  # noqa: F401  (load_portfolio re-exported for the CLI)
from qt.runs import RunContext, Step
from qt import learning as L
from qt import monthly as MO
from qt import weekly as W


def _now(ctx: RunContext) -> datetime:
    return ctx.services.get("now", lambda: datetime.now(timezone.utc))()


def _send(ctx: RunContext, text: str, buttons=None) -> None:
    bot = ctx.services.get("telegram")
    if bot is not None:
        bot.send(text, buttons)
    if ctx.services.get("notifier") is not None:
        ctx.services["notifier"](text)


def expire(ctx: RunContext, _: str) -> dict:
    return {"expired": Queue(ctx.db).expire(_now(ctx))}


# ---------------------------------------------------------------- premarket

def quotes(ctx: RunContext, _: str) -> dict:
    """Latest price per symbol with an open or approved item: the pre-market quote when the provider has one,
    else the last close (both sources are kept)."""
    rows = ctx.db.all("SELECT DISTINCT symbol FROM approval_queue WHERE symbol IS NOT NULL AND status IN "
                      "('pending','postponed','approved')")
    out = {"prices": {}, "sources": {}, "missing": []}
    for r in rows:
        s = r["symbol"]
        px, src = None, ""
        if ctx.services.get("quote") is not None:
            try:
                px, src = ctx.services["quote"](s)
            except Exception as e:                                   # noqa: BLE001 - fall back to the close
                ctx.event(f"{s}: quote {type(e).__name__}: {e}", "warn", "quotes")
        if px is None and ctx.services.get("prices") is not None:
            try:
                bars, src = ctx.services["prices"].daily_bars(s, ctx.as_of - timedelta(days=10), ctx.as_of)
                px = bars[-1].close if bars else None
            except Exception as e:                                   # noqa: BLE001
                ctx.event(f"{s}: {type(e).__name__}: {e}", "warn", "quotes")
        if px is None:
            out["missing"].append(s)
        else:
            out["prices"][s], out["sources"][s] = px, src
    return out


def recheck(ctx: RunContext, _: str) -> dict:
    """7.2 rule 4. A triggered portfolio stop (a hard rule) blocks every new entry and add."""
    pf = ctx.services.get("portfolio") or {}
    blocked = set()
    if pf.get("portfolio_stop_triggered"):
        blocked = {r["symbol"] for r in ctx.db.all("SELECT symbol FROM approval_queue WHERE kind IN ('enter','add') "
                                                   "AND status IN ('pending','postponed','approved')")}
    return {"changed": Queue(ctx.db).recheck(ctx.out("quotes")["prices"], blocked, _now(ctx))}


def premarket_notify(ctx: RunContext, _: str) -> dict:
    changed = ctx.out("recheck")["changed"]
    if not changed and not ctx.out("expire")["expired"]:
        return {"sent": False}                                       # nothing new: no message before the open
    q = Queue(ctx.db)
    _send(ctx, f"לפני פתיחה {ctx.as_of.isoformat()} · {len(changed)} פריטים השתנו · "
               f"{len(ctx.out('expire')['expired'])} פגו")
    bot = ctx.services.get("telegram")
    if bot is not None:
        from qt.telegram import send_item
        for iid in changed:
            send_item(bot, q, q.get(iid))
    return {"sent": True}


# ---------------------------------------------------------------- weekly

def report(ctx: RunContext, _: str) -> dict:
    since = (ctx.as_of - timedelta(days=6)).isoformat()
    runs = ctx.db.all("SELECT kind, status, resumes FROM runs WHERE as_of >= ?", (since,))
    cost = ctx.db.all("SELECT agent, COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS usd FROM agent_calls "
                      "WHERE created_at >= ? GROUP BY agent ORDER BY agent", (since,))
    queue = ctx.db.all("SELECT status, COUNT(*) AS n FROM approval_queue WHERE as_of >= ? GROUP BY status", (since,))
    agents = ctx.db.all("SELECT status, COUNT(*) AS n FROM decisions WHERE decided_by LIKE ? "
                        "AND created_at >= ? GROUP BY status", ("agent:%", since))
    q = {r["status"]: int(r["n"]) for r in queue}
    asked = sum(q.get(s, 0) for s in ("approved", "rejected", "expired", "changed", "pending", "postponed"))
    return {
        "runs": len(runs), "failed": sum(r["status"] == "failed" for r in runs),
        "resumed": sum(1 for r in runs if r["resumes"]),
        "cost": [{"agent": r["agent"], "calls": int(r["calls"]), "usd": round(float(r["usd"]), 2)} for r in cost],
        "cost_total": round(sum(float(r["usd"]) for r in cost), 2),
        "queue": q, "not_reviewed": q.get("expired", 0),
        "not_reviewed_pct": round(100 * q.get("expired", 0) / asked, 1) if asked else 0.0,
        "agents": {r["status"]: int(r["n"]) for r in agents},
    }


def weekly_notify(ctx: RunContext, _: str) -> dict:
    r = ctx.out("report")
    lines = [f"שבועית עד {ctx.as_of.isoformat()}",
             f"ריצות {r['runs']} · נכשלו {r['failed']} · הומשכו {r['resumed']}",
             f"עלות Agents {r['cost_total']:.2f}$" + "".join(f" · {c['agent']} {c['usd']:.2f}$" for c in r["cost"]),
             "תור: " + " · ".join(f"{k} {v}" for k, v in sorted(r["queue"].items())),
             f"לא נבדקו (פגו): {r['not_reviewed']} ({r['not_reviewed_pct']}%)"]
    if r["agents"]:
        lines.append("החלטות Agents: " + " · ".join(f"{k} {v}" for k, v in sorted(r["agents"].items())))
    text = "\n".join(lines)
    if ctx.out("week") is not None:                  # the review first, operations in a second message (4096 cap)
        review = W.summary(ctx)
        learned = L.weekly_lines(ctx.out("learn"))
        if learned:
            review += "\n\n" + "\n".join(learned + _journal_link())
        _send(ctx, review)
        text = review + "\n\nתפעול\n" + text
        _send(ctx, "תפעול\n" + "\n".join(lines))
    else:
        _send(ctx, text)
    return {"text": text}


# ---------------------------------------------------------------- monthly

class ManualFileMissing(RuntimeError):
    """The monthly ISM / NFIB file is not in yet: the run waits for it (runtime plan 4)."""


def _prev_month(d: date) -> str:
    first = d.replace(day=1) - timedelta(days=1)
    return f"{first.year:04d}-{first.month:02d}"


def manual(ctx: RunContext, _: str) -> dict:
    from qt.data.macro_feed import parse_manual
    up = Queue(ctx.db).latest_upload("manual_macro")
    need = _prev_month(ctx.as_of)
    if up is not None:
        import yaml
        raw = yaml.safe_load(up["content"]) or {}
        if need in {str(k) for k in (raw.get("ism_pmi") or {})}:
            return {"upload_id": up["id"], "series": parse_manual(up["content"], ctx.as_of)}
    _send(ctx, f"חודשית {ctx.as_of.isoformat()} מחכה לקובץ המקרו הידני: ISM PMI/NMI ו-NFIB עד {need}. "
               "שלח לי אותו כאן כקובץ YAML ({series: {YYYY-MM: value}}) והריצה תמשיך לבד.")
    raise ManualFileMissing(f"manual macro file with ism_pmi for {need}")


def fred(ctx: RunContext, _: str) -> dict:
    from qt.data.macro_feed import fetch_fred
    monthly, daily, src = fetch_fred(ctx.services["fred"], ctx.as_of)
    return {"monthly": monthly, "daily_last": {k: v[-1] for k, v in daily.items() if v}, "sources": src}


def stance(ctx: RunContext, _: str) -> dict:
    from qt import macro as M
    from qt.data.macro_feed import yoy_pct
    from qt.policy import load_policy
    man, fr = ctx.out("manual")["series"], ctx.out("fred")["monthly"]
    series = {**{k: v for k, v in fr.items()}, **man}
    scores, skipped = [], []
    for name in load_policy().macro.activity_indicators:
        if len(series.get(name, [])) >= 13:
            scores.append(M.score_series(name, series[name]))
        else:
            skipped.append(name)
    st = M.stance(scores,
                  fed=M.fed_scale(fr["ffr"], yoy_pct(fr["m2"]), fr["fed_balance_sheet"]),
                  infl=M.inflation(yoy_pct(fr["cpi"])),
                  lab=M.labor(fr["unemployment"]),
                  ism=M.ism_state(man["ism_pmi"]) if len(man.get("ism_pmi", [])) >= 4 else None)
    out = asdict(st)
    out["skipped_indicators"] = skipped
    return out


def monthly_queue(ctx: RunContext, _: str) -> dict:
    st = ctx.out("stance")
    detail = (f"סולם פעילות {st['activity']['pct']:.0f}% · אזור {st['zone']} · הטיה {st['bias'] or 'אזור אפור: DC-01'}"
              f" · מצפן {st['compass_now']} · פד {st['fed']['period'] if st['fed'] else '-'}"
              + (f" · דגלים: {', '.join(st['flags'])}" if st["flags"] else "")
              + (f" · חסרים: {', '.join(st['skipped_indicators'])}" if st["skipped_indicators"] else "")
              + f" · כרטיסים ל-A1: {', '.join(st['cards'])}")
    a1 = ctx.out("a1") or {}
    if a1.get("DC-01"):
        detail += " · A1: " + ", ".join(f"{k} {a1[k]['choice']}" for k in ("DC-01", "DC-02", "DC-03", "DC-04") if k in a1)
    iid = Queue(ctx.db).add(ctx.run_id, ctx.as_of, dict(kind="flag", rule_id="S02-STANCE", symbol=None, qty=None,
                                                         price=None, preapproved=False, detail=detail))
    return {"item": iid, "detail": detail}


def monthly_notify(ctx: RunContext, _: str) -> dict:
    text = f"חודשית {ctx.as_of.isoformat()} · מקרו: {ctx.out('queue')['detail']}"
    prep = MO.summary(ctx)
    _send(ctx, text)
    _send(ctx, prep)
    learned = "\n".join(L.monthly_lines(ctx.out("learn")) + _journal_link())
    if learned:
        _send(ctx, learned)
    return {"text": text, "prep": prep, "learn": learned}


def _journal_link() -> list[str]:
    from qt.telegram import journal_link
    link = journal_link()
    return [f"יומן המסחר (טבלאות ו-CSV): {link}"] if link else []


PREMARKET = (Step("expire", expire), Step("quotes", quotes), Step("recheck", recheck), Step("notify", premarket_notify))
WEEKLY = (Step("expire", expire), Step("report", report), Step("wfetch", W.wfetch), Step("arena", W.arena),
          Step("revisit", W.revisit), Step("week", W.week), Step("research", W.research), Step("learn", L.learn_weekly),
          Step("notify", weekly_notify))
MONTHLY = (Step("manual", manual), Step("fred", fred), Step("stance", stance), Step("a1", MO.a1),
           Step("mfetch", MO.mfetch), Step("candles", MO.candles), Step("sectors", MO.sectors), Step("ism", MO.ism),
           Step("calendar", MO.calendar), Step("season", MO.season), Step("sentiment", MO.sentiment),
           Step("exposure", MO.exposure),
           Step("queue", monthly_queue), Step("learn", L.learn_monthly), Step("notify", monthly_notify))
PIPELINES = {"daily": DAILY, "premarket": PREMARKET, "weekly": WEEKLY, "monthly": MONTHLY}
