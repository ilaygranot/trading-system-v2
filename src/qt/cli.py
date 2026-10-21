"""Command line: `qt check trade.yaml`, `qt macro stance.yaml`, `qt rules`, `qt replay`,
`qt run daily|premarket|weekly|monthly|resume|status [--scheduled]`, `qt research`, `qt web`, `qt set-webhook`."""
from __future__ import annotations

import argparse
import json
import os
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


def _run(args: argparse.Namespace) -> int:
    from datetime import date

    from qt import app
    from qt.db import DB
    from qt.runs import AlreadyRunning

    db = DB(args.db) if args.db else DB()
    if db.url == ":memory:" and args.what != "status":
        print("set QT_DATABASE_URL (or --db) so the run can be resumed")
    if args.scheduled:
        print(app.scheduled(args.what, db))
        return 0
    svc = app.services(db, args.portfolio) if args.what != "status" else {}
    runner = app.runner(db, svc)
    try:
        if args.what == "status":
            for r in runner.status(args.limit):
                print(f"{r['id']}  {r['status']}  {r['error'] or ''}")
                for s in r["steps"]:
                    print(f"   {s['step']:<8} {s['subject'] or '-':<8} {s['status']:<7} x{s['attempts']} {s['error'] or ''}")
            return 0
        rid = runner.resume(args.run_id) if args.what == "resume" else \
            runner.start(args.what, date.fromisoformat(args.as_of) if args.as_of else app.as_of_today())
    except AlreadyRunning as e:
        print(f"already running: {e}")
        return 0
    print(rid)
    return 0


def _web(args: argparse.Namespace) -> int:
    from qt.web import serve
    serve(args.port)
    return 0


def _webhook(args: argparse.Namespace) -> int:
    """Point the bot at the web service (once, after the first deploy)."""
    from qt.telegram import Telegram
    bot = Telegram.from_env()
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    base = args.url or os.environ.get("QT_PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if bot is None or not secret or not base:
        print("need TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_WEBHOOK_SECRET and the public URL")
        return 1
    bot.set_webhook(f"{base.rstrip('/')}/telegram/{secret}", secret)
    print("webhook set")
    return 0


def _research(args: argparse.Namespace) -> int:
    from datetime import date
    from qt.data.providers import FRED
    from qt.data.store import Store
    from qt.research import report, run
    as_of = date.fromisoformat(args.as_of)
    out, work = Path(args.out), Path(args.work or f".qt-research/{as_of}")
    work.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    judge = None
    if args.agents:                                  # the cards instead of a judgments file (ANTHROPIC_API_KEY)
        from qt.agents import AgentRunner
        from qt.agents.runner import config
        from qt.db import DB
        from qt.research.judge import Judge
        db = DB(f"sqlite:///{work / 'agents.sqlite'}")
        judge = Judge(AgentRunner(db, limit_usd=config()["budget"]["weekly_research_usd"]), run_id=f"research-{as_of}")
    d = run.run(as_of, work, Path(args.manual), Path(args.judgments) if args.judgments else None, work / "yahoo",
                FRED(Store(str(work / "fred.sqlite"))), judge=judge)
    (out / "dossier.json").write_text(json.dumps(d, default=str, ensure_ascii=False), encoding="utf-8")
    (out / "report.html").write_text(report.render(d), encoding="utf-8")
    import yaml
    from qt.watch import from_dossier
    (out / "watchlist.yaml").write_text(yaml.safe_dump(from_dossier(json.loads(json.dumps(d, default=str))),
                                                       allow_unicode=True, sort_keys=False), encoding="utf-8")
    for f in d["funnel"]:
        print(f"{f['n']:>6}  {f['label']}")
    print(f"report: {out / 'report.html'} · watchlist: {out / 'watchlist.yaml'} (send it to the bot)")
    return 0


def _verify(args: argparse.Namespace) -> int:
    """Verification on a dossier that already exists: writes it back with a verification section and re-renders."""
    from qt.data.store import Store
    from qt.research import report, verify
    path = Path(args.dossier)
    d = json.loads(path.read_text(encoding="utf-8"))
    src = verify.default_sources(Store(args.cache) if args.cache else None)
    if args.no_fetch:
        src["fetch"] = None
    d["verification"] = verify.run(d, **src)
    out = Path(args.out or path.parent)
    out.mkdir(parents=True, exist_ok=True)
    (out / "dossier.json").write_text(json.dumps(d, default=str, ensure_ascii=False), encoding="utf-8")
    (out / "report.html").write_text(report.render(d), encoding="utf-8")
    t = d["verification"]["summary"]["total"]
    print(f"verified: {t['ok']} ok, {t['single']} single source, {t['bad']} bad -> {out / 'report.html'}")
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
    r = sub.add_parser("run", help="runs: daily | premarket | weekly | monthly | resume <run-id> | status")
    r.add_argument("what", choices=["daily", "premarket", "weekly", "monthly", "resume", "status"])
    r.add_argument("run_id", nargs="?")
    r.add_argument("--as-of")
    r.add_argument("--portfolio", help="cash, peak equity and open positions (YAML; default QT_PORTFOLIO, "
                                       "else the last file sent to the bot)")
    r.add_argument("--scheduled", action="store_true", help="cron mode: run only when due in New York time")
    r.add_argument("--db", help="database URL or SQLite path (default QT_DATABASE_URL)")
    r.add_argument("--limit", type=int, default=10)
    r.set_defaults(fn=_run)
    rs = sub.add_parser("research", help="one full research run (stages 0-11) into a dossier and an HTML report")
    rs.add_argument("--as-of", required=True)
    rs.add_argument("--manual", required=True, help="the ISM / NFIB values (YAML, with sources)")
    js = rs.add_mutually_exclusive_group(required=True)
    js.add_argument("--judgments", help="the judgment fields written by hand (YAML: A1, A2, ideas)")
    js.add_argument("--agents", action="store_true", help="the agents decide through the cards (qt.research.judge)")
    rs.add_argument("--out", required=True, help="where dossier.json and report.html go")
    rs.add_argument("--work", help="stage cache (default .qt-research/<as-of>)")
    rs.set_defaults(fn=_research)
    vf = sub.add_parser("verify", help="data verification on an existing dossier (SEC, second close, sources)")
    vf.add_argument("--dossier", required=True)
    vf.add_argument("--out", help="where dossier.json and report.html go (default: next to the dossier)")
    vf.add_argument("--cache", help="HTTP cache (SQLite path)")
    vf.add_argument("--no-fetch", action="store_true", help="do not open source pages to check quotes")
    vf.set_defaults(fn=_verify)
    w = sub.add_parser("web", help="the dashboard and Telegram webhook (local; gunicorn qt.web:application in the cloud)")
    w.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    w.set_defaults(fn=_web)
    sub.add_parser("set-webhook", help="point the Telegram bot at the web service").set_defaults(fn=_webhook)
    sub.choices["set-webhook"].add_argument("--url")
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
