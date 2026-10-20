"""Command line: `qt check trade.yaml`, `qt rules`, `qt replay`."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from qt.calc.portfolio import Portfolio, Position
from qt.journal import Journal, decision_from_verdict
from qt.playbook import load_playbook
from qt.rules.checks import Context, OptionLeg, Readiness, TradeProposal, evaluate
from qt.rules.engine import Override


def _check(args: argparse.Namespace) -> int:
    spec = yaml.safe_load(Path(args.file).read_text(encoding="utf-8"))
    t = spec["trade"]
    if "option" in t:
        t["option"] = OptionLeg(**t["option"])
    proposal = TradeProposal(**t)
    pf = spec.get("portfolio", {})
    ctx = Context(Portfolio(pf.get("equity", 100_000), [Position(**p) for p in pf.get("positions", [])]),
                  regime=spec.get("regime", "portfolio"), readiness=Readiness(**spec.get("readiness", {})),
                  gross_limit=spec.get("gross_limit"))
    overrides = tuple(Override(o["rule_id"], o["principle"], tuple(o["fact_ids"]), o["reason"])
                      for o in spec.get("overrides", []))
    v = evaluate(proposal, ctx, overrides, tuple(spec.get("approvals", [])))
    print(f"{proposal.symbol} {proposal.action}: {v.status.value}")
    for f in v.findings:
        print(f"  [{f.severity.value:>17}] {f.rule_id}: {f.message}")
    if args.journal:
        did = Journal(args.journal).record(decision_from_verdict(v, proposal.symbol, load_playbook().version))
        print(f"journal entry #{did}")
    return 0


def _rules(args: argparse.Namespace) -> int:
    pb = load_playbook()
    print(json.dumps({"playbook_version": pb.version, "hard": pb.hard_rules()}, indent=1))
    return 0


def _replay(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
    from conftest import load_bars  # fixtures live with the tests

    from qt.replay import report
    print(report(load_bars))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="qt")
    sub = ap.add_subparsers(required=True)
    c = sub.add_parser("check", help="run the rules engine on a trade spec (YAML)")
    c.add_argument("file")
    c.add_argument("--journal", help="SQLite journal path to record the verdict")
    c.set_defaults(fn=_check)
    sub.add_parser("rules", help="list hard rules and playbook version").set_defaults(fn=_rules)
    sub.add_parser("replay", help="replay the lecturer's trades").set_defaults(fn=_replay)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
