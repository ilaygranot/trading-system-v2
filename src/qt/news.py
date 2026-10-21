"""The daily news pass (S13-R22, S13-R27, S08): headlines on the positions and the watchlist, each new one classified
by A5 on DC-14 (class Macro Lead / Macro Noise / A-D, for or against the idea, already priced in).

A class A or B headline against an open position, not priced in, is an event for A7 (DC-21) in the agents step;
against a watchlist idea it is a flag in the summary (a possible thesis breaker, S08-R30). The day's news diary
(S13-R27: 3-5 events) is the strongest of the day's headlines. Without the agents the headlines are listed
unclassified. A headline is classified once: news_seen keeps every link already handled.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from qt import watch as W
from qt.facts import FactSheet
from qt.runs import RunContext

SCHEMA = """
CREATE TABLE IF NOT EXISTS news_seen (
  link TEXT PRIMARY KEY,
  symbol TEXT NOT NULL,
  title TEXT,
  published TEXT,
  seen_on TEXT NOT NULL,
  klass TEXT,                            -- DC-14 option, NULL when not classified
  direction TEXT,                        -- for_idea | against_idea
  priced_in INTEGER
);
"""
LOOKBACK_DAYS = 3                        # headlines this recent count as new (not from course)
MAX_CLASSIFY = 12                        # DC-14 calls per day, positions first (not from course: cost)
DIARY = 5                                # S13-R27: 3-5 events a day
RANK = {"A": 0, "macro_lead": 1, "B": 2, "C": 3, "D": 4, "macro_noise": 5, None: 6}
STRONG = ("A", "B")


def _facts(item: dict, ctx_info: dict, as_of: date) -> FactSheet:
    fs = FactSheet()
    s = item["symbol"]
    fs.add(f"{s}.headline", item["title"], f"{item['source']} ({item['publisher']})", date.fromisoformat(item["published"]))
    for k, v in ctx_info.items():
        if v not in (None, "", []):
            fs.add(f"{s}.{k}", v, "code:daily", as_of)
    return fs


def news(ctx: RunContext, _: str) -> dict:
    prov = ctx.services.get("news") or ctx.services.get("prices")
    if prov is None or not hasattr(prov, "news"):
        return {"enabled": False, "items": [], "adverse": {}, "diary": []}
    ctx.db.ddl(SCHEMA)
    from qt.daily import _pf
    held = {p["symbol"]: p for p in _pf(ctx).get("positions", [])}
    watch = {e.symbol: e for e in W.load(ctx.services.get("watchlist")) if e.symbol not in held}
    since = (ctx.as_of - timedelta(days=LOOKBACK_DAYS)).isoformat()
    fresh: list[dict[str, Any]] = []
    for sym in list(held) + list(watch):
        try:
            items, src = prov.news(sym)
        except Exception as e:                                       # noqa: BLE001 - news is never a blocker
            ctx.event(f"{sym}: news {type(e).__name__}: {e}", "warn", "news")
            continue
        for it in items:
            if not it["link"] or it["published"] < since or it["published"] > ctx.as_of.isoformat():
                continue
            if ctx.db.one("SELECT link FROM news_seen WHERE link = ?", (it["link"],)) or \
                    any(f["link"] == it["link"] for f in fresh):
                continue
            fresh.append({**it, "symbol": sym, "held": sym in held, "source": src})
    runner = ctx.services.get("agents")
    calc = ctx.out("calc") or {}
    regime = (ctx.out("rules") or {}).get("regime")
    for i, it in enumerate(fresh):
        sym = it["symbol"]
        if runner is not None and i < MAX_CLASSIFY:
            p, e = held.get(sym), watch.get(sym)
            info = dict(direction=(p or {}).get("direction") or (e.direction if e else None),
                        return_20d_pct=calc.get("ret_20d", {}).get(sym),
                        idea=(e.tldr if e else (p or {}).get("thesis")), thesis_breakers=(e.breakers if e else None),
                        on_list="position" if p else "watchlist")
            try:
                r = runner.decide("A5", "DC-14", _facts(it, info, ctx.as_of),
                                  {"symbol": sym, "regime": regime, "held": bool(p), "write_in": "Hebrew"},
                                  run_id=ctx.run_id, step="news", subject=sym)
                it.update(klass=r.option, direction=r.output.get("direction"), priced_in=r.output.get("priced_in"),
                          why=r.output.get("reasoning", ""), status=r.status)
            except Exception as ex:                                  # noqa: BLE001 - budget or API: listed raw
                ctx.event(f"{sym}: DC-14 {type(ex).__name__}: {ex}", "warn", "news")
        ctx.db.insert_new("news_seen", dict(link=it["link"], symbol=sym, title=it["title"], published=it["published"],
                                            seen_on=ctx.as_of.isoformat(), klass=it.get("klass"),
                                            direction=it.get("direction"),
                                            priced_in=None if it.get("priced_in") is None else int(bool(it["priced_in"]))),
                          ["link"])
    against = [it for it in fresh if it.get("klass") in STRONG and it.get("direction") == "against_idea"
               and not it.get("priced_in")]
    adverse = {it["symbol"]: f"{it['klass']}: {it['title']}" for it in against if it["held"]}
    flags = {it["symbol"]: f"{it['klass']}: {it['title']}" for it in against if not it["held"]}
    diary = sorted(fresh, key=lambda x: (RANK.get(x.get("klass"), 6), not x["held"]))[:DIARY]
    return {"enabled": True, "classified": sum(1 for x in fresh if x.get("klass")), "items": fresh,
            "adverse": adverse, "watch_flags": flags, "diary": diary}


DIR_HE = {"for_idea": "בעד", "against_idea": "נגד"}


def diary_lines(out: dict) -> list[str]:
    """The news diary for the end-of-day summary."""
    if not out or not out.get("enabled"):
        return []
    if not out["items"]:
        return ["יומן חדשות: אין כותרות חדשות על הפוזיציות והרשימה"]
    lines = ["יומן חדשות:"]
    for it in out["diary"]:
        tag = (f"[{it['klass']}, {DIR_HE.get(it.get('direction'), '?')}"
               + (", מתומחר" if it.get("priced_in") else "") + "] ") if it.get("klass") else ""
        lines.append(f"• {it['symbol']} {tag}{it['title']} ({it['publisher']})")
    for sym, what in out.get("adverse", {}).items():
        lines.append(f"⚠️ {sym}: ידיעה נגד הפוזיציה ({what}), עוברת ל-A7")
    for sym, what in out.get("watch_flags", {}).items():
        lines.append(f"⚠️ {sym} ברשימה: ידיעה נגד הרעיון ({what}), לבדוק שובר תזה")
    return lines
