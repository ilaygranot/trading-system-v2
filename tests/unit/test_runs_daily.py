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
    assert [s["step"] for s in st["steps"]] == ["fetch", "fill", "calc", "rules", "watch", "news", "agents", "journal", "queue",
                                            "book", "learn", "notify"]
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


class NewsPrices(Prices):
    """Prices plus two headlines on BA: one against the position (class A, not priced in), one old."""
    def news(self, symbol, n=8):
        if symbol != "BA":
            return [], "fake"
        return [dict(title="FAA halts 737 deliveries", publisher="Reuters", link="https://x/1", published="2023-05-31"),
                dict(title="Old story", publisher="AP", link="https://x/0", published="2023-05-01")], "yahoo:test"


class FakeAgents:
    """DC-14 says class A against the idea; A7 answers hold. Records the cards asked."""
    def __init__(self):
        self.calls = []

    def decide(self, agent, card, fs, world, **kw):
        from types import SimpleNamespace as NS
        self.calls.append((card, kw.get("subject"), dict(world)))
        out = {"direction": "against_idea", "priced_in": False, "reasoning": "x"} if card == "DC-14" else {}
        return NS(option="A" if card == "DC-14" else "hold", output=out, status="decided", decision_id=1, critic=None,
                  for_ilay="", cost_usd=0.0)


def test_news_classifies_once_and_an_adverse_headline_reaches_a7(db):
    sent, ag = [], FakeAgents()
    r = _runner(db, prices=NewsPrices(), notifier=sent.append, agents=ag)
    r.start("daily", AS_OF)
    asked = [c[:2] for c in ag.calls if c[0] in ("DC-14", "DC-21")]
    assert asked == [("DC-14", "BA"), ("DC-21", "BA")]                         # the old headline is skipped
    assert "FAA halts" in ag.calls[1][2]["event"]
    assert "FAA halts 737 deliveries" in sent[0] and "A7" in sent[0]
    row = db.one("SELECT klass, direction FROM news_seen WHERE link = 'https://x/1'")
    assert (row["klass"], row["direction"]) == ("A", "against_idea")
    ag.calls.clear()
    r.start("daily", AS_OF + timedelta(days=1))
    assert ("DC-14", "BA") not in [c[:2] for c in ag.calls]                    # seen: not classified again


def test_news_without_agents_lists_raw_headlines(db):
    sent = []
    _runner(db, prices=NewsPrices(), notifier=sent.append).start("daily", AS_OF)
    assert "יומן חדשות" in sent[0] and "• BA FAA halts" in sent[0]


def test_news_digest_lists_every_stock_after_the_summary(db):
    sent = []
    _runner(db, prices=NewsPrices(), notifier=sent.append, agents=FakeAgents()).start("daily", AS_OF)
    assert len(sent) == 2 and sent[0].startswith("יומית") and sent[1].startswith("📰 חדשות לפי מניה")
    assert "BA (בתיק) · 1 כותרות" in sent[1] and "• [A, נגד] FAA halts 737 deliveries (Reuters, 05-31)" in sent[1]
    assert "Old story" not in sent[1]


def test_news_digest_names_quiet_stocks_and_splits_long_days():
    from qt.news import chunks, digest_lines
    out = {"enabled": True, "symbols": [{"symbol": "BA", "held": True}, {"symbol": "GE", "held": False}],
           "items": [dict(symbol="BA", title=f"t{i}", publisher="P", published="2023-05-31", link=str(i))
                     for i in range(6)]}
    lines = digest_lines(out)
    assert "• ועוד 2 בדשבורד" in lines and lines[-1] == "בלי כותרות חדשות: GE"
    parts = chunks(["x" * 50] * 200, limit=1000)
    assert all(len(p) <= 1000 for p in parts) and sum(p.count("x" * 50) for p in parts) == 200


# ---------------------------------------------------------------- heat (DC-15 / DC-16) and peers (S08-J25)

class HeatAgents:
    """DC-15 answers `hot`, DC-16 answers `frozen_exit`; DC-14 as FakeAgents. Records the cards asked."""
    def __init__(self, dc15="hot"):
        self.calls, self.dc15 = [], dc15

    def decide(self, agent, card, fs, world, **kw):
        from types import SimpleNamespace as NS
        self.calls.append((card, kw.get("subject"), dict(world)))
        opt = {"DC-14": "A", "DC-15": self.dc15, "DC-16": "frozen_exit"}.get(card, "hold")
        out = {"direction": "against_idea", "priced_in": False, "reasoning": "x"} if card == "DC-14" else {"reasoning": "r"}
        return NS(option=opt, output=out, status="decided", decision_id=None, critic=None, for_ilay="", cost_usd=0.0)


def _watch_out(db, rid):
    return db.one("SELECT output FROM run_steps WHERE run_id=? AND step='watch'", (rid,), ["output"])["output"]


def test_dc15_proposal_needs_approval(db):
    from datetime import UTC, datetime

    from qt.approvals import Queue
    pf = {"cash": 100_000, "peak_equity": 100_000, "positions": []}
    added = (AS_OF - timedelta(days=61)).isoformat()                     # day 61 (S08-R29, D-S08-1)
    wl = _wl(bucket="watch", trigger_kind="now", trigger_level=None, added=added, price_at_add=195.0, heat="cold")
    ag = HeatAgents()
    rid = Runner(db, PIPELINES, {"prices": Prices(), "portfolio": pf, "watchlist": wl, "agents": ag}).start("daily", AS_OF)
    assert [c[:2] for c in ag.calls] == [("DC-15", "BA")]
    world = ag.calls[0][2]
    assert world["current_state"] == "cold" and "day_61" in world["reasons"] and world["days_on_list"] == 61
    items = db.all("SELECT id, kind, rule_id, status, payload FROM approval_queue WHERE rule_id='S08-R26'",
                   json_cols=("payload",))
    assert len(items) == 1 and items[0]["status"] == "pending" and items[0]["payload"]["heat"] == "hot"
    assert db.one("SELECT ref_price FROM approval_queue WHERE id=?", (items[0]["id"],))["ref_price"] is None
    check = next(c for c in _watch_out(db, rid)["checks"] if c["symbol"] == "BA")
    assert check["heat"] == "cold"                                      # code never changes heat by itself
    Queue(db).decide(items[0]["id"], "approve", now=datetime(2023, 5, 31, 22, tzinfo=UTC))
    ag.calls.clear()
    rid2 = Runner(db, PIPELINES, {"prices": Prices(), "portfolio": pf, "watchlist": wl, "agents": ag}).start("daily", AS_OF)
    check = next(c for c in _watch_out(db, rid2)["checks"] if c["symbol"] == "BA")
    assert check["heat"] == "hot"
    assert len(db.all("SELECT id FROM approval_queue WHERE rule_id='S08-R26'")) == 1   # hot -> hot: nothing new


def test_dc16_only_without_position(db):
    pf = {"cash": 100_000, "peak_equity": 100_000, "positions": []}
    wl = _wl(direction="short", bucket="watch", trigger_kind="now", trigger_level=None, invalidate_level=None,
             price_at_add=150.0)                                          # BA ~200: +33% against a short
    ag = HeatAgents()
    Runner(db, PIPELINES, {"prices": Prices(), "portfolio": pf, "watchlist": wl, "agents": ag}).start("daily", AS_OF)
    assert ("DC-16", "BA") in [c[:2] for c in ag.calls]
    item = db.one("SELECT kind, status, payload FROM approval_queue WHERE rule_id='DC-16'", json_cols=("payload",))
    assert item["kind"] == "heat" and item["status"] == "pending" and item["payload"]["heat"] == "frozen"
    db.execute("DELETE FROM approval_queue")
    ag2 = HeatAgents()
    Runner(db, PIPELINES, {"prices": Prices(), "portfolio": PF, "watchlist": wl, "agents": ag2}).start("daily", AS_OF)
    assert "DC-16" not in [c[0] for c in ag2.calls]                       # held: A7's territory, never DC-16


def test_frozen_entry_makes_no_action(db):
    from datetime import UTC, datetime

    from qt.approvals import Queue
    pf = {"cash": 100_000, "peak_equity": 100_000, "positions": []}
    q = Queue(db)
    iid = q.add("x", date(2023, 5, 30), {"kind": "heat", "rule_id": "DC-16", "symbol": "BA", "heat": "frozen",
                                        "preapproved": False, "detail": "frozen"})
    q.decide(iid, "approve", now=datetime(2023, 5, 30, 22, tzinfo=UTC))
    rid = Runner(db, PIPELINES, {"prices": Prices(), "portfolio": pf, "watchlist": _wl()}).start("daily", AS_OF)
    check = next(c for c in _watch_out(db, rid)["checks"] if c["symbol"] == "BA")
    assert check["status"] == "frozen" and check["heat"] == "frozen"
    assert not db.all("SELECT id FROM approval_queue WHERE rule_id='S10-R4'")   # S08-R27: no entry


def test_peer_note_on_same_etf_entry(db):
    sent = []
    pf = {"cash": 100_000, "peak_equity": 100_000, "positions": []}
    wl = _wl(bucket="watch", trigger_kind="now", trigger_level=None)
    wl["entries"].append(dict(wl["entries"][0], symbol="GE", peers=[]))       # same sector ETF (XLI)
    wl["entries"].append(dict(wl["entries"][0], symbol="XOM", etf="XLE"))     # another sector: no note
    rid = Runner(db, PIPELINES, {"prices": NewsPrices(), "portfolio": pf, "watchlist": wl, "agents": HeatAgents(),
                                 "notifier": sent.append}).start("daily", AS_OF)
    out = db.one("SELECT output FROM run_steps WHERE run_id=? AND step='news'", (rid,), ["output"])["output"]
    assert list(out["peer_notes"]) == ["GE"]
    assert "מתחרה: BA FAA halts" in out["peer_notes"]["GE"][0] and out["peer_notes"]["GE"][0].startswith("[C]")
    assert "GE: [C] דיווח/אירוע אצל מתחרה" in sent[0]


# ---------------------------------------------------------------- the heat in force gates the entry (S08-R28, R33, R25)

def _entry_items(db):
    return db.all("SELECT id FROM approval_queue WHERE rule_id='S10-R4'")


@pytest.mark.parametrize("heat,status", [("hot", "opportunity"), ("cold", "recheck"), ("boiling", "waiting")])
def test_only_hot_heat_opens_the_entry(db, heat, status):
    pf = {"cash": 100_000, "peak_equity": 100_000, "positions": []}
    rid = Runner(db, PIPELINES, {"prices": Prices(), "portfolio": pf, "watchlist": _wl(heat=heat)}).start("daily", AS_OF)
    check = next(c for c in _watch_out(db, rid)["checks"] if c["symbol"] == "BA")
    assert check["triggered"] and check["status"] == status and check["heat"] == heat
    assert bool(_entry_items(db)) is (heat == "hot")


def test_approved_cold_closes_the_entry(db):
    """A DC-15 move hot -> cold that Ilay approved stops the trigger from becoming an S10-R4 entry."""
    from datetime import UTC, datetime

    from qt.approvals import Queue
    pf = {"cash": 100_000, "peak_equity": 100_000, "positions": []}
    q = Queue(db)
    iid = q.add("x", date(2023, 5, 30), {"kind": "heat", "rule_id": "S08-R26", "symbol": "BA", "heat": "cold",
                                        "preapproved": False, "detail": "hot -> cold"})
    q.decide(iid, "approve", now=datetime(2023, 5, 30, 22, tzinfo=UTC))
    rid = Runner(db, PIPELINES, {"prices": Prices(), "portfolio": pf, "watchlist": _wl(heat="hot")}).start("daily", AS_OF)
    check = next(c for c in _watch_out(db, rid)["checks"] if c["symbol"] == "BA")
    assert check["heat"] == "cold" and check["status"] == "recheck" and not _entry_items(db)


def test_approved_heat_survives_the_premarket_recheck(db):
    """A heat has no price: a >2% premarket gap must not turn Ilay's approval into 'changed' -> 'expired'."""
    from datetime import UTC, datetime

    from qt.approvals import Queue
    pf = {"cash": 100_000, "peak_equity": 100_000, "positions": []}
    q = Queue(db)
    iid = q.add("x", date(2023, 5, 30), {"kind": "heat", "rule_id": "S08-R26", "symbol": "BA", "heat": "cold",
                                        "preapproved": False, "detail": "hot -> cold"}, ref_price=100.0)
    q.decide(iid, "approve", now=datetime(2023, 5, 30, 22, tzinfo=UTC))
    assert q.recheck({"BA": 103.0}, {"BA"}, now=datetime(2023, 5, 31, 12, tzinfo=UTC)) == []
    q.expire(now=datetime(2023, 6, 9, 15, tzinfo=UTC))
    assert q.get(iid)["status"] == "approved"
    rid = Runner(db, PIPELINES, {"prices": Prices(), "portfolio": pf, "watchlist": _wl(heat="hot")}).start("daily", AS_OF)
    check = next(c for c in _watch_out(db, rid)["checks"] if c["symbol"] == "BA")
    assert check["heat"] == "cold" and not _entry_items(db)


def test_pending_heat_proposal_waits_review_days_and_has_no_price(db):
    from datetime import UTC, datetime

    from qt.approvals import REVIEW_DAYS, Queue
    q = Queue(db)
    iid = q.add("x", date(2023, 5, 30), {"kind": "heat", "rule_id": "S08-R26", "symbol": "BA", "heat": "hot",
                                        "preapproved": False, "detail": "cold -> hot"})
    q.expire(now=datetime(2023, 6, 1, 15, tzinfo=UTC))                  # the next open passed: still waiting
    assert q.get(iid)["status"] == "pending"
    q.expire(now=datetime(2023, 5, 30, 22, tzinfo=UTC) + timedelta(days=REVIEW_DAYS + 3))
    assert q.get(iid)["status"] == "expired"


def test_adverse_catalyst_closes_the_entry(db):
    """S08-R28: hot but an A/B against the idea, not priced in and still active: no entry."""
    pf = {"cash": 100_000, "peak_equity": 100_000, "positions": []}
    row = {"date": "2023-05-25", "class": "A", "direction": "against_idea", "priced_in": False,
           "persistence": "until_next_report", "timing": "occurred", "kind": "results", "what": "תחזית חלשה"}
    rid = Runner(db, PIPELINES, {"prices": Prices(), "portfolio": pf,
                                 "watchlist": _wl(heat="hot", catalysts=[row])}).start("daily", AS_OF)
    check = next(c for c in _watch_out(db, rid)["checks"] if c["symbol"] == "BA")
    assert check["status"] == "waiting" and not _entry_items(db)


def test_day_61_review_runs_once_even_after_a_missed_day(db):
    """S08-R29: more than 60 days, not exactly 61; once reviewed, not again."""
    pf = {"cash": 100_000, "peak_equity": 100_000, "positions": []}
    added = (AS_OF - timedelta(days=64)).isoformat()
    wl = _wl(bucket="watch", trigger_kind="now", trigger_level=None, added=added, price_at_add=195.0, heat="cold")
    ag = HeatAgents(dc15="cold")
    Runner(db, PIPELINES, {"prices": Prices(), "portfolio": pf, "watchlist": wl, "agents": ag}).start("daily", AS_OF)
    assert [c[:2] for c in ag.calls] == [("DC-15", "BA")] and "day_61" in ag.calls[0][2]["reasons"]
    ag.calls.clear()
    rid = Runner(db, PIPELINES, {"prices": Prices(), "portfolio": pf, "watchlist": wl, "agents": ag}).start("daily", AS_OF)
    check = next(c for c in _watch_out(db, rid)["checks"] if c["symbol"] == "BA")
    assert "day_61" not in check["dc15"] and not ag.calls


class RiskAgents:
    """A7's cards: DC-21 hold, DC-22 and DC-23 as given. Records the cards and fact sheets."""
    def __init__(self, dc22="switch", dc23="half"):
        self.calls, self.dc22, self.dc23 = [], dc22, dc23

    def decide(self, agent, card, fs, world, **kw):
        from types import SimpleNamespace as NS
        self.calls.append((card, kw.get("subject"), dict(world), fs.render()))
        opt = {"DC-22": self.dc22, "DC-23": self.dc23}.get(card, "hold")
        out = {"reasoning": "r", "cut_method": "order_084", "cut_order": ["BA"]} if card == "DC-22" else {"reasoning": "r"}
        return NS(option=opt, output=out, status="decided", decision_id=None, critic=None, for_ilay=True, cost_usd=0.0)


def test_dc22_on_the_day_the_vix_jump_starts(db):
    ag = RiskAgents()
    _runner(db, agents=ag).start("daily", AS_OF)
    card, subject, world, facts = next(c for c in ag.calls if c[0] == "DC-22")
    assert subject == "portfolio" and world["state"] == "a" and world["regime"] == "portfolio"
    assert "market.vix_vs_recent_low = 1.4" in facts and "BA.weight_pct" in facts
    item = db.one("SELECT kind, status, payload FROM approval_queue WHERE rule_id='DC-22'", (), ["payload"])
    assert (item["kind"], item["status"]) == ("block_new_risk", "pending") and "order_084" in item["payload"]["detail"]


def test_dc22_stay_queues_nothing(db):
    _runner(db, agents=RiskAgents(dc22="stay")).start("daily", AS_OF)
    assert not db.all("SELECT id FROM approval_queue WHERE rule_id='DC-22'")


def test_dc23_on_profit_flags_and_not_again_for_five_days(db, monkeypatch):
    import qt.timing
    monkeypatch.setattr(qt.timing, "profit_flags", lambda bars, d: {"rsi_turn": 74.2, "extended_days": 25})
    ag = RiskAgents(dc22="stay")
    from qt.journal import Decision, Journal
    _runner(db, agents=ag).start("daily", AS_OF)
    card, subject, world, facts = next(c for c in ag.calls if c[0] == "DC-23")
    assert subject == "BA" and world["flags"] == ["extended_days", "rsi_turn"]
    assert "BA.flag.rsi_turn = 74.2" in facts and "BA.pnl_pct" in facts and "BA.open_r" in facts
    item = db.one("SELECT kind, status, payload FROM approval_queue WHERE rule_id='DC-23'", (), ["payload"])
    assert (item["kind"], item["status"], item["payload"]["qty"]) == ("sell", "pending", 50.0)
    Journal(db).record(Decision(decided_by="agent:A7", subject="BA", choice="half", playbook_version="x", card_id="DC-23"))
    ag.calls.clear()
    _runner(db, agents=ag).start("daily", AS_OF + timedelta(days=1))
    assert "DC-23" not in [c[0] for c in ag.calls]


def test_dc23_not_on_a_losing_position(db, monkeypatch):
    import qt.timing
    monkeypatch.setattr(qt.timing, "profit_flags", lambda bars, d: {"rsi_turn": 74.2})
    pf = {**PF, "positions": [{**PF["positions"][0], "entry": 400.0, "entry_stop": 100.0, "stop": 100.0}]}
    ag = RiskAgents(dc22="stay")
    Runner(db, PIPELINES, {"prices": Prices(), "portfolio": pf, "agents": ag}).start("daily", AS_OF)
    assert "DC-23" not in [c[0] for c in ag.calls]


def test_dc21_gets_the_position_facts(db):
    ag = RiskAgents(dc22="stay", dc23="none")
    pf = {**PF, "positions": [{**PF["positions"][0], "adverse_catalyst": True, "event": "guidance cut",
                               "thesis_breakers": ["deliveries halted again"], "target": 260.0}]}
    Runner(db, PIPELINES, {"prices": Prices(), "portfolio": pf, "agents": ag}).start("daily", AS_OF)
    facts = next(c for c in ag.calls if c[0] == "DC-21")[3]
    for key in ("BA.close", "BA.entry = 150.0", "BA.pnl_pct", "BA.to_stop_pct", "BA.to_target_pct", "BA.open_r",
                "BA.weight_pct", "BA.thesis_breakers", "BA.event = guidance cut", "portfolio.regime", "market.atr_20_60_pct"):
        assert key in facts, key


def test_profit_flags_on_a_steady_rise():
    from qt.timing import profit_flags
    start = date(2022, 1, 3)
    up = [Bar(start + timedelta(days=i), 100 + i, 101 + i, 99.5 + i, 100.8 + i) for i in range(120)]
    f = profit_flags(up)
    assert f["extended_days"] > 21 and "high_intervals_red" not in f
    assert profit_flags(up[:30]) == {}                                   # too little history
    assert "extended_days" not in profit_flags(up, "short")
