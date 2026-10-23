"""DC-25, the deep-dive article: code keeps a sentence only when its fact ids exist, its numbers are in the facts it
cites, and its quotes appear word for word in them (qt.research.article)."""
import copy
import json
from datetime import date
from pathlib import Path

from qt.facts import FactSheet
from qt.research import article as AR
from qt.research import judge as JG

FIX = json.loads((Path(__file__).parent.parent / "fixtures" / "research_dossier.json").read_text(encoding="utf-8"))
AS_OF = date(2026, 10, 20)
COMP = ("The market for our products is intensely competitive and is characterized by rapid technological change "
        "and evolving industry standards.")


def _sheet() -> tuple[FactSheet, dict]:
    fs = FactSheet()
    ids = {
        "pe": fs.add("SHEL.row.pe1", 8.4214, "code:research", AS_OF).fact_id,
        "sector_pe": fs.add("SHEL.sector_avg.pe", 26.3016, "code:research", AS_OF).fact_id,
        "gm": fs.add("SHEL.fin_data.grossMargins", 0.2609, "yahoo:financialData", AS_OF).fact_id,
        "rev": fs.add("SHEL.row.sales", 266886000000, "code:research", AS_OF).fact_id,
        "comp": fs.add("X.business.competition[0]", COMP, "sec:10-K Item 1 Competition https://www.sec.gov/x.htm",
                       date(2026, 2, 25), url="https://www.sec.gov/x.htm").fact_id,
        "guide": fs.add("X.results.guidance[0]", "[8-K 2026-08-26] revenue Q 52000000000.0-54000000000.0 USD: Revenue "
                        "is expected to be $53.0 billion, plus or minus 2%.", "sec:8-K https://www.sec.gov/r.htm",
                        date(2026, 8, 26), url="https://www.sec.gov/r.htm").fact_id,
    }
    return fs, ids


def S(text, ids, quotes=()):
    return {"text": text, "fact_ids": list(ids), "quotes": list(quotes)}


def test_numbers_are_read_at_the_precision_written():
    got = AR.numbers_in_text("ההכנסות 266.9 מיליארד דולר, מרווח 26%, מכפיל 8.4 לפי S07-R19 וה-10-K, RSI(20) ו-Q2/26")
    assert [(round(v), p, t) for v, _, p, t in got] == [(266900000000, False, "266.9 מיליארד"), (26, True, "26%"),
                                                       (8, False, "8.4")]
    v, tol, _, _ = AR.numbers_in_text("$4.5B")[0]
    assert v == 4.5e9 and abs(tol - 0.05e9) < 1


def test_number_must_be_in_a_cited_fact():
    fs, f = _sheet()
    assert AR.check_sentence(S("המכפיל הצפוי 8.4 מול 26.3 בסקטור.", [f["pe"], f["sector_pe"]]), fs) is None
    assert AR.check_sentence(S("המכפיל הצפוי 8 מול 26 בסקטור.", [f["pe"], f["sector_pe"]]), fs) is None   # rounded
    assert AR.check_sentence(S("המרווח הגולמי 26.1%.", [f["gm"]]), fs) is None                     # a fraction as %
    assert AR.check_sentence(S("ההכנסות 266.9 מיליארד דולר.", [f["rev"]]), fs) is None
    assert "8.4" in AR.check_sentence(S("המכפיל 8.4.", [f["sector_pe"]]), fs)                      # not cited here
    assert "8.6" in AR.check_sentence(S("המכפיל 8.6.", [f["pe"]]), fs)                             # wrong number
    assert "68" in AR.check_sentence(S("הנחה של 68% מול הסקטור.", [f["pe"], f["sector_pe"]]), fs)  # computed: dropped
    assert AR.check_sentence(S("שני מתחרים ו-3 סגמנטים.", [f["comp"]]), fs) is None               # small counts pass
    assert AR.check_sentence(S("בלי מקור.", []), fs) == "no fact cited"
    assert "F-999" in AR.check_sentence(S("טקסט.", ["F-999"]), fs)


def test_quotes_must_appear_word_for_word_in_the_fact_they_name():
    fs, f = _sheet()
    ok = {"fact_id": f["comp"], "text": "intensely competitive and is characterized by rapid technological change",
          "he": "תחרותי מאוד"}
    assert AR.check_sentence(S("השוק תחרותי מאוד.", [f["comp"]], [ok]), fs) is None
    joined = dict(ok, text="The market for our products … evolving industry standards")
    assert AR.check_sentence(S("השוק תחרותי.", [f["comp"]], [joined]), fs) is None
    bad = dict(ok, text="intensely competitive with few barriers")
    assert "does not appear" in AR.check_sentence(S("השוק תחרותי.", [f["comp"]], [bad]), fs)
    assert "does not cite" in AR.check_sentence(S("השוק תחרותי.", [f["pe"]], [ok]), fs)
    curly = dict(ok, text="The market for our products is  intensely   competitive")       # spacing aside
    assert AR.check_sentence(S("השוק תחרותי.", [f["comp"]], [curly]), fs) is None
    g = {"fact_id": f["guide"], "text": "Revenue is expected to be $53.0 billion, plus or minus 2%", "he": "תחזית"}
    assert AR.check_sentence(S("ההנהלה צופה הכנסות של 53 מיליארד דולר ברבעון.", [f["guide"]], [g]), fs) is None


def test_build_drops_failed_sentences_numbers_the_notes_and_marks_empty_sections():
    fs, f = _sheet()
    out = {"headline": S("Shell זולה מהסקטור.", [f["pe"], f["sector_pe"]]), "dek": S("מכפיל 8.4.", [f["pe"]]),
           "sections": {k: {"answer": "yes", "paragraphs": []} for k in AR.SECTION_KEYS}}
    out["sections"]["vs_sector"]["paragraphs"] = [{"sentences": [
        S("המכפיל 8.4 מול 26.3 בסקטור.", [f["pe"], f["sector_pe"]]), S("הנחה של 68%.", [f["pe"]])]}]
    out["sections"]["advantage"] = {"answer": "partial", "paragraphs": [{"sentences": [
        S("השוק תחרותי מאוד.", [f["comp"]], [{"fact_id": f["comp"], "text": "intensely competitive", "he": "תחרותי"}])]}]}
    art = AR.build(out, fs, AS_OF)
    assert art["headline"]["notes"] == [1, 2] and art["dek"]["notes"] == [1]
    vs = art["sections"]["vs_sector"]
    assert vs["answer"] == "yes" and len(vs["paragraphs"][0]) == 1 and vs["paragraphs"][0][0]["notes"] == [1, 2]
    assert art["sections"]["advantage"]["answer"] == "no_source"          # "intensely competitive" is two words
    assert art["sections"]["business"] == {"answer": "no_source", "paragraphs": []}
    assert [d["section"] for d in art["dropped"]] == ["vs_sector", "advantage"]
    assert art["stats"] == {"kept": 3, "dropped": 2, "facts": len(fs)}
    assert [n["n"] for n in art["notes"]] == [1, 2] and art["notes"][0]["key"] == "SHEL.row.pe1"


def test_notes_carry_the_link_date_and_a_readable_source():
    fs, f = _sheet()
    out = {"headline": S("השוק תחרותי מאוד.", [f["comp"]]), "dek": {}, "sections": {}}
    n = AR.build(out, fs)["notes"][0]
    assert n["url"] == "https://www.sec.gov/x.htm" and n["as_of"] == "2026-02-25"
    assert n["source"] == "SEC · 10-K Item 1 Competition" and n["value"].startswith("The market")


def test_the_article_sheet_adds_macro_sector_statements_chart_and_the_earlier_answers():
    d = copy.deepcopy(FIX["ideas"]["SHEL"])
    d["tech"] = {"rsi": [55.0, 61.234], "macd": [0.1], "signal": [0.05], "hist": [0.05], "adr_ratio": [None, 1.1],
                 "rs_sector": [100, 104.2], "rs_spy": [100, 108.0], "weekly": {"close": [100, 105.5]},
                 "periods": {"rsi": 20, "macd": [20, 60, 10], "adr_wait": 1.5}}
    d["documents"] = {"business": {"form": "10-K", "filed": "2026-02-25", "url": "https://www.sec.gov/x.htm",
                                   "overview": ["Shell is an energy company."], "segments": [], "competition": [COMP],
                                   "kpi": []}}
    d["peers"] = [{"sym": "XOM", "name": "Exxon", "industry": "Oil & Gas Integrated", "same": "industry",
                   "market_cap": 4.5e11, "eg1": 12.3456, "pe1": 14.2, "on_list": True}]
    text = JG.article_facts(d, FIX["macro"], AS_OF, d["judgment"], FIX["sectors"]).render()
    assert "sector.XLE.macro = 6" in text and "macro.fed.period = easing" in text
    assert "SHEL.statements.quarterly.TotalRevenue = {'2025-06-30'" in text
    assert "SHEL.chart.now.rsi = 61.234" in text and "SHEL.chart.now.adr20_over_adr60 = 1.1" in text
    assert "SHEL.judgment.DC-11 = justified:" in text and "SHEL.judgment.DC-12[0]" in text
    assert "SHEL.business.competition[0] = The market" in text and "sec:10-K Item 1 Competition https" in text
    assert "SHEL.peers[0] = {'sym': 'XOM', 'name': 'Exxon'" in text and "'eg1': 12.35" in text
    assert text.count("macro.stance.zone") == 1
