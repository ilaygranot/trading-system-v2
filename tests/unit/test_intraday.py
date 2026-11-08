"""The intraday watch (qt.intraday, Ilay 08.11): fresh prices, filings and headlines three times a session; alerts
once a day; A7 on a shock or a strong event against a position; nothing changes a stop or sends an order."""
from datetime import datetime, timezone

from test_runs_daily import AS_OF, PF, FakeAgents, NewsPrices, Prices, _wl

from qt.pipelines import PIPELINES
from qt.intraday import position_alerts, watch_alerts
from qt.runs import Runner
from qt.watch import load as load_watch

NOW = datetime(2023, 5, 31, 16, 30, tzinfo=timezone.utc)


def test_position_alerts_stop_shock_target():
    p = dict(symbol="BA", direction="long", stop=140.0, target=230.0)
    kinds = lambda q: [a["kind"] for a in position_alerts(p, q)]               # noqa: E731
    assert kinds(dict(price=138.0, prev_close=150.0, low=137.0, adr=2.0)) == ["stop_breach", "shock"]
    assert kinds(dict(price=141.0, prev_close=142.0, low=140.5, adr=2.0)) == ["near_stop"]
    assert kinds(dict(price=231.0, prev_close=229.0, low=228.0, adr=2.0)) == ["target"]
    assert kinds(dict(price=180.0, prev_close=181.0, low=179.0, adr=2.0)) == []
    short = dict(symbol="X", direction="short", stop=60.0)
    assert [a["kind"] for a in position_alerts(short, dict(price=59.0, prev_close=55.0, high=60.5, adr=2.0))] == \
        ["stop_breach", "shock"]


def test_watch_alert_only_on_the_crossing():
    e = load_watch(_wl())[0]                                                    # close_above 190
    assert [a["kind"] for a in watch_alerts(e, dict(price=191.0, prev_close=189.0))] == ["trigger"]
    assert watch_alerts(e, dict(price=192.0, prev_close=191.0)) == []           # crossed before today


class Sec:
    def ok(self):
        return True

    def current_reports(self, sym, as_of, days=3, max_n=3):
        if sym != "BA":
            return []
        return [dict(form="8-K", date="2023-05-31", items=["2.02", "9.01"],
                     item_names=["Results of Operations and Financial Condition", "Financial Statements and Exhibits"],
                     kind="results", index_url="https://sec/ba-1", exhibits=[dict(title="BA Q1 results", lead="x")]),
                dict(form="8-K", date="2023-05-31", items=["5.07"], item_names=["vote"], kind="governance",
                     index_url="https://sec/ba-2", exhibits=[])]


def _live(px, prev=200.0):
    return lambda s: dict(price=px, prev_close=prev, low=px - 1, high=prev, source="test:live")


def test_intraday_run_alerts_once_and_asks_a7_once(db):
    sent, ag = [], FakeAgents()
    base = {"portfolio": PF, "agents": ag, "notifier": sent.append, "now": lambda: NOW}
    Runner(db, PIPELINES, {**base, "prices": Prices()}).start("daily", AS_OF)
    sent.clear()
    ag.calls.clear()
    svc = {**base, "prices": NewsPrices(), "live": _live(138.0), "sec": Sec()}
    rid = Runner(db, PIPELINES, svc).start("intraday", AS_OF)
    asked = [c[:2] for c in ag.calls]
    assert ("DC-14", "BA") in asked and asked.count(("DC-21", "BA")) == 1
    titles = [c[2] for c in ag.calls if c[0] == "DC-14"]
    assert len(titles) == 2                                         # the headline and the results 8-K, not the vote
    msg = sent[-1]
    assert msg.startswith("⚡ ניטור תוך-יומי 12:30 ניו יורק")
    assert "מעבר לסטופ במהלך היום · BA (בתיק)" in msg and "תנועה חדה נגד הפוזיציה" in msg
    assert "8-K: Results of Operations and Financial Condition · BA Q1 results" in msg
    out = db.one("SELECT output FROM run_steps WHERE run_id=? AND step='agents'", (rid,), ["output"])["output"]
    assert "תנועה חדה" in out["decisions"]["BA"]["event"] and "8-K" in out["decisions"]["BA"]["event"] and "FAA halts" in out["decisions"]["BA"]["event"]
    n = len(sent)
    ag.calls.clear()
    Runner(db, PIPELINES, svc).start("intraday", AS_OF)             # the next look, same prices and news
    assert len(sent) == n                                           # nothing new: no message
    assert not [c for c in ag.calls if c[0] in ("DC-14", "DC-21")]


def test_evening_run_keeps_the_days_headlines_without_a_second_event(db):
    sent, ag = [], FakeAgents()
    base = {"portfolio": PF, "agents": ag, "notifier": sent.append, "now": lambda: NOW, "prices": NewsPrices()}
    Runner(db, PIPELINES, {**base, "live": _live(199.0)}).start("intraday", AS_OF)
    assert ("DC-21", "BA") not in [c[:2] for c in ag.calls]          # no daily run yet: A7 has no background
    ag.calls.clear()
    sent.clear()
    Runner(db, PIPELINES, base).start("daily", AS_OF)
    assert not [c for c in ag.calls if c[0] in ("DC-14", "DC-21")]   # classified at noon; the watch already saw it
    assert "FAA halts 737 deliveries" in "\n".join(sent)             # still in the evening digest


def test_quiet_session_sends_nothing(db):
    sent = []
    Runner(db, PIPELINES, {"portfolio": PF, "prices": Prices(), "live": _live(199.0), "notifier": sent.append,
                           "now": lambda: NOW}).start("intraday", AS_OF)
    assert sent == []


def test_intraday_is_due_three_times_a_session():
    from qt.schedule import due, next_run
    ok = [h for h in range(13, 21) if due("intraday", datetime(2026, 11, 6, h, 35, tzinfo=timezone.utc))[0]]
    assert ok == [15, 17, 19]                                        # EST: 10:35, 12:35, 14:35
    assert next_run("intraday", datetime(2026, 11, 6, 16, 0, tzinfo=timezone.utc)).strftime("%H:%M") == "12:30"
