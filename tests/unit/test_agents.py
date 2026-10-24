"""The agents module with a fake Messages API client: request shape, checks, retry, Critic, records, budget."""
import json
from datetime import date
from types import SimpleNamespace as NS

import pytest

from qt.agents import AgentRunner, BudgetExceeded, build_request, load_cards
from qt.agents.runner import config
from qt.facts import FactSheet


def _resp(payload, stop="end_turn", model="claude-opus-5-5", cache_read=0):
    return NS(content=[NS(type="text", text=json.dumps(payload))], stop_reason=stop, model=model,
              usage=NS(input_tokens=1000, output_tokens=500, cache_read_input_tokens=cache_read,
                       cache_creation_input_tokens=0))


class FakeClient:
    """Returns queued answers; records every request (beta path for fallbacks, plain path for Haiku)."""
    def __init__(self, answers):
        self.answers, self.requests = list(answers), []
        self.beta = NS(messages=NS(create=self._create))
        self.messages = NS(create=self._create)

    def _create(self, **req):
        self.requests.append(req)
        a = self.answers.pop(0)
        return a if isinstance(a, NS) else _resp(a, model=req["model"])


def _facts():
    fs = FactSheet()
    fs.add("BA.eps.F1", -1.12, "fmp", date(2023, 4, 18))
    fs.add("BA.price", 198.0, "yahoo:v8/chart", date(2023, 5, 1))
    return fs


GOOD = {"option": "cold", "confidence": "medium", "reasoning": "No catalyst yet (S08-R33).", "fact_ids": ["F-001"],
        "cited_rules": ["S08-R33", "S07-J10"], "deviations": [], "missing_facts": [], "not_from_course": []}
PASS = {"verdict": "pass", "issues": []}


def test_cards_and_schema():
    cards = load_cards()
    assert len(cards) == 26 and all(c.text for c in cards.values())
    assert not {"True", "False"} & {o for c in cards.values() for o in c.options}     # YAML no/yes traps
    s = cards["DC-13"].schema()
    assert s["properties"]["option"]["enum"] == ["no", "cold", "hot"] and s["additionalProperties"] is False
    assert "thesis_breakers" in cards["DC-12"].schema()["required"]


def test_request_shape_caches_the_fixed_parts():
    card = load_cards()["DC-13"]
    req = build_request("A4", card, _facts(), {"macro": "cold"}, "claude-opus-5-5", "high", 16000)
    assert req["system"][1]["cache_control"] == {"type": "ephemeral"} and "S07" in req["system"][1]["text"]
    assert "F-001: BA.eps.F1 = -1.12" in req["messages"][0]["content"]
    assert req["output_config"]["format"]["type"] == "json_schema" and req["output_config"]["effort"] == "high"
    assert "thinking" not in req                                   # Opus 5.5: thinking is always on


def test_decide_records_call_and_journal(db):
    client = FakeClient([GOOD, PASS])
    r = AgentRunner(db, client).decide("A4", "DC-13", _facts(), {}, run_id="daily-x")
    assert r.status == "decided" and r.option == "cold" and r.for_ilay        # DC-13 needs Ilay's approval
    assert client.requests[0]["model"] == "claude-opus-5-5"
    assert client.requests[1]["model"] == "claude-sonnet-5-5"                # the Critic
    calls = db.all("SELECT agent, model, cost_usd FROM agent_calls ORDER BY id")
    assert [c["agent"] for c in calls] == ["A4", "C"]
    assert calls[0]["cost_usd"] == pytest.approx((1000 * 4 + 500 * 20) / 1e6)
    row = r and db.one("SELECT * FROM decisions WHERE id = ?", (r.decision_id,), ["cited_rules"])
    assert row["decided_by"] == "agent:A4" and row["choice"] == "cold" and row["run_id"] == "daily-x"


def test_invented_fact_gets_one_retry_then_undecided(db):
    bad = {**GOOD, "fact_ids": ["F-009"]}
    client = FakeClient([bad, bad])
    r = AgentRunner(db, client).decide("A4", "DC-13", _facts(), {})
    assert r.status == "agent_undecided" and r.option == "cold"             # the card's 'when undecided'
    assert "F-009" in client.requests[1]["messages"][0]["content"]           # the retry says what was wrong
    assert r.for_ilay


def test_deviation_needs_j_principle_and_never_on_hard_rules(db):
    dev = {**GOOD, "deviations": [{"rule_id": "S08-R28", "principle": "gut", "fact_ids": ["F-001"], "reason": "x"}]}
    probs = AgentRunner.check(dev, load_cards()["DC-13"], _facts())
    assert any("J principle" in p for p in probs) and any("hard rule" in p for p in probs)


def test_critic_returns_once_then_escalates(db):
    ret = {"verdict": "return", "issues": [{"kind": "wrong_rule", "detail": "S07-J10 says something else"}]}
    client = FakeClient([GOOD, ret, ret, {**GOOD, "cited_rules": ["S08-R33"]}, PASS])
    r = AgentRunner(db, client).decide("A4", "DC-13", _facts(), {})
    models = [q["model"] for q in client.requests]
    assert models == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5-5", "claude-opus-5-5"]
    assert r.status == "decided" and r.output["cited_rules"] == ["S08-R33"]


def test_refusal_falls_to_undecided(db):
    refusal = NS(content=[], stop_reason="refusal", model="claude-opus-5-5",
                 usage=NS(input_tokens=10, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0))
    r = AgentRunner(db, FakeClient([refusal, refusal])).decide("A4", "DC-13", _facts(), {})
    assert r.status == "refused" and r.option == "cold"


def test_fallbacks_on_the_beta_path(db):
    client = FakeClient([GOOD, PASS])
    calls = []
    client.beta = NS(messages=NS(create=lambda **k: calls.append(k) or client._create(**{x: v for x, v in k.items()
                                                                                       if x not in ("betas", "fallbacks")})))
    AgentRunner(db, client).decide("A4", "DC-13", _facts(), {})
    assert calls[0]["fallbacks"] == "default" and calls[0]["betas"] == ["server-side-fallback-2026-07-01"]


def test_card_belongs_to_its_agent_and_budget(db):
    r = AgentRunner(db, FakeClient([]))
    with pytest.raises(ValueError):
        r.decide("A5", "DC-13", _facts(), {})
    db.insert("agent_calls", dict(agent="A4", model="claude-opus-5-5", cost_usd=config()["budget"]["daily_usd"],
                                  created_at="2999-01-01T00:00:00+00:00"))
    with pytest.raises(BudgetExceeded):
        r.decide("A4", "DC-13", _facts(), {})
    ok = AgentRunner(db, FakeClient([{**GOOD, "option": "hold"}, PASS])).decide("A7", "DC-21", _facts(), {})
    assert ok.option == "hold"                                               # the risk officer still runs
