"""The dashboard as four tabs and the system's page (Ilay 24.10, the approved design): the status dot that turns red
when something failed, the ideas with their filter and analysis date, the stocks closest to their trigger, the
position cards, and the buttons that send the page back where they were pressed."""
from __future__ import annotations

import base64
import io
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from qt.approvals import Queue
from qt.runs import SCHEMA
from qt.web import App
from test_web_positions import PF, _call, web  # noqa: F401 - the fixture

FIX = (Path(__file__).parent.parent / "fixtures" / "research_dossier.json").read_text(encoding="utf-8")
PAGES = ("/", "/ideas", "/ideas?f=enter", "/positions", "/research", "/system", "/runs", "/macro", "/sectors", "/journal",
         "/stock/SHEL", "/stock/ZZZZ")


def _get(app, page, **kw):
    path, _, query = page.partition("?")
    return _call(app, path, query=query, **kw)


def _post(app, path, body: bytes):
    env = {"PATH_INFO": path, "REQUEST_METHOD": "POST", "QUERY_STRING": "", "wsgi.input": io.BytesIO(body),
           "CONTENT_LENGTH": str(len(body)), "HTTP_HOST": "qt.test",
           "HTTP_AUTHORIZATION": "Basic " + base64.b64encode(b"ilay:pw").decode()}
    out = {}
    app(env, lambda s, h: out.update(status=s, headers=dict(h)))
    return out["status"], out["headers"]


def _run(db, rid, kind="daily", status="done", error=None, started=None):
    db.ddl(SCHEMA)
    db.insert("runs", dict(id=rid, kind=kind, as_of=rid[-10:], status=status, error=error, resumes=0,
                           started_at=started or datetime.now(timezone.utc).isoformat(timespec="seconds"),
                           finished_at=None if status == "running" else datetime.now(timezone.utc).isoformat(timespec="seconds")),
              returning=None)


def _watch(db, checks):
    _run(db, "daily-2026-10-22")
    db.insert("run_steps", dict(run_id="daily-2026-10-22", step="watch", subject="", seq=0, status="done",
                                output=json.dumps({"checks": checks}), attempts=1), returning=None)


WL = {"source": "research 2026-10-20", "entries": [
    {"symbol": "SHEL", "direction": "long", "etf": "XLE", "bucket": "enter", "trigger_level": 105.54, "target": 129.52},
    {"symbol": "TALO", "direction": "long", "etf": "XLE", "bucket": "conditional", "trigger_level": 18.73, "target": 31.34},
    {"symbol": "RGLD", "direction": "long", "etf": "XLB", "bucket": "conditional", "trigger_level": 248.35, "target": 389.55},
    {"symbol": "AAA", "direction": "long", "etf": "XLB", "bucket": "conditional", "trigger_level": 10.0, "target": 20.0},
    {"symbol": "EGY", "direction": "long", "etf": "XLE", "bucket": "watch", "target": 8.77}]}
CHECKS = [{"symbol": "SHEL", "close": 106.0, "status": "ready", "triggered": True, "rr": 4.1, "dist_to_trigger_pct": 0.0},
          {"symbol": "TALO", "close": 18.05, "status": "waiting", "triggered": False, "rr": 5.0, "dist_to_trigger_pct": 3.8},
          {"symbol": "RGLD", "close": 238.78, "status": "waiting", "triggered": False, "rr": 6.1, "dist_to_trigger_pct": 4.0},
          {"symbol": "AAA", "close": 9.9, "status": "invalidated", "invalidated": True, "triggered": False, "rr": 3.0,
           "dist_to_trigger_pct": 1.0},
          {"symbol": "EGY", "close": 6.24, "status": "waiting", "triggered": False, "rr": 3.4}]


def test_every_screen_opens_on_an_empty_and_on_a_full_database(web, db):
    for page in PAGES:
        st, _, body = _get(web, page)
        assert st.startswith("200") and body != "error", page
        assert _get(web, page + ("&" if "?" in page else "?") + "fragment=1")[0].startswith("200"), page
    Queue(db).upload("portfolio", "portfolio.yaml", yaml.safe_dump(PF))
    Queue(db).upload("watchlist", "watchlist.yaml", yaml.safe_dump(WL))
    Queue(db).upload("dossier", "dossier-2026-10-20.json", FIX)
    Queue(db).upload("report", "report-2026-10-20.html", "<h1>r</h1>")
    _watch(db, CHECKS)
    for page in PAGES:
        st, _, body = _get(web, page)
        assert st.startswith("200") and body != "error", page


def test_the_dot_is_green_when_all_is_well_and_red_with_the_reason_when_a_run_failed(web, db):
    _run(db, "daily-2026-10-22")
    h = web.health()
    assert h["state"] == "ok" and h["text"] == "המערכת תקינה"
    assert "המערכת תקינה" in _call(web, "/")[2] and "class=banner" not in _call(web, "/")[2]
    _run(db, "weekly-2026-10-24", kind="weekly", status="failed", error="ProviderError: yahoo 429")
    h = web.health()
    assert h["state"] == "bad" and h["text"] == "ריצה נכשלה" and "yahoo 429" in h["issues"][0][1]
    home, system = _call(web, "/")[2], _call(web, "/system")[2]
    for page, back in ((home, ""), (system, "<input type=hidden name=back value=system>")):
        assert "banner bad" in page and "ריצה נכשלה" in page and "yahoo 429" in page
        assert f"<form method=post action='/runs/weekly-2026-10-24/resume'>{back}<button class=go>" in page
    assert "dot bad" in _call(web, "/positions")[2]                   # the dot is on every tab
    assert "dot bad" in _call(web, "/ideas", query="fragment=1")[2]  # and refreshes with the page
    assert json.loads(_call(web, "/api/status")[2])["health"]["state"] == "bad"


def test_the_dot_tells_apart_what_needs_a_look(web, db, monkeypatch):
    _run(db, "monthly-2026-10-17", kind="monthly", status="failed", error="waiting for manual macro file")
    assert web.health()["state"] == "warn"                            # waits for Ilay's file, not broken
    db.execute("DELETE FROM runs")
    _run(db, "daily-2026-10-22", status="running")
    h = web.health()
    assert h["state"] == "" and h["text"] == "ריצה יומית רצה עכשיו"
    db.execute("UPDATE runs SET status = 'done'")
    db.insert("run_events", dict(run_id="daily-2026-10-22", at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                 level="error", step="prices", message="yahoo 429"))
    assert web.health()["state"] == "warn"                            # an error the run got past
    db.execute("UPDATE runs SET status = 'failed'")
    assert web.health()["state"] == "bad"
    db.execute("DELETE FROM run_events")
    db.execute("UPDATE runs SET status = 'done'")
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
    db.insert("run_events", dict(run_id="daily-2026-10-22", at=old, level="error", step="prices", message="old"))
    assert web.health()["state"] == "ok"                              # older than a day
    monkeypatch.setattr(web, "gateway", lambda: ("bad", "לא עונה"))
    assert web.health()["issues"] == [("bad", "IB Gateway לא עונה")]
    monkeypatch.setattr(web, "gateway", lambda: (None, "לא מוגדר"))
    assert web.health(None)["issues"] == [("bad", "לא הצלחתי לטעון את התיק")]
    d = json.loads(FIX)
    d["meta"]["as_of"] = (date.today() - timedelta(days=12)).isoformat()
    Queue(db).upload("dossier", "dossier-old.json", json.dumps(d))
    h = web.health()
    assert h["state"] == "warn" and "בן 12 ימים" in h["issues"][0][1]


def test_a_failed_price_turns_the_home_banner_red(db):
    def boom(_):
        raise RuntimeError("yahoo down")
    app = App(bot=None, password="pw", webhook_secret="hook", resume=lambda rid: None, prices=boom)
    app._db = db
    Queue(db).upload("portfolio", "portfolio.yaml", yaml.safe_dump(PF))
    page = _call(app, "/")[2]
    assert "banner bad" in page and "לא הצלחתי לטעון את התיק" in page and "dot bad" in page


def test_ideas_filter_by_basket_and_show_the_analysis_date(web, db):
    Queue(db).upload("watchlist", "watchlist.yaml", yaml.safe_dump(WL))
    Queue(db).upload("dossier", "dossier-2026-10-20.json", FIX)
    _watch(db, CHECKS)
    page = _call(web, "/ideas")[2]
    for chip in ("הכל 5", "כניסה 1", "מותנה 3", "מעקב 1"):
        assert chip in page, chip
    assert "ניתוח מ-2026-10-20" in page and "אין עדיין ניתוח מחקר" in page    # SHEL is in the research, RGLD is not
    assert "עוד 3.8% לטריגר (18.73)" in page and "הטריגר הופעל (105.54)" in page and "הטריגר בוטל" in page
    assert "אין טריגר: ממתינה" in page and "מוכנה לכניסה" in page and "🟢" not in page
    only = _call(web, "/ideas", query="f=conditional")[2]
    assert "id='wl-TALO'" in only and "id='wl-SHEL'" not in only and "class=on aria-current=true>מותנה 3" in only
    assert "id='wl-SHEL'" in _call(web, "/ideas", query="f=nonsense")[2]      # an unknown filter shows everything
    assert "id='wl-TALO'" in _call(web, "/ideas", query="f=conditional&fragment=1")[2]


def test_today_shows_the_stocks_closest_to_their_trigger(web, db):
    Queue(db).upload("watchlist", "watchlist.yaml", yaml.safe_dump(WL))
    _watch(db, CHECKS)
    home = _call(web, "/")[2]
    rows = home[home.index("קרובות לטריגר"):]
    assert rows.index("/stock/SHEL") < rows.index("/stock/TALO") < rows.index("/stock/RGLD")   # fired, then by distance
    top = web.top_ideas()
    assert "AAA" not in top and "EGY" not in top                             # cancelled and no-trigger stay out
    assert "עוד 3.8%</b>" in top and "עוד 4.0%</b>" in top and "עוד 0.0%" not in top     # the distance, not the status
    assert "<span class='pill ok'>מוכנה לכניסה</span>" in top                 # a fired trigger says so
    assert "כל הרעיונות (5)" in home


def test_position_cards_place_the_price_between_the_stop_and_the_target(web, db):
    Queue(db).upload("portfolio", "portfolio.yaml", yaml.safe_dump(PF))
    page = _call(web, "/positions")[2]
    assert "עצירה 280.00</span><b>330.00</b><span>יעד 360.00" in page       # MSFT long: 50 of 80 on the way
    assert "width:62%" in page
    assert "עצירה 220.00</span><b>12.8% עד העצירה</b><span>אין יעד" in page  # BA short, no target
    assert App._between(220, 195, 180) == 62.5                               # a short's way down counts the same
    assert App._between(280, None, 360) is None and App._between(280, 300, 280) is None
    assert "כל המספרים בטבלה" in page and "מרחק לסטופ" in page               # the full table, one tap away


def test_buttons_send_the_page_back_where_they_were_pressed(web, db):
    _run(db, "daily-2026-10-21", status="failed", error="x")
    assert _post(web, "/runs/daily-2026-10-21/resume", b"back=system")[1]["Location"] == "/system"
    assert _post(web, "/runs/daily-2026-10-21/resume", b"")[1]["Location"] == "/"
    assert _post(web, "/runs/daily-2026-10-21/resume", b"back=https://evil.example")[1]["Location"] == "/"
    iid = Queue(db).add("daily-x", date.today(), dict(kind="tighten_stop", rule_id="S11-R7", symbol="SHEL", stop=101.0,
                                                      detail="הסטופ עולה"), 105.0)
    page = _call(web, "/stock/SHEL")[2]
    assert "מחכה לאישורך (1)" in page and "<input type=hidden name=back value=stock:SHEL>" in page
    st, h = _post(web, f"/item/{iid}/postpone", b"back=stock:SHEL")
    assert st.startswith("303") and h["Location"] == "/stock/SHEL"
    assert web._back({}, "back=stock:../../x") == "/"


def test_the_stock_page_leads_with_what_to_know(web, db):
    d = json.loads(FIX)
    d["ideas"]["SHEL"]["judgment"]["tldr"] = {"line": "קניתי את Shell כי הנפט יקר", "why_now": "פרצה לשיא",
                                              "wrong": "הפסקת אש תוריד את הנפט"}
    Queue(db).upload("dossier", "dossier-2026-10-20.json", json.dumps(d))
    page = _call(web, "/stock/SHEL")[2]
    for part in ("<h1>SHEL</h1>", "Shell plc", "ניתוח מ-2026-10-20", "קניתי את Shell כי הנפט יקר", "למה עכשיו", "פרצה לשיא",
                 "מה יכול להשתבש", "הפסקת אש", "המאמר המלא · מהמחקר של 2026-10-20", 'id="idea-SHEL"', "חדשות"):
        assert part in page, part
    assert page.index("למה עכשיו") < page.index("המחיר עכשיו") < page.index('id="idea-SHEL"')
    assert page.count("<h1>") == 1                                           # the ticker is the page's title


def test_the_report_keeps_its_own_colours_inside_the_app():
    from qt import webui as ui
    css = ui.report_css()
    assert ":root{" not in css and ".rep{" in css and ':root[data-theme="dark"] .rep{' in css
    assert "qt-shell-v2" in (Path(ui.__file__).parent / "static" / "sw.js").read_text()
