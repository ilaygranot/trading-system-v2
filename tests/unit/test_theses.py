"""The thesis store (qt.theses, phase A): the research's chain kept as standing theses with versions, filled from
the weekly dossier and the watchlist, and shown read-only on the dashboard."""
from __future__ import annotations

import copy
import json
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
    assert sync_dossier(s, DOSSIER) == {"macro": 1, "sector": 11, "company": 3, "idea": 2}
    assert sync_dossier(s, DOSSIER) == dict.fromkeys(("macro", "sector", "company", "idea"), 0)
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
