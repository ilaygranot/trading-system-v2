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
    "DC-17": _answer("DC-17", "level_break", level=106.7, return_condition="סגירה מעל 106.7"),
    "DC-19": _answer("DC-19", "k_mid"),
    "DC-20": _answer("DC-20", "2", first_size_pct=3, add_conditions=["סגירה מעל 110"]),
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


def _deep_dive(calls):
    def dd(sym, etf, direction, j):
        calls.append((sym, dict(j)))
        d = copy.deepcopy(FIX["ideas"][SYM])
        d.update(sym=sym, etf=etf, direction=direction, judgment=j)
        return d
    return dd


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
    assert client.cards == ["DC-10", "DC-11", "DC-12", "DC-13", "DC-17", "DC-19", "DC-20"]
    assert calls == [(SYM, {"etf": "XLE", "direction": "long", "options": False})]
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
    assert len(J["agent_log"]) == 4 + 2 * 7 and all(x["status"] == "decided" for x in J["agent_log"])

    class Broke(AgentRunner):
        def decide(self, agent, card_id, *a, **k):
            if agent == "A4":
                raise BudgetExceeded("agent budget $25 reached")
            return super().decide(agent, card_id, *a, **k)
    J = JG.Judge(Broke(db, CardClient()))({"as_of": AS_OF, "macro": FIX["macro"], "timing": _timing(3)}, _deep_dive([]))
    assert J["ideas"] == {} and list(J["errors"]) == ["S0"] and "budget" in J["errors"]["S0"]   # stops at the first
    assert J["A3"]["cut_by_budget"] == ["S1", "S2"] and set(J["A3"]["skipped"]) == {"S0", "S1", "S2"}
    assert JG.unexplained_greens(_timing(3), set(J["ideas"]), J["A3"]["skipped"]) == []   # each has a reason


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
