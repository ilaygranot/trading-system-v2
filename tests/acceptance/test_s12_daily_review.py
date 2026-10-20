"""Stage 12 daily review of open positions (AT-S12-1/2/3/4/5/8/9 through qt.manage)."""
import pytest

from qt.calc.portfolio import PortfolioStop
from qt.calc.volatility import vix_check
from qt.manage import Held, daily_review, position_actions, tactical_cuts


def _h(sym="X", **kw):
    d = dict(direction="long", sector="XLK", qty=100, entry=10.0, entry_stop=9.0, stop=9.0, price=10.0)
    d.update(kw)
    return Held(sym, **d)


def test_stop_hit_is_a_preapproved_exit():
    a = position_actions(_h(price=8.9))
    assert [(x.kind, x.rule_id, x.preapproved) for x in a] == [("exit", "S12-R2", True)]
    assert position_actions(_h(direction="short", entry=10, entry_stop=11, stop=11, price=11.2))[0].kind == "exit"


def test_at_s12_2_sixty_forty_in_the_review():
    a = position_actions(_h(price=13))
    assert (a[0].kind, a[0].price, a[0].preapproved) == ("tighten_stop", 11.80, True)
    assert position_actions(_h(price=13, stop=12.0)) == []               # never moves the stop back
    opt = _h("OPT", instrument="option", qty=10, entry=2.0, entry_stop=None, stop=None, price=5.0, multiplier=100)
    s = position_actions(opt)[0]
    assert (s.kind, s.qty) == ("sell", 3.6)


def test_at_s12_3_winner_in_the_review():
    r = daily_review([_h("W", qty=310, price=100)], equity=100_000)
    a = [x for x in r.actions if x.rule_id == "S12-R12"]
    assert a[0].kind == "sell" and a[0].qty == pytest.approx(310 * 7 / 31, abs=0.01)
    r = daily_review([_h("W", qty=180, price=100)], equity=100_000)
    assert not [x for x in r.actions if x.rule_id == "S12-R12"]


def test_at_s12_1_portfolio_stop_in_the_review():
    ps = PortfolioStop(peak=60_000)
    r = daily_review([], equity=50_700, portfolio_stop=ps)
    assert [x.kind for x in r.actions if x.rule_id == "S12-R1"] == ["block_new_risk", "reduce"]
    assert r.metrics["drawdown_pct"] == pytest.approx(15.50)
    assert any(x.rule_id == "S12-R1" for x in daily_review([], 51_500, ps).actions)    # still latched


def test_at_s12_5_vix_is_flag_only():
    r = daily_review([], 100_000, vix=vix_check([15.0] * 20 + [21.0]))
    assert [(x.kind, x.rule_id) for x in r.actions] == [("flag", "S12-R24")]


def test_at_s12_8_tactical_cut_order():
    held = [_h("A", qty=1, price=12_000, unwanted=True), _h("B", qty=1, price=12_000, beta=2.1),
            _h("C", qty=1, price=12_000, speculative=True), _h("D", qty=1, price=12_000, below_mas=True),
            _h("E", qty=1, price=11_000), _h("F", qty=1, price=11_000)]                        # 70% invested
    cuts = tactical_cuts(held, 100_000)
    assert [c.symbol for c in cuts] == ["A", "B"]                     # 70 -> 46 < 50
    r = daily_review(held, 100_000, regime="tactical", regime_changed=True)
    assert "S12-R3" in r.rules() and [c.symbol for c in r.actions if c.rule_id == "S12-R26"] == ["A", "B"]
    big = [_h(s, qty=1, price=20_000, **kw) for s, kw in (("A", {"unwanted": True}), ("E", {}), ("F", {}), ("G", {}))]
    assert tactical_cuts(big, 100_000)[-1].kind == "flag"                # ordered cuts not enough: 20% alternative


def test_at_s12_4_high_net_into_shock():
    held = [_h("P", qty=1, price=60_000, entry=50_000), _h("Q", qty=1, price=50_000, entry=55_000)]
    r = daily_review(held, 100_000, market_shock=True)
    shock = [x for x in r.actions if x.rule_id == "S12-R7"]
    assert shock and shock[0].detail.endswith("Q, P")                   # no-profit first
    assert not [x for x in r.actions if x.kind in ("exit",) and x.rule_id == "S12-R7"]


def test_at_s12_9_adverse_catalyst_and_beta_disconnect():
    r = daily_review([_h("PCG", adverse_catalyst=True, stock_ret=-8.0)], 100_000, market_ret=1.0)
    assert ("reduce", "S12-R27") in [(x.kind, x.rule_id) for x in r.actions]
    assert "S12-R28" in r.rules()


def test_metrics():
    r = daily_review([_h("X", qty=500, price=100, beta=1.5)], 100_000)
    assert r.metrics["net"] == pytest.approx(0.5) and r.metrics["stress_loss_pct"][20] == pytest.approx(15.0)
