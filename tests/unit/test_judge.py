"""The agents' judgment layer of the research run (qt.research.judge) with a fake Messages API client:
which cards run for which idea, how their answers become the judgments dict, the DC-09 cap, the budget stop,
and that the report renders an agent-judged idea."""
import copy
import json
import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace as NS

from qt.agents import AgentRunner
from qt.agents.runner import BudgetExceeded
from qt.research import article as AR
from qt.research import catalyst_chain as CC
from qt.research import judge as JG
from qt.research import report, run

FIX = json.loads((Path(__file__).parent.parent / "fixtures" / "research_dossier.json").read_text(encoding="utf-8"))
AS_OF = date(2026, 10, 20)
SYM = "SHEL"
TLDR = {"line": "שורה לחבר", "macro": "מאקרו חם", "sector": "אנרגיה מובילה", "company": "רווח עולה",
        "why_now": "פריצה", "wrong": "הורדות תחזית"}


def _answer(card: str, option: str, **extra):
    base = {"option": option, "confidence": "medium", "reasoning": f"נימוק {card}", "fact_ids": ["F-001"],
            "cited_rules": ["S07-R3"], "deviations": [], "missing_facts": [], "not_from_course": []}
    return {**base, **extra}


DEFAULTS = {
    "DC-01": _answer("DC-01", "hot"), "DC-02": _answer("DC-02", "growth+_inflation+", in_six_months="growth+_inflation+"),
    "DC-03": _answer("DC-03", "aligned"), "DC-04": _answer("DC-04", "medium"),
    "DC-10": _answer("DC-10", "momentum_long"),
    "DC-11": _answer("DC-11", "justified", target_price=129.5, target_method="hist_3y"),
    "DC-12": _answer("DC-12", "breakers", thesis_breakers=[{"kind": "kpi", "text": "ירידה בתפוקה", "check": "דוח רבעוני"}]),
    "DC-13": _answer("DC-13", "hot", tldr=TLDR),
    "DC-14": _answer("DC-14", "B", priced_in=False, direction="for_idea", level="company", timing="expected",
                     expected_date="2026-10-29", persistence="until_next_report", follow_on_expected=True,
                     strength="בינונית", flow_technical=False, peers_affected=[], what="דוח רבעון"),
    "DC-17": _answer("DC-17", "level_break", level=106.7, return_condition="סגירה מעל 106.7"),
    "DC-19": _answer("DC-19", "k_mid"),
    "DC-20": _answer("DC-20", "2", first_size_pct=3, add_conditions=["סגירה מעל 110"]),
    "DC-27": _answer("DC-27", "story",
                     story=[{"sentences": [{"text": "הכלכלה צומחת והשוק עולה.", "fact_ids": ["F-001"], "quotes": []}]},
                            {"sentences": [{"text": "המקרו חם, ולכן מעדיפים מחזוריות.", "fact_ids": ["F-001"], "quotes": []}]}],
                     fed={"headline": {"text": "הפד מקל.", "fact_ids": ["F-001"], "quotes": []}, "paragraphs": [],
                          "next": {"text": "ההחלטה הבאה בהמשך החודש.", "fact_ids": ["F-001"], "quotes": []}},
                     events=[{"date": "2026-10", "title": "השוק בשיא", "charts": ["spx"],
                              "sentences": [{"text": "השוק עלה לשיא.", "fact_ids": ["F-001"], "quotes": []}]}]),
    "DC-25": _answer("DC-25", "article",
                     headline={"text": "כותרת", "fact_ids": ["F-001"], "quotes": []},
                     dek={"text": "תת-כותרת", "fact_ids": ["F-001"], "quotes": []},
                     sections={k: {"answer": "no_source", "paragraphs": []} for k in AR.SECTION_KEYS}),
}


class CardClient:
    """Answers each request by its card (the Critic always passes); records which cards were asked."""
    def __init__(self, overrides=None):
        self.answers = {**DEFAULTS, **(overrides or {})}
        self.cards, self.requests = [], []
        self.beta = NS(messages=NS(create=self._create))
        self.messages = NS(create=self._create)

    def _create(self, **req):
        self.requests.append(req)
        props = req["output_config"]["format"]["schema"]["properties"]
        if "verdict" in props:
            payload = {"verdict": "pass", "issues": []}
        else:
            card = re.search(r'<decision_card id="(DC-\d\d)"', req["messages"][0]["content"]).group(1)
            self.cards.append(card)
            payload = self.answers[card]
        return NS(content=[NS(type="text", text=json.dumps(payload, ensure_ascii=False))], stop_reason="end_turn",
                  model=req["model"], usage=NS(input_tokens=1000, output_tokens=300, cache_read_input_tokens=0,
                                               cache_creation_input_tokens=0))


def _deep_dive(calls, **inject):
    def dd(sym, etf, direction, j):
        calls.append((sym, dict(j)))
        d = copy.deepcopy(FIX["ideas"][SYM])
        d.update(sym=sym, etf=etf, direction=direction, judgment=j, **copy.deepcopy(inject))
        return d
    return dd


def _n_events(**inject):
    d = copy.deepcopy(FIX["ideas"][SYM])
    d.update(inject)
    return len(CC.select_for_agent(CC.events(d, AS_OF)))


def _world(msg):
    return json.loads(msg.split("<world_state>\n", 1)[1].split("\n</world_state>", 1)[0])


def _dc(client, card):
    return [r["messages"][0]["content"] for r in client.requests if f'<decision_card id="{card}"' in r["messages"][0]["content"]]


def _timing(n):
    return [{"sym": f"S{i}", "etf": "XLE", "dir": "long", "profile": "L1", "combined": "green", "stock": "green",
             "sector": "green", "market": "orange", "rr_extreme": float(i), "adr": 2.0, "price": 10.0} for i in range(n)]


def _judge(db, client, **kw):
    return JG.Judge(AgentRunner(db, client), run_id="weekly-x", **kw)


# ------------------------------------------------------------------ fact sheets

def test_idea_facts_carry_the_code_numbers_with_ids():
    fs = JG.idea_facts(FIX["ideas"][SYM], FIX["macro"], AS_OF)
    text = fs.render()
    assert "F-001:" in text and "SHEL.plan.rr" in text and "SHEL.valuation.target.price = 129.52" in text
    assert "SHEL.timing.stock.overall" in text and "SHEL.news[0]" in text and "macro.stance.zone = hot" in text
    assert "SHEL.chart" not in text                                    # bulky series stay out
    m = JG.macro_facts(FIX["macro"], AS_OF).render()
    assert "macro.stance.zone = hot" in m and "macro.score.ism_pmi" in m


# ------------------------------------------------------------------ one idea

def test_hot_idea_runs_a4_then_a6_and_fills_the_judgment(db):
    client, calls = CardClient(), []
    j = _judge(db, client).idea(_timing(1)[0] | {"sym": SYM}, _deep_dive(calls), FIX["macro"], AS_OF)
    n = _n_events()
    assert n >= 1
    assert client.cards[:3] == ["DC-10", "DC-11", "DC-12"] and client.cards[3:3 + n] == ["DC-14"] * n
    assert client.cards[3 + n:] == ["DC-13", "DC-17", "DC-19", "DC-20", "DC-25"]   # the article comes last
    assert calls == [(SYM, {"etf": "XLE", "direction": "long", "options": False})]
    assert j["catalyst_expected"] is True and j["catalysts"] and j["heat"] == "hot" and "code_overrides" not in j
    own = next(r for r in j["catalysts"] if r["kind"] == "own_report")
    assert own["class"] == "B" and own["decided_by"] == "DC-14" and own["what"] == "דוח רבעון"
    assert isinstance(j["news_adverse"], bool) and "catalyst_chain" in j and "catalyst_wait" in j
    assert j["decision"] == "long" and j["story_type"] == "momentum_long" and j["story"] == "נימוק DC-10"
    assert j["premium"]["choice"] == "justified" and j["target_source"] == "own_multiple_target"
    assert j["breakers"][0]["text"] == "ירידה בתפוקה" and j["tldr"] == TLDR
    assert j["trigger"] == {"choice": "level_break", "level": 106.7, "text": "סגירה מעל 106.7"}
    assert j["k"] == 4.0 and j["sizing"] == {"choice": "2", "first_size_pct": 3, "add_conditions": ["סגירה מעל 110"]}
    assert j["structure"]["choice"] == "stock"
    sent = client.requests[0]["messages"][0]["content"]
    assert "SHEL.plan.rr" in sent and "Hebrew" in sent                # the facts and the language reach the card
    later = next(r for r in client.requests if '<decision_card id="DC-17"' in r["messages"][0]["content"])
    assert '"DC-13": {"option": "hot"}' in later["messages"][0]["content"]   # earlier answers ride along


def test_cold_idea_stops_before_timing_and_no_is_rejected(db):
    client = CardClient({"DC-13": _answer("DC-13", "cold", tldr=TLDR)})
    j = _judge(db, client).idea(_timing(1)[0] | {"sym": SYM}, _deep_dive([]), FIX["macro"], AS_OF)
    assert j["decision"] == "watch_only" and j["why"] == "נימוק DC-13" and "DC-17" not in client.cards
    client = CardClient({"DC-13": _answer("DC-13", "no", tldr=TLDR)})
    j = _judge(db, client).idea(_timing(1)[0] | {"sym": SYM}, _deep_dive([]), FIX["macro"], AS_OF)
    assert j["decision"] == "no"
    d = copy.deepcopy(FIX["ideas"][SYM])
    d["judgment"] = j
    assert run.classify(d)[0] == "reject"


def test_wait_trigger_keeps_the_idea_on_the_watchlist(db):
    client = CardClient({"DC-17": _answer("DC-17", "wait", level=0, return_condition="מעבר לירוק")})
    j = _judge(db, client).idea(_timing(1)[0] | {"sym": SYM}, _deep_dive([]), FIX["macro"], AS_OF)
    assert j["decision"] == "watch_only" and j["why"] == "מעבר לירוק" and "DC-19" not in client.cards


# ------------------------------------------------------------------ the run

def test_dc09_ranks_only_above_the_cap_and_red_never_passes(db):
    client = CardClient()
    jg = _judge(db, client, max_deep=3)
    tm = _timing(3) + [{"sym": "RED", "etf": "XLE", "dir": "long", "combined": "red"}, {"sym": "ERR", "error": "x"}]
    assert [t["sym"] for t in jg.pick(tm, FIX["macro"], AS_OF)] == ["S0", "S1", "S2"] and not client.cards
    client = CardClient({"DC-09": _answer("DC-09", "ranked", ranked=["S4", "S9", "S1", "S0"], skipped=[])})
    picked = _judge(db, client, max_deep=3).pick(_timing(5), FIX["macro"], AS_OF)
    assert client.cards == ["DC-09"] and [t["sym"] for t in picked] == ["S4", "S1", "S0"]   # S9 is not a candidate
    client = CardClient({"DC-09": _answer("DC-09", "ranked", ranked=[], skipped=[])})       # nothing usable
    picked = _judge(db, client, max_deep=2).pick(_timing(5), FIX["macro"], AS_OF)
    assert [t["sym"] for t in picked] == ["S4", "S3"]                 # the code's order: R:R to the extreme


def test_full_judgment_and_budget_stop(db):
    client, calls = CardClient(), []
    J = _judge(db, client)({"as_of": AS_OF, "macro": FIX["macro"], "timing": _timing(2)}, _deep_dive(calls))
    assert J["A1"]["DC-01"]["choice"] == "hot" and J["A1"]["DC-02"]["in_six_months"] == "growth+_inflation+"
    assert set(J["ideas"]) == {"S0", "S1"} and J["judged_by"].startswith("Agents")
    assert [x["card"] for x in J["agent_log"]][-2:] == ["DC-25", "DC-25"]        # the articles after both ideas' cards
    assert all(j["article"]["status"] == "decided" for j in J["ideas"].values())
    assert len(J["agent_log"]) == 5 + 2 * (8 + _n_events()) and all(x["status"] == "decided" for x in J["agent_log"])
    assert "warnings" not in J

    class Broke(AgentRunner):
        def decide(self, agent, card_id, *a, **k):
            if agent == "A4":
                raise BudgetExceeded("agent budget $25 reached")
            return super().decide(agent, card_id, *a, **k)
    J = JG.Judge(Broke(db, CardClient()))({"as_of": AS_OF, "macro": FIX["macro"], "timing": _timing(3)}, _deep_dive([]))
    assert J["ideas"] == {} and list(J["errors"]) == ["S0"] and "budget" in J["errors"]["S0"]   # stops at the first
    assert J["A3"]["cut_by_budget"] == ["S1", "S2"] and set(J["A3"]["skipped"]) == {"S0", "S1", "S2"}
    assert JG.unexplained_greens(_timing(3), set(J["ideas"]), J["A3"]["skipped"]) == []   # each has a reason


def test_budget_stop_inside_dc14_stops_the_run(db):
    """BudgetExceeded in DC-14 is not one event's agent_error: the idea stops and the rest are cut by budget."""
    n = []

    class Broke(AgentRunner):
        def decide(self, agent, card_id, *a, **k):
            if card_id == "DC-14":
                n.append(card_id)
                raise BudgetExceeded("agent budget $25 reached")
            return super().decide(agent, card_id, *a, **k)
    J = JG.Judge(Broke(db, CardClient()))({"as_of": AS_OF, "macro": FIX["macro"], "timing": _timing(2)}, _deep_dive([]))
    assert n == ["DC-14"] and J["ideas"] == {} and "budget" in J["errors"]["S0"]
    assert J["A3"]["cut_by_budget"] == ["S1"]


def test_runner_uses_the_run_cap_instead_of_the_daily_one(db):
    r = AgentRunner(db, CardClient(), limit_usd=0.0)
    try:
        r.decide("A4", "DC-10", JG.idea_facts(FIX["ideas"][SYM], FIX["macro"], AS_OF), {})
        raise AssertionError("expected the cap")
    except BudgetExceeded as e:
        assert "$0.0" in str(e)


def test_report_renders_an_agent_judged_idea(db):
    client = CardClient()
    j = _judge(db, client).idea(_timing(1)[0] | {"sym": SYM}, _deep_dive([]), FIX["macro"], AS_OF)
    d = copy.deepcopy(FIX)
    d["ideas"] = {SYM: d["ideas"][SYM]}
    d["ideas"][SYM]["judgment"] = j
    d["ideas"][SYM]["exit"] = run.exit_plan(d["ideas"][SYM]["plan"], "long", "enter", AS_OF, "2026-10-29", j)
    d["meta"]["judged_by"] = JG.JUDGED_BY
    h = report.render(d)
    for s in ("שורה לחבר", "נימוק DC-10", "ירידה בתפוקה", 'class="tldr"', "Agents A1"):
        assert s in h, s


def test_dc09_keeps_who_was_left_out_and_orders_by_profile_when_undecided(db):
    tm = _timing(4)
    tm[3] |= {"profile": "L4"}                                         # best R:R but the weakest profile
    tm[0] |= {"combined": "orange"}
    client = CardClient({"DC-09": _answer("DC-09", "ranked", ranked=[], skipped=[])})
    jg = _judge(db, client, max_deep=2)
    assert [t["sym"] for t in jg.pick(tm, FIX["macro"], AS_OF)] == ["S2", "S1"]   # L1 green first, by R:R
    assert jg.selection["mode"] == "code_order" and jg.selection["candidates"] == 4
    assert list(jg.selection["skipped"]) == ["S0", "S3"] and all(jg.selection["skipped"].values())
    jg = _judge(db, CardClient(), max_deep=8)
    jg.pick(tm, FIX["macro"], AS_OF)
    assert jg.selection["mode"] == "all" and jg.selection["skipped"] == {}


def _mixed(n_green, n_orange):
    tm = _timing(n_green + n_orange)
    for t in tm[n_green:]:
        t["combined"] = "orange"
    return tm


def test_dc09_cap_is_12_and_every_candidate_up_to_it_passes(db):
    assert JG.MAX_DEEP == 12 and JG.MIN_DEEP == 8
    client = CardClient()
    assert len(_judge(db, client).pick(_mixed(5, 7), FIX["macro"], AS_OF)) == 12 and not client.cards


def test_dc09_a_green_left_out_without_a_reason_still_goes_on(db):
    tm = _mixed(3, 12)                                                 # S0-S2 green, S3-S14 orange
    ranked = [f"S{i}" for i in range(3, 15)]                           # the agent ranks only oranges
    client = CardClient({"DC-09": _answer("DC-09", "ranked", ranked=ranked, skipped=[])})
    jg = _judge(db, client)
    picked = [t["sym"] for t in jg.pick(tm, FIX["macro"], AS_OF)]
    assert len(picked) == 12 and {"S0", "S1", "S2"} <= set(picked) and set(picked[:3]) == {"S0", "S1", "S2"}   # greens first
    sk = jg.selection["skipped"]
    assert set(sk) == {"S12", "S13", "S14"} and all(sk.values())       # the last oranges, each with a reason
    assert JG.unexplained_greens(tm, set(picked), sk) == []


def test_dc09_a_green_skipped_with_the_agents_reason_stays_out(db):
    tm = _mixed(3, 12)
    ranked = [f"S{i}" for i in range(1, 13)]
    client = CardClient({"DC-09": _answer("DC-09", "ranked", ranked=ranked,
                                          skipped=[{"sym": "S0", "reason": "דוח בעוד יומיים"}])})
    jg = _judge(db, client)
    picked = [t["sym"] for t in jg.pick(tm, FIX["macro"], AS_OF)]
    assert "S0" not in picked and jg.selection["skipped"]["S0"] == "דוח בעוד יומיים"
    assert JG.unexplained_greens(tm, set(picked), jg.selection["skipped"]) == []


def test_dc09_too_few_ranked_is_filled_to_8(db):
    client = CardClient({"DC-09": _answer("DC-09", "ranked", ranked=["S20", "S19"], skipped=[])})
    picked = _judge(db, client).pick(_mixed(0, 21), FIX["macro"], AS_OF)
    assert len(picked) == 8 and [t["sym"] for t in picked][:2] == ["S20", "S19"]


def test_unexplained_greens_and_the_hand_written_run_refuses_them():
    tm = _mixed(2, 1)
    assert JG.unexplained_greens(tm, {"S0"}, {}) == ["S1"]
    assert JG.unexplained_greens(tm, {"S0"}, {"S1": "  "}) == ["S1"]          # a blank reason is no reason
    assert JG.unexplained_greens(tm, {"S0"}, {"S1": "סיבה"}) == []
    assert JG.unexplained_greens(tm, set(), ["S0", "S1"]) == ["S0", "S1"]     # the old list form has no reasons


# ------------------------------------------------------------------ catalysts (DC-14 and the code's gate)

def test_hot_without_counted_catalyst_becomes_watch_only(db):
    for dc14 in ({**DEFAULTS["DC-14"], "option": "C"}, {**DEFAULTS["DC-14"], "priced_in": True}):
        client = CardClient({"DC-14": dc14})
        j = _judge(db, client).idea(_timing(1)[0] | {"sym": SYM}, _deep_dive([]), FIX["macro"], AS_OF)
        assert j["decision"] == "watch_only" and j["heat"] == "cold" and j["catalyst_expected"] is False
        assert j["why"].startswith("S08-R33/S07-R13") and j["code_overrides"][0]["from"] == "hot"
        assert "DC-13" in client.cards and "DC-17" not in client.cards and j["direction"] == "long"   # same direction


def test_dc14_failure_is_the_lower_class(db):
    class Flaky(CardClient):
        def _create(self, **req):
            if '<decision_card id="DC-14"' in req["messages"][0]["content"]:
                raise RuntimeError("api down")
            return super()._create(**req)
    j = _judge(db, Flaky()).idea(_timing(1)[0] | {"sym": SYM}, _deep_dive([]), FIX["macro"], AS_OF)
    agent_rows = [r for r in j["catalysts"] if (r["code_reason"] or "").startswith("agent_error")]
    assert agent_rows and all(r["class"] == "D" for r in agent_rows) and j["decision"] == "watch_only"


DOCS = {"symbol": SYM, "cik": 1306965, "ciks": [1306965], "foreign": True, "as_of": "2026-10-20",
        "current": [{"form": "8-K", "date": "2026-10-10", "accession": "0001-26-000123", "items": ["2.02", "9.01"],
                     "item_names": ["Results of Operations and Financial Condition"], "kind": "results",
                     "url": "https://www.sec.gov/a.htm", "index_url": "https://www.sec.gov/0001-26-000123-index.htm",
                     "exhibits": [{"type": "EX-99.1", "url": "https://www.sec.gov/ex991.htm",
                                   "title": "Third quarter update note", "lead": "Integrated Gas production 740-780 kboe/d"}]}],
        "results": {"form": "8-K", "date": "2026-10-10", "url": "https://www.sec.gov/ex991.htm", "source": "sec:8-K EX-99.1",
                    "title": "Third quarter update note", "lead": "Integrated Gas production 740-780 kboe/d",
                    "outlook": ["Production expected to be 740-780 kboe/d in Q4"], "guidance": [], "commentary": None},
        "results_prev": None,
        "risk_factors": {"form": "20-F", "filed": "2026-03-12", "url": "https://www.sec.gov/20f.htm", "chars_total": 90000,
                         "headlines": ["Rising climate change concerns could lead to lower demand",
                                       "Our future hydrocarbon production depends on new resources"],
                         "excerpt": "Risk factors text", "delta": None},
        "transcript": None, "errors": []}


def test_documents_become_facts_and_events(db):
    d = copy.deepcopy(FIX["ideas"][SYM])
    d["documents"] = DOCS
    text = JG.idea_facts(d, FIX["macro"], AS_OF).render()
    assert "SHEL.filing[0] = 2026-10-10 8-K 2.02 9.01" in text and "Third quarter update note" in text
    assert "SHEL.risk_factors.headline[0] = Rising climate" in text and "SHEL.risk_factors.headline[1] = Our future" in text and "sec:20-F Item 1A https://www.sec.gov/20f.htm" in text
    assert "SHEL.results.outlook[0] = [8-K 2026-10-10] Production expected" in text  # dated: never a stale "latest"
    assert "SHEL.results.date = 2026-10-10" in text
    assert "SHEL.results.outlook[0]" in text and "SHEL.catalysts.analyst_actions[0] = 2026-09-28 TD Cowen" in text
    client = CardClient()
    j = _judge(db, client).idea(_timing(1)[0] | {"sym": SYM}, _deep_dive([], documents=DOCS), FIX["macro"], AS_OF)
    assert client.cards.count("DC-14") == _n_events(documents=DOCS)
    sent = [m for m in _dc(client, "DC-14") if "0001-26-000123" in m]
    assert sent and "Integrated Gas production" in sent[0] and "SHEL.event.facts.pre_move_20d_pct" in sent[0]
    assert "SHEL.chart" not in sent[0] and "SHEL.plan.rr" not in sent[0] and "SHEL.valuation" not in sent[0]
    assert {s["url"] for s in j["sources"]} >= {"https://www.sec.gov/0001-26-000123-index.htm", "https://www.sec.gov/20f.htm"}
    assert any(r.get("accession") == "0001-26-000123" and r["kind"] == "results" for r in j["catalysts"])


def test_dc14_world_has_regime_and_profile(db):
    client = CardClient()
    _judge(db, client).idea(_timing(1)[0] | {"sym": SYM}, _deep_dive([]), FIX["macro"], AS_OF)
    m = _dc(client, "DC-14")[0]
    w = _world(m)
    assert w["regime"] == {"zone": "hot", "bias": "bullish"} and w["profile"]["industry"] == "Oil & Gas Integrated"
    assert w["event"]["kind"] == "own_report" and w["peers_on_list"] == [] and "macro.stance.zone = hot" in m
    assert w["DC-12"]["thesis_breakers"] and "SHEL.thesis_breaker[0]" in m      # story and breakers (S08-R23)


def test_dc17_world_has_catalyst_code(db):
    client = CardClient()
    _judge(db, client).idea(_timing(1)[0] | {"sym": SYM}, _deep_dive([]), FIX["macro"], AS_OF)
    for card in ("DC-13", "DC-17"):
        w = _world(_dc(client, card)[0])
        assert w["catalyst_code"]["expected"] is True and w["DC-14"] and "wait" in w["catalyst_code"], card


def test_more_than_four_hot_warns(db):
    J = _judge(db, CardClient())({"as_of": AS_OF, "macro": FIX["macro"], "timing": _timing(5)}, _deep_dive([]))
    assert sum(j["heat"] == "hot" for j in J["ideas"].values()) == 5 and J["warnings"] == ["hot_above_4"]
