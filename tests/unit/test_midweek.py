"""The midweek deep dive (qt.midweek, Ilay 09.11): the scan's new greens, up to 4 on Wednesday night, the weekly cap of
4 new ideas counted over the whole week, the watchlist and Telegram the same night."""
import json
import uuid
from datetime import date, datetime, timezone

import yaml

from qt import midweek as M
from qt.approvals import Queue
from qt.pipelines import PIPELINES
from qt.runs import SCHEMA, Runner

WED = date(2026, 10, 21)
SAT_AS_OF = "2026-10-16"                     # the Friday before the last Saturday run


def _radar(sym, light="green", fit="aligned", etf="XLE", rr=2.0):
    return dict(sym=sym, etf=etf, dir="long", light=light, fit=fit, rr=rr, on_list=False)


def _scan(db, as_of, new=(), upgraded=(), radar=()):
    db.ddl(SCHEMA)
    rid = f"scan-{as_of}-{uuid.uuid4().hex[:4]}"
    at = datetime(2026, 10, 1, tzinfo=timezone.utc).isoformat()
    db.insert("runs", dict(id=rid, kind="scan", as_of=as_of, status="done", started_at=at), returning=None)
    for seq, (step, out) in enumerate((("screen", {"radar": list(radar)}),
                                       ("compare", {"new": list(new), "upgraded": list(upgraded)}))):
        db.insert("run_steps", dict(run_id=rid, step=step, subject="", seq=seq, status="done", output=json.dumps(out)),
                  returning=None)


def _saturday(db, new_ideas=1):
    ideas = {f"S{n}": {"bucket": "enter"} for n in range(new_ideas)}
    ideas["BF"] = {"bucket": "conditional", "backfill": {"failed": []}}     # a watchlist backfill: not a new idea
    ideas["RJ"] = {"bucket": "reject"}
    Queue(db).upload("dossier", "dossier.json", json.dumps({"meta": {"as_of": SAT_AS_OF}, "ideas": ideas}))


def _ctx(db, services):
    from qt.runs import RunContext
    return RunContext(db=db, run_id="mid-1", kind="midweek", as_of=WED, services=services)


def test_the_queue_is_the_scans_new_greens_on_their_sectors_side_still_green_tonight(db):
    _saturday(db, new_ideas=1)
    _scan(db, "2026-10-15", new=[_radar("OLD")], radar=[_radar("OLD")])          # before Saturday's research
    _scan(db, "2026-10-19", new=[_radar("AAA"), _radar("ORG", light="orange"), _radar("NTR", fit="watch_only"),
                                 _radar("WL")], radar=[_radar("AAA")])
    _scan(db, "2026-10-20", upgraded=[_radar("BBB")], new=[_radar("GONE")], radar=[_radar("AAA"), _radar("BBB")])
    _scan(db, "2026-10-21", new=[_radar("CCC", rr=5.0)],
          radar=[_radar("CCC", rr=5.0), _radar("BBB", rr=3.0), _radar("AAA", rr=2.0), _radar("GONE", light="orange"),
                 _radar("WL"), _radar("DDD"), _radar("EEE")])
    _scan(db, "2026-10-20", new=[_radar("DDD"), _radar("EEE"), _radar("FFF")], radar=[])
    q = M.scan_candidates(db, date.fromisoformat(SAT_AS_OF), WED)
    assert [c["sym"] for c in q] == ["CCC", "BBB", "AAA", "WL", "DDD", "EEE"]   # tonight's order; GONE is orange now
    assert {c["sym"]: c["why"] for c in q}["BBB"] == "מכתום לירוק ב-2026-10-20"
    assert {c["sym"]: c["why"] for c in q}["AAA"] == "עלתה לירוק ב-2026-10-19"
    p = M.pick(_ctx(db, {"watchlist": {"entries": [{"symbol": "WL"}]}}), "pick")
    assert p["saturday_new"] == ["S0"] and p["cap"] == 4 and p["room"] == 3
    assert p["picked"] == ["CCC", "BBB", "AAA"] and p["waiting"] == ["DDD", "EEE"]   # WL is on the list already


def test_a_full_week_asks_the_agents_nothing_and_says_so(db):
    _saturday(db, new_ideas=4)
    _scan(db, "2026-10-20", new=[_radar("AAA")], radar=[_radar("AAA")])
    ctx = _ctx(db, {})
    p = M.pick(ctx, "pick")
    ctx.outputs["pick"] = {"": p}
    assert p["room"] == 0 and p["picked"] == [] and [c["sym"] for c in p["queue"]] == ["AAA"]
    r = M.research(ctx, "research")
    assert r == {"ran": False, "why": "full"}
    text = M.message(WED, p, r)
    assert "לא רץ" in text and "4 מתוך 4" in text and "AAA" in text
    empty = {**p, "queue": [], "room": 2}
    assert "אין מניות חדשות" in M.message(WED, empty, {"ran": False, "why": "empty"})


def test_without_a_saturday_report_this_week_the_whole_cap_is_free(db):
    Queue(db).upload("dossier", "old.json", json.dumps({"meta": {"as_of": "2026-10-02"}, "ideas": {"X": {"bucket": "enter"}}}))
    _scan(db, "2026-10-20", new=[_radar("AAA")], radar=[_radar("AAA")])
    p = M.pick(_ctx(db, {}), "pick")
    assert p["since"] == "2026-10-14" and p["room"] == 4 and p["picked"] == ["AAA"]


def test_the_judge_analyses_only_the_midweek_stocks_on_saturdays_a1():
    from dossiers import fixture
    from qt.research.judge import MIDWEEK_SKIP, Judge
    timing = fixture()["timing"]                       # TALO orange, CVX green, SHEL green, MAA red
    j = Judge(runner=None)
    j.only = ["CVX", "TALO"]
    j.prior_a1 = a1 = {"zone": "green", "story": ["השוק."], "fed": {"headline": "הפד."}, "events": [{"title": "x"}]}

    def no_macro(*a):
        raise AssertionError("A1 is not asked again midweek")
    j.macro = j.narrative = no_macro
    j._idea = lambda t, dd, m, a: ({"etf": t["etf"], "direction": t["dir"], "article": {"headline": "h"}}, {}, {})
    j.article = lambda *a: None
    J = j({"as_of": WED, "macro": {}, "timing": timing}, None)
    assert list(J["ideas"]) == ["CVX"] and J["A1"] is a1 and "incomplete" not in J
    assert J["A3"]["picked"] == ["CVX"] and J["A3"]["midweek"]
    assert J["A3"]["dropped"] == {"TALO": "כבר לא ירוקה ברמזור של ריצת המחקר"}
    assert J["A3"]["skipped"] == {"TALO": MIDWEEK_SKIP, "SHEL": MIDWEEK_SKIP}   # every other green is explained


def test_the_midweek_run_end_to_end_watchlist_cap_and_telegram(db, monkeypatch):
    from dossiers import complete
    from qt.agents import AgentRunner
    from qt.agents.runner import config
    from test_judge import CardClient
    from test_weekly import Bot, _entry
    monkeypatch.setenv("QT_PUBLIC_URL", "qt.example")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    _saturday(db, new_ideas=3)                                  # one new idea left this week
    _scan(db, "2026-10-20", new=[_radar("SHEL"), _radar("TALO")], radar=[_radar("SHEL", rr=3), _radar("TALO")])
    Queue(db).upload("manual_macro", "ism.yaml", "ism_pmi: {'2026-09': 49}\n")
    seen = {}

    def fake(as_of, judge, manual_text, fred):
        seen.update(only=judge.only, limit=judge.runner.limit_usd, state=judge.state.as_of, a1=judge.prior_a1)
        d = complete()                                          # SHEL enter, TALO conditional, MAA reject
        d["meta"]["as_of"] = as_of.isoformat()
        return d

    bot = Bot()
    runner = AgentRunner(db, CardClient())
    old = {"entries": [vars(_entry(symbol="KEEP", added="2026-09-01"))]}
    svc = {"agents": runner, "fred": object(), "research_run": fake, "telegram": bot, "watchlist": old,
           "portfolio": {"cash": 1000, "positions": []}}
    rid = Runner(db, PIPELINES, svc).start("midweek", WED)
    assert seen["only"] == ["SHEL"] and seen["limit"] == config()["budget"]["midweek_research_usd"]
    assert seen["state"] == "2026-10-21:midweek" and runner.limit_usd is None
    out = db.one("SELECT output FROM run_steps WHERE run_id=? AND step='research'", (rid,), ["output"])["output"]
    assert out["ran"] and Queue(db).latest_upload("midweek_report")["id"] == out["report_id"]
    assert Queue(db).latest_upload("dossier")["name"] == "dossier.json"          # Saturday's report stays the newest
    assert json.loads(Queue(db).latest_upload("midweek_dossier")["content"])["meta"]["midweek"]["room"] == 1
    wl = {e["symbol"]: e for e in yaml.safe_load(Queue(db).latest_upload("watchlist")["content"])["entries"]}
    assert set(wl) == {"KEEP", "SHEL", "TALO"}                  # not CVX: a green the run skipped is Saturday's
    assert wl["SHEL"]["bucket"] == "enter"
    assert wl["TALO"]["bucket"] == "watch" and "תקרת 4" in wl["TALO"]["why_not"]  # above the week's room
    text = bot.sent[-1]
    assert text.startswith("🔎 מחקר אמצע השבוע 2026-10-21") and "3 מתוך 4" in text and "https://qt.example/report/" in text
    assert "• SHEL (עלתה לירוק ב-2026-10-20): מוכנה לכניסה" in text and "מעבר לתקרה השבועית" in text
    assert "שום כניסה בלי האישור שלך" in text
    # the next Saturday does not analyse them again (qt.weekly.analysed reads the midweek report too)
    from qt.weekly import analysed
    assert set(analysed(db, ["SHEL", "TALO", "CVX"], date(2026, 10, 23))) == {"SHEL", "TALO"}


def test_midweek_is_due_on_wednesday_night_new_york_only():
    from qt.schedule import due, next_run
    assert due("midweek", datetime(2026, 10, 22, 0, 5, tzinfo=timezone.utc)) == (True, WED)    # 20:05 EDT Wed
    assert not due("midweek", datetime(2026, 10, 22, 1, 5, tzinfo=timezone.utc))[0]            # the other firing
    assert due("midweek", datetime(2026, 11, 12, 1, 5, tzinfo=timezone.utc))[0]                # 20:05 EST
    assert not due("midweek", datetime(2026, 10, 23, 0, 5, tzinfo=timezone.utc))[0]            # Thursday
    n = next_run("midweek", datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc))
    assert n.weekday() == 2 and (n.hour, n.minute) == (20, 0)
