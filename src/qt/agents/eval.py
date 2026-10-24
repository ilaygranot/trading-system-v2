"""The course test set for the agents (runtime plan 8.3): `qt eval agents`.

Each case is one decision card from the application lessons (098-102) with the facts as they were known that day
(eval_cases/course.yaml). The agent decides it through the real AgentRunner (the card's schema, the code checks, the
retry, the Critic), on a blind Playbook (qt.agents.blind), once per model. A case scores hit (the lecturer's option),
near (a close option) or miss. The Critic gets its own test: planted flaws it must catch and one sound decision it
must pass.

Pass bar (Ilay 24.10, proposed by Claude, not from course): no miss on a card that needs Ilay's approval, and at least
80% hit or near over all the cases. A model change must pass it (models.yaml).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from qt.agents.cards import load_cards
from qt.agents.runner import AgentRunner, BudgetExceeded, config
from qt.facts import FactSheet

CASES = Path(__file__).parent / "eval_cases" / "course.yaml"
MARK = {"hit": "✅", "near": "🟡", "miss": "❌", "not_run": "⏸"}


@lru_cache(maxsize=None)
def load_suite(path: str | None = None) -> dict:
    return yaml.safe_load(Path(path or CASES).read_text(encoding="utf-8"))


def _as_date(v: Any) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def fact_sheet(suite: dict, case: dict) -> FactSheet:
    """The scenario's facts, then the case's own: F ids in that order (the traps cite them)."""
    sc = suite["scenarios"][case["scenario"]]
    fs = FactSheet()
    for key, value in sc.get("facts", []) + case.get("facts", []):
        fs.add(key, value, f"course:{sc['lesson']}", _as_date(sc["as_of"]))
    return fs


def score(case: dict, option: str | None) -> str:
    if option in [str(o) for o in case["expect"]]:
        return "hit"
    return "near" if option in [str(o) for o in case.get("near", [])] else "miss"


@dataclass
class CaseResult:
    case_id: str
    card_id: str
    model: str
    score: str
    option: str | None = None
    status: str = ""
    expect: list[str] = field(default_factory=list)
    reasoning: str = ""
    critic: str = ""
    reading: bool = False
    cost_usd: float = 0.0


@dataclass
class TrapResult:
    trap_id: str
    case_id: str
    flaw: str
    verdict: str
    caught: bool
    kinds: list[str] = field(default_factory=list)
    check_problems: list[str] = field(default_factory=list)


def _cases(suite: dict, only: list[str] | None) -> list[dict]:
    return [c for c in suite["cases"] if not only or any(c["id"] == o or c["id"].startswith(o) or c["card"] == o for o in only)]


def run_cases(runner: AgentRunner, suite: dict, only: list[str] | None = None) -> list[CaseResult]:
    cards, out, stopped = load_cards(), [], False
    for case in _cases(suite, only):
        card = cards[case["card"]]
        base = dict(case_id=case["id"], card_id=card.card_id, model=runner.model or "default",
                    expect=[str(o) for o in case["expect"]], reading=bool(case.get("reading")))
        if stopped:
            out.append(CaseResult(score="not_run", status="budget", **base))
            continue
        try:
            r = runner.decide(card.agent, card.card_id, fact_sheet(suite, case), case.get("world") or {},
                              run_id=f"eval-{runner.model or 'default'}", step="eval", subject=case.get("subject"))
        except BudgetExceeded:
            stopped = True
            out.append(CaseResult(score="not_run", status="budget", **base))
            continue
        crit = r.critic or {}
        out.append(CaseResult(score=score(case, r.option), option=r.option, status=r.status,
                              reasoning=r.output.get("reasoning", ""), cost_usd=round(r.cost_usd, 4),
                              critic="; ".join(f"{i['kind']}: {i['detail']}" for i in crit.get("issues", []))
                              or crit.get("verdict", "") or "; ".join(r.problems), **base))
    return out


def run_traps(runner: AgentRunner, suite: dict) -> list[TrapResult]:
    """The Critic on planted decisions: each trap must pass the code checks (else it tests nothing) and be caught;
    the control must pass."""
    cards, by_id, out = load_cards(), {c["id"]: c for c in suite["cases"]}, []
    for t in suite.get("traps", []) + [{**c, "flaw": "control"} for c in suite.get("control", [])]:
        case = by_id[t["case"]]
        card, facts = cards[case["card"]], fact_sheet(suite, case)
        problems = runner.check(t["output"], card, facts)
        if problems:
            out.append(TrapResult(t["id"], case["id"], t["flaw"], "invalid", False, check_problems=problems))
            continue
        try:
            res = runner.critique(card.agent, card, facts, case.get("world") or {}, t["output"],
                                  dict(run_id="eval-critic", step="eval", subject=case.get("subject")))
        except BudgetExceeded:
            out.append(TrapResult(t["id"], case["id"], t["flaw"], "not_run", False))
            continue
        v = res["verdict"]
        out.append(TrapResult(t["id"], case["id"], t["flaw"], v, (v == "pass") if t["flaw"] == "control" else v != "pass",
                              kinds=[i["kind"] for i in res.get("issues", [])]))
    return out


def summary(results: list[CaseResult]) -> dict:
    cards = load_cards()
    ran = [r for r in results if r.score != "not_run"]
    good = sum(r.score in ("hit", "near") for r in ran)
    blocking = [r.case_id for r in ran if r.score == "miss" and cards[r.card_id].approval == "required"]
    share = good / len(ran) if ran else 0.0
    bar = config().get("eval", {}).get("pass_share", 0.8)
    return dict(cases=len(results), ran=len(ran), hit=sum(r.score == "hit" for r in ran),
                near=sum(r.score == "near" for r in ran), miss=sum(r.score == "miss" for r in ran),
                share=round(share, 3), blocking_misses=blocking,
                passed=bool(ran) and len(ran) == len(results) and not blocking and share >= bar,
                cost_usd=round(sum(r.cost_usd for r in results), 2))


def evaluate(db, client, models: list[str], only: list[str] | None = None, traps: bool = True,
             suite: dict | None = None, limit_usd: float | None = None) -> dict:
    """One run of the test set: every case once per model, then the Critic traps. One cap over the whole run."""
    suite = suite or load_suite()
    cap = limit_usd if limit_usd is not None else config().get("eval", {}).get("limit_usd", 40)
    report: dict[str, Any] = {"models": {}, "traps": []}
    for m in models:
        runner = AgentRunner(db, client, limit_usd=cap, blind=True, model=m)
        res = run_cases(runner, suite, only)
        report["models"][m] = {"summary": summary(res), "results": [asdict(r) for r in res]}
    if traps:
        report["traps"] = [asdict(t) for t in run_traps(AgentRunner(db, client, limit_usd=cap, blind=True), suite)]
    report["spent_usd"] = round(AgentRunner(db, client).spent_today(), 2)
    return report


def render(report: dict) -> str:
    """The report as Markdown: one table per model, the Critic traps, and the cases that are our reading."""
    lines = ["# Agent test set (course cases, blind Playbook)", ""]
    for m, body in report["models"].items():
        s = body["summary"]
        lines += [f"## {m}: {'PASS' if s['passed'] else 'FAIL'}",
                  f"{s['hit']} hit, {s['near']} near, {s['miss']} miss of {s['ran']}/{s['cases']} run "
                  f"({s['share']:.0%} hit or near). Misses on approval cards: {', '.join(s['blocking_misses']) or 'none'}. "
                  f"Cost at list price: ${s['cost_usd']}.", "",
                  "| case | card | lecturer | agent | status | critic |", "|---|---|---|---|---|---|"]
        for r in body["results"]:
            exp = " / ".join(r["expect"]) + (" *" if r["reading"] else "")
            crit = (r["critic"] or "").replace("|", "/").replace("\n", " ")[:160]
            lines.append(f"| {r['case_id']} | {r['card_id']} | {exp} | {MARK[r['score']]} {r['option'] or '-'} | "
                         f"{r['status']} | {crit} |")
        lines.append("")
    if report.get("traps"):
        caught = sum(t["caught"] for t in report["traps"])
        lines += [f"## Critic: {caught}/{len(report['traps'])}", "", "| trap | flaw | verdict | issues |", "|---|---|---|---|"]
        for t in report["traps"]:
            issues = ", ".join(t["kinds"]) or "; ".join(t["check_problems"])
            lines.append(f"| {t['trap_id']} | {t['flaw']} | {'✅' if t['caught'] else '❌'} {t['verdict']} | {issues} |")
        lines.append("")
    lines += ["\\* the lecturer's option as we read his words, not his words: Ilay reviews these.",
              f"Total at list price: ${report.get('spent_usd', 0)}."]
    return "\n".join(lines)


def main(out: Path, models: list[str], only: list[str] | None, traps: bool, limit_usd: float | None) -> int:
    from qt.agents.cli_client import agent_client
    from qt.db import DB
    client = agent_client()
    if client is None:
        print("no agent client: set ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN")
        return 1
    out.mkdir(parents=True, exist_ok=True)
    db = DB(f"sqlite:///{out / 'eval.sqlite'}")                      # its own journal: nothing reaches the live one
    report = evaluate(db, client, models, only, traps, limit_usd=limit_usd)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    text = render(report)
    (out / "report.md").write_text(text, encoding="utf-8")
    print(text)
    return 0 if all(b["summary"]["passed"] for b in report["models"].values()) else 2
