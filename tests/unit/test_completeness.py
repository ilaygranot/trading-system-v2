"""The whole-report contract (qt.research.completeness): each way the 23.10 report, or a report like it, falls short."""
from __future__ import annotations

import copy

import pytest

from qt.research import completeness as C
from qt.research import report
from dossiers import complete, fixture


def codes(c: dict, kind: str = "blocks") -> set[str]:
    return {x["code"] for x in c[kind]}


def test_a_finished_report_is_complete():
    c = C.check(complete())
    assert c["status"] == "complete" and c["blocks"] == [] and c["flags"] == []
    assert c["counts"] == {"picked": 3, "analysed": 3, "articles": 3, "backfill": 0}
    assert C.line(c).startswith("דוח מלא: 3/3 מניות, 3 מאמרים")


def test_the_hand_written_fixture_is_not_a_finished_report():
    """The 20.10 dossier was judged in a session, not by the agents: no heat, no article, no data check."""
    c = C.check(fixture())
    assert c["status"] == "partial"
    assert {"judged_by", "article_missing", "heat", "verification", "narrative", "funnel"} <= codes(c)


def test_23_10_four_of_twelve_and_no_article():
    """The run of 23.10: twelve picked, eight cut by the budget, no article at all."""
    d = complete()
    J = d["judgments"]
    J["A3"]["picked"] += ["EOG", "PBR-A"]
    J["A3"]["cut_by_budget"] = ["EOG", "PBR-A"]
    for i in d["ideas"].values():
        i["judgment"].pop("article")
    c = C.check(d)
    assert c["status"] == "partial"
    assert {"cut_by_budget", "picked_missing", "article_missing"} <= codes(c)
    assert c["counts"] == {"picked": 5, "analysed": 3, "articles": 0, "backfill": 0}
    assert "דוח חלקי" in C.line(c)


@pytest.mark.parametrize("part", ["story", "fed", "events"])
def test_a_missing_or_copied_market_story_blocks(part):
    d = complete()
    d["judgments"]["A1"].pop(part)
    assert "narrative" in codes(C.check(d))
    d = complete()
    d["judgments"]["carried"] = {"from": "2026-10-20", "what": [f"A1.{part}"]}
    c = C.check(d)
    assert "narrative" in codes(c) and "הועתק מ-2026-10-20" in c["blocks"][0]["text"]


@pytest.mark.parametrize("card", ["DC-01", "DC-02", "DC-03", "DC-04"])
def test_a1_must_decide_every_card(card):
    d = complete()
    d["judgments"]["A1"][card]["choice"] = None
    assert "a1" in codes(C.check(d))


@pytest.mark.parametrize("field,code", [("story", "story"), ("thesis", "thesis"), ("breakers", "breakers"),
                                        ("heat", "heat"), ("decision", "decision")])
def test_every_idea_carries_its_cards(field, code):
    d = complete()
    d["ideas"]["SHEL"]["judgment"].pop(field)
    assert code in codes(C.check(d))


@pytest.mark.parametrize("field", ["trigger", "k", "sizing"])
def test_a_hot_idea_has_its_trade_cards(field):
    d = complete()
    j = d["ideas"]["SHEL"]["judgment"]
    assert j["decision"] in ("long", "short")
    j.pop(field)
    assert field in codes(C.check(d))


@pytest.mark.parametrize("field", ["entry", "stop", "target", "rr"])
def test_the_plan_has_every_number(field):
    d = complete()
    d["ideas"]["TALO"]["plan"][field] = None
    assert "plan_missing" in codes(C.check(d))


def test_a_stop_or_target_on_the_wrong_side_blocks():
    d = complete()
    p = d["ideas"]["SHEL"]["plan"]
    sg = 1 if d["ideas"]["SHEL"]["direction"] == "long" else -1
    p["stop"] = p["entry"] + sg * 1
    p["target"] = p["entry"] - sg * 1
    assert {"stop_side", "target_side"} <= codes(C.check(d))


def test_an_empty_or_thin_article():
    d = complete()
    a = d["ideas"]["SHEL"]["judgment"]["article"]
    a["headline"] = None
    assert "article_empty" in codes(C.check(d))
    from qt.research import article as AR
    AR.settle(a)                                     # 25.10 PAA: the dek, which passed the check, takes its place
    c = C.check(d)
    assert c["status"] == "complete" and "headline_from_dek" in codes(c, "flags")
    a["headline"] = a["dek"] = None
    AR.settle(a)
    assert "article_empty" in codes(C.check(d))
    d = complete()
    secs = d["ideas"]["SHEL"]["judgment"]["article"]["sections"]
    for k in list(secs)[:8]:
        secs[k]["paragraphs"] = []
    c = C.check(d)
    assert c["status"] == "complete" and "article_thin" in codes(c, "flags")


def test_missing_data_inside_an_idea_blocks():
    d = complete()
    i = d["ideas"]["SHEL"]
    i["documents"] = {"errors": ["SEC: HTTP 403"]}
    i["judgment"]["call_error"] = "transcript: timeout"
    i["valuation"]["target_error"] = "no shares"
    i["judgment"].setdefault("catalysts", []).append({"date": "2026-10-30", "what": "דוח", "code_reason": "agent_error: X"})
    assert {"documents", "call_error", "target_error", "dc14_error"} <= codes(C.check(d))


def test_stocks_that_fell_out_on_a_failed_request_block():
    d = complete()
    d["meta"]["universe"] = {"n": 3000, "errors": {"AAA": "yahoo quoteSummary: HTTP 429", "BBB": "yahoo: HTTP 404"}}
    d["timing"].append({"sym": "CCC", "etf": "XLE", "error": "RuntimeError: yahoo chart: HTTP 429"})
    d["timing"].append({"sym": "DDD", "etf": "XLE", "error": "only 120 daily bars"})       # a rule, not a failure
    d["screens"]["XLE"]["dc08"] = {"EEE": {"option": "watch_only", "status": "error", "reason": "RuntimeError"}}
    d["meta"]["news"] = {"n": 40, "errors": {"FOMC minutes": "RuntimeError: HTTP 503"}, "empty": ["Treasury yields"]}
    c = C.check(d)
    assert {"universe", "timing", "dc08_error", "news"} <= codes(c) and "news_quiet" in codes(c, "flags")
    u = next(x for x in c["blocks"] if x["code"] == "universe")
    assert "1 מניות" in u["text"] and "AAA" in u["text"] and "BBB" not in u["text"]
    t = next(x for x in c["blocks"] if x["code"] == "timing")
    assert "CCC" in t["text"] and "DDD" not in t["text"]


def test_the_data_check_must_run_with_its_sources():
    d = complete()
    d["verification"]["sec"] = False
    d["verification"]["second_price"] = False
    d["verification"]["checks"] = [{"subject": "SHEL", "field": "close", "status": "single",
                                    "note": "המקור השני לא זמין: timeout"}]
    assert {"verify_sec", "verify_price", "verify_price_down"} <= codes(C.check(d))
    d["verification"]["sec_error"] = "ProviderError: company_tickers.json: HTTP 503"
    sec = next(x for x in C.check(d)["blocks"] if x["code"] == "verify_sec")
    assert "HTTP 503" in sec["text"]                                      # 25.10: the reason, not a guess
    d = complete()
    d["verification"]["summary"]["total"]["bad"] = 2
    c = C.check(d)
    assert c["status"] == "complete" and "verification_bad" in codes(c, "flags")


def test_config_missing_on_the_server_blocks():
    d = complete()
    d["meta"]["events"] = [{"level": "warn", "where": "documents", "text": "ROIC_API_KEY not set: no earnings-call"}]
    assert "config" in codes(C.check(d))


def test_undecided_cards_are_shown():
    d = complete()
    d["judgments"]["agent_log"] = [{"card": "DC-11", "subject": "SHEL", "status": "critic_rejected"}]
    c = C.check(d)
    assert c["status"] == "complete" and "agents_undecided" in codes(c, "flags")


def test_the_report_opens_with_the_verdict():
    d = complete()
    d["completeness"] = C.check(d)
    assert 'class="verdict ok"' in report.render(d) and "דוח מלא" in report.render(d)
    d = fixture()
    d["completeness"] = C.check(d)
    html = report.render(d)
    assert 'class="verdict partial"' in html and "אין מאמר" in html
    assert html.index('id="verdict"') < html.index('class="rail"')


def test_every_watchlist_stock_has_a_full_analysis():
    """Ilay 25.10: nine stocks on the list had none; the report is not complete while one is missing."""
    d = complete()
    sym = next(iter(d["ideas"]))
    d["meta"]["watchlist"] = {"symbols": [sym, "OLD", "PREV", "GONE", "DRY"], "analysed_before": {"PREV": "2026-10-16"},
                              "deferred": ["DRY"]}
    d["meta"]["backfill"] = {"wanted": ["OLD", "GONE", "DRY"], "errors": {"GONE": "לא ביקום של השבוע (אין ציטוט)"}}
    c = C.check(d)
    assert codes(c) == {"watchlist_unanalysed"} and "OLD" in c["blocks"][0]["text"]
    assert {"watchlist_gone", "watchlist_deferred"} <= codes(c, "flags")
    i = copy.deepcopy(d["ideas"][sym]) | {"backfill": {"failed": []}}
    d["ideas"]["OLD"] = i
    c = C.check(d)
    assert c["status"] == "complete" and c["counts"]["backfill"] == 1 and "1 מרשימת המעקב" in C.line(c)
    i["judgment"].pop("article")
    assert "watchlist_unanalysed" in codes(C.check(d))
