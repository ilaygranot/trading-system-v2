"""The daily news pass (S13-R22, S13-R27, S08): headlines on the positions and the watchlist, each new one classified
by A5 on DC-14 (class Macro Lead / Macro Noise / A-D, for or against the idea, already priced in).

A class A or B headline against an open position, not priced in, is an event for A7 (DC-21) in the agents step;
against a watchlist idea it is a flag in the summary (a possible thesis breaker, S08-R30). The day's news diary
(S13-R27: 3-5 events) is the strongest of the day's headlines. Without the agents the headlines are listed
unclassified. A headline is classified once: news_seen keeps every link already handled. A link first seen earlier the
same day (by the intraday watch, qt.intraday) comes back with its stored class for the day's diary and digest, and
does not raise an event again: the watch already acted on it.

SEC current reports (8-K, 8-K/A, 6-K; step `filings`) join the headlines as items of their own, so a company's own
filing is classified and can reach A7 like a headline (not from course: the course reads the news flow by hand).

Peers (S08-J25, code only): a strong headline (A/B, or a C about results) on one stock is recorded as a class C note
on every other watchlist entry of the same sector ETF, or that names it as a peer. A note, never an action
(S08-R18: C is recorded only because the stock is on the list); no extra DC-14 call.
"""
from __future__ import annotations

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
# DC-14 has no daily count cap (Ilay 08.11): positions first, then the watchlist, until the runner's daily budget.
BUDGET_STOPS = ("BudgetExceeded", "AgentUnavailable")
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


FILINGS_DAYS = 3                         # current reports this recent count as new, as the headlines (not from course)
FILINGS_PER_STOCK = 3


def filing_item(sym: str, f: dict) -> dict:
    """One SEC current report as a news item: the form, its items and the first exhibit's title as the headline."""
    what = ", ".join(n for i, n in zip(f["items"], f["item_names"]) if i != "9.01") or f.get("title") or f["kind"]
    ex = next((x["title"] for x in f.get("exhibits") or [] if x.get("title")), "")
    title = f"{f['form']}: {what}" + (f" · {ex}" if ex else "")
    return dict(title=title[:300], publisher="SEC", link=f["index_url"], published=f["date"], symbol=sym,
                source=f"sec:{f['form']}", kind=f["kind"], lead=next((x["lead"] for x in f.get("exhibits") or []
                                                                     if x.get("lead")), "")[:600])


def filings(ctx: RunContext, _: str) -> dict:
    """The SEC current reports of the positions and the watchlist (governance-only filings left out). Without
    SEC_CONTACT the step records that it is off; a failure on one stock is a warning, never a stop."""
    sec = ctx.services.get("sec")
    if sec is None or (hasattr(sec, "ok") and not sec.ok()):
        return {"enabled": False, "items": []}
    from qt.daily import _pf
    held = [p["symbol"] for p in _pf(ctx).get("positions", []) if p.get("instrument", "stock") == "stock"]
    syms = list(dict.fromkeys(held + [e.symbol for e in W.load(ctx.services.get("watchlist"))]))
    items, errors = [], {}
    for sym in syms:
        try:
            rows = sec.current_reports(sym, ctx.as_of, days=FILINGS_DAYS, max_n=FILINGS_PER_STOCK)
        except Exception as e:                                       # noqa: BLE001 - filings are never a blocker
            errors[sym] = f"{type(e).__name__}: {str(e)[:120]}"
            continue
        items += [filing_item(sym, f) for f in rows if f["kind"] != "governance"]
    if errors:
        ctx.event(f"SEC filings: {len(errors)} stocks failed", "warn", "filings", data=errors)
    return {"enabled": True, "items": items, "errors": errors}


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

    def add(it: dict, sym: str, src: str) -> None:
        if any(f["link"] == it["link"] for f in fresh):
            return
        row = ctx.db.one("SELECT seen_on, klass, direction, priced_in FROM news_seen WHERE link = ?", (it["link"],))
        if row is not None and row["seen_on"] != ctx.as_of.isoformat():
            return                                                    # handled on an earlier day
        known = {} if row is None else dict(known=True, klass=row["klass"], direction=row["direction"],
                                            priced_in=None if row["priced_in"] is None else bool(row["priced_in"]))
        fresh.append({**it, "symbol": sym, "held": sym in held, "source": src, **known})
    for sym in list(held) + list(watch):
        try:
            items, src = prov.news(sym)
        except Exception as e:                                       # noqa: BLE001 - news is never a blocker
            ctx.event(f"{sym}: news {type(e).__name__}: {e}", "warn", "news")
            continue
        for it in items:
            if not it["link"] or it["published"] < since or it["published"] > ctx.as_of.isoformat():
                continue
            add(it, sym, src)
    for it in (ctx.out("filings") or {}).get("items") or []:
        if it["symbol"] in held or it["symbol"] in watch:
            add(it, it["symbol"], it["source"])
    fresh.sort(key=lambda x: not x["held"])                          # positions first, before the budget can end
    runner = ctx.services.get("agents")
    calc = ctx.out("calc") or {}
    regime = (ctx.out("rules") or {}).get("regime")
    cut: list[str] = []                                              # headlines the budget left unclassified
    for it in fresh:
        sym = it["symbol"]
        if it.get("known"):
            continue                                                  # classified earlier today
        if runner is not None and cut:
            cut.append(sym)
        elif runner is not None:
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
                if type(ex).__name__ in BUDGET_STOPS:
                    cut.append(sym)
        ctx.db.insert_new("news_seen", dict(link=it["link"], symbol=sym, title=it["title"], published=it["published"],
                                            seen_on=ctx.as_of.isoformat(), klass=it.get("klass"),
                                            direction=it.get("direction"),
                                            priced_in=None if it.get("priced_in") is None else int(bool(it["priced_in"]))),
                          ["link"])
    against = [it for it in fresh if it.get("klass") in STRONG and it.get("direction") == "against_idea"
               and not it.get("priced_in") and not it.get("known")]
    adverse: dict[str, str] = {}
    flags: dict[str, str] = {}
    for it in against:                                  # every strong item, not only the last one per symbol
        d = adverse if it["held"] else flags
        line = f"{it['klass']}: {it['title']}"
        d[it["symbol"]] = f"{d[it['symbol']]} · {line}" if it["symbol"] in d else line
    diary = sorted(fresh, key=lambda x: (RANK.get(x.get("klass"), 6), not x["held"]))[:DIARY]
    etf_of = {s: e.etf for s, e in watch.items()} | {s: p.get("sector") for s, p in held.items()}
    if cut:
        ctx.event(f"agent budget reached: {len(cut)} headlines not classified", "warn", "news", data={"budget_cut": cut})
    return {"enabled": True, "classified": sum(1 for x in fresh if x.get("klass")), "items": fresh,
            "budget_cut": sorted(set(cut)),
            "symbols": [{"symbol": s, "held": s in held} for s in list(held) + list(watch)],
            "adverse": adverse, "watch_flags": flags, "diary": diary,
            "peer_notes": peer_notes(fresh, list(watch.values()), etf_of)}


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
        related = it.get("related") or []
        if related and related[0] != sym:
            # not from the course: Yahoo's search for one ticker returns headlines about others (30.10: "Chevron Q3
            # Earnings Beat" under SHEL went to every XLE name as a SHEL event). The first related ticker is the
            # headline's company; a headline about another company is not an event at this one
            continue
        for e in entries:
            if e.symbol == sym or e.symbol in related or not ((etf and e.etf == etf) or sym in (e.peers or [])):
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


def digest_lines(out: dict) -> list[str]:
    """The per-stock digest (not from course: Ilay 27.10): every position and watchlist stock with its new
    headlines, strongest first, and one line naming the stocks with nothing new. Sent after the day summary."""
    if not out or not out.get("enabled") or not out.get("symbols"):
        return []
    by: dict[str, list[dict]] = {}
    for it in out["items"]:
        by.setdefault(it["symbol"], []).append(it)
    lines, quiet = ["📰 חדשות לפי מניה (3 ימים אחרונים, רק כותרות שלא הוצגו קודם)"], []
    for s in out["symbols"]:
        got = sorted(by.get(s["symbol"], []), key=lambda x: (RANK.get(x.get("klass"), 6), x["published"]))
        if not got:
            quiet.append(s["symbol"])
            continue
        lines.append("")
        lines.append(f"{s['symbol']} ({'בתיק' if s['held'] else 'ברשימה'}) · {len(got)} כותרות")
        for it in got[:PER_SYMBOL]:
            lines.append(f"• {_tag(it)}{it['title']} ({it['publisher']}, {it['published'][5:]})")
        if len(got) > PER_SYMBOL:
            lines.append(f"• ועוד {len(got) - PER_SYMBOL} בדשבורד")
    if quiet:
        lines += ["", "בלי כותרות חדשות: " + ", ".join(quiet)]
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
    by_note: dict[str, list[str]] = {}                # one line per headline, not one per watchlist name (30.10: 34)
    for sym, notes in (out.get("peer_notes") or {}).items():
        for n in notes:
            by_note.setdefault(n, []).append(sym)
    lines += [f"• {n} ← {', '.join(syms)} (S08-J25)" for n, syms in by_note.items()]
    return lines
