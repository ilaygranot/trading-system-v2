"""The course test set (qt.agents.eval) with a fake client: the cases are well formed, the traps pass the code
checks, the agents see a blind Playbook, scoring and the pass bar."""
from types import SimpleNamespace as NS

from qt.agents import AgentRunner, load_cards
from qt.agents.blind import blind, leaks
from qt.agents.context import playbook_pack
from qt.agents.eval import evaluate, fact_sheet, load_suite, render, score, summary, CaseResult
from test_agents import FakeClient, PASS

SUITE = load_suite()


def _answer(case, option=None):
    return {"option": option or str(case["expect"][0]), "confidence": "medium", "reasoning": "x", "fact_ids": ["F-001"],
            "cited_rules": ["S07-R21"], "deviations": [], "missing_facts": [], "not_from_course": []}


def test_cases_are_well_formed():
    cards = load_cards()
    ids = [c["id"] for c in SUITE["cases"]]
    assert len(ids) == len(set(ids)) >= 20
    for c in SUITE["cases"]:
        card = cards[c["card"]]
        assert c["scenario"] in SUITE["scenarios"] and c["source"] and c["quote"]
        assert {str(o) for o in c["expect"] + c.get("near", [])} <= set(card.options), c["id"]
        assert len(fact_sheet(SUITE, c)) > 0


def test_traps_pass_the_code_checks():
    """A trap the code already rejects tests nothing about the Critic."""
    cards, by_id = load_cards(), {c["id"]: c for c in SUITE["cases"]}
    for t in SUITE["traps"] + SUITE["control"]:
        case = by_id[t["case"]]
        assert AgentRunner.check(t["output"], cards[case["card"]], fact_sheet(SUITE, case)) == [], t["id"]
    assert len(SUITE["traps"]) >= 6 and SUITE["control"]


def test_blind_pack_and_cards_have_no_case_names():
    for agent in ("A1", "A3", "A4", "A5", "A6", "A7"):
        assert leaks(playbook_pack(agent, True)[0]) == [], agent
    for card in load_cards().values():
        assert leaks(blind(card.text)[0]) == [], card.card_id
    assert "Boeing" not in blind("A rule stays. Boeing fell (BA) here.\n")[0]
    assert blind("A rule stays. Boeing fell here.\n")[0] == "A rule stays."


def test_score():
    case = {"expect": ["hold", "reduce"], "near": ["exit"]}
    assert [score(case, o) for o in ("reduce", "exit", "add", None)] == ["hit", "near", "miss", "miss"]


def test_evaluate_runs_blind_with_the_model_and_scores(db):
    only = ["E2E-1.AXON.DC-13", "E2E-3.BA.DC-21"]
    cases = [c for c in SUITE["cases"] if c["id"] in only]
    # AXON right; the BA spread exited on a price rally: a miss on an approval card
    client = FakeClient([_answer(cases[0]), PASS, _answer(cases[1], "add"), PASS] + [PASS] * 7)
    rep = evaluate(db, client, ["sonnet"], only=only, traps=True)
    s = rep["models"]["sonnet"]["summary"]
    assert (s["hit"], s["miss"], s["blocking_misses"], s["passed"]) == (1, 1, ["E2E-3.BA.DC-21"], False)
    assert client.requests[0]["model"] == "claude-sonnet-5-5"           # the model under test, A4 is opus by default
    pack = client.requests[0]["system"][1]["text"]
    assert leaks(pack) == [] and "AXON" in client.requests[0]["messages"][0]["content"]   # facts are not blinded
    traps = rep["traps"]
    assert [t["caught"] for t in traps if t["flaw"] != "control"] == [False] * 6   # the fake Critic passes everything
    assert [t["caught"] for t in traps if t["flaw"] == "control"] == [True]
    assert "FAIL" in render(rep) and "Critic: 1/7" in render(rep)


def test_budget_stops_the_run(db):
    AgentRunner(db, FakeClient([]))                                       # creates agent_calls
    db.insert("agent_calls", dict(agent="A4", model="claude-opus-5-5", cost_usd=100, created_at="2999-01-01T00:00:00+00:00"))
    rep = evaluate(db, FakeClient([]), ["opus"], only=["E2E-1.AXON"], traps=False, limit_usd=1)
    r = rep["models"]["opus"]["results"]
    assert [x["score"] for x in r] == ["not_run", "not_run"] and not rep["models"]["opus"]["summary"]["passed"]


def test_pass_bar_needs_every_case_run():
    ok = CaseResult("a", "DC-10", "opus", "hit")
    assert summary([ok, CaseResult("b", "DC-10", "opus", "near")])["passed"]
    assert not summary([ok, CaseResult("b", "DC-10", "opus", "not_run")])["passed"]
    assert not summary([ok] * 4 + [CaseResult("c", "DC-13", "opus", "miss")])["passed"]   # an approval card
