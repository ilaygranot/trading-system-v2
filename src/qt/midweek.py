"""The midweek deep dive (Ilay 09.11): the nightly scan finds stocks that turn green every evening, but the deep dive
ran only on Saturday, so a stock that turned green on Tuesday waited four days. On Wednesday night, after the scan,
up to four of them get the same cards and the same Critic as on Saturday, on a budget of their own
(models.yaml budget.midweek_research_usd).

The queue: every stock a scan since the last Saturday research marked new and green, or orange to green, on the side
of its sector (the scan's "aligned" fit: a neutral sector's stocks are watch only), still green on the newest scan, not
on the watchlist and with no full analysis in the last 30 days. The cap of 4 new ideas a week (S07-R35, hot_max)
counts over the whole week: Saturday's new ready and conditional ideas come off it, and when it is full the run asks
nothing and says so. An idea that passes reaches the watchlist and Telegram the same night. Not a trading step: every
entry still goes through the daily run and Ilay's approval.
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

from qt.approvals import Queue
from qt.runs import RunContext, Step

MAX_MIDWEEK = 4                    # Ilay 09.11: up to 4 deep dives on Wednesday night
WEEK_DAYS = 7
NEW_BUCKETS = ("enter", "conditional")


def _dossier(db, as_of: date) -> dict | None:
    """The newest Saturday dossier before `as_of`, if it belongs to this week."""
    row = db.one("SELECT content FROM uploads WHERE kind = 'dossier' ORDER BY id DESC LIMIT 1")
    try:
        d = json.loads(row["content"]) if row else None
    except ValueError:
        return None
    at = str(((d or {}).get("meta") or {}).get("as_of") or "")
    if not at or at >= as_of.isoformat() or date.fromisoformat(at) < as_of - timedelta(days=WEEK_DAYS):
        return None
    return d


def new_ideas(d: dict | None) -> list[str]:
    """The research's new ready and conditional ideas: not a watchlist backfill and not a thesis revisit."""
    return [s for s, i in ((d or {}).get("ideas") or {}).items()
            if i.get("bucket") in NEW_BUCKETS and not i.get("backfill") and not i.get("revisit")]


def scan_candidates(db, since: date, as_of: date) -> list[dict]:
    """The stocks the scans after `since` (up to `as_of`) marked new and green or orange to green, on their sector's
    side, that are still green and aligned on the newest scan, in its order (green, aligned, reward to risk)."""
    runs = db.all("SELECT id, as_of FROM runs WHERE kind = 'scan' AND status = 'done' AND as_of > ? AND as_of <= ? "
                  "ORDER BY as_of, started_at", (since.isoformat(), as_of.isoformat()))
    seen: dict[str, str] = {}
    for r in runs:
        row = db.one("SELECT output FROM run_steps WHERE run_id = ? AND step = 'compare' AND status = 'done'",
                     (r["id"],), ["output"])
        c = (row or {}).get("output") or {}
        for kind in ("new", "upgraded"):
            for x in c.get(kind) or []:
                if x.get("light") == "green" and x.get("fit") == "aligned":
                    seen.setdefault(x["sym"], f"{'עלתה לירוק' if kind == 'new' else 'מכתום לירוק'} ב-{r['as_of']}")
    if not runs:
        return []
    last = db.one("SELECT output FROM run_steps WHERE run_id = ? AND step = 'screen' AND status = 'done'",
                  (runs[-1]["id"],), ["output"])
    radar = ((last or {}).get("output") or {}).get("radar") or []
    return [dict(sym=x["sym"], etf=x["etf"], dir=x["dir"], rr=x.get("rr"), why=seen[x["sym"]]) for x in radar
            if x["sym"] in seen and x.get("light") == "green" and x.get("fit") == "aligned"]


def pick(ctx: RunContext, _: str) -> dict:
    from qt.policy import load_policy
    from qt.weekly import analysed
    d = _dossier(ctx.db, ctx.as_of)
    since = date.fromisoformat(d["meta"]["as_of"]) if d else ctx.as_of - timedelta(days=WEEK_DAYS)
    cap = int(load_policy().watchlist.hot_max)
    taken = new_ideas(d)
    room = max(0, cap - len(taken))
    on_list = {e["symbol"] for e in (ctx.services.get("watchlist") or {}).get("entries") or []}
    queue = [c for c in scan_candidates(ctx.db, since, ctx.as_of) if c["sym"] not in on_list]
    cover = analysed(ctx.db, [c["sym"] for c in queue], ctx.as_of)
    queue = [c for c in queue if c["sym"] not in cover]
    n = min(MAX_MIDWEEK, room)
    return {"since": since.isoformat(), "cap": cap, "saturday_new": taken, "room": room, "queue": queue,
            "picked": [c["sym"] for c in queue[:n]], "waiting": [c["sym"] for c in queue[n:]],
            "analysed_before": cover}


def _run_research(as_of: date, judge, manual_text: str, fred) -> dict:
    from qt.research import run as R
    work = Path(os.environ.get("QT_WORK_DIR", "/tmp/qt-research")) / f"{as_of.isoformat()}-midweek"
    work.mkdir(parents=True, exist_ok=True)
    return R.run(as_of, work, None, None, work / "yahoo", fred, judge=judge, manual_text=manual_text, state=judge.state)


def entries(d: dict, room: int) -> list[dict]:
    """The watchlist rows of the run's own ideas (not every green stock it skipped: those are Saturday's), the ready
    and conditional ones within the week's room; above it an idea is followed as watch until Saturday."""
    from qt import watch as W
    out, n = [], 0
    for e in W.from_dossier(d)["entries"]:
        if e["symbol"] not in d["ideas"]:
            continue
        if e["bucket"] in NEW_BUCKETS:
            n += 1
            if n > room:
                e = {**e, "bucket": "watch", "trigger_kind": "now",
                     "why_not": "מעבר לתקרת 4 הרעיונות החדשים בשבוע: תיבחן שוב בשבת"}
        out.append(e)
    return out


def research(ctx: RunContext, _: str) -> dict:
    """The deep dives on the picked stocks: the weekly research's stages on this week's data, the agents on these
    stocks only (qt.research.judge, `only`), Saturday's A1 and market story kept. A partial report adds nothing."""
    p = ctx.out("pick")
    if not p["picked"]:
        return {"ran": False, "why": "full" if p["queue"] and not p["room"] else "empty"}
    from qt.weekly import ResearchIncomplete, _sync_theses, merge_watchlist, research_inputs
    runner, fred, manual, _auto = research_inputs(ctx, "מחקר אמצע השבוע")
    import yaml
    from qt.agents.runner import config
    from qt.research import completeness, report
    from qt.research.judge import Judge
    from qt.research.state import State, fresh
    from qt.theses import Store
    state = State(ctx.db, ctx.as_of, tag="midweek")
    if fresh():
        state.clear()
    prior = _dossier(ctx.db, ctx.as_of)
    prev, runner.limit_usd = runner.limit_usd, config()["budget"]["midweek_research_usd"]
    try:
        judge = Judge(runner, run_id=ctx.run_id, state=state, max_deep=MAX_MIDWEEK)
        judge.only = list(p["picked"])
        judge.prior_a1 = ((prior or {}).get("judgments") or {}).get("A1")
        judge.theses = Store(ctx.db)
        d = (ctx.services.get("research_run") or _run_research)(ctx.as_of, judge, manual["content"], fred)
    finally:
        runner.limit_usd = prev
    d = json.loads(json.dumps(d, default=str))
    d["meta"]["midweek"] = {k: p[k] for k in ("since", "cap", "saturday_new", "room", "queue", "waiting")}
    d["completeness"] = done = completeness.check(d)
    q = Queue(ctx.db)
    rid = q.upload("midweek_report", f"midweek-report-{ctx.as_of.isoformat()}.html",
                   "<!doctype html><meta charset=utf-8>" + report.render(d))
    q.upload("midweek_dossier", f"midweek-dossier-{ctx.as_of.isoformat()}.json", json.dumps(d, ensure_ascii=False))
    theses = _sync_theses(ctx, d)
    if done["status"] != "complete":
        raise ResearchIncomplete("\n".join(completeness.lines(done)) + f"\n(report {rid}; resume the run to finish it)")
    rows = entries(d, p["room"])
    old = ctx.services.get("watchlist")
    wl = merge_watchlist(old, {**(old or {}), "entries": rows})
    q.upload("watchlist", f"watchlist-{ctx.as_of.isoformat()}-midweek.yaml",
             yaml.safe_dump(wl, allow_unicode=True, sort_keys=False))
    J = d.get("judgments") or {}
    added = {e["symbol"]: e["bucket"] for e in rows}
    ideas = [dict(symbol=s, bucket=added.get(s, i["bucket"]), research_bucket=i["bucket"], direction=i["direction"],
                  rr=(i.get("plan") or {}).get("rr"), data_gap=bool((i.get("plan") or {}).get("data_gap")),
                  line=((i.get("judgment") or {}).get("tldr") or {}).get("line") or (i.get("judgment") or {}).get("why", ""))
             for s, i in d["ideas"].items()]
    return {"ran": True, "report_id": rid, "ideas": ideas, "watchlist_n": len(wl["entries"]), "theses": theses,
            "dropped": (J.get("A3") or {}).get("dropped") or {},
            "cost_usd": round(sum(x.get("cost", 0) for x in J.get("agent_log", [])), 2), "errors": J.get("errors", {})}


BUCKET_HE = {"enter": "מוכנה לכניסה", "conditional": "מותנית בטריגר", "watch": "למעקב", "reject": "נפסלה"}


def message(as_of: date, p: dict, r: dict) -> str:
    head = f"🔎 מחקר אמצע השבוע {as_of.isoformat()}"
    used = f"{len(p['saturday_new'])} מתוך {p['cap']} רעיונות חדשים השבוע כבר נמצאו בשבת"
    if not r.get("ran"):
        if r.get("why") == "full":
            return (f"{head}: לא רץ. {used}, התקרה מלאה. מחכות לשבת: "
                    + ", ".join(c["sym"] for c in p["queue"]))
        return f"{head}: לא רץ. אין מניות חדשות שעלו לירוק בסקטור בכיוון שלהן מאז {p['since']}."
    from qt.telegram import report_link
    from qt.weekly import _f
    link = report_link(r["report_id"])
    lines = [head, f"נבדקו לעומק {len(r['ideas'])} מניות מהסריקה הלילית · {used} · עלות הסוכנים {r['cost_usd']:.2f}$"
             + (f" · הדוח: {link}" if link else "")]
    why = {c["sym"]: c["why"] for c in p["queue"]}
    for x in r["ideas"]:
        over = x["bucket"] != x["research_bucket"]
        lines.append(f"• {x['symbol']} ({why.get(x['symbol'], '')}): {BUCKET_HE.get(x['research_bucket'], x['research_bucket'])}"
                     + (f" · יחס {_f(x['rr'], sign=False)}" if x["research_bucket"] in NEW_BUCKETS else "")
                     + f" · {x['line']}" + (" · ⚠️ פער נתונים ביעד (בסיס EPS)" if x.get("data_gap") else "")
                     + (" · מעבר לתקרה השבועית, למעקב עד שבת" if over else ""))
    for s, w in (r.get("dropped") or {}).items():
        lines.append(f"• {s}: לא נבדקה ({w})")
    if p["waiting"]:
        lines.append("בתור, לשבת: " + ", ".join(p["waiting"]))
    if r.get("errors"):
        lines.append("לא הושלמו: " + ", ".join(f"{k} ({v})" for k, v in r["errors"].items()))
    th = r.get("theses") or {}
    if th.get("pending"):
        lines.append(f"🧱 {len(th['pending'])} עדכוני תזה בפוזיציות פתוחות מחכים לאישורך")
    if any(x["bucket"] in NEW_BUCKETS for x in r["ideas"]):
        lines.append(f"רשימת המעקב עודכנה ל-{r['watchlist_n']} רעיונות: הבדיקה היומית מתחילה מהסגירה הבאה. "
                     "שום כניסה בלי האישור שלך")
    return "\n".join(lines)


def notify(ctx: RunContext, _: str) -> dict:
    from qt.pipelines import _send
    text = message(ctx.as_of, ctx.out("pick"), ctx.out("research"))
    _send(ctx, text)
    bot, sent = ctx.services.get("telegram"), 0
    if bot is not None:                              # the thesis changes on open positions, with their buttons
        from qt.telegram import send_item
        q = Queue(ctx.db)
        for it in q.for_day(ctx.as_of):
            if it["kind"] == "thesis" and it["status"] == "pending" and not it.get("tg_message_id"):
                send_item(bot, q, it)
                sent += 1
    return {"text": text, "items_sent": sent}


MIDWEEK = (Step("pick", pick), Step("research", research), Step("notify", notify))
