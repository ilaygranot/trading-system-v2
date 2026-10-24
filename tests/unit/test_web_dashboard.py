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


def test_every_part_of_the_old_overview_has_its_tab(web, db):
    """Ilay 24.10, the approved design: the one long overview became four tabs and the system's page; every part it
    had is still there, on the tab that now holds it."""
    _runs(db)
    Queue(db).upload("portfolio", "portfolio.yaml", yaml.safe_dump(PF))
    Queue(db).upload("report", "report-2026-10-17.html", "<h1>r</h1>")
    Queue(db).add("daily-2026-10-22", date.today(), dict(kind="tighten_stop", rule_id="S11-R7", symbol="MSFT",
                                                         stop=301.0, detail="הסטופ עולה"), 330.0)
    st, _, page = _call(web, "/")
    assert st.startswith("200") and "setInterval" in page and "data-live=queue" in page and "Frank+Ruhl" in page
    homes = {"/": ("מחכה לאישורך (1)", "הידוק סטופ", "/item/", "שווי התיק", "MSFT", "הריצות הבאות", "קרובות לטריגר",
                   "דוח המחקר"),
             "/system": ("ריצות", "התראות מהריצות", "COT חסר", "מה הריצות אמרו", "לוח זמנים", "IB Gateway", "עלות Agents",
                         "0.50$", "/runs/daily-2026-10-21/resume", "קבצים שהתקבלו", "portfolio.yaml",
                         "מחכה להחלטה או לפעולה שלך"),
             "/positions": ("פוזיציות פתוחות (2)", "עסקאות נייר", "החלטות אחרונות", "/journal"),
             "/research": ("כל הדוחות", "report-2026-10-17.html", "/report/")}
    for path, parts in homes.items():
        page = _call(web, path)[2]
        for part in parts:
            assert part in page, (path, part)
    for path in ("/", "/ideas", "/positions", "/research", "/system"):            # the four tabs on every page
        page = _call(web, path)[2]
        assert all(f"href='{t}'" in page for t in ("/", "/ideas", "/positions", "/research")), path
        assert "data-live=health" in page and "href='/system'" in page, path
    frag = _call(web, "/", query="fragment=1")[2]
    assert "<script" not in frag and "מחכה לאישורך" in frag and "data-live=health" in frag


def test_old_addresses_still_open(web, db):
    """Links made before the tabs: the pages keep their addresses, and the old overview's anchors move to their tab."""
    for path in ("/runs", "/system", "/positions", "/macro", "/sectors", "/journal", "/ideas", "/research", "/stock/SHEL"):
        assert _call(web, path)[0].startswith("200"), path
    page = _call(web, "/")[2]
    assert "watch:'/ideas'" in page and "'/research#'+h" in page
    for path in ("/ideas", "/research"):
        assert _call(web, path, auth=None)[0].startswith("401"), path


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


def test_the_research_tab_is_the_report_and_today_keeps_its_card(web, db):
    from pathlib import Path
    fix = (Path(__file__).parent.parent / "fixtures" / "research_dossier.json").read_text(encoding="utf-8")
    assert "עוד אין דוח מחקר" in _call(web, "/")[2] and "עוד אין דוח מחקר" in _call(web, "/research")[2]
    assert web.upload("dossier-2026-10-20.json", fix).startswith("דוח המחקר")
    assert web.upload("dossier.json", "{}").startswith("הקובץ לא נקרא")
    page = _call(web, "/research")[2] + _call(web, "/research", query="part=report")[2]
    for part in ("class=rep", "id=\"macro\"", "id=\"sectors\"", "id=\"ideas\"", "href='#macro'", "href='#reports'", "rep-slot"):
        assert part in page, part
    home = _call(web, "/")[2]
    assert "report-card" in home and "2026-10-20" in home and "rep-slot" not in home      # the report left the home page
    for part in ("data-live=alert", "data-live=queue", "data-live=book", "data-live=watch", "data-live=next"):
        assert part in home, part
    frag = _call(web, "/", query="fragment=1")[2]
    assert "data-live=queue" in frag and "id=\"macro\"" not in frag            # the report is not re-fetched
    assert "id=\"macro\"" in _call(web, "/", query="part=report")[2]          # the old address of the report still answers


def test_watchlist_shows_the_ideas_with_the_latest_daily_check(web, db):
    Queue(db).upload("watchlist", "watchlist-2026-10-20.yaml", yaml.safe_dump({"source": "research 2026-10-20", "entries": [
        {"symbol": "SHEL", "direction": "long", "etf": "XLE", "bucket": "enter", "trigger_level": 105.54,
         "target": 129.52, "tldr": "הנפט יקר"}]}, allow_unicode=True))
    _runs(db)
    db.execute("UPDATE run_steps SET step = 'watch', output = ? WHERE run_id = 'daily-2026-10-22'", (json.dumps(
        {"checks": [{"symbol": "SHEL", "close": 104.9, "status": "ready", "rr": 4.3, "stop": 99.0, "trend_1y_pct": 18.2,
                     "dist_to_trigger_pct": 0.0, "triggered": True, "days_on_list": 2}]}),))
    html = web.watch_html()
    assert "SHEL" in html and "104.90" in html and "מוכנה לכניסה" in html and "+18.2%" in html and "הנפט יקר" in html
    assert "<details class=wl id='wl-SHEL'>" in html and "href='/stock/SHEL'" in html   # depth only on a click


def test_the_ideas_tab_lists_the_watchlist_and_the_report_lists_the_ideas_as_rows(web, db):
    """Ilay, 24.10: a list to scroll through, each stock in a row with a short overview; the article on its page."""
    from pathlib import Path
    fix = (Path(__file__).parent.parent / "fixtures" / "research_dossier.json").read_text(encoding="utf-8")
    web.upload("dossier-2026-10-20.json", fix)
    Queue(db).upload("watchlist", "watchlist-2026-10-20.yaml", yaml.safe_dump({"entries": [
        {"symbol": "SHEL", "direction": "long", "etf": "XLE", "bucket": "enter", "tldr": "הנפט יקר",
         "breakers": ["הורדות תחזית"]}]}, allow_unicode=True))
    page = _call(web, "/ideas")[2]
    assert "Shell plc" in page and "הורדות תחזית" in page and "ניתוח מ-2026-10-20" in page
    rep = _call(web, "/research", query="part=report")[2]
    ideas = rep[rep.index('id="ideas"'):rep.index('id="method"')]
    assert "<details class=wl id='ir-SHEL'>" in ideas and "id='ir-MAA'" in ideas and "<article" not in ideas
    assert "תחזית 2027 יורדת" in ideas                                              # the reason, in short


def test_overview_text_is_cut_at_a_sentence():
    from qt.web import App
    one = "מילה " * 30 + "סוף."
    assert App.overview({"tldr": " ".join([one] * 4)}, None) == " ".join([one] * 2)
    assert App.overview({"tldr": "מילה " * 120}, None).endswith("מילה…")
    assert App.overview({"tldr": "קצר"}, {"judgment": {"tldr": {"line": "שורה", "why_now": "עכשיו"}}}) == "שורה עכשיו"
