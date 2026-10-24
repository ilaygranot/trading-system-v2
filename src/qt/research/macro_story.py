"""The market story of the week (DC-27, A1): what is happening in plain words, the Fed's stance, and the events that
moved the charts, in the report's macro section (Ilay 24.10: a report without them is not a report).

The agents have no web tools, so the story stands on facts the code gathers:
  * the macro sheet (qt.research.judge.macro_facts: the stance, the scores, stages 1 and 3);
  * the chart series' largest moves, found by code (MOVES), each with its month or day;
  * headlines from Google News RSS (the queries in QUERIES, dated up to the run's day) and the Fed's press releases
    (its RSS), each a fact with its date, publisher and link;
  * A1's own choices this week (DC-01..04), as agent facts: they support words, never a number.

Every sentence goes through qt.research.article.check_sentence: it cites facts on the sheet, its numbers are in
them, its dates are theirs. An event is kept only with a kept sentence and a date its cited facts hold; its
sources are the links of the headlines it cites, set by code. What survives is the report's A1.story, A1.fed and
A1.events (qt.research.report._macro); what did not, and why, is in the narrative's dropped list.
"""
from __future__ import annotations

import html as H
import re
from datetime import date, timedelta
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import quote_plus

from qt.facts import FactSheet
from qt.research import article as AR

GNEWS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
FED_RSS = "https://www.federalreserve.gov/feeds/press_monetary.xml"
# what moves the course's macro picture (S01-S03): the Fed, inflation, jobs, activity, credit, oil, the market
QUERIES = (
    "Federal Reserve interest rate decision", "FOMC minutes", "CPI inflation report", "jobs report unemployment rate",
    "ISM manufacturing PMI", "consumer sentiment University of Michigan", "oil prices Brent", "Treasury yields",
    "stock market S&P 500", "high yield credit spreads",
)
DAYS_BACK = 120                # the window of the headlines (not from course: about one quarter and a month)
PER_QUERY = 8
CHARTS = ("ism_pmi", "ism_nmi", "small_business", "michigan", "permits", "cpi_yoy", "ffr", "unemployment", "hy",
          "curve", "spx")
MOVES = 2                      # the largest moves per series, by code
MIN_STORY, MIN_EVENTS = 2, 1   # below these the narrative is incomplete: the run fails and asks again

_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
_TAG = lambda tag: re.compile(rf"<{tag}[^>]*>(.*?)</{tag}>", re.S)  # noqa: E731
_TITLE, _LINK, _PUB, _SRC = _TAG("title"), _TAG("link"), _TAG("pubDate"), _TAG("source")


def _text(m: re.Match | None) -> str:
    return H.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1))).strip() if m else ""


def parse_rss(xml: str, default_source: str) -> list[dict]:
    out = []
    for it in _ITEM.findall(xml):
        title, link, pub = _text(_TITLE.search(it)), _text(_LINK.search(it)), _text(_PUB.search(it))
        try:
            day = parsedate_to_datetime(pub).date()
        except (TypeError, ValueError):
            continue
        src = _text(_SRC.search(it)) or default_source
        if src and title.endswith(f" - {src}"):
            title = title[: -len(src) - 3].strip()
        if title and link:
            out.append({"date": day.isoformat(), "title": title, "publisher": src, "url": link})
    return out


def fetch_news(as_of: date, get: Callable[[str], str] | None = None) -> dict:
    """{"items": [...], "errors": {...}}: the headlines of the window, newest first, one per title."""
    if get is None:
        from qt.data.macro_auto import Http
        get = Http().text
    after, before = as_of - timedelta(days=DAYS_BACK), as_of + timedelta(days=1)
    items, errors = [], {}
    for q in QUERIES:
        url = GNEWS.format(q=quote_plus(f"{q} after:{after.isoformat()} before:{before.isoformat()}"))
        try:
            got = parse_rss(get(url), "Google News")
        except Exception as e:                       # noqa: BLE001 - one query's failure is listed, the rest stand
            errors[q] = f"{type(e).__name__}: {e}"
            continue
        items += [dict(x, query=q) for x in got if after.isoformat() <= x["date"] <= as_of.isoformat()][:PER_QUERY]
    try:
        items += [dict(x, query="fed_press", publisher="Federal Reserve") for x in parse_rss(get(FED_RSS), "Federal Reserve")
                  if after.isoformat() <= x["date"] <= as_of.isoformat()][:PER_QUERY]
    except Exception as e:                           # noqa: BLE001
        errors["fed_press"] = f"{type(e).__name__}: {e}"
    seen, out = set(), []
    for x in sorted(items, key=lambda x: x["date"], reverse=True):
        k = x["title"].lower()
        if k not in seen:
            seen.add(k)
            out.append(x)
    return {"items": out, "errors": errors}


def moves(charts: dict) -> list[dict]:
    """The MOVES largest steps of each chart series (month to month; day series over 20 trading days), by code."""
    out = []
    for key in CHARTS:
        c = charts.get(key) or {}
        ds, vs = c.get("dates") or [], c.get("values") or []
        step = 20 if ds and len(ds[0]) == 10 else 1
        steps = [(ds[i], vs[i - step], vs[i]) for i in range(step, len(vs))
                 if vs[i] is not None and vs[i - step] is not None]
        steps.sort(key=lambda s: abs(s[2] - s[1]), reverse=True)
        picked: list[tuple] = []
        for s in steps:                                      # not two moves in the same month
            if all(s[0][:7] != p[0][:7] for p in picked):
                picked.append(s)
            if len(picked) == MOVES:
                break
        for d, a, b in picked:
            out.append({"chart": key, "date": d, "from": round(a, 2), "to": round(b, 2), "change": round(b - a, 2),
                        "pct": round((b / a - 1) * 100, 1) if a else None, "window": "20d" if step > 1 else "1m"})
    return out


def _day(s: str, default: date) -> date:
    try:
        return date.fromisoformat(s if len(s) == 10 else f"{s[:7]}-01")
    except ValueError:
        return default


def story_sheet(base: FactSheet, as_of: date, news: dict | None, mv: list[dict], a1: dict) -> tuple[FactSheet, dict]:
    """The macro sheet plus the moves, the headlines and A1's choices; and the headline behind each fact id."""
    fs = base
    for i, m in enumerate(mv):
        fs.add(f"macro.move.{m['chart']}[{i}]", {k: v for k, v in m.items() if k != "chart"}, "code:macro_story.moves",
               _day(m["date"], as_of))
    by_id: dict[str, dict] = {}
    for i, n in enumerate((news or {}).get("items") or []):
        f = fs.add(f"macro.news[{i}]", f"{n['date']} {n['publisher']}: {n['title']}", f"news:{n['publisher']}",
                   _day(n["date"], as_of), url=n["url"])
        by_id[f.fact_id] = n
    if (news or {}).get("errors"):
        fs.add("macro.news.errors", news["errors"], "code:macro_story.fetch_news", as_of)
    for card, v in (a1 or {}).items():
        if isinstance(v, dict) and v.get("choice"):
            fs.add(f"A1.{card}", f"{v['choice']}: {v.get('why', '')}", f"agent:{card}", as_of)
    return fs, by_id


def world(a1: dict) -> dict:
    return {"write_in": "Hebrew, plain words for a friend who does not trade",
            "a1_choices": {k: v.get("choice") for k, v in (a1 or {}).items() if isinstance(v, dict) and v.get("choice")},
            "charts": list(CHARTS),
            "story": "3-6 paragraphs: what the economy and the market are doing, why (the headlines), what it means "
                     "for the trades (the zone and the compass)",
            "fed": "the stance in one headline sentence, what came before and why, what is next",
            "events": "the 3-8 events that moved the charts, each dated by the facts it cites, with the charts it moved"}


_PARA = {"type": "object", "properties": {"sentences": {"type": "array", "items": AR.SENTENCE}},
         "required": ["sentences"], "additionalProperties": False}


def schema_extra() -> dict:
    """DC-27's own fields (cards.yaml points here)."""
    return {
        "story": {"type": "array", "items": _PARA, "description": "3-6 paragraphs"},
        "fed": {"type": "object", "properties": {
            "headline": AR.SENTENCE, "paragraphs": {"type": "array", "items": _PARA}, "next": AR.SENTENCE},
            "required": ["headline", "paragraphs", "next"], "additionalProperties": False},
        "events": {"type": "array", "description": "3-8, oldest first", "items": {"type": "object", "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD or YYYY-MM, a date of the facts the event cites"},
            "title": {"type": "string", "description": "Hebrew, a few words, no numbers"},
            "sentences": {"type": "array", "items": AR.SENTENCE},
            "charts": {"type": "array", "items": {"type": "string", "enum": list(CHARTS)}}},
            "required": ["date", "title", "sentences", "charts"], "additionalProperties": False}}}


def verify(out: dict, fs: FactSheet, by_id: dict[str, dict]) -> dict:
    """The narrative as the report keeps it: each sentence checked against its facts (article.check_sentence)."""
    dropped: list[dict] = []

    def keep(ss: list[dict], where: str) -> list[dict]:
        kept = []
        for s in ss or []:
            why = AR.check_sentence(s, fs)
            if why:
                dropped.append({"where": where, "text": s.get("text", ""), "why": why})
            else:
                kept.append(s)
        return kept

    def text(ss: list[dict]) -> str:
        return " ".join(s["text"].strip() for s in ss)

    def sources(ss: list[dict]) -> list[dict]:
        seen, got = set(), []
        for s in ss:
            for fid in s["fact_ids"]:
                n = by_id.get(fid)
                if n and n["url"] not in seen:
                    seen.add(n["url"])
                    got.append({"title": f"{n['publisher']}: {n['title']}", "url": n["url"]})
        return got

    story = [t for t in (text(keep(p.get("sentences"), "story")) for p in out.get("story") or []) if t]
    f = out.get("fed") or {}
    head = keep([f.get("headline") or {}], "fed.headline")
    paras = [keep(p.get("sentences"), "fed") for p in f.get("paragraphs") or []]
    nxt = keep([f.get("next") or {}], "fed.next")
    fed = {"headline": text(head), "text": [text(p) for p in paras if p], "next": text(nxt),
           "sources": sources(head + [s for p in paras for s in p] + nxt)}
    events = []
    for e in out.get("events") or []:
        ss = keep(e.get("sentences"), f"event {e.get('date')}")
        title = (e.get("title") or "").strip()
        if not ss or not title:
            continue
        d = str(e.get("date") or "")
        held = {f"{y:04d}-{m:02d}" for y, m, _ in AR.fact_dates(fs, [i for s in ss for i in s["fact_ids"]])}
        if d[:7] not in held:
            dropped.append({"where": f"event {d}", "text": title, "why": "the date is not a date of the cited facts"})
            continue
        if AR.numbers_in_text(AR.strip_dates(title, AR.fact_dates(fs, [i for s in ss for i in s["fact_ids"]]))):
            dropped.append({"where": f"event {d}", "text": title, "why": "a number in the title"})
            continue
        ev = {"date": d, "title": title, "text": text(ss), "charts": [c for c in e.get("charts") or [] if c in CHARTS],
              "sources": sources(ss)}
        if len(d) == 10:
            ev["chart_date"] = d[:7]
        events.append(ev)
    events.sort(key=lambda e: e["date"])
    return {"story": story, "fed": fed, "events": events, "dropped": dropped,
            "stats": {"story": len(story), "events": len(events), "dropped": len(dropped),
                      "headlines": len(by_id)}}


def complete(n: dict | None) -> bool:
    """Enough of the narrative for the report: a story, the Fed's headline and at least one event."""
    return bool(n) and len(n.get("story") or []) >= MIN_STORY and bool((n.get("fed") or {}).get("headline")) \
        and len(n.get("events") or []) >= MIN_EVENTS
