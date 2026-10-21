"""The paper book and the life of one trade through the daily run: entry, 60/40, half at the target,
the trailing stop, the exit. Every step is checked against the course rule it implements."""
from datetime import date, datetime, timedelta, timezone

import pytest

from qt import book as B
from qt.approvals import Queue
from qt.calc.volatility import Bar
from qt.daily import PIPELINES
from qt.manage import Held, position_actions
from qt.runs import Runner
from qt.schedule import is_trading_day

PF = {"cash": 100_000.0, "peak_equity": 100_000.0, "positions": []}


# ---------------------------------------------------------------- book: pure functions

def test_enter_long_opens_the_position_with_its_stop_and_target():
    pf, what = B.apply_fill(PF, "enter", "XYZ", 50, 100.0, {"direction": "long", "sector": "XLE", "stop": 92.0,
                                                           "target": 130.0, "as_of": "2024-05-01"})
    p = pf["positions"][0]
    assert pf["cash"] == 95_000.0
    assert (p["qty"], p["entry"], p["entry_stop"], p["stop"], p["target"], p["sector"]) == (50, 100.0, 92.0, 92.0, 130.0, "XLE")
    assert "opened" in what and PF["positions"] == []                  # the input is not mutated


def test_add_averages_the_entry():
    pf, _ = B.apply_fill(PF, "enter", "XYZ", 50, 100.0, {"direction": "long", "stop": 92.0})
    pf, _ = B.apply_fill(pf, "add", "XYZ", 50, 110.0, {})
    p = pf["positions"][0]
    assert (p["qty"], p["entry"], pf["cash"]) == (100, 105.0, 89_500.0)


def test_half_at_target_marks_the_step_and_keeps_the_rest():
    pf, _ = B.apply_fill(PF, "enter", "XYZ", 50, 100.0, {"direction": "long", "stop": 92.0, "target": 130.0})
    pf, what = B.apply_fill(pf, "sell", "XYZ", 25, 131.0, {"rule_id": "S12-R13"})
    p = pf["positions"][0]
    assert (p["qty"], p["partial_taken"]) == (25, True)
    assert pf["cash"] == 95_000.0 + 25 * 131.0 and "25 left" in what


def test_exit_closes_and_records_the_result():
    pf, _ = B.apply_fill(PF, "enter", "XYZ", 50, 100.0, {"direction": "long", "stop": 92.0})
    pf, what = B.apply_fill(pf, "exit", "XYZ", 50, 90.0, {"rule_id": "S12-R2"})
    assert pf["positions"] == [] and pf["cash"] == 99_500.0
    assert pf["closed"][0] == {"symbol": "XYZ", "qty": 50, "entry": 100.0, "exit": 90.0, "pnl": -500.0, "rule": "S12-R2"}


def test_short_cash_and_result_have_the_right_sign():
    pf, _ = B.apply_fill(PF, "enter", "BA", 10, 200.0, {"direction": "short", "stop": 220.0})
    assert pf["cash"] == 102_000.0
    pf, _ = B.apply_fill(pf, "exit", "BA", 10, 180.0, {})
    assert pf["cash"] == 100_200.0 and pf["closed"][0]["pnl"] == 200.0


@pytest.mark.parametrize("direction, old, new, moved", [
    ("long", 92.0, 95.0, True), ("long", 92.0, 90.0, False),          # S12-R2: never away from the price
    ("short", 220.0, 210.0, True), ("short", 220.0, 230.0, False)])
def test_stop_only_tightens(direction, old, new, moved):
    pf = {"cash": 0, "positions": [{"symbol": "X", "direction": direction, "qty": 1, "entry": 100, "stop": old}]}
    out, what = B.apply_stop(pf, "X", new)
    assert out["positions"][0]["stop"] == (new if moved else old) and bool(what) == moved


def test_unsized_or_unknown_orders_change_nothing():
    pf, what = B.apply_fill(PF, "reduce", "XYZ", None, 100.0, {})
    assert pf == PF and "not applied" in what
    pf, what = B.apply_fill(PF, "exit", "XYZ", 10, 100.0, {})
    assert pf == PF and "no open position" in what


# ---------------------------------------------------------------- the half at the target (S12-R13)

def _held(**kw):
    d = dict(symbol="XYZ", direction="long", sector="XLE", qty=50, entry=100.0, entry_stop=92.0, stop=92.0,
             price=131.0, target=130.0)
    d.update(kw)
    return Held(**d)


def test_target_reached_proposes_half_for_ilay():
    a = [x for x in position_actions(_held()) if x.rule_id == "S12-R13"]
    assert len(a) == 1 and a[0].kind == "sell" and a[0].qty == 25 and not a[0].preapproved


@pytest.mark.parametrize("kw", [dict(price=129.0), dict(partial_taken=True), dict(qty=1), dict(target=None)])
def test_no_half_before_the_target_after_it_was_taken_or_for_one_share(kw):
    assert not [x for x in position_actions(_held(**kw)) if x.rule_id == "S12-R13"]


def test_short_target_is_below():
    h = _held(direction="short", entry=100.0, entry_stop=108.0, stop=108.0, price=79.0, target=80.0)
    assert [x.rule_id for x in position_actions(h)].count("S12-R13") == 1


# ---------------------------------------------------------------- one trade, start to finish

def _days(n: int, start=date(2024, 1, 2)) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = _days(108)
T = DAYS[100]                                                            # the breakout day
PATH = {DAYS[100]: (100.5, 102.0), DAYS[101]: (102.5, 103.0), DAYS[102]: (128.0, 130.0),
        DAYS[103]: (135.0, 141.0), DAYS[104]: (141.0, 141.0), DAYS[105]: (124.0, 120.0), DAYS[106]: (119.0, 119.0),
        DAYS[107]: (119.0, 119.0)}


class Path:
    def daily_bars(self, symbol, start, end):
        if symbol == "^VIX":
            return [Bar(d, 15, 15, 15, 15) for d in DAYS if start <= d <= end], "fake"
        out = []
        for d in DAYS:
            if start <= d <= end:
                o, c = PATH.get(d, (100.0, 100.0)) if symbol == "XYZ" else (4000.0, 4000.0)
                out.append(Bar(d, o, max(o, c) * 1.01, min(o, c) * 0.99, c, 1))
        return out, "fake"


WL = {"as_of": DAYS[90].isoformat(), "entries": [dict(
    symbol="XYZ", direction="long", etf="XLE", bucket="conditional", added=DAYS[90].isoformat(),
    trigger_kind="close_above", trigger_level=101.0, k=4.0, target=140.0, target_source="own_multiple_target",
    invalidate_level=95.0, time_stop=DAYS[107].isoformat(), next_earnings=None, first_size_pct=6.0,
    price_at_add=100.0, tldr="פריצה עם רוח גבית")]}


def _run(db, day):
    from qt.app import portfolio
    pf = portfolio(db) or PF
    Runner(db, PIPELINES, {"prices": Path(), "portfolio": pf, "watchlist": WL}).start("daily", day)
    return portfolio(db)


def _approve(db, day, rule):
    q = Queue(db)
    item = next(i for i in q.for_day(day) if i["rule_id"] == rule and i["status"] == "pending")
    q.decide(item["id"], "approve", now=datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(hours=21))
    return item


def test_one_trade_from_breakout_to_trailing_stop(db):
    # day 0: the close breaks the trigger -> an entry proposal with size, stop and target (S10-R4, S11-R5)
    pf = _run(db, DAYS[100])
    item = _approve(db, DAYS[100], "S10-R4")
    p = item["payload"]
    assert p["qty"] == int(100_000 * 0.06 // 102.0) and p["target"] == 140.0 and p["stop"] < 102.0
    assert pf["positions"] == []                                         # nothing held until the fill
    qty = p["qty"]

    # day 1: the approved order fills at the open; the book holds it with the approved stop and target
    pf = _run(db, DAYS[101])
    pos = pf["positions"][0]
    assert (pos["qty"], pos["entry"], pos["stop"], pos["target"]) == (qty, 102.5, p["stop"], 140.0)
    assert pf["cash"] == pytest.approx(100_000 - qty * 102.5)
    assert not [i for i in Queue(db).for_day(DAYS[101]) if i["rule_id"] == "S10-R4"]   # held: no new proposal

    # day 2: 130 is past 3R -> the 60/40 stop (approved with the trade) is in the book (S12-R11)
    pf = _run(db, DAYS[102])
    r = 102.5 - p["stop"]
    assert 130.0 >= 102.5 + 3 * r
    assert pf["positions"][0]["stop"] == pytest.approx(102.5 + 0.6 * (130.0 - 102.5), abs=0.01)

    # day 3: past the 140 target -> half is proposed (S12-R13), the stop keeps trailing
    pf = _run(db, DAYS[103])
    half = _approve(db, DAYS[103], "S12-R13")
    assert half["payload"]["qty"] == qty // 2
    trail = pf["positions"][0]["stop"]
    assert trail == pytest.approx(102.5 + 0.6 * (141.0 - 102.5), abs=0.01)

    # day 4: the half fills; the rest runs and the target step is marked done
    pf = _run(db, DAYS[104])
    pos = pf["positions"][0]
    assert (pos["qty"], pos["partial_taken"]) == (qty - qty // 2, True)
    assert not [i for i in Queue(db).for_day(DAYS[104]) if i["rule_id"] == "S12-R13"]

    # day 5: the close goes through the trailing stop -> a standing exit order (S12-R2)
    pf = _run(db, DAYS[105])
    ex = [i for i in Queue(db).for_day(DAYS[105]) if i["rule_id"] == "S12-R2"]
    assert len(ex) == 1 and ex[0]["status"] == "standing_order"

    # day 6: the exit fills at the open; the trade is closed with a profit on both halves
    pf = _run(db, DAYS[106])
    assert pf["positions"] == []
    assert [c["symbol"] for c in pf["closed"]] == ["XYZ"] and pf["closed"][0]["exit"] == 119.0
    assert pf["cash"] > 100_000                                          # half at 141, the rest at 119, from 102.5
    assert pf["peak_equity"] >= pf["cash"]


def test_a_new_portfolio_file_replaces_the_book(db):
    _run(db, DAYS[100])
    Queue(db).upload("portfolio", "portfolio.yaml", "cash: 5\npositions: []\n")
    from qt.app import portfolio
    assert portfolio(db)["cash"] == 5
