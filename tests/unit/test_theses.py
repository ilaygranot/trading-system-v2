"""The thesis store (qt.theses, phase A): the research's chain kept as standing theses with versions, filled from
the weekly dossier and the watchlist, and shown read-only on the dashboard."""
from __future__ import annotations

import copy
import json
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from qt.approvals import Queue
from qt.db import DB
from qt.theses import Store, backfill, macro_thesis, sync_dossier, sync_watchlist
from test_web_positions import _call, web  # noqa: F401 - the fixture

DOSSIER = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "research_dossier.json").read_text())


def _next_week(d: dict, **a1) -> dict:
    d = copy.deepcopy(d)
    d["meta"]["as_of"] = "2026-10-27"
    for card, choice in a1.items():
        d["judgments"]["A1"][card.replace("_", "-")]["choice"] = choice
    return d


def test_a_dossier_becomes_the_chain_and_a_second_sync_adds_nothing():
    s = Store(DB())
    assert sync_dossier(s, DOSSIER) == {"macro": 1, "sector": 11, "company": 3, "idea": 2, "due": [], "pending": []}
    assert sync_dossier(s, DOSSIER) == dict.fromkeys(("macro", "sector", "company", "idea"), 0) | {"due": [], "pending": []}
    chain = s.chain("SHEL:long")
    assert [(c["kind"], c["subject"], c["current"]) for c in chain] == [
        ("macro", "stance", True), ("sector", "XLE", True), ("company", "SHEL", True), ("idea", "SHEL:long", True)]
    shel = s.get("company", "SHEL")
    assert shel["stale_after"] == "2026-10-29"                     # the next report, not 90 days
    assert [p["kind"] for p in shel["pillars"]][:2] == ["estimates_surprises", "kpi"]   # DC-12's breakers
    assert all(p["status"] == "holding" and p["check_text"] for p in shel["pillars"])
    assert s.get("idea", "MAA:long") is None                       # rejected: a company thesis, no idea
    assert s.get("company", "MAA") is not None
    assert s.get("sector", "XLE")["body"]["side"] == "long" and s.get("sector", "XLP")["body"]["side"] == "short"


def test_a_macro_change_leaves_the_ideas_on_the_old_version_and_finds_them():
    s = Store(DB())
    sync_dossier(s, DOSSIER)
    week2 = _next_week(DOSSIER, DC_01="cold")
    s.put("macro", "stance", *macro_thesis(week2),
          as_of="2026-10-27", author="agents", source="test")
    m = s.get("macro", "stance")
    assert m["version"] == 2 and m["body"]["zone"] == "cold"
    assert next(p for p in m["pillars"] if p["key"] == "DC-02")["since_version"] == 1      # unchanged pillar kept
    assert next(p for p in m["pillars"] if p["key"] == "DC-01")["since_version"] == 2
    built_on_old = s.dependents("macro", "stance", version=1)
    assert {t["subject"] for t in built_on_old if t["kind"] == "idea"} == {"SHEL:long", "TALO:long"}
    assert len([t for t in built_on_old if t["kind"] == "sector"]) == 11
    chain = s.chain("SHEL:long")
    assert chain[0]["kind"] == "macro" and chain[0]["current"] is False and chain[0]["rested_on"] == 1
    assert "zone: hot → cold" in s.history("macro", "stance")[0]["change"]


def test_an_older_report_never_overwrites_a_newer_thesis_and_a_reject_archives_the_idea():
    s = Store(DB())
    week2 = _next_week(DOSSIER)
    week2["ideas"]["SHEL"]["bucket"] = "reject"
    sync_dossier(s, week2)
    assert s.get("idea", "SHEL:long") is None                      # never an idea: nothing to archive
    sync_dossier(s, DOSSIER)                                       # the week before, replayed late
    assert s.get("macro", "stance")["as_of"] == "2026-10-27" and s.get("macro", "stance")["version"] == 1
    s2 = Store(DB())
    sync_dossier(s2, DOSSIER)
    sync_dossier(s2, week2)
    assert s2.get("idea", "SHEL:long")["status"] == "archived"
    assert s2.get("idea", "SHEL:long")["body"]["bucket"] == "reject"


def test_watchlist_entries_without_research_get_a_thesis_by_code():
    s = Store(DB())
    sync_dossier(s, DOSSIER)
    wl = {"entries": [{"symbol": "SHEL", "direction": "long", "etf": "XLE"},
                      {"symbol": "GLD", "direction": "long", "etf": "XLB", "bucket": "watch", "added": "2026-10-15",
                       "tldr": "זהב בשיא", "breakers": ["הדולר מתחזק"], "trigger_kind": "close_above", "trigger_level": 250}]}
    assert sync_watchlist(s, wl, "2026-10-21") == 1
    g = s.get("idea", "GLD:long")
    assert g["body"]["line"] == "זהב בשיא" and g["pillars"][0]["claim"] == "הדולר מתחזק"
    assert {r["kind"] for r in g["rests_on"]} == {"macro", "sector"}
    assert s.history("idea", "GLD:long")[0]["author"] == "code"


def test_backfill_replays_every_saved_report_and_the_watchlist():
    db = DB()
    q = Queue(db)
    q.upload("dossier", "dossier-2026-10-20.json", json.dumps(DOSSIER, ensure_ascii=False))
    q.upload("dossier", "dossier-2026-10-27.json", json.dumps(_next_week(DOSSIER, DC_04="high"), ensure_ascii=False))
    q.upload("dossier", "broken.json", "{not json")
    q.upload("watchlist", "watchlist.yaml", yaml.safe_dump({"entries": [{"symbol": "AAPL", "direction": "long", "etf": "XLK"}]}))
    out = backfill(db)
    assert out["reports"] == 2 and out["from_watchlist"] == 1 and out["versions"]["macro"] == 2
    assert out["theses"] == {"macro": 1, "sector": 11, "company": 3, "idea": 3}
    assert backfill(db)["versions"] == dict.fromkeys(("macro", "sector", "company", "idea"), 0)


def test_the_dashboard_fills_the_store_on_first_visit_and_shows_the_chain(web, db):
    Queue(db).upload("dossier", "dossier-2026-10-20.json", json.dumps(DOSSIER, ensure_ascii=False))
    st, _, page = _call(web, "/theses")
    assert st.startswith("200") and "SHEL:long" in page and "XLE" in page and "/thesis/idea/SHEL%3Along" in page
    st, _, page = _call(web, "/thesis/idea/SHEL%3Along")
    assert st.startswith("200") and "השרשרת" in page and "/thesis/company/SHEL" in page and "העמודים" in page
    st, _, page = _call(web, "/thesis/company/SHEL")
    assert "הורדות תחזית" in page and "מחזיק" in page and "נשענות עליה" in page
    assert "אין תזה כזאת" in _call(web, "/thesis/company/NOPE")[2]
    assert "/theses" in _call(web, "/research")[2]


# ---------------------------------------------------------------------- phase B: evidence and pillar links
class PillarAgents:
    """DC-14: class C for the idea (never an adverse event by itself), but it breaks the company's first pillar."""
    def __init__(self):
        self.calls = []

    def decide(self, agent, card, fs, world, **kw):
        from types import SimpleNamespace as NS
        self.calls.append((card, kw.get("subject"), dict(world), fs))
        out = {"direction": "for_idea", "priced_in": False, "reasoning": "x", "what": "FAA עוצרת מסירות",
               "pillars": [{"key": "BA.pillar.company:b1", "effect": "breaks"}, {"key": "company:nope", "effect": "breaks"}]} \
            if card == "DC-14" else {}
        return NS(option="C" if card == "DC-14" else "hold", output=out, status="decided", decision_id=7, critic=None,
                  for_ilay="", cost_usd=0.0)


def test_a_headline_that_breaks_a_pillar_is_kept_as_evidence_and_reaches_a7(db):
    from datetime import timedelta
    from test_runs_daily import AS_OF, NewsPrices, _runner
    s = Store(db)
    s.put("company", "BA", {"name": "Boeing"}, [{"key": "b1", "claim": "המסירות של 737 ממשיכות", "kind": "kpi",
                                                 "check": "עצירת מסירות"},
                                                {"key": "b2", "claim": "התזרים חיובי", "kind": "kpi", "check": "FCF שלילי"}],
          as_of=AS_OF, author="agents")
    sent, ag = [], PillarAgents()
    r = _runner(db, prices=NewsPrices(), notifier=sent.append, agents=ag)
    r.start("daily", AS_OF)
    dc14 = next(c for c in ag.calls if c[0] == "DC-14")
    assert any(f.key == "BA.pillar.company:b1" and "שובר תזה" in str(f.value) and "עצירת מסירות" in str(f.value) for f in dc14[3]._facts.values())
    ba = {p["key"]: p for p in s.get("company", "BA")["pillars"]}
    assert ba["b1"]["status"] == "broken" and ba["b2"]["status"] == "holding"
    ev = s.evidence_for(ba["b1"]["id"])
    assert len(ev) == 1 and ev[0]["effect"] == "breaks" and ev[0]["url"] == "https://x/1" and ev[0]["summary"] == "FAA עוצרת מסירות"
    dc21 = [c for c in ag.calls if c[0] == "DC-21"]
    assert len(dc21) == 1 and "עמוד בתזה נשבר: המסירות של 737 ממשיכות" in dc21[0][2]["event"]   # C alone never asks A7
    assert any("🧱 BA" in m for m in sent)
    # phase C: A7 weighs the event against the standing thesis (DC-21's first input)
    assert dc21[0][2]["thesis"]["company"]["pillars"][0]["status"] == "broken"
    assert any(f.key == "BA.pillar.company:b1" and str(f.value).startswith("[נשבר]") and "FAA" in str(f.value)
               for f in dc21[0][3]._facts.values())
    # a new version keeps the pillar row, so its evidence stays attached; supports never restores it
    s.put("company", "BA", {"name": "Boeing Co"}, [{"key": "b1", "claim": "המסירות של 737 ממשיכות", "kind": "kpi",
                                                    "check": "עצירת מסירות"}], as_of=AS_OF + timedelta(days=1), author="agents")
    b1 = s.get("company", "BA")["pillars"][0]
    assert b1["id"] == ba["b1"]["id"] and b1["status"] == "broken" and len(s.get("company", "BA")["pillars"]) == 1
    assert s.link(ev[0]["id"], b1, "supports", "agent:A5", None, AS_OF) is None
    assert s.get("company", "BA")["pillars"][0]["status"] == "broken"


def test_the_thesis_page_lists_each_pillars_evidence(web, db):
    s = Store(db)
    s.put("company", "BA", {"name": "Boeing"}, [{"key": "b1", "claim": "המסירות ממשיכות", "check": "עצירה"}],
          as_of="2026-10-20", author="agents")
    p = s.get("company", "BA")["pillars"][0]
    eid = s.add_evidence("BA", "8-K", "https://sec/1", "2026-10-21", "8-K: halt")
    assert s.add_evidence("BA", "8-K", "https://sec/1", "2026-10-21", "8-K: halt") == eid       # kept once
    assert s.link(eid, p, "weakens", "agent:A5", None, "2026-10-21") == "weakened"
    page = _call(web, "/thesis/company/BA")[2]
    assert "8-K: halt" in page and "מחלישה" in page and "נחלש" in page


# ---------------------------------------------------------------------- phase C: the agents work from the store
def _breakers(d: dict, sym: str, texts: list[str]) -> None:
    old = {b["text"]: b for b in d["ideas"][sym]["judgment"]["breakers"]}
    d["ideas"][sym]["judgment"]["breakers"] = [old.get(t) or {"kind": "kpi", "text": t, "check": "דוח"} for t in texts]


def test_a_reordered_week_keeps_each_pillar_its_key_and_an_addition_goes_straight_in():
    s = Store(DB())
    sync_dossier(s, DOSSIER, held=["SHEL"])
    keys = {p["claim"]: p["key"] for p in s.get("company", "SHEL")["pillars"]}
    week2 = _next_week(DOSSIER)
    texts = [b["text"] for b in DOSSIER["ideas"]["SHEL"]["judgment"]["breakers"]]
    _breakers(week2, "SHEL", list(reversed(texts)) + ["מחיר הגז האירופי יורד מתחת ל-30 אירו"])
    week2["ideas"]["SHEL"]["judgment"]["thesis_change"] = "נוסף שובר על הגז האירופי"
    out = sync_dossier(s, week2, held=["SHEL"])
    assert out["pending"] == [] and out["company"] == 1                  # an addition is not material
    shel = s.get("company", "SHEL")
    assert {p["claim"]: p["key"] for p in shel["pillars"] if p["claim"] in keys} == keys
    assert next(p for p in shel["pillars"] if "הגז" in p["claim"])["key"] == "b5"
    assert s.history("company", "SHEL")[0]["change"] == "נוסף שובר על הגז האירופי"     # A4's own line


def test_dropping_a_pillar_of_an_open_position_waits_for_ilay():
    db = DB()
    s, q = Store(db), Queue(db)
    sync_dossier(s, DOSSIER, held=["SHEL"])
    week2 = _next_week(DOSSIER)
    texts = [b["text"] for b in DOSSIER["ideas"]["SHEL"]["judgment"]["breakers"]]
    _breakers(week2, "SHEL", texts[1:])
    _breakers(week2, "TALO", ["ירידה בתפוקה"])                            # not held: goes in directly
    out = sync_dossier(s, week2, held=["SHEL"])
    assert [(p["kind"], p["subject"]) for p in out["pending"]] == [("company", "SHEL")]
    assert "עמוד הוסר: הורדות תחזית" in out["pending"][0]["what"]
    assert s.get("company", "SHEL")["version"] == 1 and len(s.get("company", "SHEL")["pillars"]) == 4
    from qt.thesis_page import thesis_html
    assert "מחכה לאישור שלך" in thesis_html(s, "company", "SHEL")
    assert s.get("company", "TALO")["version"] == 2
    iid = q.add("weekly-x", date(2026, 10, 27), dict(kind="thesis", rule_id="DC-12", symbol="SHEL", preapproved=False,
                                                    pending_id=out["pending"][0]["id"], detail="x"))
    assert q.get(iid)["status"] == "pending"
    q.decide(iid, "approve", now=datetime(2026, 10, 27, 12, tzinfo=timezone.utc))
    shel = s.get("company", "SHEL")
    assert shel["version"] == 2 and len(shel["pillars"]) == 3 and s.history("company", "SHEL")[0]["author"].endswith("+ilay")
    # a rejected change stays out
    week3 = _next_week(week2)
    week3["meta"]["as_of"] = "2026-11-03"
    _breakers(week3, "SHEL", texts[2:])
    pid = sync_dossier(s, week3, held=["SHEL"])["pending"][0]["id"]
    iid = q.add("weekly-y", date(2026, 11, 3), dict(kind="thesis", rule_id="DC-12", symbol="SHEL", pending_id=pid, detail="x"))
    q.decide(iid, "reject", now=datetime(2026, 11, 3, 12, tzinfo=timezone.utc))
    assert s.get("company", "SHEL")["version"] == 2 and s.pending(pid)[0]["status"] == "rejected"


def test_a_macro_change_marks_what_the_report_did_not_rewrite_as_due():
    s = Store(DB())
    sync_dossier(s, DOSSIER)
    week2 = _next_week(DOSSIER, DC_01="cold")
    del week2["ideas"]["TALO"]
    out = sync_dossier(s, week2)
    assert "idea:TALO:long" in out["due"] and "company:TALO" in out["due"] and "idea:SHEL:long" not in out["due"]
    assert s.get("idea", "TALO:long")["stale_after"] == "2026-10-27"
    assert s.behind(s.get("idea", "TALO:long"))


def test_a4_gets_the_standing_thesis_and_writes_what_changed(db):
    from qt.agents import AgentRunner
    from qt.research import judge as JG
    from test_judge import AS_OF, DEFAULTS, CardClient, _answer, _dc, _deep_dive, _timing, _world
    s = Store(db)
    sync_dossier(s, DOSSIER)
    p = s.get("company", "SHEL")["pillars"][0]
    s.link(s.add_evidence("SHEL", "8-K", "https://sec/9", "2026-10-22", "8-K: guidance cut"), p, "breaks", "agent:A5",
           None, "2026-10-22")
    brk = dict(DEFAULTS["DC-12"], change="שובר התחזיות התממש: הסיפור נבדק מחדש")
    client = CardClient({"DC-12": brk})
    jg = JG.Judge(AgentRunner(db, client), run_id="weekly-x")
    jg.theses = s
    j = jg.idea(_timing(1)[0] | {"sym": "SHEL", "etf": "XLE"}, _deep_dive([]), DOSSIER["macro"], AS_OF)
    msg = _dc(client, "DC-12")[0]
    w = _world(msg)
    assert w["prior_thesis"]["company"]["pillars"][0]["status"] == "broken" and "word for word" in w["prior_thesis"]["how"]
    assert "SHEL.pillar.company:b1" in msg and "[נשבר]" in msg and "8-K: guidance cut" in msg
    assert j["thesis_change"] == "שובר התחזיות התממש: הסיפור נבדק מחדש"
    # no store, or nothing on the stock: the cards are asked as before
    client = CardClient({"DC-12": _answer("DC-12", "breakers", thesis_breakers=[], change="x")})
    j = JG.Judge(AgentRunner(db, client), run_id="weekly-y").idea(_timing(1)[0] | {"sym": "SHEL"}, _deep_dive([]),
                                                                   DOSSIER["macro"], AS_OF)
    assert "prior_thesis" not in _world(_dc(client, "DC-12")[0]) and "thesis_change" not in j


def test_a_stance_change_at_night_writes_the_macro_thesis_and_marks_the_chain(db, monkeypatch, tmp_path):
    from datetime import date as D
    from qt.pipelines import PIPELINES
    from qt.research import macro_stage, sector_screen
    from qt.runs import Runner
    from test_scan import Bot, Y
    monkeypatch.setenv("QT_WORK_DIR", str(tmp_path))
    zone = {D(2026, 11, 5): "hot", D(2026, 11, 6): "cold"}
    monkeypatch.setattr(macro_stage, "run", lambda fred, manual, spx, as_of, br: dict(
        stage1=dict(credit=dict(state="calm"), curve=dict(shape="normal")),
        stage2=dict(stance=dict(zone=zone[as_of], bias="bullish", compass_now="goldilocks", activity=dict(pct=50.0), flags=[]),
                    scores=[dict(name="ism_pmi", cycle_quadrant="expansion")]),
        stage3=dict(market=dict(regime="bull", drawdown_pct=1.0))))
    monkeypatch.setattr(sector_screen, "run", lambda y, rows, phase, flags, zone, as_of: dict(
        scorecard={"XLE": {}}, long=["XLE"], short=[]))
    monkeypatch.setattr(sector_screen, "screens", lambda rows, longs, shorts: {e: dict(long=[], short=[]) for e in longs + shorts})
    s = Store(db)
    sync_dossier(s, DOSSIER)
    Queue(db).upload("manual_macro", "m.yaml", "ism_pmi: {}")
    bot = Bot()
    r = Runner(db, PIPELINES, dict(fred=object(), telegram=bot, research_yahoo=Y(), watchlist={"entries": []},
                                   scan_universe={"quotes": {"AAA": dict(price=10, ma200=9)}, "summaries": {}},
                                   scan_timing=lambda y, screens, as_of: []))
    r.start("scan", D(2026, 11, 5))
    r.start("scan", D(2026, 11, 6))
    m = s.get("macro", "stance")
    assert m["version"] == 2 and m["body"]["zone"] == "cold" and s.history("macro", "stance")[0]["author"] == "code"
    assert "שינוי בתמונת המקרו" in s.history("macro", "stance")[0]["change"]
    assert s.get("idea", "SHEL:long")["stale_after"] <= "2026-11-06" and s.behind(s.get("idea", "SHEL:long"))   # due now
    assert "🧱 תזת המקרו עודכנה לגרסה 2" in bot.sent[-1]


# ---------------------------------------------------------------------- phase D: the research looks again
def test_due_for_review_puts_positions_first_and_skips_rejected_stocks():
    from qt.theses import due_for_review
    s = Store(DB())
    sync_dossier(s, DOSSIER)
    assert due_for_review(s, "2026-10-23", held=["SHEL"]) == []                  # nothing moved yet
    p = s.get("company", "SHEL")["pillars"][1]
    s.link(s.add_evidence("SHEL", "news", "https://x/buyback", "2026-10-22", "buyback paused"), p, "weakens",
           "agent:A5", None, "2026-10-22")
    got = due_for_review(s, "2026-11-07", held=["SHEL"])                   # and TALO reported on 3.11
    assert [(r["sym"], r["revisit"], r["held"]) for r in got] == [("SHEL", ["pillar", "report"], True),
                                                                   ("TALO", ["report"], False)]
    assert got[0]["etf"] == "XLE" and got[0]["dir"] == "long"
    s.put("macro", "stance", *macro_thesis(_next_week(DOSSIER, DC_01="cold")), as_of="2026-10-27", author="agents")
    got = due_for_review(s, "2026-10-27", held=[], limit=5)
    assert {r["sym"] for r in got} == {"SHEL", "TALO"} and all("upstream" in r["revisit"] for r in got)   # not MAA
    assert len(due_for_review(s, "2026-10-27", limit=1)) == 1


def test_a_revisit_tells_a4_why_it_looks_again(db):
    from qt.agents import AgentRunner
    from qt.research import judge as JG
    from test_judge import AS_OF, CardClient, _dc, _deep_dive, _timing, _world
    client = CardClient()
    JG.Judge(AgentRunner(db, client), run_id="weekly-x").idea(
        _timing(1)[0] | {"sym": "SHEL", "backfill": True, "revisit": ["pillar"]}, _deep_dive([]), DOSSIER["macro"], AS_OF)
    w = _world(_dc(client, "DC-10")[0])
    assert "backfill" not in w and w["revisit"]["reasons"] == [JG.REVISIT_WHY["pillar"]]


def test_the_weekly_research_revisits_a_position_whose_pillar_weakened(db):
    from dossiers import complete
    from qt.agents import AgentRunner
    from qt.pipelines import PIPELINES
    from qt.runs import Runner
    from test_judge import CardClient
    from test_weekly import AS_OF, Bot, _svc
    s = Store(db)
    sync_dossier(s, DOSSIER)
    s.link(s.add_evidence("SHEL", "news", "https://x/b", "2026-10-22", "buyback paused"),
           s.get("company", "SHEL")["pillars"][1], "weakens", "agent:A5", None, "2026-10-22")
    Queue(db).upload("manual_macro", "ism.yaml", "ism_pmi: {'2026-09': 49}\n")
    seen = {}

    def research(as_of, judge, manual_text, fred):
        seen["wanted"] = [(w["sym"], w.get("revisit")) for w in judge.backfill_wanted]
        seen["store"] = judge.theses is not None
        return complete(DOSSIER)
    bot = Bot()
    svc = _svc(bot, None)
    svc["portfolio"]["positions"].append({"symbol": "SHEL", "direction": "long", "qty": 10, "entry": 30.0, "stop": 25.0})
    Runner(db, PIPELINES, {**svc, "agents": AgentRunner(db, CardClient()), "fred": object(), "research_run": research}).start("weekly", AS_OF)
    assert seen == {"wanted": [("SHEL", ["pillar"])], "store": True}
    assert any("🔁 SHEL (בתיק) נבדקה מחדש (עמוד נשבר או נחלש מאז הגרסה האחרונה): התזה נשארה כמו שהיא" in m for m in bot.sent)
