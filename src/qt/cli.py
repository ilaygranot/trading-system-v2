"""Command line: `qt check trade.yaml`, `qt macro stance.yaml`, `qt rules`, `qt replay`."""
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
                  gross_limit=spec.get("gross_limit"), macro_bias=spec.get("macro_bias"),
                  market_trend=spec.get("market_trend"))
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


def _macro(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from qt import macro as M
    spec = yaml.safe_load(Path(args.file).read_text(encoding="utf-8"))
    scores = [M.score_indicator(n, *(v[k] for k in ("now", "m3", "m6", "m12", "ref")))
              for n, v in spec["indicators"].items()]
    fed = spec.get("fed")
    st = M.stance(scores,
                  fed=M.fed_scale(fed["ffr"], fed["m2_yoy"], fed["balance_sheet"]) if fed else None,
                  infl=M.inflation(spec["cpi_yoy"]) if "cpi_yoy" in spec else None,
                  ism=M.ism_state(spec["ism_pmi_history"]) if "ism_pmi_history" in spec else None,
                  grey_choice=spec.get("grey_choice"), previous_zone=spec.get("previous_zone"))
    out = asdict(st)
    if st.compass_now:
        out["assets"] = M.preferred_assets(st.compass_now)
    print(json.dumps(out, indent=1, default=str))
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
    m = sub.add_parser("macro", help="compute the macro stance from indicator values (YAML)")
    m.add_argument("file")
    m.set_defaults(fn=_macro)
    sub.add_parser("rules", help="list hard rules and playbook version").set_defaults(fn=_rules)
    sub.add_parser("replay", help="replay the lecturer's trades").set_defaults(fn=_replay)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
