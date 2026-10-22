"""The open-positions screen: the paper book marked to the latest price, and the live-refreshing dashboard."""
from __future__ import annotations

import base64
import io
import json
from datetime import date, datetime, timezone

import pytest
import yaml

from qt.approvals import Queue
from qt.positions import snapshot
from qt.web import App

PF = {"cash": 108500, "peak_equity": 110000,
      "positions": [{"symbol": "BA", "direction": "short", "sector": "XLI", "qty": 100, "entry": 205.0, "stop": 220.0},
                    {"symbol": "MSFT", "direction": "long", "sector": "XLK", "qty": 40, "entry": 300.0, "stop": 280.0,
                     "target": 360.0, "opened": "2026-10-01"}],
      "realized": [{"symbol": "SHEL", "qty": 10, "price": 70, "pnl": 150.0}]}


def test_snapshot_marks_longs_and_shorts_like_the_book():
    s = snapshot(PF, {"BA": 195.0, "MSFT": 330.0}, now=datetime(2026, 10, 22, tzinfo=timezone.utc))
    ba, msft = {p["symbol"]: p for p in s["positions"]}["BA"], {p["symbol"]: p for p in s["positions"]}["MSFT"]
    assert ba["pl"] == 1000 and ba["pl_pct"] == pytest.approx(4.88, abs=0.01)        # a short gains when it falls
    assert msft["pl"] == 1200 and msft["pl_pct"] == 10.0
    assert s["equity"] == 108500 - 100 * 195 + 40 * 330                               # cash + signed value (qt.book)
    assert s["unrealized"] == 2200 and s["realized"] == 150
    assert msft["stop_dist_pct"] == pytest.approx(15.15, abs=0.01) and ba["stop_dist_pct"] == pytest.approx(12.82, abs=0.01)
    assert s["long_value"] == 13200 and s["short_value"] == 19500
    assert s["peak_equity"] == 110000 and s["drawdown_pct"] == pytest.approx(-7.09, abs=0.01)


def test_snapshot_without_a_price_keeps_the_entry_and_says_so():
    s = snapshot(PF, {"MSFT": 300.0})
    ba = next(p for p in s["positions"] if p["symbol"] == "BA")
    assert ba["priced"] is False and ba["last"] is None and ba["pl"] == 0
    assert snapshot(None, {})["has_portfolio"] is False


def _call(app, path, auth="pw", query=""):
    env = {"PATH_INFO": path, "REQUEST_METHOD": "GET", "QUERY_STRING": query, "wsgi.input": io.BytesIO(b""),
           "HTTP_HOST": "qt.test"}
    if auth:
        env["HTTP_AUTHORIZATION"] = "Basic " + base64.b64encode(f"ilay:{auth}".encode()).decode()
    out = {}
    body = b"".join(app(env, lambda s, h: out.update(status=s, headers=dict(h))))
    return out["status"], out["headers"], body.decode()


@pytest.fixture
def web(db):
    asked = []
    app = App(bot=None, password="pw", webhook_secret="hook", resume=lambda rid: None,
              prices=lambda syms: asked.append(syms) or {"BA": 195.0, "MSFT": 330.0})
    app._db = db
    app.asked = asked
    return app


def test_positions_need_the_password(web):
    for path in ("/positions", "/api/positions"):
        assert _call(web, path, auth=None)[0].startswith("401")
        assert _call(web, path, auth="bad")[0].startswith("401")


def test_positions_screen_reads_the_newest_book_and_refreshes(web, db):
    Queue(db).upload("portfolio", "portfolio.yaml", yaml.safe_dump(PF))
    st, h, page = _call(web, "/positions")
    assert st.startswith("200") and "MSFT" in page and "BA" in page and "id=live" in page and "fragment=1" in page
    assert web.asked[-1] == ["BA", "MSFT"]
    st, h, frag = _call(web, "/positions", query="fragment=1")             # what the script fetches
    assert st.startswith("200") and "<script" not in frag and "MSFT" in frag and h["Cache-Control"] == "no-store"
    st, h, body = _call(web, "/api/positions")
    data = json.loads(body)
    assert h["Content-Type"].startswith("application/json") and data["unrealized"] == 2200
    # the daily run's book replaces the uploaded file: a position it closed is gone from the screen
    book = dict(PF, positions=PF["positions"][1:])
    Queue(db).upload("portfolio", "book-2026-10-22.yaml", yaml.safe_dump(book))
    assert [p["symbol"] for p in json.loads(_call(web, "/api/positions")[2])["positions"]] == ["MSFT"]


class _Bot:                                                       # refresh_item fails on it; the decision stands
    pass


def test_a_prepared_entry_opens_with_one_button_and_waits_for_the_open(web, db):
    web._bot = _Bot()
    Queue(db).upload("portfolio", "portfolio.yaml", yaml.safe_dump(PF))
    iid = Queue(db).add("daily-x", date.today(), dict(
        kind="enter", rule_id="S10-R4", symbol="SHEL", qty=56, price=105.54, preapproved=False, direction="long",
        sector="XLE", stop=99.62, target=129.52, detail="מוכנה לכניסה"), 105.54)
    page = _call(web, "/positions")[2]
    assert "מוכנות לפתיחה (1)" in page and "פתח פוזיציה" in page and f"/item/{iid}/approve" in page
    assert "332$" in page                                                   # 56 x (105.54 - 99.62) at risk
    env = {"PATH_INFO": f"/item/{iid}/approve", "REQUEST_METHOD": "POST", "QUERY_STRING": "",
           "wsgi.input": io.BytesIO(b"back=positions"), "CONTENT_LENGTH": "14", "HTTP_HOST": "qt.test",
           "HTTP_AUTHORIZATION": "Basic " + base64.b64encode(b"ilay:pw").decode()}
    out = {}
    web(env, lambda s, h: out.update(status=s, headers=dict(h)))
    assert out["status"].startswith("303") and out["headers"]["Location"] == "/positions"
    data = json.loads(_call(web, "/api/positions")[2])
    assert [(o["symbol"], o["kind"], o["qty"]) for o in data["pending"]] == [("SHEL", "enter", 56)]
    page = _call(web, "/positions")[2]
    assert "מוכנות לפתיחה" not in page and "ממתינות לביצוע" in page


def test_dashboard_shows_positions_and_refreshes_itself(web, db):
    st, _, page = _call(web, "/")
    assert st.startswith("200") and "עוד אין תיק" in page and "setInterval" in page
    Queue(db).upload("portfolio", "portfolio.yaml", yaml.safe_dump(PF))
    st, _, frag = _call(web, "/", query="fragment=1")
    assert "MSFT" in frag and "<script" not in frag and "ריצות" in frag


def test_a_price_failure_does_not_take_the_dashboard_down(db):
    def boom(_):
        raise RuntimeError("yahoo down")
    app = App(bot=None, password="pw", webhook_secret="hook", resume=lambda rid: None, prices=boom)
    app._db = db
    Queue(db).upload("portfolio", "portfolio.yaml", yaml.safe_dump(PF))
    st, _, page = _call(app, "/")
    assert st.startswith("200") and "לא הצלחתי לטעון את התיק" in page and "ריצות" in page


def test_yahoo_prices_cache_for_a_minute(db, monkeypatch):
    from qt.data import http
    from qt.data.store import Store
    from qt.positions import yahoo_prices
    calls = []

    def fake(url, store=None, **kw):
        calls.append(url)
        if "NOPE" in url:
            raise http.ProviderError("404")
        return {"chart": {"result": [{"meta": {"regularMarketPrice": 101.5}}]}}
    monkeypatch.setattr(http, "get_json", fake)
    prices = yahoo_prices(Store(db))
    assert prices(["SHEL", "NOPE"]) == {"SHEL": 101.5}
    assert prices(["SHEL"]) == {"SHEL": 101.5} and len(calls) == 2           # the second read is from the cache
