"""The end-of-week review (qt.weekly, S13-R21): sector arena, old ideas, the week in the book, the message."""
from datetime import date, datetime, timedelta, timezone

from qt import book as B
from qt import watch as W
from qt import weekly as WK
from qt.calc.volatility import Bar
from qt.pipelines import PIPELINES
from qt.runs import Runner

AS_OF = date(2026, 10, 23)                      # a Friday


def _series(growth: float, start: float = 100.0, end: date = AS_OF, days: int = 400) -> list[Bar]:
    """Weekday bars with a constant daily growth rate (close = open; a 1% range)."""
    out, d, px = [], end - timedelta(days=days), start
    while d <= end:
        if d.weekday() < 5:
            out.append(Bar(d, px, px * 1.005, px * 0.995, px, 1))
            px *= 1 + growth
        d += timedelta(days=1)
    return out


GROWTH = {"SPY": 0.0005, "XLK": 0.002, "XLU": -0.001, "^VIX": 0.0}


class Prices:
    def __init__(self, growth=None):
        self.growth = {**GROWTH, **(growth or {})}

    def daily_bars(self, symbol, start, end):
        return [b for b in _series(self.growth.get(symbol, 0.0005), end=end) if b.day >= start], "fake"


def _json(bars):
    return [[b.day.isoformat(), b.open, b.high, b.low, b.close] for b in bars]


def _entry(**kw):
    base = dict(symbol="ABC", direction="long", etf="XLK", bucket="conditional", added="2026-10-20",
                trigger_kind="close_above", trigger_level=200.0, k=4.0, target=260.0, target_source="t",
                invalidate_level=None, time_stop="2026-11-17", next_earnings="2026-10-29", price_at_add=150.0)
    base.update(kw)
    return W.WatchEntry(**base)


# ------------------------------------------------------------------ arena

def test_arena_ranks_strongest_sector_highest_and_shows_the_move():
    bars = {s: _json(_series(g)) for s, g in GROWTH.items()}
    bars["XLE"] = _json(_series(0.0005))
    rows = WK.sector_arena(bars, prev={"XLK": 1, "XLU": 3})
    by = {r["etf"]: r for r in rows}
    assert [r["etf"] for r in rows] == ["XLK", "XLE", "XLU"]          # sorted strongest first
    assert by["XLK"]["rank"] == 3 and by["XLU"]["rank"] == 1          # n..1 over the sectors present (S04-R12)
    assert by["XLK"]["1w"] > 0 > by["XLU"]["1w"] and by["XLK"]["1y"] is not None
    assert by["XLK"]["rank_change"] == 2 and by["XLU"]["rank_change"] == -2 and by["XLE"]["rank_change"] is None
    assert WK.sector_arena({"XLK": bars["XLK"]}) == []                # no benchmark: no arena


# ------------------------------------------------------------------ old ideas

def test_revisit_proposes_archive_and_review_but_keeps_the_rest():
    up = _json(_series(0.0015, start=150))                             # ends ~ 230: above the 200 trigger
    flat = _json(_series(0.0, start=150))
    entries = [
        _entry(symbol="KEEP"),
        _entry(symbol="GONE", invalidate_level=160.0),                 # closes 150 < 160 before triggering
        _entry(symbol="OLD", added="2026-08-01"),                      # 83 days on the list
        _entry(symbol="LATE", time_stop="2026-10-01"),                 # trigger window over, never triggered
        _entry(symbol="NODATA"),
    ]
    bars = {"KEEP": flat, "GONE": flat, "OLD": flat, "LATE": flat}
    rows = {r["symbol"]: r for r in WK.revisit_entries(entries, bars, AS_OF)}
    assert rows["KEEP"]["propose"] == "keep" and not rows["KEEP"]["reasons"]
    assert rows["GONE"]["propose"] == "archive" and rows["GONE"]["reasons"][0] == "הטריגר בוטל"
    assert rows["LATE"]["propose"] == "archive" and rows["LATE"]["status"] == "expired"
    assert rows["OLD"]["propose"] == "review" and "S08-R29" in rows["OLD"]["reasons"][0]
    assert rows["NODATA"]["status"] == "stale"
    trig = WK.revisit_entries([_entry(symbol="UP", time_stop="2026-10-01")], {"UP": up}, AS_OF)[0]
    assert trig["status"] != "expired" and trig["propose"] != "archive"   # a fired trigger never expires


def test_revisit_flags_potential_halved():
    near = _json(_series(0.0, start=245))                             # target 260 from 245: 6% left vs 73% at add
    row = WK.revisit_entries([_entry(trigger_kind="now", trigger_level=None)], {"ABC": near}, AS_OF)[0]
    assert row["propose"] == "review" and "S08-J1" in row["reasons"][-1]


# ------------------------------------------------------------------ the week in the book

def test_week_book_counts_the_week_only_and_finds_what_hurts():
    pf = {"cash": 0, "positions": [
        {"symbol": "UP", "direction": "long", "qty": 10, "entry": 100.0, "stop": 90.0},
        {"symbol": "DOWN", "direction": "long", "qty": 10, "entry": 150.0, "stop": 120.0},
        {"symbol": "SH", "direction": "short", "qty": 10, "entry": 160.0, "stop": 170.0}],
        "realized": [{"symbol": "X", "qty": 5, "price": 1, "pnl": 300.0, "on": "2026-10-21"},
                     {"symbol": "Y", "qty": 5, "price": 1, "pnl": -100.0, "on": "2026-10-10"}]}
    bars = {"UP": _json(_series(0.002)), "DOWN": _json(_series(-0.002, start=200)),
            "SH": _json(_series(-0.002, start=200))}
    fills = [{"symbol": "UP", "kind": "enter", "qty": 10, "fill_on": "2026-10-20", "fill_price": 100.0},
             {"symbol": "X", "kind": "sell", "qty": 5, "fill_on": "2026-10-21", "fill_price": 1.0}]
    entries = [_entry(symbol="UP", next_earnings="2026-10-29", time_stop="2026-12-01"),
               _entry(symbol="DOWN", next_earnings="2026-11-20", time_stop="2026-11-05")]
    w = WK.week_book(pf, fills, bars, entries, AS_OF)
    assert [f["symbol"] for f in w["entries"]] == ["UP"] and [f["symbol"] for f in w["exits"]] == ["X"]
    assert w["realized_pnl"] == 300.0                                  # 10.10 is outside the week
    pos = {p["symbol"]: p for p in w["positions"]}
    assert pos["UP"]["week_pct"] > 0 and not pos["UP"]["hurting"]
    assert pos["DOWN"]["week_pct"] < 0 and pos["DOWN"]["hurting"]
    assert pos["SH"]["week_pct"] > 0 and not pos["SH"]["hurting"]       # a falling short gains (direction sign)
    assert w["hurting"] == ["DOWN"]
    assert w["ahead"] == [{"date": "2026-10-29", "symbol": "UP", "kind": "earnings"},
                          {"date": "2026-11-05", "symbol": "DOWN", "kind": "time_stop"}]   # 20.11 is past 14 days


def test_partial_and_full_sells_are_realized_with_their_date():
    pf = {"cash": 0.0, "positions": [{"symbol": "Z", "direction": "long", "qty": 10, "entry": 100.0, "stop": 90.0}]}
    pf, _ = B.apply_fill(pf, "sell", "Z", 5, 120.0, {"rule_id": "S12-R13", "as_of": "2026-10-21"})
    pf, _ = B.apply_fill(pf, "exit", "Z", 5, 110.0, {"rule_id": "S12-R2", "as_of": "2026-10-22"})
    assert [(r["pnl"], r["on"]) for r in pf["realized"]] == [(100.0, "2026-10-21"), (50.0, "2026-10-22")]
    assert len(pf["closed"]) == 1


# ------------------------------------------------------------------ the run

class Bot:
    def __init__(self):
        self.sent = []

    def send(self, text, buttons=None, force_reply=False):
        self.sent.append(text)
        return len(self.sent)


def _svc(bot, watchlist):
    pf = {"cash": 90_000, "peak_equity": 100_000, "positions": [
        {"symbol": "XLE", "direction": "long", "qty": 100, "entry": 110.0, "stop": 95.0}]}
    return {"prices": Prices(), "portfolio": pf, "watchlist": watchlist, "telegram": bot,
            "now": lambda: datetime(2026, 10, 24, 14, 0, tzinfo=timezone.utc)}


def test_weekly_run_end_to_end_and_rank_moves_from_last_week(db):
    wl = {"entries": [vars(_entry(symbol="ABC", added="2026-10-12"))]}
    bot = Bot()
    r = Runner(db, PIPELINES, _svc(bot, wl))
    r.start("weekly", AS_OF - timedelta(days=7))
    rid = r.start("weekly", AS_OF)
    st = [x for x in r.status() if x["id"] == rid][0]
    assert [s["step"] for s in st["steps"]] == ["expire", "report", "wfetch", "arena", "revisit", "week", "research",
                                                    "learn", "notify"]
    assert bot.sent[-2].startswith("סיכום שבוע עד 2026-10-23") and bot.sent[-1].startswith("תפעול")
    text = bot.sent[-2] + bot.sent[-1]
    for s in ("זירת סקטורים", "XLK 11", "XLU 1", "פוזיציות:", "• XLE", "רשימת מעקב: 1 (1 לונג, 0 שורט)",
              "פחות מ-2 רעיונות חדשים", "שבועיים קדימה: 10-29 דוח ABC", "בהכנה החודשית", "תפעול", "לא נבדקו",
              "מחקר שבועי לא רץ: חסר Claude (ANTHROPIC_API_KEY או CLAUDE_CODE_OAUTH_TOKEN), FRED_API_KEY, קובץ המקרו החודשי"):
        assert s in text, s
    arena = db.one("SELECT output FROM run_steps WHERE run_id=? AND step='arena'", (rid,), ["output"])["output"]
    assert all(x["rank_change"] is not None for x in arena["rows"])   # last week's run is found


# ------------------------------------------------------------------ the weekly research by the agents

def test_merge_watchlist_keeps_old_entries_and_the_first_added_date():
    old = {"entries": [vars(_entry(symbol="OLD", added="2026-09-01")), vars(_entry(symbol="SAME", added="2026-09-15"))]}
    new = {"as_of": "2026-10-23", "entries": [vars(_entry(symbol="SAME", added="2026-10-23", trigger_level=210.0)),
                                              vars(_entry(symbol="NEW", added="2026-10-23"))]}
    by = {e["symbol"]: e for e in WK.merge_watchlist(old, new)["entries"]}
    assert set(by) == {"OLD", "SAME", "NEW"} and by["SAME"]["trigger_level"] == 210.0
    assert by["SAME"]["added"] == "2026-09-15" and by["NEW"]["added"] == "2026-10-23"
    assert {e["symbol"] for e in WK.merge_watchlist(None, new)["entries"]} == {"SAME", "NEW"}


def test_weekly_research_saves_report_updates_watchlist_and_links_it(db, monkeypatch):
    import copy
    import json as _json
    from pathlib import Path

    import yaml

    from qt.agents import AgentRunner
    from qt.approvals import Queue
    from qt.telegram import sign_report
    from qt.web import App
    from test_judge import CardClient

    fix = _json.loads((Path(__file__).parent.parent / "fixtures" / "research_dossier.json").read_text(encoding="utf-8"))
    monkeypatch.setenv("QT_PUBLIC_URL", "qt.example")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    runner = AgentRunner(db, CardClient())
    seen = {}

    def fake_research(as_of, judge, manual_text, fred):
        seen.update(as_of=as_of, manual=manual_text, limit=judge.runner.limit_usd, run_id=judge.run_id)
        d = copy.deepcopy(fix)
        d["judgments"]["agent_log"] = [{"card": "DC-10", "cost": 0.25}, {"card": "DC-13", "cost": 0.5}]
        d["judgments"]["errors"] = {"XYZ": "BudgetExceeded: agent budget $25 reached"}
        return d

    first = next(iter(fix["ideas"]))
    old_wl = {"entries": [vars(_entry(symbol="KEEP", added="2026-09-01")), vars(_entry(symbol=first, added="2026-09-10"))]}
    Queue(db).upload("manual_macro", "ism.yaml", "ism_pmi: {'2026-09': 49}\n")
    bot = Bot()
    svc = {**_svc(bot, old_wl), "agents": runner, "fred": object(), "research_run": fake_research}
    rid = Runner(db, PIPELINES, svc).start("weekly", AS_OF)
    assert seen["as_of"] == AS_OF and "ism_pmi" in seen["manual"] and seen["limit"] == 25 and seen["run_id"] == rid
    assert runner.limit_usd is None                                    # the daily cap is back after the run
    out = db.one("SELECT output FROM run_steps WHERE run_id=? AND step='research'", (rid,), ["output"])["output"]
    assert out["cost_usd"] == 0.75 and "XYZ" in out["errors"]
    wl = yaml.safe_load(Queue(db).latest_upload("watchlist")["content"])
    by = {e["symbol"]: e for e in wl["entries"]}
    assert "KEEP" in by and len(by) == out["watchlist_n"]
    rep = Queue(db).latest_upload("report")
    assert rep["id"] == out["report_id"] and rep["content"].startswith("<!doctype html>")
    text = bot.sent[-2]
    link = f"https://qt.example/report/{rep['id']}?sig={sign_report(rep['id'], 'pw')}"
    assert f"מחקר שבועי: {len(fix['ideas'])} רעיונות נבדקו · עלות הסוכנים 0.75$ · הדוח: {link}" in text
    assert "רשימת המעקב עודכנה" in text and "לא הושלמו: XYZ" in text
    for sym, i in fix["ideas"].items():
        if i["bucket"] in ("enter", "conditional"):
            assert f"• {sym} " in text
    # the report behind the signed link
    web = App(bot=bot, password="pw", webhook_secret="hook", resume=lambda r: None)
    web._db = db

    def call(path, query=""):
        got = {}
        body = web({"PATH_INFO": path, "REQUEST_METHOD": "GET", "QUERY_STRING": query},
                   lambda st, h: got.update(st=st))
        return got["st"], b"".join(body).decode()
    assert call(f"/report/{rep['id']}", "sig=bad")[0].startswith("401")
    st, html = call(f"/report/{rep['id']}", f"sig={sign_report(rep['id'], 'pw')}")
    assert st.startswith("200") and 'id="ideas"' in html
    assert call("/report/99999", f"sig={sign_report(99999, 'pw')}")[0].startswith("404")
