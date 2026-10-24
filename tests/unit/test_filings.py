"""SEC filings fetcher (work package B): parsers on recorded snippets, a fake HTTP session, no network."""
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from qt.data import filings as F
from qt.data.filings import SECFilings
from qt.data.providers import BROWSER_UA
from qt.data.store import Store
from qt.data.textfetch import RateLimiter, get_text, html_to_text

FIX = Path(__file__).parents[1] / "fixtures" / "sec"
AS_OF = date(2026, 10, 23)
NVDA_BASE = "https://www.sec.gov/Archives/edgar/data/1045810"


def fx(name: str) -> str:
    return (FIX / name).read_text()


class Resp:
    def __init__(self, status: int, text: str):
        self.status_code, self.text = status, text

    def json(self):
        return json.loads(self.text)


class FakeSession:
    def __init__(self, routes: dict[str, tuple[int, str]]):
        self.routes, self.calls = routes, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, dict(headers or {})))
        status, body = self.routes.get(url, (404, "not found"))
        return Resp(status, body)


def nvda_routes() -> dict[str, tuple[int, str]]:
    return {
        F.TICKERS_URL: (200, fx("tickers.json")),
        "https://data.sec.gov/submissions/CIK0001045810.json": (200, fx("sub_nvda.json")),
        f"{NVDA_BASE}/000104581026000078/0001045810-26-000078-index.htm": (200, fx("index_plain_8k.htm")),
        f"{NVDA_BASE}/000104581026000073/0001045810-26-000073-index.htm": (200, fx("index_nvda_8k.htm")),
        f"{NVDA_BASE}/000104581026000051/0001045810-26-000051-index.htm": (200, fx("index_nvda_8k.htm")),
        f"{NVDA_BASE}/000104581026000073/q2fy27pr.htm": (200, fx("nvda_ex991.htm")),
        f"{NVDA_BASE}/000104581026000073/q2fy27cfocommentary.htm": (200, fx("nvda_ex992.htm")),
        f"{NVDA_BASE}/000104581026000021/nvda-20260125.htm": (200, fx("tenk.htm")),
        f"{NVDA_BASE}/000104581026000075/nvda-20260726.htm": (200, fx("tenq_nmc.htm")),
    }


def sec(routes=None, store=None, contact="research@example.com"):
    s = FakeSession(nvda_routes() if routes is None else routes)
    return SECFilings(store, contact=contact, session=s, limiter=RateLimiter(0)), s


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "s.sqlite")


# ---------------------------------------------------------------- submissions and current reports

def test_submissions_columns_zipped():
    f, _ = sec()
    rows = f.submissions(1045810)
    assert rows[0]["accessionNumber"] == "0001045810-26-000080" and rows[0]["form"] == "8-K"
    assert rows[0]["items"] == "5.02" and rows[0]["cik"] == 1045810
    assert len(rows) == 7


def test_current_reports_window_no_look_ahead_and_items():
    f, _ = sec()
    cur = f.current_reports("NVDA", AS_OF)
    assert [c["date"] for c in cur] == ["2026-09-03", "2026-08-26"]  # 2026-10-30 is after as_of; Form 4 is not current
    gov, res = cur
    assert gov["items"] == ["5.07"] and gov["kind"] == "governance" and gov["exhibits"] == []
    assert res["items"] == ["2.02", "9.01"] and res["kind"] == "results"
    assert res["item_names"][0] == "Results of Operations and Financial Condition"
    assert res["index_url"].endswith("/000104581026000073/0001045810-26-000073-index.htm")
    assert [x["type"] for x in res["exhibits"]] == ["EX-99.1", "EX-99.2"]
    assert res["exhibits"][0]["title"] == "NVIDIA Announces Financial Results for Second Quarter Fiscal 2027"
    assert 0 < len(res["exhibits"][0]["lead"]) <= 1200
    # 90-day lower bound: from 2026-11-30 the window starts 2026-09-01
    assert [c["date"] for c in f.current_reports("NVDA", date(2026, 11, 30))] == ["2026-10-30", "2026-09-03"]


def test_item_kind_rules():
    assert F.item_kind(["5.07"]) == "governance" and F.item_kind(["5.03", "9.01"]) == "governance"
    assert F.item_kind(["9.01", "2.02"]) == "results" and F.item_kind(["1.01", "2.03", "7.01"]) == "agreement"
    assert F.item_kind(["3.02"]) == "other_event" and F.item_kind([]) == "unknown"


def test_6k_kind_from_titles():
    shel = "https://www.sec.gov/Archives/edgar/data/1306965"
    routes = {
        F.TICKERS_URL: (200, fx("tickers.json")),
        "https://data.sec.gov/submissions/CIK0001306965.json": (200, fx("sub_shel.json")),
        f"{shel}/000117184326006591/0001171843-26-006591-index.htm": (200, fx("index_shel_pdmr.htm")),
        f"{shel}/000117184326006591/f6k_101226.htm": (200, fx("shel_pdmr.htm")),
        f"{shel}/000162828026050763/0001628280-26-050763-index.htm": (200, fx("index_shel_results.htm")),
        f"{shel}/000162828026050763/q22026exhibit991.htm": (200, fx("shel_ex991.htm")),
        f"{shel}/000162828026050763/shel-20260630.htm": (200, fx("shel_ex992.htm")),
    }
    f, _ = sec(routes)
    cur = f.current_reports("SHEL", AS_OF)
    assert [(c["form"], c["items"], c["kind"]) for c in cur] == [("6-K", [], "governance"), ("6-K", [], "results")]
    assert cur[0]["title"] == "Director/PDMR Shareholding"
    latest, prev = f.results("SHEL", AS_OF)
    assert latest["source"] == "sec:6-K EX-99.1" and prev is None
    assert latest["commentary"]["type"] == "EX-99.2" and latest["commentary"]["outlook"]
    assert "adjusted earnings" in latest["lead"]  # cover-note EX-99.1: the lead comes from the report
    assert F.results_kind("CANADIAN NATURAL RESOURCES LIMITED ANNOUNCES QUARTERLY DIVIDEND") == "other_event"
    assert F.results_kind("Transaction in Own Shares") == "governance"
    assert F.results_kind("Shell completes acquisition of ARC Resources") == "acquisition"


# ---------------------------------------------------------------- index parsing

def test_parse_index_strips_ixbrl_and_filters_ex99():
    rows = F.parse_index(fx("index_nvda_8k.htm"))
    assert rows[0]["document"] == "nvda-20260826.htm"
    assert rows[0]["url"] == f"{NVDA_BASE}/000104581026000073/nvda-20260826.htm"
    assert rows[1]["size"] == 341113 and rows[1]["description"] == "EX-99.1"
    ex = F.ex99(rows)
    assert [r["type"] for r in ex] == ["EX-99.1", "EX-99.2"]
    assert all("/ix?doc=" not in r["url"] for r in rows)


# ---------------------------------------------------------------- risk factors

def test_cut_risk_factors_10k_longest_span_skips_toc_and_bare_item_1c():
    text = html_to_text(fx("tenk.htm"))
    sec_ = F.cut_risk_factors(text, "10-K")
    assert sec_.startswith("ITEM 1A. RISK FACTORS")
    assert "Cybersecurity incidents could disrupt" in sec_  # past the bare "Item 1C" line
    assert "UNRESOLVED STAFF COMMENTS" not in sec_ and "Unresolved Staff Comments" not in sec_
    heads = F.risk_headlines(sec_)
    assert "Supply and Demand" in heads and "Economic conditions" in heads
    assert "Competition could adversely impact our market share and financial results." in heads


def test_cut_risk_factors_10q_and_variants():
    assert F.cut_risk_factors(html_to_text(fx("tenq_no1a.htm")), "10-Q") == ""
    q = F.cut_risk_factors(html_to_text(fx("tenq_nmc.htm")), "10-Q")
    assert q.startswith("Item 1A. Risk Factors") and F.no_material_change(q)
    s = F.cut_risk_factors(html_to_text(fx("twentyf.htm")), "20-F")
    assert "Portfolio risks" in s and "How we manage risks" not in s and "23-30" not in s
    assert "Portfolio risks" in F.risk_headlines(s)
    c = F.cut_risk_factors(html_to_text(fx("fortyf.htm")), "40-F")
    assert c.startswith("D. RISK FACTORS") and "Volatility of Crude Oil" in c and "reserves evaluators" not in c
    assert F.cut_risk_factors("nothing here", "10-K") == ""


def test_risk_factors_10q_without_1a_falls_back_to_10k():
    routes = nvda_routes()
    routes[f"{NVDA_BASE}/000104581026000075/nvda-20260726.htm"] = (200, fx("tenq_no1a.htm"))
    f, _ = sec(routes)
    rf = f.risk_factors("NVDA", AS_OF)
    assert rf["form"] == "10-K" and rf["filed"] == "2026-02-25" and rf["delta"] is None
    assert rf["chars_total"] > 500 and len(rf["excerpt"]) <= 4000 and len(rf["headlines"]) <= 25


def test_risk_factors_caps():
    big = "<p>ITEM 1A. RISK FACTORS</p>" + "".join(
        f"<p>Risk heading number {i} about Markets and Things</p><p>{'x' * 300}.</p>" for i in range(60)) \
        + "<p>ITEM 1B. UNRESOLVED STAFF COMMENTS</p>"
    r = F.risk_extract(big, "10-K")
    assert len(r["excerpt"]) <= 4000 and len(r["headlines"]) == 25 and all(len(h) <= 200 for h in r["headlines"])
    assert r["chars_total"] > 4000


def test_business_section_paragraphs_verbatim():
    b = F.business_extract(html_to_text(fx("tenk.htm")), "10-K")
    assert b["overview"] == ["NVIDIA pioneered accelerated computing to help solve the most challenging computational "
                             "problems. NVIDIA is now a data center scale AI infrastructure company reshaping all industries."]
    assert b["segments"][0] == "We report our business results in two segments." and len(b["segments"]) == 3
    comp = b["competition"]
    assert comp[0].startswith("The market for our products is intensely competitive")
    assert comp[0].endswith("at competitive prices.")             # the page break ("8", "Table of Contents") is skipped
    assert comp[1].startswith("Our current competitors include: suppliers")   # the bullet stays with its opener
    assert not any("Human Capital" in x or "36,000" in x for x in comp)      # the next heading ends the section
    assert b["kpi"][0].startswith("We use the following key metrics") and "$115.2 billion" in b["kpi"][0]
    assert F.business_extract("nothing here", "10-K") == {"chars_total": 0, "overview": [], "segments": [],
                                                          "competition": [], "kpi": []}


def test_annual_report_in_its_own_layout_gives_the_segments_by_heading():
    html = ("<p>Our businesses</p><p>Reporting segments</p><p>Integrated Gas</p><p>Integrated Gas includes natural gas "
            "and liquids exploration and extraction, and the production of liquefied natural gas.</p><p>See pages 45-51 for "
            "a review of our performance.</p><p>Upstream</p><p>Upstream explores for and extracts crude oil, natural gas and "
            "natural gas liquids, and markets and transports them.</p><p>Strategy</p><p>Our strategy is to deliver more "
            "value with less emissions over the long run.</p>")
    b = F.business_extract(html_to_text(html), "20-F")
    assert [x.split()[0] for x in b["segments"]] == ["Integrated", "Upstream"]       # past the sub-headings,
    assert not any("See pages" in x or "strategy" in x for x in b["segments"])        # not the cross-references or
                                                                                      # the next section


def test_shrink_drops_whole_business_paragraphs():
    para = "word " * 200
    b = {"overview": [para] * 3, "segments": [para], "competition": [para] * 2, "kpi": []}
    F._trim_business(b, 1000)
    assert b["overview"] == [para] and b["competition"] == [para] and b["segments"] == [para]


# ---------------------------------------------------------------- predecessor CIK

def test_predecessor_cik_from_accession_prefix():
    routes = {
        F.TICKERS_URL: (200, fx("tickers.json")),
        "https://data.sec.gov/submissions/CIK0002115436.json": (200, fx("sub_xom_new.json")),
        "https://data.sec.gov/submissions/CIK0000034088.json": (200, fx("sub_xom_old.json")),
    }
    f, s = sec(routes)
    assert f.ciks("XOM") == [2115436, 34088]
    # the filing agent prefix (1193125) was tried and rejected (no annual report there)
    assert any("CIK0001193125" in u for u, _ in s.calls)
    rows = f._rows([2115436, 34088])
    assert len({r["accessionNumber"] for r in rows}) == len(rows)  # the shared 10-Q is deduped
    assert next(r for r in rows if r["form"] == "10-K")["cik"] == 34088


# ---------------------------------------------------------------- guidance and outlook

def test_guidance_ranges():
    nv = "NVIDIA's outlook for the third quarter of fiscal 2027 is as follows:\nRevenue is expected to be $54.0 billion, plus or minus 2%."
    g = F.guidance_ranges(nv)
    assert len(g) == 1 and g[0]["period"] == "Q" and g[0]["low"] == pytest.approx(52.92e9) and g[0]["high"] == pytest.approx(55.08e9)
    fy = F.guidance_ranges("The company raised its full-year revenue guidance to $10.2 billion to $10.6 billion.")
    assert fy == [{"metric": "revenue", "period": "FY", "low": 10.2e9, "high": 10.6e9, "unit": "USD",
                   "sentence": "The company raised its full-year revenue guidance to $10.2 billion to $10.6 billion."}]
    fy2 = F.guidance_ranges("For fiscal year 2027, revenue is expected to be $4.1 billion to $4.3 billion.")
    assert fy2[0]["period"] == "FY" and fy2[0]["low"] == 4.1e9 and fy2[0]["high"] == 4.3e9
    assert F.guidance_ranges("Revenue was $96.2 billion, up 106% from a year ago. We expect strong demand.") == []
    assert F.guidance_ranges("Gross margins are expected to be 74.0%, plus or minus 50 basis points.") == []
    unk = F.guidance_ranges("Revenue is expected to be approximately $2.0 billion.")
    assert unk[0]["period"] == "unknown"


def test_outlook_paragraphs_need_a_number_and_skip_boilerplate():
    text = html_to_text(fx("nvda_ex991.htm"))
    out = F.outlook_paragraphs(text, cap=4000)
    assert any("Revenue is expected to be $108.0 billion" in p for p in out)
    assert not any("forward-looking" in p for p in out)
    assert sum(map(len, out)) <= 4000
    assert F.outlook_paragraphs("We expect demand to stay strong across all of our markets this year.") == []
    assert sum(map(len, F.outlook_paragraphs("\n".join(["Revenue is expected to grow 5% in fiscal 2027. " * 3] * 50), 500))) <= 500


def test_html_to_text_drops_script_style_ixheader():
    t = html_to_text(fx("nvda_ex991.htm"))
    assert "hidden-xbrl-junk" not in t and "var x" not in t and "p{margin" not in t
    assert "\n" in t and "  " not in t


# ---------------------------------------------------------------- documents(): contract, headers, errors, cache

def test_documents_contract_and_caps(store):
    f, _ = sec(store=store)
    d = f.documents("NVDA", AS_OF)
    assert d["errors"] == []
    assert set(d) == {"symbol", "cik", "ciks", "foreign", "as_of", "current", "results", "results_prev",
                      "risk_factors", "business", "transcript", "errors"}
    assert d["symbol"] == "NVDA" and d["cik"] == 1045810 and d["ciks"] == [1045810] and d["foreign"] is False
    assert d["as_of"] == "2026-10-23" and d["transcript"] is None
    assert len(d["current"]) <= 8 and d["current"][0]["date"] >= d["current"][-1]["date"]
    r = d["results"]
    assert r["form"] == "8-K" and r["date"] == "2026-08-26" and r["source"] == "sec:8-K EX-99.1"
    assert len(r["title"]) <= 200 and len(r["lead"]) <= 1500 and sum(map(len, r["outlook"])) <= 4000
    assert r["guidance"][0]["period"] == "Q"
    assert r["commentary"]["type"] == "EX-99.2" and sum(map(len, r["commentary"]["outlook"])) <= 2500
    assert d["results_prev"]["date"] == "2026-05-20"
    rf = d["risk_factors"]
    assert rf["form"] == "10-K" and rf["delta"]["form"] == "10-Q" and rf["delta"]["no_material_change"] is True
    assert len(rf["delta"]["excerpt"]) <= 2500
    b = d["business"]                                   # the same 10-K download as the risk factors
    assert b["form"] == "10-K" and b["url"] == rf["url"] and b["overview"][0].startswith("NVIDIA pioneered")
    assert F.size(d) <= F.MAX_CHARS + 500


def test_user_agent_has_contact_and_no_browser_ua():
    f, s = sec()
    f.documents("NVDA", AS_OF)
    assert s.calls
    for _, h in s.calls:
        assert h["User-Agent"] == "QuantumTrader research research@example.com"
        assert h["User-Agent"] != BROWSER_UA and "Mozilla" not in h["User-Agent"]


def test_missing_contact_makes_no_http_calls(monkeypatch):
    monkeypatch.delenv("SEC_CONTACT", raising=False)
    s = FakeSession(nvda_routes())
    f = SECFilings(None, contact=None, session=s)
    assert not f.ok()
    d = f.documents("NVDA", AS_OF)
    assert d["errors"] == ["SEC_CONTACT not set"] and s.calls == [] and d["current"] == [] and d["transcript"] is None


def test_403_is_recorded_not_raised():
    routes = nvda_routes()
    routes["https://data.sec.gov/submissions/CIK0001045810.json"] = (403, "Undeclared Automated Tool")
    f, _ = sec(routes)
    d = f.documents("NVDA", AS_OF)
    assert d["errors"] and "403" in d["errors"][0] and d["current"] == []
    routes = nvda_routes()
    routes[f"{NVDA_BASE}/000104581026000021/nvda-20260125.htm"] = (403, "forbidden")
    d = sec(routes)[0].documents("NVDA", AS_OF)
    assert d["risk_factors"] is None and any("403" in e for e in d["errors"]) and d["results"] is not None


def test_second_documents_call_is_served_from_cache_as_text(store):
    routes = nvda_routes()
    raw = fx("nvda_ex991.htm").replace("<body>", "<body>" + "<div><ix:header>" + "<ix:hidden>x</ix:hidden>" * 2000
                                       + "</ix:header></div>")
    routes[f"{NVDA_BASE}/000104581026000073/q2fy27pr.htm"] = (200, raw)
    f, s = sec(routes, store=store)
    first = f.documents("NVDA", AS_OF)
    n = len(s.calls)
    assert n > 0
    second = f.documents("NVDA", AS_OF)
    assert len(s.calls) == n and second == first
    key = f"text:x4000:{NVDA_BASE}/000104581026000073/q2fy27pr.htm"
    cached = store.cached(key, 3600)
    assert isinstance(cached, dict) and "<p>" not in json.dumps(cached) and len(json.dumps(cached)) < len(raw)


def test_get_text_caches_transformed_text(store):
    s = FakeSession({"https://x.test/a.htm": (200, "<html><body><p>Hello</p><p>World</p></body></html>")})
    t = get_text("https://x.test/a.htm", store, session=s, transform=html_to_text, key="t")
    assert t == "Hello\n\nWorld" or t == "Hello\nWorld"
    assert get_text("https://x.test/a.htm", store, session=s, transform=html_to_text, key="t") == t
    assert len(s.calls) == 1
    assert store.cached("text:t:https://x.test/a.htm", 60) == t


def test_shrink_caps_total_size():
    lead = "word " * 300
    d = {"symbol": "X", "cik": 1, "ciks": [1], "foreign": False, "as_of": "2026-10-23", "transcript": None, "errors": [],
         "current": [{"form": "8-K", "date": "2026-10-01", "accession": "a", "items": [], "item_names": [],
                      "kind": "unknown", "url": "u", "index_url": "i",
                      "exhibits": [{"type": "EX-99.1", "url": "u", "title": "t", "lead": lead[:1200]}] * 3}] * 8,
         "results": {"form": "8-K", "date": "d", "url": "u", "source": "s", "title": "t", "lead": lead[:1500],
                     "outlook": [lead[:900]] * 4, "guidance": [], "commentary": None},
         "results_prev": None,
         "risk_factors": {"form": "10-K", "filed": "f", "url": "u", "chars_total": 1, "headlines": [],
                          "excerpt": lead[:4000], "delta": None}}
    assert F.size(F.shrink(d)) <= F.MAX_CHARS + 500


# ---------------------------------------------------------------- live (opt-in)

LIVE = os.environ.get("QT_LIVE") == "1" and bool(os.environ.get("SEC_CONTACT"))


@pytest.mark.network
@pytest.mark.skipif(not LIVE, reason="needs QT_LIVE=1 and SEC_CONTACT")
@pytest.mark.parametrize("ticker", ["XOM", "NVDA", "SHEL", "CNQ"])
def test_live_documents(ticker, tmp_path):
    d = SECFilings(Store(tmp_path / "live.sqlite")).documents(ticker, datetime.now(UTC).date())
    assert d["ciks"] and d["risk_factors"] and d["risk_factors"]["chars_total"] > 1000
    assert d["results"] is not None
    if ticker == "XOM":
        assert 34088 in d["ciks"] and d["cik"] == 34088
    if ticker in ("SHEL", "CNQ"):
        assert d["foreign"] is True


# ---------------------------------------------------------------- robustness: circuit breaker and missing sources

def test_sec_circuit_breaker_stops_after_consecutive_failures(monkeypatch):
    """After MAX_SEC_FAILURES failed SEC requests in a row, the rest of the run makes no SEC calls."""
    import qt.data.http as H
    import qt.data.textfetch as T
    monkeypatch.setattr(H.time, "sleep", lambda _s: None)
    monkeypatch.setattr(T.time, "sleep", lambda _s: None)
    f, s = sec({F.TICKERS_URL: (503, "down")})
    for sym in ("NVDA", "XOM", "SHEL", "CNQ", "MUR", "TTE"):
        d = f.documents(sym, AS_OF)
        assert d["errors"] and d["current"] == []
    assert f.down and len(s.calls) == F.MAX_SEC_FAILURES * 3            # 3 tries each, then nothing
    assert "SEC unreachable" in f.documents("NVDA", AS_OF)["errors"][0]


def test_missing_sources_leave_an_error():
    routes = nvda_routes()
    url = "https://data.sec.gov/submissions/CIK0001045810.json"
    sub = json.loads(routes[url][1])
    rec = sub["filings"]["recent"]
    keep = [i for i, frm in enumerate(rec["form"]) if frm not in F.ANNUAL_FORMS and "2.02" not in rec["items"][i]]
    sub["filings"]["recent"] = {k: [v[i] for i in keep] for k, v in rec.items()}
    routes[url] = (200, json.dumps(sub))
    d = sec(routes)[0].documents("NVDA", AS_OF)
    assert d["results"] is None and d["risk_factors"] is None
    assert any("no results release" in e for e in d["errors"])
    assert any("no annual report" in e for e in d["errors"])


def test_dotted_ticker_resolves_to_the_dash_form():
    f, _ = sec({F.TICKERS_URL: (200, json.dumps({"0": {"cik_str": 1067983, "ticker": "BRK-B", "title": "B"}}))})
    assert f.cik("BRK.B") == 1067983


def test_results_older_than_the_window_are_not_the_latest():
    """A five-year-old release must never pass as the latest one: None plus an error naming the newest date."""
    f, _ = sec()
    errs: list[str] = []
    latest, prev = f.results("NVDA", date(2027, 6, 1), errors=errs)
    assert latest is None and prev is None
    assert any(f"last {F.MAX_RESULT_AGE_DAYS} days" in e and "2026-08-26" in e for e in errs)


def test_6k_titled_by_its_period_end_is_a_results_release():
    f, _ = sec({})
    url = "https://www.sec.gov/Archives/edgar/data/879764/x/tte.htm"
    f._index = lambda cik, acc: [{"type": "EX-99.1", "url": url, "description": "TotalEnergies SE_2026-06-30"}]
    f.exhibit_extract = lambda u, outlook_cap=4000: {"title": "TotalEnergies SE_2026-06-30", "lead": "x" * 50}
    row = {"cik": 879764, "accessionNumber": "0000879764-26-000001", "form": "6-K", "filingDate": "2026-07-23",
           "reportDate": "2026-06-30"}
    rel = f._release(row)
    assert rel is not None and rel["date"] == "2026-07-23" and rel["source"] == "sec:6-K EX-99.1"
    assert f._release(dict(row, reportDate="2026-07-23")) is None       # not a period end: title words decide


@pytest.mark.parametrize("head", ["Item 1. Business", "Items 1 and 2. Business and Properties", "ITEMS 1 & 2. BUSINESS AND PROPERTIES"])
def test_the_business_section_under_its_10k_headings(head):
    """Talos files "Items 1 and 2. Business and Properties" (the dry run of 24.10 found no business section)."""
    body = "We explore for and produce oil and natural gas in the Gulf of America. " * 5
    text = f"Table of Contents\n{head}\n1\nItem 1A. Risk Factors\n2\n\nPART I\n{head}\nOverview\n{body}\nItem 1A. Risk Factors\nRisks."
    biz = F.cut_business(text, "10-K")
    assert biz.splitlines()[0] == head and "Gulf of America" in biz and "Risks." not in biz
