"""The macro file without a person (qt.data.macro_auto): the parsers on the sources' formats, the checks, the merge
over the seed, and the monthly and weekly runs going on without an uploaded file."""
from datetime import date, datetime, timezone

import yaml

from qt.approvals import Queue
from qt.data import macro_auto as MA
from qt.pipelines import PIPELINES
from qt.runs import Runner

AS_OF = date(2026, 11, 14)          # October's ISM (published 2.11) and NFIB (published 10.11) are out

PRN_HTML = """
<a href="/news-releases/manufacturing-pmi-at-52-1-october-2026-ism-manufacturing-pmi-report-302911111.html">x</a>
<a href="/news-releases/services-pmi-at-55-0-october-2026-ism-services-pmi-report-302912222.html">x</a>
<a href="/news-releases/manufacturing-pmi-at-54-5-september-2026-ism-manufacturing-pmi-report-302894520.html">x</a>
<a href="/news-releases/something-else-302000000.html">x</a>
"""

PMI_BODY = """<html><body><h1>Manufacturing PMI® at 52.1%; October 2026 ISM® Manufacturing PMI® Report</h1>
<p>The Manufacturing PMI® registered 52.1 percent in October, 2.4 percentage points below the September figure of
54.5 percent.</p>
<p>The nine manufacturing industries reporting growth in October — in the following order — are: Primary Metals;
Machinery; Computer &amp; Electronic Products; and Chemical Products. The five industries reporting contraction in
October — in the following order — are: Textile Mills; Wood Products; Apparel, Leather &amp; Allied Products; and Paper
Products.</p>
<h3>Commodities in Short Supply</h3>
<p>Aluminum (2); Electrical Components (11); and Steel Products (4).</p>
<p>Note: The number of consecutive months the commodity is listed is indicated after each item.</p>
</body></html>"""

NMI_BODY = """<p>Services PMI® at 55.0%; October 2026 ISM® Services PMI® Report</p>
<p>The Services PMI® registered 55.0 percent in October, 0.1 percentage point above the September figure.</p>"""

GNEWS_PMI = """<rss><channel><item><title>Manufacturing PMI® at 52.1%; October 2026 ISM® Manufacturing PMI® Report - PR Newswire</title>
<link>https://news.google.com/rss/articles/abc</link><pubDate>Mon, 02 Nov 2026 15:00:00 GMT</pubDate></item></channel></rss>"""
GNEWS_NMI = GNEWS_PMI.replace("Manufacturing", "Services").replace("52.1", "55.0")
GNEWS_NFIB = """<rss><channel><item><title>NFIB Small Business Optimism Index Rises to 98.6 in October</title>
<link>https://news.google.com/rss/articles/def</link><pubDate>Tue, 10 Nov 2026 11:00:00 GMT</pubDate></item></channel></rss>"""
NFIB_PDF_TEXT = ("NFIB SMALL BUSINESS ECONOMIC TRENDS\nThe NFIB Small Business Optimism Index rose 0.7 points in October "
                 "to 98.6, above the 52-year average of 98.0.")
NAAIM_HTML = "<div><p>This week's NAAIM Exposure Index number is: 79.27</p><p>Last Quarter Average: 82.00</p></div>"


class FakeHttp:
    def __init__(self, pages: dict[str, str | bytes], fail: set[str] = frozenset()):
        self.pages, self.fail, self.seen = pages, fail, []

    def bytes(self, url):
        self.seen.append(url)
        for k, v in self.pages.items():
            if k in url and not any(f in url for f in self.fail):
                return v if isinstance(v, bytes) else v.encode()
        raise RuntimeError("HTTP 404")

    def text(self, url):
        return self.bytes(url).decode()


def _pages():
    return {"institute-for-supply-management": PRN_HTML, "manufacturing-pmi-at-52-1": PMI_BODY,
            "services-pmi-at-55-0": NMI_BODY, "Manufacturing+PMI": GNEWS_PMI, "Services+PMI": GNEWS_NMI,
            "Small+Business+Optimism": GNEWS_NFIB, "NFIB-SBET-Report-October-2026": b"%PDF", "naaim.org": NAAIM_HTML}


def _sources(monkeypatch, pages=None, fail=frozenset()):
    monkeypatch.setattr(MA, "_pdf_text", lambda blob: NFIB_PDF_TEXT)
    return MA.Sources(FakeHttp(_pages() if pages is None else pages, set(fail)))


# ------------------------------------------------------------------ parsers

def test_parsers_read_the_sources_formats():
    assert MA.prn_releases(PRN_HTML)[0][:3] == ("ism_pmi", "2026-10", 52.1)
    assert [r[:3] for r in MA.prn_releases(PRN_HTML)][1] == ("ism_nmi", "2026-10", 55.0)
    body = MA._text(PMI_BODY)
    assert MA.ism_headline(body, "ism_pmi")[:2] == ("2026-10", 52.1)
    assert MA.ism_headline(MA._text(NMI_BODY), "ism_nmi")[:2] == ("2026-10", 55.0)
    t = MA.ism_tables(body)
    assert t["growing"] == ["Primary Metals", "Machinery", "Computer & Electronic Products", "Chemical Products"]
    assert t["contracting"][-2:] == ["Apparel, Leather & Allied Products", "Paper Products"]
    assert t["short_supply"] == {"Aluminum": 2, "Electrical Components": 11, "Steel Products": 4}
    assert MA.gnews_ism(GNEWS_PMI, "ism_pmi")[0][:2] == ("2026-10", 52.1)
    assert MA.gnews_nfib(GNEWS_NFIB)[0][:2] == ("2026-10", 98.6)
    jan = GNEWS_NFIB.replace("October", "December").replace("10 Nov 2026", "12 Jan 2027")
    assert MA.gnews_nfib(jan)[0][0] == "2026-12"                       # December's report comes out in January
    assert MA.nfib_pdf_value(NFIB_PDF_TEXT, "2026-10")[0] == 98.6
    assert MA.naaim_value(MA._text(NAAIM_HTML))[0] == 79.27
    assert MA.last_wednesday(date(2026, 10, 24)) == date(2026, 10, 21)
    assert MA.last_wednesday(date(2026, 10, 21)) == date(2026, 10, 14)


def test_checks_bound_the_value_the_jump_and_the_month():
    known = {"2026-08": 54.6, "2026-09": 54.5}
    assert MA.check("ism_pmi", "2026-10", 52.1, known, AS_OF) is None
    assert "outside" in MA.check("ism_pmi", "2026-10", 5.21, known, AS_OF)
    assert "from 2026-09" in MA.check("ism_pmi", "2026-10", 44.0, known, AS_OF)
    assert "not before" in MA.check("ism_pmi", "2026-11", 52.0, known, AS_OF)
    assert "months after" in MA.check("ism_pmi", "2027-06", 52.0, known, date(2027, 7, 10))
    assert MA.expected_month("ism_pmi", date(2026, 11, 3)) == "2026-09"
    assert MA.expected_month("ism_pmi", AS_OF) == "2026-10"
    assert MA.expected_month("small_business", date(2026, 11, 14)) == "2026-09"   # NFIB by the 16th
    assert MA.expected_month("small_business", date(2026, 11, 21)) == "2026-10"


# ------------------------------------------------------------------ refresh

def test_refresh_adds_the_new_months_over_the_seed_with_sources(monkeypatch):
    rep = MA.refresh(None, date(2026, 11, 21), _sources(monkeypatch), now=datetime(2026, 11, 21, tzinfo=timezone.utc))
    f = yaml.safe_load(rep["content"])
    assert f["ism_pmi"]["2026-10"] == 52.1 and f["ism_nmi"]["2026-10"] == 55.0 and f["small_business"]["2026-10"] == 98.6
    assert f["ism_pmi"]["2025-01"] == 50.9                               # the seed history stays
    assert f["ism_growing"]["2026-10"][0] == "Primary Metals" and f["ism_short_supply"]["2026-10"]["Aluminum"] == 2
    assert f["naaim"] == {"2026-11-18": 79.27}
    src = f["sources"]["ism_pmi"][-1]
    assert src["url"].startswith("https://www.prnewswire.com/news-releases/manufacturing-pmi-at-52-1")
    assert "registered 52.1 percent in October" in src["quote"] and src["checked"].startswith("https://news.google")
    assert f["sources"]["small_business"][-1]["url"].endswith("NFIB-SBET-Report-October-2026.pdf")
    assert rep["changed"] and not rep["stale"] and not rep["rejected"]
    assert "נוספו" in MA.lines(rep)[0]


def test_refresh_never_overwrites_and_flags_disagreement_and_stale(monkeypatch):
    latest = {"content": "ism_pmi: {'2026-09': 54.5, '2026-10': 51.0}\n"}
    pages = _pages()
    pages["Services+PMI"] = GNEWS_NMI.replace("55.0", "56.0")           # the check source disagrees
    rep = MA.refresh(latest, date(2026, 11, 21), _sources(monkeypatch, pages, fail={"nfib.com", "Small+Business"}))
    f = yaml.safe_load(rep["content"])
    assert f["ism_pmi"]["2026-10"] == 51.0                               # kept: a month in the file is not replaced
    assert "2026-10" not in f["ism_nmi"] and "disagree" in rep["rejected"]["ism_nmi"][0]
    assert rep["stale"]["small_business"] == {"have": "2026-09", "expected": "2026-10"}
    assert any("nfib pdf" in e for e in rep["errors"]["small_business"])
    text = "\n".join(MA.lines(rep))
    assert "NFIB" in text and "נדחה" in text


def test_refresh_with_every_source_down_keeps_the_file(monkeypatch):
    rep = MA.refresh({"content": "ism_pmi: {'2026-09': 54.5}\n"}, date(2026, 11, 21), _sources(monkeypatch, {}))
    assert not rep["changed"] and set(rep["stale"]) == {"ism_pmi", "ism_nmi", "small_business"}


def test_the_check_source_stands_in_when_the_primary_is_down(monkeypatch):
    rep = MA.refresh(None, date(2026, 11, 21), _sources(monkeypatch, fail={"prnewswire", "ismworld"}))
    f = yaml.safe_load(rep["content"])
    assert f["ism_pmi"]["2026-10"] == 52.1 and f["sources"]["ism_pmi"][-1]["url"].startswith("https://news.google")


# ------------------------------------------------------------------ the runs

class Bot:
    def __init__(self):
        self.sent = []

    def send(self, text, buttons=None):
        self.sent.append(("send", text, buttons))
        return 1


def test_monthly_runs_without_an_uploaded_file(db, monkeypatch):
    from test_monthly import Fred
    monkeypatch.setattr(MA, "_pdf_text", lambda blob: NFIB_PDF_TEXT)
    bot = Bot()
    svc = {"telegram": bot, "fred": Fred(), "macro_auto": lambda: MA.Sources(FakeHttp({}))}
    r = Runner(db, PIPELINES, svc)
    rid = r.start("monthly", date(2026, 11, 14))              # every source down: the seed, flagged stale, no waiting
    out = db.one("SELECT output FROM run_steps WHERE run_id=? AND step='manual'", (rid,), ["output"])["output"]
    assert out["auto"]["stale"]["ism_pmi"]["have"] == "2026-09"
    assert Queue(db).latest_upload("manual_macro")["name"] == "auto-2026-11-14.yaml"
    assert not any("מחכה לקובץ" in s[1] for s in bot.sent)
    assert any("ISM תעשייה: הנתון האחרון 2026-09" in s[1] for s in bot.sent)


def test_monthly_fetches_the_new_month(db, monkeypatch):
    from test_monthly import Fred
    monkeypatch.setattr(MA, "_pdf_text", lambda blob: NFIB_PDF_TEXT)
    svc = {"telegram": Bot(), "fred": Fred(), "macro_auto": lambda: MA.Sources(FakeHttp(_pages()))}
    rid = Runner(db, PIPELINES, svc).start("monthly", date(2026, 11, 14))
    ism = db.one("SELECT output FROM run_steps WHERE run_id=? AND step='ism'", (rid,), ["output"])["output"]
    assert ism["available"] and ism["growing"][0]["name"] == "Primary Metals"
    se = db.one("SELECT output FROM run_steps WHERE run_id=? AND step='manual'", (rid,), ["output"])["output"]
    assert se["auto"]["added"]["ism_pmi"] == ["2026-10"]


def test_weekly_research_no_longer_waits_for_the_manual_file(db, monkeypatch):
    from test_weekly import AS_OF as W_AS_OF, Bot as WBot, _svc
    monkeypatch.setattr(MA, "_pdf_text", lambda blob: NFIB_PDF_TEXT)
    bot = WBot()
    svc = {**_svc(bot, None), "macro_auto": lambda: MA.Sources(FakeHttp(_pages()))}
    rid = Runner(db, PIPELINES, svc).start("weekly", W_AS_OF)
    out = db.one("SELECT output FROM run_steps WHERE run_id=? AND step='research'", (rid,), ["output"])["output"]
    assert "קובץ המקרו החודשי" not in out["skipped"] and Queue(db).latest_upload("manual_macro") is not None
    assert out["macro_auto"]["latest"]["ism_pmi"] >= "2026-09"
