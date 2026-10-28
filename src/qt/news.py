"""The daily news pass (S13-R22, S13-R27, S08): headlines on the positions and the watchlist, each new one classified
by A5 on DC-14 (class Macro Lead / Macro Noise / A-D, for or against the idea, already priced in).

A class A or B headline against an open position, not priced in, is an event for A7 (DC-21) in the agents step;
against a watchlist idea it is a flag in the summary (a possible thesis breaker, S08-R30). The day's news diary
(S13-R27: 3-5 events) is the strongest of the day's headlines. Without the agents the headlines are listed
unclassified. A headline is classified once: news_seen keeps every link already handled.

The per-stock digest (not from course: Ilay 27.10 and 28.10): every position and watchlist stock with its new
headlines as links, under a short Hebrew summary of the last days' headlines on it. A5 writes the summaries in one
DC-28 call a day; each sentence must stand on the headlines it cites (the article's sentence check), and a sentence
that cites no headline of its own stock is dropped.

Peers (S08-J25, code only): a strong headline (A/B, or a C about results) on one stock is recorded as a class C note
on every other watchlist entry of the same sector ETF, or that names it as a peer. A note, never an action
(S08-R18: C is recorded only because the stock is on the list); no extra DC-14 call.
"""
from __future__ import annotations

import html
import re
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
PER_SYMBOL = 4                           # headlines per stock in the per-stock digest (not from course: length)
RANK = {"A": 0, "macro_lead": 1, "B": 2, "C": 3, "D": 4, "macro_noise": 5, None: 6}
STRONG = ("A", "B")
RESULTS_WORDS = re.compile(r"result|earning|quarter|revenue|guidance|outlook|profit", re.IGNORECASE)


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
    recent: dict[str, list[dict[str, Any]]] = {}
    for sym in list(held) + list(watch):
        try:
            items, src = prov.news(sym)
        except Exception as e:                                       # noqa: BLE001 - news is never a blocker
            ctx.event(f"{sym}: news {type(e).__name__}: {e}", "warn", "news")
            continue
        for it in items:
            if not it["link"] or it["published"] < since or it["published"] > ctx.as_of.isoformat():
                continue
            if any(r["link"] == it["link"] for r in recent.get(sym, [])):
                continue
            seen = ctx.db.one("SELECT klass, direction FROM news_seen WHERE link = ?", (it["link"],))
            row = {**it, "symbol": sym, "held": sym in held, "source": src}
            recent.setdefault(sym, []).append({**row, **(seen or {})} if seen else row)
            if seen or any(f["link"] == it["link"] for f in fresh):
                continue
            fresh.append(row)
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
    etf_of = {s: e.etf for s, e in watch.items()} | {s: p.get("sector") for s, p in held.items()}
    for it in fresh:                                                 # today's classes reach the summary's facts
        for r in recent.get(it["symbol"], []):
            if r["link"] == it["link"]:
                r.update(klass=it.get("klass"), direction=it.get("direction"))
    summaries = summarize(ctx, runner, recent, held, watch) if runner is not None and recent else {}
    return {"summaries": summaries,"enabled": True, "classified": sum(1 for x in fresh if x.get("klass")), "items": fresh,
            "symbols": [{"symbol": s, "held": s in held} for s in list(held) + list(watch)],
            "adverse": adverse, "watch_flags": flags, "diary": diary,
            "peer_notes": peer_notes(fresh, list(watch.values()), etf_of)}


def _summary_facts(recent: dict[str, list[dict]], held: dict, watch: dict, as_of: date) -> tuple[FactSheet, dict]:
    """DC-28's sheet: each stock's place (position or watchlist, side, idea) and its headlines of the last days."""
    fs, owner = FactSheet(), {}
    for sym, items in recent.items():
        p, e = held.get(sym), watch.get(sym)
        info = {"on_list": "position" if p else "watchlist",
                "direction": (p or {}).get("direction") or (e.direction if e else None),
                "idea": e.tldr if e else (p or {}).get("thesis")}
        for k, v in info.items():
            if v not in (None, "", []):
                owner[fs.add(f"{sym}.{k}", v, "code:daily", as_of).fact_id] = sym
        for i, it in enumerate(items[:8]):
            f = fs.add(f"{sym}.headline[{i}]", it["title"], f"{it['source']} ({it['publisher']})",
                       date.fromisoformat(it["published"]), url=it["link"])
            owner[f.fact_id] = sym
            if it.get("klass"):
                owner[fs.add(f"{sym}.headline[{i}].class", f"{it['klass']}, {it.get('direction') or '?'}",
                             "agent:DC-14", as_of).fact_id] = sym
    return fs, owner


def summarize(ctx: RunContext, runner: Any, recent: dict[str, list[dict]], held: dict, watch: dict) -> dict[str, str]:
    """DC-28: one call for all the stocks with headlines; the sentences that pass the check, per stock."""
    from qt.research.article import check_sentence
    fs, owner = _summary_facts(recent, held, watch, ctx.as_of)
    try:
        r = runner.decide("A5", "DC-28", fs, {"symbols": list(recent), "write_in": "Hebrew"},
                          run_id=ctx.run_id, step="news", subject="news_summary")
    except Exception as ex:                                          # noqa: BLE001 - budget or API: headlines only
        ctx.event(f"DC-28 {type(ex).__name__}: {ex}", "warn", "news")
        return {}
    out: dict[str, str] = {}
    for st in r.output.get("stocks") or []:
        sym = st.get("symbol")
        if sym not in recent:
            continue
        kept = []
        for sn in st.get("sentences") or []:
            why = check_sentence(sn, fs) or (None if any(owner.get(f) == sym for f in sn.get("fact_ids") or [])
                                             else "cites no headline of its stock")
            if why:
                ctx.event(f"{sym}: DC-28 sentence dropped ({why}): {sn.get('text', '')[:80]}", "info", "news")
            else:
                kept.append(sn["text"].strip())
        if kept:
            out[sym] = " ".join(kept)
    return out


def schema_extra() -> dict:
    """DC-28's own fields (cards.yaml points here)."""
    from qt.research.article import SENTENCE
    return {"stocks": {"type": "array", "description": "one entry per symbol in world.symbols", "items": {
        "type": "object", "properties": {
            "symbol": {"type": "string"},
            "sentences": {"type": "array", "items": SENTENCE,
                          "description": "1-3: what is happening with the stock by its headlines, then what it means "
                                         "for the position or the idea"}},
        "required": ["symbol", "sentences"], "additionalProperties": False}}}


def peer_notes(items: list[dict], entries: list, etf_of: dict[str, str | None]) -> dict[str, list[str]]:
    """S08-J25: a strong event at a peer as a C note on each same-ETF (or named-peer) watchlist entry."""
    from qt.watchlist import classify
    cls = classify("competitor_results", on_list=True).cls             # C: recorded, never acted on (S08-R18)
    out: dict[str, list[str]] = {}
    for it in items:
        k = it.get("klass")
        if not (k in STRONG or (k == "C" and RESULTS_WORDS.search(it.get("title") or ""))):
            continue
        sym, etf = it["symbol"], etf_of.get(it["symbol"])
        for e in entries:
            if e.symbol == sym or not ((etf and e.etf == etf) or sym in (e.peers or [])):
                continue
            note = f"[{cls}] דיווח/אירוע אצל מתחרה: {sym} {it['title']}"
            if note not in out.get(e.symbol, []):
                out.setdefault(e.symbol, []).append(note)
    return out


DIR_HE = {"for_idea": "בעד", "against_idea": "נגד"}


def _tag(it: dict) -> str:
    if not it.get("klass"):
        return ""
    return (f"[{it['klass']}, {DIR_HE.get(it.get('direction'), '?')}" + (", מתומחר" if it.get("priced_in") else "") + "] ")


def _h(t: Any) -> str:
    return html.escape(str(t), quote=False)


def digest_lines(out: dict) -> list[str]:
    """The per-stock digest (not from course: Ilay 27.10, 28.10), in Telegram HTML: every position and watchlist
    stock with A5's summary (DC-28) and its new headlines as links, strongest first, and one line naming the stocks
    with nothing new. Sent after the day summary."""
    if not out or not out.get("enabled") or not out.get("symbols"):
        return []
    by: dict[str, list[dict]] = {}
    for it in out["items"]:
        by.setdefault(it["symbol"], []).append(it)
    lines, quiet = ["📰 <b>חדשות לפי מניה</b> (סיכום של 3 הימים האחרונים, וכותרות שלא הוצגו קודם)"], []
    summ = out.get("summaries") or {}
    for s in out["symbols"]:
        got = sorted(by.get(s["symbol"], []), key=lambda x: (RANK.get(x.get("klass"), 6), x["published"]))
        if not got and not summ.get(s["symbol"]):
            quiet.append(s["symbol"])
            continue
        lines.append("")
        lines.append(f"<b>{_h(s['symbol'])}</b> ({'בתיק' if s['held'] else 'ברשימה'}) · {len(got)} כותרות חדשות")
        if summ.get(s["symbol"]):
            lines.append(f"📝 {_h(summ[s['symbol']])}")
        for it in got[:PER_SYMBOL]:
            lines.append(f"• {_h(_tag(it))}<a href=\"{html.escape(it['link'])}\">{_h(it['title'])}</a>"
                         f" ({_h(it['publisher'])}, {it['published'][5:]})")
        if len(got) > PER_SYMBOL:
            lines.append(f"• ועוד {len(got) - PER_SYMBOL} בדשבורד")
    if quiet:
        lines += ["", "בלי כותרות חדשות: " + _h(", ".join(quiet))]
    return lines


def chunks(lines: list[str], limit: int = 4000) -> list[str]:
    """Lines joined into messages under Telegram's 4096 characters, never splitting a line."""
    out, cur = [], ""
    for ln in lines:
        if cur and len(cur) + len(ln) + 1 > limit:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{ln}" if cur else ln[:limit]
    return out + ([cur] if cur else [])


def diary_lines(out: dict) -> list[str]:
    """The news diary for the end-of-day summary."""
    if not out or not out.get("enabled"):
        return []
    if not out["items"]:
        return ["יומן חדשות: אין כותרות חדשות על הפוזיציות והרשימה"]
    lines = ["יומן חדשות:"]
    for it in out["diary"]:
        lines.append(f"• {it['symbol']} {_tag(it)}{it['title']} ({it['publisher']})")
    for sym, what in out.get("adverse", {}).items():
        lines.append(f"⚠️ {sym}: ידיעה נגד הפוזיציה ({what}), עוברת ל-A7")
    for sym, what in out.get("watch_flags", {}).items():
        lines.append(f"⚠️ {sym} ברשימה: ידיעה נגד הרעיון ({what}), לבדוק שובר תזה")
    for sym, notes in (out.get("peer_notes") or {}).items():
        lines += [f"• {sym}: {n} (S08-J25)" for n in notes]
    return lines
