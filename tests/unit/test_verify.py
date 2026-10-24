import json
from datetime import date
from pathlib import Path

from qt.research import report, verify

FIX = Path(__file__).parent.parent / "fixtures" / "research_dossier.json"


def _facts(rev: dict[str, float] | None = None, eps: float | None = None, unit: str = "USD",
           shares: float | None = None, tax: str = "us-gaap", rev_tag: str = "Revenues") -> dict:
    f: dict = {tax: {}, "dei": {}}
    for tag, val in (rev or {}).items():
        f[tax][tag] = {"units": {unit: [{"end": "2025-12-31", "start": "2025-01-01", "val": val, "fp": "FY", "form": "10-K"}]}}
    if eps is not None:
        tag = "EarningsPerShareDiluted" if tax == "us-gaap" else "DilutedEarningsLossPerShare"
        f[tax][tag] = {"units": {f"{unit}/shares": [{"end": "2025-12-31", "start": "2025-01-01", "val": eps, "fp": "FY",
                                                     "form": "10-K"}]}}
    if shares is not None:
        f["dei"]["EntityCommonStockSharesOutstanding"] = {"units": {"shares": [{"end": "2026-07-31", "val": shares}]}}
    return {"facts": f}


def _idea(rev=100e9, eps=5.0, price=50.0, mc=50e9, pe1=10.0, ratio=1.0, target="technical_level", last="2026-10-20",
          sales=(100e9, 110e9), direction="long", website="https://www.acme.com", judgment=None) -> dict:
    return {
        "direction": direction,
        "fundamentals": {"annual": {"TotalRevenue": [["2025-12-31", rev]], "DilutedEPS": [["2025-12-31", eps]]}},
        "quote": {"market_cap": mc}, "plan": {"entry": price, "target_source": target},
        "row": {"pe1": pe1, "sales": sales[0], "sales_f1": sales[1]}, "valuation": {"eps_basis_ratio": ratio},
        "chart": {"dates": ["2026-10-19", last]}, "profile": {"website": website}, "judgment": judgment or {},
    }


def _by(checks, field):
    return next(c for c in checks if c.field == field)


def test_statements_match_the_filing():
    cs = verify.statement_checks("ACME", _idea(), _facts({"Revenues": 100.5e9}, eps=5.02), "sec")
    assert [c.status for c in cs] == ["ok", "ok"]


def test_revenue_compares_against_the_closest_tag():
    # Chevron: the vendor reports RevenueFromContract..., the filing also has a larger Revenues line
    f = _facts({"Revenues": 189.031e9, "RevenueFromContractWithCustomerExcludingAssessedTax": 184.432e9})
    c = _by(verify.statement_checks("CVX", _idea(rev=184.432e9), f, "sec"), "revenue_fy")
    assert c.status == "ok" and c.second == 184.432e9


def test_revenue_gap_is_bad():
    c = _by(verify.statement_checks("X", _idea(rev=100e9), _facts({"Revenues": 90e9}), "sec"), "revenue_fy")
    assert c.status == "bad" and round(c.diff_pct) == 11


def test_adr_ratio_is_named_not_failed():
    # Shell: the vendor EPS is per ADR (2 shares), the 20-F per ordinary share
    c = _by(verify.statement_checks("SHEL", _idea(eps=6.0), _facts(eps=3.0, tax="ifrs-full"), "sec"), "eps_fy")
    assert c.status == "single" and "ADR" in c.note and "2" in c.note


def test_filing_in_another_currency_is_bad():
    # Eni: the 20-F is in EUR, the price in USD
    c = _by(verify.statement_checks("E", _idea(eps=1.56), _facts(eps=0.78, unit="EUR", tax="ifrs-full"), "sec"), "eps_fy")
    assert c.status == "bad" and "EUR" in c.note


def test_no_filing_is_single_source():
    cs = verify.statement_checks("X", _idea(), None, "sec")
    assert {c.status for c in cs} == {"single"}


def test_market_cap_against_price_times_shares():
    assert verify.market_cap_check("X", _idea(price=50, mc=50e9), _facts(shares=1.02e9), "sec").status == "ok"
    adr = verify.market_cap_check("SHEL", _idea(price=50, mc=25e9), _facts(shares=1e9), "sec")
    assert adr.status == "single" and "ADR" in adr.note
    assert verify.market_cap_check("X", _idea(price=50, mc=70e9), _facts(shares=1e9), "sec").status == "bad"


def test_second_close():
    i = _idea(price=50.0)
    assert verify.close_check("X", i, date(2026, 10, 20), lambda s, d: (50.1, "fmp")).status == "ok"
    assert verify.close_check("X", i, date(2026, 10, 20), lambda s, d: (51.0, "fmp")).status == "bad"
    assert verify.close_check("X", i, date(2026, 10, 20), None).status == "single"

    def down(s, d):
        raise RuntimeError("limit")
    c = verify.close_check("X", i, date(2026, 10, 20), down)
    assert c.status == "single" and "limit" in c.note


def test_sanity_ranges():
    cs = verify.sanity_checks("X", _idea(pe1=-12, sales=(1e9, 3.3e9)))
    assert _by(cs, "pe1").status == "bad" and _by(cs, "sales_f1").status == "bad"
    assert {c.status for c in verify.sanity_checks("X", _idea())} == {"ok"}


def test_eps_basis_gap_is_bad_only_when_the_target_rests_on_it():
    assert _by(verify.sanity_checks("X", _idea(ratio=0.42, target="own_multiple_target")), "eps_basis").status == "bad"
    assert _by(verify.sanity_checks("X", _idea(ratio=0.42, target="technical_level")), "eps_basis").status == "single"


def test_freshness():
    assert verify.bars_age_check("X", _idea(last="2026-10-20"), date(2026, 10, 20)).status == "ok"
    assert verify.bars_age_check("X", _idea(last="2026-10-09"), date(2026, 10, 20)).status == "bad"
    macro = {"charts": {"hy": {"dates": ["2026-10-19"]}, "ism_pmi": {"dates": ["2026-09"]},
                        "permits": {"dates": ["2026-07"]}, "michigan": {"dates": ["2026-08"]}},
             "filled_months": {"cpi": ["2025-10"]}}
    st = {c.field: c.status for c in verify.macro_checks(macro, date(2026, 10, 20))}
    assert st == {"hy": "ok", "ism_pmi": "ok", "permits": "bad", "michigan": "ok", "cpi_gap": "single"}


def test_quote_on_page_ignores_markup_and_spacing():
    page = "<p>The Manufacturing PMI&reg; registered <b>54.5</b> percent in September,\n 0.1 point below</p>"
    assert verify.quote_on_page("Manufacturing PMI® registered 54.5 percent ... 0.1 point below", page)
    assert not verify.quote_on_page("registered 55.5 percent", page)


def test_source_checks():
    no_quote = verify.source_check("X", {"url": "https://www.securities.io/a", "title": "a"}, None, None)
    assert no_quote.status == "single" and "תקציר חיפוש" in no_quote.note
    page = "Royal Gold reported revenue of $310 million"
    good = verify.source_check("RGLD", {"url": "https://www.sec.gov/x.htm", "quote": "revenue of $310 million"}, None,
                               lambda u: page)
    assert good.status == "ok" and "ראשוני" in good.note
    wrong = verify.source_check("RGLD", {"url": "https://www.sec.gov/x.htm", "quote": "revenue of $410 million"}, None,
                                lambda u: page)
    assert wrong.status == "bad"
    own_site = verify.source_check("ACME", {"url": "https://investors.acme.com/q3", "quote": "q"}, "acme.com", None)
    assert own_site.status == "ok"


def test_run_collects_every_link_once():
    d = {"meta": {"as_of": "2026-10-20"}, "macro": {"manual_sources": {"ism_pmi": [{"url": "https://ycharts.com/x"}]}},
         "judgments": {"A1": {"fed": {"sources": [{"url": "https://federalreserve.gov/a", "title": "fed"}]}}},
         "ideas": {"ACME": _idea(judgment={"sources": [{"url": "https://acme.com/r", "title": "r"},
                                                       {"url": "https://acme.com/r", "title": "r"}]})}}
    v = verify.run(d)
    src = [c for c in v["checks"] if c["field"] == "source"]
    assert sorted(c["value"] for c in src) == ["https://acme.com/r", "https://federalreserve.gov/a", "https://ycharts.com/x"]
    assert v["summary"]["worst"]["ACME"] == "single" and not v["sec"]


def test_report_shows_verification():
    d = json.loads(FIX.read_text())
    sym = next(iter(d["ideas"]))
    d["verification"] = {"checks": [{"subject": sym, "field": "pe1", "label": "מכפיל צפוי (F1)", "status": "bad", "value": -3.0,
                                     "source": "code", "second": None, "second_source": "", "diff_pct": None,
                                     "note": "מחוץ לטווח"}],
                         "summary": {"total": {"ok": 0, "single": 0, "bad": 1}}, "sec": True, "second_price": False,
                         "fetched_pages": False}
    html = report.render(d)
    assert 'id="verify"' in html and "לטיפול (1)" in html and "נתונים בעייתי" in html


def test_nasdaq_second_close(monkeypatch):
    """nasdaq.com answers every US listing without a key (FMP's free plan: HTTP 402 for TALO, 24.10)."""
    from datetime import date
    from qt.data import providers as P
    seen = {}

    def fake(url, store=None, headers=None, **k):
        seen["url"] = url
        return {"data": {"tradesTable": {"rows": [{"date": "10/23/2026", "close": "$1,018.39"},
                                                  {"date": "10/22/2026", "close": "$17.66"}]}}}
    monkeypatch.setattr(P, "get_json", fake)
    closes, src = P.Nasdaq().daily_closes("PBR-A", date(2026, 10, 16), date(2026, 10, 23))
    assert closes == {"2026-10-23": 1018.39, "2026-10-22": 17.66} and src == "nasdaq:quote/historical"
    assert "/quote/PBR.A/historical" in seen["url"]
    monkeypatch.setattr(P, "get_json", lambda *a, **k: {"data": None})
    import pytest
    with pytest.raises(P.ProviderError):
        P.Nasdaq().daily_closes("XXX", date(2026, 10, 16), date(2026, 10, 23))
