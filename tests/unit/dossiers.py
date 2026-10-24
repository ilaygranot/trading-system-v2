"""Dossiers for the tests. The 20.10 fixture was judged by hand in a session, not by the agents: it lacks what the
production cards write (heat, the article, the data check), so on its own it is a partial report. complete() fills
those parts the way the agents' pipeline does, for the tests of what happens after a finished report."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from qt.research.article import SECTION_KEYS
from qt.research.judge import JUDGED_BY

FIX = Path(__file__).parent.parent / "fixtures" / "research_dossier.json"


def fixture() -> dict:
    return json.loads(FIX.read_text(encoding="utf-8"))


def article(sym: str) -> dict:
    s = {"text": f"{sym}: משפט שעבר את בדיקת הקוד.", "notes": [1], "quotes": []}
    return {"headline": s, "dek": s, "sections": {k: {"answer": "yes", "paragraphs": [[s]]} for k in SECTION_KEYS},
            "notes": [{"n": 1, "fact_id": "F001", "key": f"{sym}.row.price", "value": "1", "source": "קוד", "as_of": "2026-10-20",
                       "url": None}], "dropped": [], "stats": {"kept": len(SECTION_KEYS) + 2, "dropped": 0, "facts": 10},
            "status": "decided"}


def complete(d: dict | None = None) -> dict:
    d = copy.deepcopy(d or fixture())
    d["meta"]["judged_by"] = JUDGED_BY
    J = d.setdefault("judgments", {})
    J["A1"].update(story=["השוק בשבוע."], fed={"line": "הפד."}, events=[{"date": d["meta"]["as_of"], "title": "אירוע", "text": "מה קרה"}])
    J["A3"] = {"candidates": len(d["ideas"]), "cap": 12, "mode": "all", "picked": list(d["ideas"]), "skipped": {}}
    for sym, i in d["ideas"].items():
        j = i["judgment"]
        j.setdefault("thesis", j.get("story") or "תזה")
        j.setdefault("breakers", [{"text": "שובר תזה", "kind": "fundamental", "check": "מה בודקים"}])
        j.setdefault("heat", "hot" if j.get("decision") in ("long", "short") else "no" if j.get("decision") == "no" else "cold")
        j["article"] = article(sym)
    for f in d["funnel"]:
        if f["stage"] == "deep_dive":
            f["n"] = len(d["ideas"])
    d["verification"] = {"checks": [], "summary": {"total": {"ok": 0, "single": 0, "bad": 0}}, "sec": True,
                         "second_price": True, "fetched_pages": True}
    return d


def research_services(db) -> dict:
    """What the weekly research needs to run in a test: the agents (answered by the card client), FRED, the monthly
    macro file, and a research that returns a finished report. Without them the weekly run fails (Ilay 24.10)."""
    from qt.agents import AgentRunner
    from qt.approvals import Queue
    from test_judge import CardClient
    Queue(db).upload("manual_macro", "ism.yaml", "ism_pmi: {'2026-09': 49}\n")
    return {"agents": AgentRunner(db, CardClient()), "fred": object(),
            "research_run": lambda as_of, judge, manual_text, fred: complete()}
