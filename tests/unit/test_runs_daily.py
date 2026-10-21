"""Runs and steps (runtime plan 5) and the daily run without agents, on SQLite and Postgres."""
from datetime import date, timedelta

import pytest

from conftest import load_bars
from qt.calc.volatility import Bar
from qt.daily import PIPELINES
from qt.runs import AlreadyRunning, Runner, Step

AS_OF = date(2023, 5, 31)


class Prices:
    """Fixture prices; VIX is flat 15 then 21 on the last day (a jump: flag only)."""
    def __init__(self, fail=()):
        self.fail, self.calls = set(fail), []

    def daily_bars(self, symbol, start, end):
        self.calls.append(symbol)
        if symbol in self.fail:
            raise ConnectionError("down")
        if symbol == "^VIX":
            days = [end - timedelta(days=i) for i in range(30, -1, -1)]
            return [Bar(d, 15, 15, 15, 21.0 if d == end else 15.0) for d in days], "fake"
        name = {"^GSPC": "GSPC_2023.json", "BA": "BA_prices_2023.json"}[symbol]
        return load_bars(name), "fixture"


PF = {"cash": 120_000, "peak_equity": 100_000, "positions": [
    {"symbol": "BA", "direction": "long", "sector": "XLI", "qty": 100, "entry": 150.0, "entry_stop": 140.0,
     "stop": 140.0}]}


def _runner(db, prices=None, **extra):
    return Runner(db, PIPELINES, {"prices": prices or Prices(), "portfolio": PF, **extra})


def test_daily_run_end_to_end(db):
    sent = []
    r = _runner(db, notifier=sent.append)
    rid = r.start("daily", AS_OF)
    st = r.status()[0]
    assert st["id"] == rid and st["status"] == "done"
    assert [s["step"] for s in st["steps"]] == ["fetch", "fill", "calc", "rules", "watch", "agents", "journal", "queue",
                                            "book", "notify"]
    items = db.all("SELECT rule_id, status FROM approval_queue", json_cols=())
    rules = {i["rule_id"]: i["status"] for i in items}
    assert rules["S12-R11"] == "standing_order"            # BA ~200 vs entry 150 / stop 140: 60/40 trail
    assert rules["S12-R24"] == "info"                      # VIX jump is a flag only
    assert sent and sent[0].startswith("יומית 2023-05-31")
    n = len(items)
    r2 = _runner(db).start("daily", AS_OF)                  # a re-run of the same day adds no queue item
    assert len(db.all("SELECT id FROM approval_queue")) == n and r2 != rid


def test_resume_skips_done_steps_and_reuses_the_snapshot(db):
    calls = {"n": 0}

    def boom(ctx, _):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("crash in the middle")
        return {"ok": True, "price_seen": ctx.out("calc")["prices"]["BA"]}

    prices = Prices()
    pipes = {"daily": PIPELINES["daily"][:3] + (Step("boom", boom),) + PIPELINES["daily"][3:]}
    r = Runner(db, pipes, {"prices": prices, "portfolio": PF})
    with pytest.raises(RuntimeError):
        r.start("daily", AS_OF)
    run = r.status()[0]
    assert run["status"] == "failed" and [s["status"] for s in run["steps"]][-1] == "failed"
    fetched = len(prices.calls)
    r.resume(run["id"])
    assert len(prices.calls) == fetched                    # rule 2: data fetched once per run
    run = r.status()[0]
    assert run["status"] == "done" and run["resumes"] == 1
    boom_step = [s for s in run["steps"] if s["step"] == "boom"][0]
    assert boom_step["attempts"] == 2


def test_lock_one_run_per_kind(db):
    r = _runner(db)
    db.insert("run_locks", dict(kind="daily", run_id="daily-x", acquired_at="2999-01-01T00:00:00+00:00"),
              returning=None)
    with pytest.raises(AlreadyRunning):
        r.start("daily", AS_OF)


def test_provider_fallback_and_stale_data(db):
    r = _runner(db, prices=Prices(fail={"BA"}), prices_fallback=None)
    r.start("daily", AS_OF)
    items = {i["rule_id"]: i for i in db.all("SELECT rule_id, status, payload FROM approval_queue",
                                             json_cols=["payload"])}
    assert "DATA-STALE" in items and "BA" in items["DATA-STALE"]["payload"]["detail"]
    r2 = _runner(db, prices=Prices(fail={"BA"}), prices_fallback=Prices())
    rid = r2.start("daily", AS_OF + timedelta(days=0))
    assert db.one("SELECT output FROM run_steps WHERE run_id=? AND step='fetch'", (rid,),
                  ["output"])["output"]["stale"] == []


def _wl(**kw):
    e = {"symbol": "BA", "direction": "long", "etf": "XLI", "bucket": "conditional", "added": "2023-05-01",
         "trigger_kind": "close_above", "trigger_level": 190.0, "k": 4.0, "target": 300.0,
         "target_source": "own_multiple_target", "invalidate_level": 150.0, "time_stop": "2023-06-30",
         "next_earnings": "2023-06-03", "first_size_pct": 6.0, "price_at_add": 185.0, "tldr": "שורה לחבר"}
    e.update(kw)
    return {"as_of": "2023-05-01", "entries": [e]}


def test_watchlist_trigger_becomes_an_entry_proposal(db):
    sent = []
    pf = {"cash": 100_000, "peak_equity": 100_000, "positions": []}
    r = Runner(db, PIPELINES, {"prices": Prices(), "portfolio": pf, "watchlist": _wl(), "notifier": sent.append})
    rid = r.start("daily", AS_OF)
    w = r.status()[0]
    assert "watch" in [s["step"] for s in w["steps"]]
    item = db.one("SELECT kind, rule_id, status, payload FROM approval_queue WHERE symbol='BA'", json_cols=("payload",))
    assert (item["kind"], item["rule_id"], item["status"]) == ("enter", "S10-R4", "pending")
    assert item["payload"]["qty"] > 0 and "שורה לחבר" in item["payload"]["detail"]
    assert "סיכום יום" in sent[0] and "BA" in sent[0] and "דוח בעוד 3 ימים" in sent[0]
    assert rid


def test_watchlist_held_expired_and_watch_bucket_make_no_entry(db):
    pf = {"cash": 100_000, "peak_equity": 100_000, "positions": []}
    for wl in (_wl(time_stop="2023-05-10", trigger_level=500.0),            # not triggered, past its time stop
               _wl(bucket="watch", trigger_kind="now", trigger_level=None)):  # watch: a recheck, never an entry
        Runner(db, PIPELINES, {"prices": Prices(), "portfolio": pf, "watchlist": wl}).start("daily", AS_OF)
        assert not db.all("SELECT id FROM approval_queue WHERE rule_id='S10-R4'")
        db.execute("DELETE FROM approval_queue")
