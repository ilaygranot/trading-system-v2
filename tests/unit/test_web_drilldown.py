"""The stock, macro and sectors pages behind the overview, and the report parts that stay from week to week."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from qt.approvals import Queue
from qt.calc.volatility import Bar
from qt.research import report
from qt.runs import SCHEMA
from qt.weekly import carry_forward
from qt.web import App
from test_web_positions import _call

FIX = (Path(__file__).parent.parent / "fixtures" / "research_dossier.json").read_text(encoding="utf-8")


@pytest.fixture
def site(db):
    d0 = date(2025, 9, 20)
    bars = lambda s: [Bar(d0 + timedelta(days=i), 0, 0, 0, 100 + i / 10) for i in range(300)]  # noqa: E731
    news = lambda s: [dict(title=f"{s} news", publisher="Reuters", link="https://example.com", published="2026-10-21")]  # noqa: E731
    app = App(bot=None, password="pw", webhook_secret="hook", resume=lambda rid: None,
              prices=lambda syms: {}, bars=bars, news=news)
    app._db = db
    db.ddl(SCHEMA)
    Queue(db).upload("dossier", "dossier-2026-10-20.json", FIX)
    return app


def test_every_idea_has_its_page_with_the_whole_dossier(site):
    d = json.loads(FIX)
    for sym in d["ideas"]:
        st, _, page = _call(site, f"/stock/{sym}")
        assert st.startswith("200") and f'id="idea-{sym}"' in page and "המחיר עכשיו" in page, sym
        assert f"{sym} news" in page
    if d.get("portfolio"):
        assert "התאמה לתיק" in _call(site, f"/stock/{d['portfolio']['syms'][0]}")[2]
    assert "data-live=main" in _call(site, "/stock/SHEL")[2]
    assert "לא הופיעה" in _call(site, "/stock/ZZZZ")[2]


def test_overview_links_tickers_to_their_pages(site):
    assert "rep-slot" in _call(site, "/")[2]                              # the report comes in after the live parts
    page = _call(site, "/", query="part=report")[2]
    assert 'href="#idea-' not in page and 'href="/stock/SHEL"' in page
    assert "id='ir-SHEL'" in page and "href='/stock/SHEL'" in page      # the idea's row opens its page
    assert 'href="/sectors">XLE' in page and 'href="/stock/XLE"' not in page
    assert "href='/macro'" in page and "href='/sectors'" in page


def test_macro_and_sectors_pages(site):
    m, s = _call(site, "/macro")[2], _call(site, "/sectors")[2]
    assert 'id="macro"' in m and "הריצה החודשית האחרונה" in m
    assert 'id="sectors"' in s and 'id="screen"' in s and "הריצה השבועית האחרונה" in s


def test_an_old_report_says_so(site):
    assert "בן " not in site.stale(date.today().isoformat())
    assert "המחקר השבועי לא הפיק" in site.stale((date.today() - timedelta(days=12)).isoformat())


def test_what_the_agents_do_not_write_stays_from_the_last_research():
    prev = json.loads(FIX)
    prev["judgments"]["A1"].update(story=["הסיפור של השבוע"], fed={"headline": "הפד מקל"},
                                   events=[{"date": "2026-10-01", "title": "אירוע", "text": "טקסט", "charts": []}])
    new = json.loads(FIX)
    new["judgments"]["A1"]["notes"] = "A1 note"                            # the agents write one line, not a list
    new["judgments"].pop("A2", None)
    new["meta"]["as_of"] = "2026-10-24"
    carry_forward(new, {"content": json.dumps(prev)})
    J = new["judgments"]
    assert J["A1"]["story"] == prev["judgments"]["A1"]["story"] and J["A2"] == prev["judgments"]["A2"]
    assert J["carried"]["from"] == prev["meta"]["as_of"]
    html = report.render(new)
    assert f"מהמחקר של {prev['meta']['as_of']}" in html and "הסיפור של השבוע" in html and "הפד מקל" in html and "• A1 note" in html and "• A<" not in html
    carry_forward(new, None)                                               # nothing before it: nothing changes


def test_big_answers_go_out_gzipped(site):
    import gzip
    import io
    import base64
    env = {"PATH_INFO": "/", "REQUEST_METHOD": "GET", "QUERY_STRING": "part=report", "wsgi.input": io.BytesIO(b""),
           "HTTP_HOST": "qt.test", "HTTP_ACCEPT_ENCODING": "gzip, br",
           "HTTP_AUTHORIZATION": "Basic " + base64.b64encode(b"ilay:pw").decode()}
    out = {}
    body = b"".join(site(env, lambda s, h: out.update(status=s, headers=dict(h))))
    assert out["headers"]["Content-Encoding"] == "gzip" and int(out["headers"]["Content-Length"]) == len(body)
    assert 'id="macro"' in gzip.decompress(body).decode()
    assert _call(site, "/health")[2] == "ok"
