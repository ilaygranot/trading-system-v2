"""The live dashboard in the status page's design: the overview, runs, one run, and the system status page."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import yaml

from qt.approvals import Queue
from qt.runs import SCHEMA
from qt.schedule import next_run
from test_web_positions import PF, _call, web  # noqa: F401 - the fixture


def _runs(db):
    db.ddl(SCHEMA)
    db.insert("runs", dict(id="daily-2026-10-21", kind="daily", as_of="2026-10-21", status="failed",
                           started_at="2026-10-21T22:30:00+00:00", error="ProviderError: yahoo 429", resumes=0),
              returning=None)
    db.insert("runs", dict(id="daily-2026-10-22", kind="daily", as_of="2026-10-22", status="done",
                           started_at="2026-10-22T22:30:00+00:00", finished_at="2026-10-22T22:37:00+00:00", resumes=1),
              returning=None)
    db.insert("run_steps", dict(run_id="daily-2026-10-22", step="prices", subject="", seq=0, status="done",
                                started_at="2026-10-22T22:30:00+00:00", finished_at="2026-10-22T22:30:20+00:00",
                                attempts=1), returning=None)
    db.insert("run_events", dict(run_id="daily-2026-10-22", at="2026-10-22T22:31:00+00:00", level="warn",
                                 step="sentiment", message="COT חסר"))
    db.insert("agent_calls", dict(run_id="daily-2026-10-22", agent="A5", model="m", cost_usd=0.5, valid=1,
                                  created_at=datetime.now(timezone.utc).isoformat(timespec="seconds")))


def test_overview_has_every_part_and_refreshes(web, db):
    _runs(db)
    Queue(db).upload("portfolio", "portfolio.yaml", yaml.safe_dump(PF))
    Queue(db).upload("report", "report-2026-10-17.html", "<h1>r</h1>")
    Queue(db).add("daily-2026-10-22", date.today(), dict(kind="tighten_stop", rule_id="S11-R7", symbol="MSFT",
                                                         stop=301.0, detail="הסטופ עולה"), 330.0)
    st, _, page = _call(web, "/")
    assert st.startswith("200") and "setInterval" in page and "id=live" in page and "Rubik" in page
    for part in ("מחכה לך (1)", "פוזיציות פתוחות (2)", "ריצות", "עסקאות נייר", "התראות מהריצות", "COT חסר",
                 "החלטות אחרונות", "דוחות וקבצים", "report-2026-10-17.html", "עלות Agents", "0.50$", "לוח זמנים",
                 "IB Gateway", "/runs/daily-2026-10-21/resume", "הידוק סטופ"):
        assert part in page, part
    frag = _call(web, "/", query="fragment=1")[2]
    assert "<script" not in frag and "מחכה לך" in frag


def test_runs_and_one_run(web, db):
    _runs(db)
    page = _call(web, "/runs")[2]
    assert "2 הריצות האחרונות" in page and "נכשלה" in page and "הושלמה" in page
    page = _call(web, "/runs/daily-2026-10-22")[2]
    assert "שלבים (1)" in page and "prices" in page and "COT חסר" in page and "קריאות ל-Agents (1)" in page
    assert "אין ריצה כזאת" in _call(web, "/runs/nope")[2]
    assert _call(web, "/runs", auth=None)[0].startswith("401")


def test_system_page_carries_the_status_and_checks_itself(web, db, monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setenv("QT_START_EQUITY", "10000")
    page = _call(web, "/system")[2]
    for part in ("מה קרה עד עכשיו", "איך זה עובד", "מה נבנה", "מחכה להחלטה או לפעולה שלך", "סיכונים שכדאי לדעת",
                 "הצעדים הבאים", "10,000$ במזומן", "IBC"):
        assert part in page, part
    assert "<b>מפתח FRED (חינם)</b> <span class='pill bad'>חסר</span>" in page
    monkeypatch.setenv("FRED_API_KEY", "k")
    Queue(db).upload("portfolio", "portfolio.yaml", yaml.safe_dump(PF))
    page = _call(web, "/system")[2]
    assert "<b>מפתח FRED (חינם)</b> <span class='pill ok'>מוגדר</span>" in page and "נשלח" in page


def test_api_status(web, db):
    _runs(db)
    data = json.loads(_call(web, "/api/status")[2])
    assert data["runs"]["daily"]["state"] == ["ok", "הושלמה"] and data["costs"]["month"] == 0.5
    assert data["gateway"][0] is None                                   # IBKR_HOST not set in tests


def test_next_run_follows_new_york_time():
    now = datetime(2026, 10, 22, 19, 40, tzinfo=timezone.utc)          # Thursday
    assert next_run("daily", now).isoformat() == "2026-10-22T18:30:00-04:00"
    assert next_run("weekly", now).isoformat() == "2026-10-24T10:00:00-04:00"
    assert next_run("monthly", now).isoformat() == "2026-11-14T10:00:00-05:00"
