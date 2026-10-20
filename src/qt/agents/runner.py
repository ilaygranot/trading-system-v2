"""Calling an agent on a decision card, checking the answer, and recording everything.

Flow per decision (runtime plan 5.2, 8.4):
1. the agent answers in the card's JSON schema (structured output);
2. code checks it: cited facts exist, cited rules exist, every deviation has a J principle and facts.
   A failed check gets one retry with what was wrong; a second failure => "agent did not decide": the card's
   undecided option, flagged for Ilay;
3. the Critic (C) checks the reasoning against the card; "return" sends it back once (the Critic escalates to the
   strong model); a second rejection goes to Ilay with the Critic's notes.
Every call is a row in agent_calls (model, tokens, cost, output); every decision is a journal entry.
Daily budget: over it, only A7 and the Critic run.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from qt.agents.cards import Card, load_cards
from qt.agents.context import build_request, playbook_pack
from qt.db import DB, dumps
from qt.facts import FactSheet
from qt.journal import Decision, Journal
from qt.playbook import load_playbook
from qt.runs import SCHEMA as RUNS_SCHEMA

FALLBACK_BETA = "server-side-fallback-2026-07-01"
J_ID = re.compile(r"^(S\d{2}|L\d{3})-J\d+$")
RULE_ID = re.compile(r"^(S\d{2}-[RJX]\d+|L\d{3}-[RJ]\d+|D-S\d{2}-\d+|V1-SCOPE|DC-\d\d|decision \d+)$")


class BudgetExceeded(RuntimeError):
    pass


@lru_cache(maxsize=None)
def config() -> dict:
    return yaml.safe_load((Path(__file__).parent / "models.yaml").read_text(encoding="utf-8"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class AgentResult:
    card_id: str
    agent: str
    status: str                        # decided | agent_undecided | critic_rejected | refused
    output: dict[str, Any]
    model: str
    critic: dict[str, Any] | None = None
    problems: list[str] = field(default_factory=list)
    decision_id: int | None = None
    cost_usd: float = 0.0

    @property
    def option(self) -> str:
        return self.output.get("option")

    @property
    def for_ilay(self) -> bool:
        return self.status != "decided" or bool(self.output.get("deviations")) or \
            load_cards()[self.card_id].approval == "required"


CRITIC_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "return", "flag_ilay"]},
        "issues": {"type": "array", "items": {
            "type": "object",
            "properties": {"kind": {"type": "string", "enum": ["missing_fact", "wrong_fact", "wrong_rule", "hidden_not_from_course",
                                                               "hard_rule_crossed", "undeclared_deviation", "reasoning"]},
                           "detail": {"type": "string"}},
            "required": ["kind", "detail"], "additionalProperties": False}},
    },
    "required": ["verdict", "issues"], "additionalProperties": False,
}


class AgentRunner:
    def __init__(self, db: DB, client: Any = None, journal: Journal | None = None):
        self.db = db
        self.db.ddl(RUNS_SCHEMA)
        self.journal = journal or Journal(db)
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import anthropic                                        # optional dependency: pip install '.[agents]'
            self._client = anthropic.Anthropic()
        return self._client

    # ------------------------------------------------------------ config
    def model_for(self, agent: str, escalate: bool = False, checked_agent: str | None = None) -> tuple[str, str]:
        c = config()
        a = c["agents"][agent]
        name, effort = a["model"], a["effort"]
        if escalate and a.get("escalate"):
            name = a["escalate"]
        if agent == "C" and checked_agent in a.get("always_opus_for", []):
            name = "opus"
        return c["models"][name], effort

    def spent_today(self) -> float:
        day = datetime.now(timezone.utc).date().isoformat()
        row = self.db.one("SELECT COALESCE(SUM(cost_usd), 0) AS s FROM agent_calls WHERE created_at >= ?", (day,))
        return float(row["s"] or 0)

    def _check_budget(self, agent: str) -> None:
        b = config()["budget"]
        if agent not in b["always_run"] and self.spent_today() >= b["daily_usd"]:
            raise BudgetExceeded(f"daily agent budget ${b['daily_usd']} reached: only {', '.join(b['always_run'])} run")

    # ------------------------------------------------------------ one API call
    def _call(self, agent: str, req: dict, ctx: dict) -> tuple[dict | None, Any]:
        model = req["model"]
        t0 = time.monotonic()
        if "haiku" in model or not config().get("fallbacks"):
            resp = self.client.messages.create(**req)
        else:
            resp = self.client.beta.messages.create(**req, betas=[FALLBACK_BETA], fallbacks=config()["fallbacks"])
        ms = int((time.monotonic() - t0) * 1000)
        out = None
        if resp.stop_reason not in ("refusal", "max_tokens"):
            text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), None)
            try:
                out = json.loads(text) if text else None
            except json.JSONDecodeError:
                out = None
        u = resp.usage
        tokens = dict(input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                      cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                      cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0)
        served = getattr(resp, "model", model) or model
        cost = self.cost(served, **tokens)
        self.db.insert("agent_calls", dict(
            run_id=ctx.get("run_id"), step=ctx.get("step"), subject=ctx.get("subject"), agent=agent, model=served,
            playbook_version=load_playbook().version, prompt_version=ctx.get("prompt_version"), **tokens,
            cost_usd=cost, duration_ms=ms, stop_reason=resp.stop_reason, output=dumps(out),
            valid=None, critic=None, created_at=_now()))
        return out, resp

    @staticmethod
    def cost(model: str, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0,
             cache_write_tokens: int = 0) -> float:
        p = config()["prices"].get(model) or config()["prices"][config()["models"]["opus"]]
        return (input_tokens * p[0] + output_tokens * p[1] + cache_read_tokens * p[2] + cache_write_tokens * p[3]) / 1e6

    # ------------------------------------------------------------ checks in code
    @staticmethod
    def check(out: dict | None, card: Card, facts: FactSheet) -> list[str]:
        if out is None:
            return ["no valid JSON output (refusal, truncation or parse error)"]
        problems = []
        if out.get("option") not in card.options:
            problems.append(f"option {out.get('option')!r} is not one of {list(card.options)}")
        for f in facts.missing(out.get("fact_ids", [])):
            problems.append(f"fact {f} is not on the fact sheet")
        for r in out.get("cited_rules", []):
            if not RULE_ID.match(r):
                problems.append(f"cited rule {r!r} is not a rule id")
        for d in out.get("deviations", []):
            if not J_ID.match(d.get("principle", "")):
                problems.append(f"deviation from {d.get('rule_id')} has no course J principle")
            if not d.get("fact_ids") or facts.missing(d["fact_ids"]):
                problems.append(f"deviation from {d.get('rule_id')} needs facts from the sheet")
            if load_playbook().is_hard(d.get("rule_id", "")):
                problems.append(f"{d.get('rule_id')} is a hard rule: no deviation")
        return problems

    # ------------------------------------------------------------ the decision
    def decide(self, agent: str, card_id: str, facts: FactSheet, world: dict[str, Any], *, escalate: bool = False,
               run_id: str | None = None, step: str | None = None, subject: str | None = None) -> AgentResult:
        card = load_cards()[card_id]
        if card.agent != agent:
            raise ValueError(f"{card_id} belongs to {card.agent}, not {agent}")
        self._check_budget(agent)
        model, effort = self.model_for(agent, escalate)
        ctx = dict(run_id=run_id, step=step, subject=subject, prompt_version=playbook_pack(agent)[1])
        max_tokens = config()["max_tokens"]

        note, out, problems, cost0 = None, None, [], self.spent_today()
        for _ in range(2):
            out, resp = self._call(agent, build_request(agent, card, facts, world, model, effort, max_tokens, note), ctx)
            problems = self.check(out, card, facts)
            if not problems:
                break
            note = "Fix these and answer again: " + "; ".join(problems)
        if problems:
            status = "refused" if resp.stop_reason == "refusal" else "agent_undecided"
            fallback = {"option": card.undecided, "confidence": "low", "reasoning": "agent did not decide",
                        "fact_ids": [], "cited_rules": [], "deviations": [], "missing_facts": [], "not_from_course": []}
            res = AgentResult(card_id, agent, status, fallback, model, problems=problems)
            return self._record(res, facts, run_id, cost0)

        critic = self.critique(agent, card, facts, world, out, ctx)
        if critic["verdict"] == "return":
            note = "The Critic returned your decision: " + "; ".join(f"{i['kind']}: {i['detail']}" for i in critic["issues"])
            out2, _ = self._call(agent, build_request(agent, card, facts, world, model, effort, max_tokens, note), ctx)
            if not self.check(out2, card, facts):
                out = out2
                critic = self.critique(agent, card, facts, world, out, ctx, escalate=True)
        status = "decided" if critic["verdict"] == "pass" else "critic_rejected"
        res = AgentResult(card_id, agent, status, out, model, critic=critic)
        return self._record(res, facts, run_id, cost0)

    def critique(self, agent: str, card: Card, facts: FactSheet, world: dict, out: dict, ctx: dict,
                 escalate: bool = False) -> dict:
        self._check_budget("C")
        model, effort = self.model_for("C", escalate, checked_agent=agent)
        decision = f"<decision agent=\"{agent}\">\n{json.dumps(out, ensure_ascii=False, sort_keys=True)}\n</decision>"
        req = build_request("C", card, facts, world, model, effort, config()["max_tokens"], pack_for=agent,
                            schema=CRITIC_SCHEMA, extra_user=decision)
        res, _ = self._call("C", req, {**ctx, "subject": ctx.get("subject")})
        if res is None:
            return {"verdict": "flag_ilay", "issues": [{"kind": "reasoning", "detail": "critic gave no valid output"}]}
        if res["verdict"] != "pass" and not escalate and config()["agents"]["C"].get("escalate"):
            return self.critique(agent, card, facts, world, out, ctx, escalate=True)   # a problem: the strong model checks
        return res

    def _record(self, res: AgentResult, facts: FactSheet, run_id: str | None, cost0: float) -> AgentResult:
        o = res.output
        res.cost_usd = self.spent_today() - cost0
        res.decision_id = self.journal.record(Decision(
            decided_by=f"agent:{res.agent}", subject=o.get("subject") or res.card_id, choice=o["option"],
            playbook_version=load_playbook().version, card_id=res.card_id, action="decide",
            options_considered=list(load_cards()[res.card_id].options), status=res.status, facts=facts.to_list(),
            reasoning=o.get("reasoning", ""), cited_rules=o.get("cited_rules", []), confidence=o.get("confidence"),
            deviations=o.get("deviations", []), prompt_version=playbook_pack(res.agent)[1], model=res.model,
            critic=json.dumps(res.critic, ensure_ascii=False) if res.critic else "; ".join(res.problems) or None),
            run_id=run_id)
        return res
