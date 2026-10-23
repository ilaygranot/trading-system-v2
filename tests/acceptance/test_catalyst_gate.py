"""Acceptance: the catalyst gate once its inputs are real (S07-R13, S08-R28, S10-R9, D-S10-5). The checks are the
existing ones in qt.rules.checks; catalyst_chain.catalyst_horizon and trade_plan now feed them."""
import json
from datetime import date, timedelta
from pathlib import Path

from qt.calc.portfolio import Portfolio
from qt.research import catalyst_chain as CC
from qt.rules.checks import Context, TradeProposal, evaluate
from qt.rules.engine import Status

AS_OF = date(2026, 10, 20)


def _p(**kw):
    base = {"symbol": "XYZ", "direction": "long", "stage": "initial", "entry": 100.0, "stop": 94.0, "target": 130.0,
            "target_source": "technical_level", "horizon_months": 2.0, "adr_pct": 1.5, "requested_pct": 3.0,
            "market_cap": 50e9, "sector": "XLE", "traffic_light": "green", "price_behaviour_qm": ("green", "green"),
            "heat": "hot", "catalyst_expected": True}
    base.update(kw)
    return TradeProposal(**base)


def _v(p):
    return evaluate(p, Context(Portfolio(100_000)))


def _ids(v, kind):
    return {f.rule_id for f in v.findings if f.kind == kind}


def _ev(days, i, cls="B"):
    return {"id": f"EV{i:02d}", "date": (AS_OF + timedelta(days=days)).isoformat(), "timing": "expected",
            "kind": "other_event", "what": f"אירוע {i}", "class": cls, "direction": "for_idea", "priced_in": False,
            "persistence": "none", "flags": []}


def test_no_catalyst_blocks_s07_r13():
    v = _v(_p(catalyst_expected=False))
    assert "S07-R13" in _ids(v, "violation") and v.status is not Status.READY_FOR_APPROVAL
    assert v.status is Status.NEEDS_OVERRIDE         # S07-R13 is a default-tier rule: only an override with a J passes
    v = _v(_p(catalyst_expected=False, heat="cold"))  # the agents path: no counted catalyst also means cold (S08-R33)
    assert v.status is Status.BLOCKED
    assert "S07-R13" not in [f.rule_id for f in _v(_p()).findings]


def test_chain_turns_needs_reason_into_flag():
    ch = CC.catalyst_horizon(2.0, [_ev(60, 1), _ev(137, 2)], AS_OF)
    assert ch["expected"] and ch["chain"] and ch["months"] > 3
    assert not CC.catalyst_horizon(2.0, [_ev(60, 1, "C"), _ev(137, 2)], AS_OF)["chain"]   # a C is never a link
    v = _v(_p(horizon_months=4.5, catalyst_chain=ch["chain"]))
    d5 = [f for f in v.findings if f.rule_id == "D-S10-5"]
    assert d5 and all(f.kind == "flag" for f in d5) and "D-S10-5" not in _ids(v, "needs_reason")
    v = _v(_p(horizon_months=4.5))
    assert "D-S10-5" in _ids(v, "needs_reason")


def test_cold_or_adverse_is_s08_r28():
    assert "S08-R28" in _ids(_v(_p(heat="cold")), "violation")
    assert "S08-R28" in _ids(_v(_p(news_adverse=True)), "violation")
    assert "S08-R28" not in [f.rule_id for f in _v(_p()).findings]


def test_trade_plan_takes_the_catalyst_rows():
    """idea.trade_plan: the rows set catalyst_expected (when the judgment has none), the chain and the horizon."""
    from qt.calc.volatility import Bar
    from qt.research import idea
    fix = json.loads((Path(__file__).parent.parent / "fixtures" / "research_dossier.json").read_text(encoding="utf-8"))
    ch = fix["ideas"]["SHEL"]["chart"]
    bars = [Bar(date.fromisoformat(d), o, h, lo, c, 1e6) for d, o, h, lo, c in
            zip(ch["dates"], ch["open"], ch["high"], ch["low"], ch["close"])]
    tl = {"combined": "green", "price_behaviour_qm": ("green", "green")}
    args = ("SHEL", "long", bars, 129.52, "own_multiple_target", 4.0, "XLE", {}, "bullish", tl, {}, None, False)
    cold = idea.trade_plan(*args, catalysts=[_ev(20, 1, "C")], as_of=AS_OF, heat="cold")
    rules = {f["rule_id"] for f in cold["verdict"]["findings"]}
    assert {"S07-R13", "S08-R28"} <= rules and cold["verdict"]["status"] == "blocked"
    hot = idea.trade_plan(*args, catalysts=[_ev(60, 1), _ev(137, 2), _ev(90, 3, "C")], as_of=AS_OF)
    assert hot["horizon_basis"] == "catalyst" and hot["catalyst_chain"] and hot["horizon_months"] >= hot["horizon_sigma_months"]
    assert "S07-R13" not in {f["rule_id"] for f in hot["verdict"]["findings"]}
    old = idea.trade_plan(*args)                                       # existing callers: no catalyst arguments
    assert "horizon_basis" not in old
