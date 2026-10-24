"""Package C wiring: SEC documents and peers into the deep dive, the catalyst rows and heat into the trade plan,
the report's catalyst section, and the watchlist entry's heat fields (no network)."""
import copy
import json
import math
from datetime import date, timedelta
from pathlib import Path

import pytest

from qt.calc.volatility import Bar
from qt.research import idea, report, run
from qt.watch import WatchEntry, check, from_dossier

FIX = json.loads((Path(__file__).parent.parent / "fixtures" / "research_dossier.json").read_text(encoding="utf-8"))
SYM, AS_OF = "SHEL", date(2026, 10, 20)


def _bars() -> list[Bar]:
    out, d, i = [], AS_OF - timedelta(days=365 * 8 + 10), 0
    while d <= AS_OF:
        if d.weekday() < 5:
            c = 40 + i * 0.02 + 3 * math.sin(i / 15)
            out.append(Bar(d, c * 0.995, c * 1.01, c * 0.99, c, 1))
            i += 1
        d += timedelta(days=1)
    return out


class FakeYahoo:
    def __init__(self):
        self.b = _bars()

    def fundamentals(self, sym):
        return {}

    def bars(self, sym, start, end):
        return [b for b in self.b if start <= b.day <= end]

    def news(self, sym):
        return []


def _universe():
    i = FIX["ideas"][SYM]
    row = dict(i["row"], ticker=SYM)
    peers = [{"ticker": "BP"}, {"ticker": "XOM"}, {"ticker": "EOG"}]
    screens = {"XLE": {"long": [row, peers[1], peers[0]], "short": [peers[2]], "averages": i["sector_avg"]}}
    summ = {SYM: {"assetProfile": {"industry": "Oil & Gas Integrated"}},
            "BP": {"assetProfile": {"industry": "Oil & Gas Integrated"},
                   "calendarEvents": {"earnings": {"earningsDate": [{"raw": 0, "fmt": "2026-11-04"}]}}},
            "XOM": {"assetProfile": {"industry": "Oil & Gas Integrated"}},
            "EOG": {"assetProfile": {"industry": "Oil & Gas E&P"}}}
    return {"summaries": summ, "quotes": {SYM: i["quote"]}}, screens


@pytest.fixture
def patched(monkeypatch):
    i = FIX["ideas"][SYM]
    monkeypatch.setattr(idea, "fundamentals_view", lambda *a, **k: copy.deepcopy(i["fundamentals"]))
    monkeypatch.setattr(idea, "valuation", lambda *a, **k: copy.deepcopy(i["valuation"]))
    monkeypatch.setattr(idea, "timing", lambda *a, **k: copy.deepcopy(i["timing"]))
    monkeypatch.setattr(idea, "catalysts", lambda *a, **k: copy.deepcopy(i["catalysts"]))


def _docs_contract(sym="SHEL"):
    return {"symbol": sym, "cik": 1306965, "ciks": [1306965], "foreign": True, "as_of": AS_OF.isoformat(),
            "current": [{"form": "6-K", "date": "2026-10-08", "accession": "0001306965-26-000111", "items": [],
                         "item_names": [], "kind": "results", "title": "Third quarter 2026 update note",
                         "url": "https://www.sec.gov/Archives/edgar/data/1306965/x/d1.htm",
                         "index_url": "https://www.sec.gov/Archives/edgar/data/1306965/x/0001306965-26-000111-index.htm",
                         "exhibits": [{"type": "EX-99.1", "url": "https://www.sec.gov/ex991.htm",
                                       "title": "Third quarter 2026 update note", "lead": "Integrated gas ..."}]}],
            "results": {"form": "6-K", "date": "2026-07-30", "url": "https://www.sec.gov/res.htm",
                        "source": "sec:6-K EX-99.1", "title": "Second quarter 2026 results", "lead": "Shell plc ...",
                        "outlook": ["Integrated Gas production is expected to be 940 - 1,000 thousand boe/d in Q3."],
                        "guidance": [], "commentary": None},
            "results_prev": None,
            "risk_factors": {"form": "20-F", "filed": "2026-03-12", "url": "https://www.sec.gov/20f.htm",
                             "chars_total": 44844, "headlines": ["Climate change and the energy transition",
                                                                 "Rising geopolitical tension"],
                             "excerpt": "Our operations ...", "delta": None},
            "transcript": None, "errors": []}


class CountingSEC:
    def __init__(self):
        self.calls = []

    def ok(self):
        return True

    def documents(self, sym, as_of):
        self.calls.append(sym)
        return _docs_contract(sym)


def _dd(y, u, screens, j, docs, peers):
    return run.deep_dive(y, u, screens, SYM, "XLE", "long", dict(j, options=False), "neutral", {"XLE": "long"}, AS_OF,
                         docs=docs, peers=peers)


def test_docs_attached_once_per_symbol(patched):
    y, (u, screens) = FakeYahoo(), _universe()
    sec, memo = CountingSEC(), {}
    docs = run.documents_source(sec, AS_OF, memo)

    def peers(sym, etf):
        return run.peers_for(u, screens, sym, etf, {"XOM"}, AS_OF)
    d1 = _dd(y, u, screens, {"etf": "XLE", "direction": "long"}, docs, peers)          # the judge's pass
    d2 = _dd(y, u, screens, {"etf": "XLE", "direction": "long", "heat": "hot"}, docs, peers)  # the ideas pass
    assert sec.calls == [SYM] and list(memo) == [SYM]
    assert d1["documents"]["current"][0]["form"] == "6-K" and d2["documents"] is d1["documents"]
    ps = d1["peers"]
    assert [p["sym"] for p in ps] == ["XOM", "BP", "EOG"]                  # same industry first, screen order
    assert [p["same"] for p in ps] == ["industry", "industry", "etf"]
    bp = next(p for p in ps if p["sym"] == "BP")
    assert bp["next_earnings"] == "2026-11-04" and not bp["on_list"]
    assert next(p for p in ps if p["sym"] == "XOM")["on_list"]
    nod = _dd(y, u, screens, {"etf": "XLE", "direction": "long"}, None, None)
    assert nod["documents"] is None and nod["peers"] == []


def test_no_contact_means_no_http_and_an_error():
    from qt.data.filings import SECFilings

    class NoNet:
        def get(self, *a, **k):
            raise AssertionError("no HTTP without SEC_CONTACT")
    sec = SECFilings(None, contact="", session=NoNet())
    memo = {}
    out = run.documents_source(sec, AS_OF, memo)(SYM)
    assert out["errors"] == ["SEC_CONTACT not set"] and out["transcript"] is None


def test_trade_plan_gets_heat(patched):
    y, (u, screens) = FakeYahoo(), _universe()
    rows = [{"id": "EV01", "date": "2026-10-29", "timing": "expected", "kind": "own_report", "class": "A",
             "direction": "for_idea", "priced_in": False, "persistence": "until_next_report", "what": "דוח רבעון"},
            {"id": "EV02", "date": "2027-03-05", "timing": "expected", "kind": "other_event", "class": "B",
             "direction": "for_idea", "priced_in": False, "persistence": "none", "what": "יום משקיעים"},
            {"id": "EV03", "date": "2027-02-05", "timing": "expected", "kind": "competitor_results", "class": "C",
             "direction": None, "priced_in": False, "persistence": "none", "what": "דוח של מתחרה: BP"}]
    hot = _dd(y, u, screens, {"etf": "XLE", "direction": "long", "heat": "hot", "catalysts": rows}, None, None)
    p = hot["plan"]
    assert p["horizon_basis"] == "catalyst" and p["horizon_months"] >= 4.4 and p["catalyst_chain"]
    assert p["horizon_months"] >= p["horizon_sigma_months"]                      # never below sigma (S10-R35)
    assert "S08-R28" not in [f["rule_id"] for f in p["verdict"]["findings"]]
    peer_only = _dd(y, u, screens, {"etf": "XLE", "direction": "long", "heat": "hot", "catalysts": [rows[0], rows[2]]},
                    None, None)                                             # a peer C is never a link (D-S08-2)
    assert peer_only["plan"]["horizon_basis"] == "sigma" and not peer_only["plan"]["catalyst_chain"]
    cold = _dd(y, u, screens, {"etf": "XLE", "direction": "long", "heat": "cold", "catalysts": rows}, None, None)
    assert "S08-R28" in [f["rule_id"] for f in cold["plan"]["verdict"]["findings"]]
    adverse = _dd(y, u, screens, {"etf": "XLE", "direction": "long", "news_adverse": True}, None, None)
    assert "S08-R28" in [f["rule_id"] for f in adverse["plan"]["verdict"]["findings"]]
    none = _dd(y, u, screens, {"etf": "XLE", "direction": "long", "catalysts": [dict(rows[0], **{"class": "C"})]},
               None, None)
    assert "S07-R13" in [f["rule_id"] for f in none["plan"]["verdict"]["findings"]]   # code: no counted catalyst
    assert "S08-R28" in [f["rule_id"] for f in none["plan"]["verdict"]["findings"]]   # no heat written: cold (S08-R33)


def test_hand_written_rows_without_ids_keep_working(patched):
    y, (u, screens) = FakeYahoo(), _universe()
    j = copy.deepcopy(FIX["ideas"][SYM]["judgment"])                       # rows with no id, timing or persistence
    d = _dd(y, u, screens, j, None, None)
    assert d["plan"]["catalyst_chain"] == j["catalyst_chain"]               # the hand-written chain wins


def test_report_shows_chain():
    d = copy.deepcopy(FIX)
    i = d["ideas"][SYM]
    i["documents"] = _docs_contract()
    i["plan"].update(horizon_basis="catalyst", horizon_sigma_months=2.1, horizon_months=4.6,
                     catalyst_chain="2026-10-29 A דוח רבעון; 2027-03-05 C דוח של מתחרה: BP",
                     catalyst_wait="no catalyst in the next month; first on 2026-12-01: extend the horizon or wait")
    i["judgment"]["catalysts"][0].update(decided_by="DC-14", persistence="until_next_report", timing="expected",
                                         expected_date="2026-10-29")
    i["judgment"].update(catalyst_expected=True, code_overrides=[])
    h = report.render(d)
    for s in ("אופק לפי שרשרת קטליסטים", "2027-03-05 C דוח של מתחרה: BP", "extend the horizon or wait",
              "0001306965-26-000111-index.htm", "Climate change and the energy transition", "https://www.sec.gov/20f.htm",
              "אין תמלול של שיחת הוועידה", "עד הדוח הבא", "צפוי 2026-10-29",
              "Integrated Gas production is expected"):
        assert s in h, s


def test_watch_entry_carries_heat_catalysts_and_peers():
    d = copy.deepcopy(FIX)
    i = d["ideas"][SYM]
    i["judgment"].update(heat="hot", catalyst_expected=True)
    i["peers"] = [{"sym": "BP", "etf": "XLE", "same": "industry", "on_list": False, "next_earnings": "2026-11-04"}]
    e = next(x for x in from_dossier(d)["entries"] if x["symbol"] == SYM)
    assert e["heat"] == "hot" and e["catalyst_expected"] is True and e["peers"] == ["BP"]
    assert [c["date"] for c in e["catalysts"]] == ["2026-10-29"]            # the past, priced-in rows drop out
    we = WatchEntry(**e)
    bars = FakeYahoo().bars(SYM, AS_OF - timedelta(days=400), AS_OF)
    c = check(we, bars, date(2026, 10, 29) if bars[-1].day >= date(2026, 10, 29) else AS_OF)
    assert c.heat == "hot"
    assert check(we, bars, AS_OF, heat="frozen").status == "frozen"
