"""The RGLD post-mortem of 10.11.2026: the day its trigger fired the price providers did not answer, the summary read
"a normal run", the proposal went out a day late, and the next one expired unanswered over a weekend. These tests hold
the fixes: a third price source, bars that must reach the session, a second pass, a loud summary, a morning catch-up
run, an ADR-sized premarket threshold, a reminder before the open, and a stop that follows a gapped fill."""
from datetime import date, datetime, timedelta, timezone

import pytest

from test_runs_daily import AS_OF, Prices, _wl

from qt import app as A
from qt import book as B
from qt.approvals import Queue
from qt.calc.volatility import Bar
from qt.data import providers as P
from qt.pipelines import PIPELINES
from qt.runs import Runner
from qt.watch import _invalidation

UTC = timezone.utc
FLAT = {"cash": 100_000, "peak_equity": 100_000, "positions": []}


class Old(Prices):
    """A provider that answers, but whose last bar is the session before (the evening's bar is not in yet)."""
    def daily_bars(self, symbol, start, end):
        bars, src = super().daily_bars(symbol, start, end)
        return ([b for b in bars if b.day < end] if symbol == "BA" else bars), "old"


class Bot:
    def __init__(self):
        self.sent = []

    def send(self, text, buttons=None, force_reply=False):
        self.sent.append((len(self.sent) + 1, text, buttons))
        return len(self.sent)


# ------------------------------------------------------------------ providers

def test_chain_takes_the_first_provider_that_reaches_the_day():
    day = date(2026, 11, 5)
    bar = lambda d: Bar(d, 1, 1, 1, 1)                                         # noqa: E731

    class Down:
        def daily_bars(self, s, start, end):
            raise P.ProviderError("HTTP 429")

    class Stale:
        def daily_bars(self, s, start, end):
            return [bar(day - timedelta(days=1))], "stale"

    class Good:
        def daily_bars(self, s, start, end):
            return [bar(day - timedelta(days=1)), bar(day)], "good"

    assert P.Chain(Down(), Stale(), Good()).daily_bars("RGLD", day - timedelta(days=5), day)[1] == "good"
    assert P.Chain(Down(), Stale()).daily_bars("RGLD", day - timedelta(days=5), day)[1] == "stale"   # the caller judges
    with pytest.raises(P.ProviderError):
        P.Chain(Down(), None).daily_bars("RGLD", day - timedelta(days=5), day)


def test_nasdaq_daily_bars_are_parsed_oldest_first(monkeypatch):
    rows = [{"date": "11/05/2026", "close": "$261.28", "volume": "1,076,200", "open": "$257.48", "high": "$264.50", "low": "$248.24"},
            {"date": "11/04/2026", "close": "$246.08", "volume": "750,500", "open": "$243.00", "high": "$247.89", "low": "$239.92"},
            {"date": "11/03/2026", "close": "N/A", "volume": "N/A", "open": "N/A", "high": "N/A", "low": "N/A"}]
    monkeypatch.setattr(P, "get_json", lambda url, *a, **k: {"data": {"tradesTable": {"rows": rows}}})
    bars, src = P.Nasdaq().daily_bars("RGLD", date(2026, 10, 1), date(2026, 11, 5))
    assert [b.day.isoformat() for b in bars] == ["2026-11-04", "2026-11-05"] and bars[-1].close == 261.28
    assert bars[-1].open == 257.48 and src.startswith("nasdaq")
    with pytest.raises(P.ProviderError):
        P.Nasdaq().daily_bars("^GSPC", date(2026, 10, 1), date(2026, 11, 5))


def test_a_rate_limited_request_waits_for_retry_after(monkeypatch):
    from qt.data import http as H
    from types import SimpleNamespace as NS
    assert H._wait(NS(headers={"Retry-After": "20"}), 0) == 20
    assert H._wait(NS(headers={"Retry-After": "600"}), 0) == H.MAX_RETRY_AFTER_S
    assert H._wait(NS(headers={}), 1) == 4 and H._wait(NS(headers={"Retry-After": "soon"}), 0) == 2


# ------------------------------------------------------------------ the daily fetch

def _fetch(db, rid):
    return db.one("SELECT output FROM run_steps WHERE run_id=? AND step='fetch'", (rid,), ["output"])["output"]


def test_bars_that_stop_before_the_session_are_not_today(db):
    """Yesterday's close answered as today's would test the trigger and the stop on the wrong day."""
    rid = Runner(db, PIPELINES, {"prices": Old(), "prices_fallback": Prices(), "portfolio": FLAT,
                                 "watchlist": _wl()}).start("daily", AS_OF)
    f = _fetch(db, rid)
    assert f["stale"] == [] and f["sources"]["BA"] == "fixture"                # the fallback had the session's bar
    db.execute("DELETE FROM approval_queue")
    rid = Runner(db, PIPELINES, {"prices": Old(), "portfolio": FLAT, "watchlist": _wl()}).start("daily", AS_OF)
    f = _fetch(db, rid)
    assert f["stale"] == ["BA"] and f["retried"] == ["BA"]
    assert f["bars"]["BA"][-1][0] < AS_OF.isoformat()                          # kept for the summary only
    assert not db.all("SELECT id FROM approval_queue WHERE rule_id='S10-R4'")  # and never an entry on old bars


def test_a_second_pass_finds_a_provider_that_came_back(db):
    class Flaky(Prices):
        def daily_bars(self, symbol, start, end):
            if symbol == "BA" and "BA" not in self.calls:
                self.calls.append("BA")
                raise ConnectionError("HTTP 429")
            return super().daily_bars(symbol, start, end)

    waited = []
    rid = Runner(db, PIPELINES, {"prices": Flaky(), "portfolio": FLAT, "watchlist": _wl(),
                                 "sleep": waited.append}).start("daily", AS_OF)
    f = _fetch(db, rid)
    assert f["stale"] == [] and f["retried"] == ["BA"] and len(waited) == 1
    assert db.one("SELECT id FROM approval_queue WHERE rule_id='S10-R4' AND symbol='BA'")


# ------------------------------------------------------------------ the summary

def test_a_blind_day_is_said_at_the_top_and_near_the_trigger(db):
    sent = []
    pf = {"cash": 100_000, "peak_equity": 100_000, "positions": [
        {"symbol": "BA", "direction": "long", "sector": "XLI", "qty": 10, "entry": 150.0, "entry_stop": 140.0, "stop": 140.0}]}
    Runner(db, PIPELINES, {"prices": Old(), "portfolio": pf, "notifier": sent.append,
                           "watchlist": _wl(symbol="BA", trigger_level=210.0)}).start("daily", AS_OF)
    text = sent[0]
    assert "ריצה תקינה" not in text and "חוסר בנתונים" in text
    assert "בתיק בלי נתוני היום: BA" in text and "הסטופ לא נבדק" in text
    assert "הריצה של לפני הפתיחה תנסה להשלים" in text
    sent.clear()
    Runner(db, PIPELINES, {"prices": Old(), "portfolio": FLAT, "notifier": sent.append,
                           "watchlist": _wl(trigger_level=210.0)}).start("daily", AS_OF + timedelta(days=0))
    assert "BA בלי נתונים, והייתה" in sent[0] and "ייתכן שהטריגר הופעל היום" in sent[0]
    sent.clear()
    Runner(db, PIPELINES, {"prices": Prices(), "portfolio": FLAT, "notifier": sent.append,
                           "watchlist": _wl()}).start("daily", AS_OF)
    assert "ריצה תקינה" in sent[0]


# ------------------------------------------------------------------ the morning catch-up

def test_premarket_runs_a_blind_day_again_once(db):
    """The evening run had no bar for BA; the next morning the provider answers, the day is run again before the
    premarket run and the entry is queued, valid until that morning's open."""
    A._schema(db)
    Queue(db).upload("portfolio", "pf.yaml", "cash: 100000\npeak_equity: 100000\npositions: []\n")
    Runner(db, PIPELINES, {"prices": Old(), "portfolio": FLAT, "watchlist": _wl()}).start("daily", AS_OF)
    assert not db.all("SELECT id FROM approval_queue WHERE rule_id='S10-R4'")
    sent = []
    svc = {"prices": Prices(), "portfolio": FLAT, "watchlist": _wl(), "notifier": sent.append,
           "now": lambda: datetime(2023, 6, 1, 12, 35, tzinfo=UTC)}
    rid = A.catch_up(db, svc, date(2023, 6, 1))
    assert rid and rid.startswith("daily-2023-05-31")
    item = db.one("SELECT status, valid_until FROM approval_queue WHERE rule_id='S10-R4' AND symbol='BA'")
    assert item["status"] == "pending" and item["valid_until"].startswith("2023-06-01")
    assert sent[0].startswith("🔁 השלמת נתונים") and "catch_up" not in svc
    assert A.catch_up(db, svc, date(2023, 6, 1)) is None                       # once


def test_no_catch_up_after_a_day_that_saw_everything(db):
    Runner(db, PIPELINES, {"prices": Prices(), "portfolio": FLAT, "watchlist": _wl()}).start("daily", AS_OF)
    assert A.catch_up(db, {"prices": Prices(), "portfolio": FLAT}, date(2023, 6, 1)) is None
    assert A.catch_up(DB_EMPTY(), {}, date(2023, 6, 1)) is None                # a new deployment has nothing to redo


def DB_EMPTY():
    from qt.db import DB
    from qt.runs import SCHEMA
    d = DB(":memory:")
    d.ddl(SCHEMA)
    return d


def test_a_second_run_of_the_day_journals_nothing_twice(db):
    svc = {"prices": Prices(), "portfolio": FLAT, "watchlist": _wl()}
    Runner(db, PIPELINES, svc).start("daily", AS_OF)
    n = db.one("SELECT COUNT(*) AS n FROM decisions WHERE subject='BA' AND action='enter'")["n"]
    Runner(db, PIPELINES, svc).start("daily", AS_OF)
    assert db.one("SELECT COUNT(*) AS n FROM decisions WHERE subject='BA' AND action='enter'")["n"] == n == 1


# ------------------------------------------------------------------ the approval window

def _enter(**kw):
    a = dict(kind="enter", rule_id="S10-R4", symbol="RGLD", qty=2, price=265.81, stop=236.73, target=389.55,
             preapproved=False, detail="x", direction="long", sector="XLB", k=4.0)
    a.update(kw)
    return a


def test_a_gap_inside_one_adr_does_not_send_the_entry_back(db):
    """RGLD 09.11.2026: +2.6% before the open on a stock whose ADR is 2.7%."""
    q = Queue(db)
    iid = q.add("r1", date(2026, 11, 6), _enter(), 265.81)
    assert q.recheck({"RGLD": 272.61}, now=datetime(2026, 11, 9, 13, 35, tzinfo=UTC)) == []
    assert q.get(iid)["status"] == "pending"
    assert q.recheck({"RGLD": 274.0}, now=datetime(2026, 11, 9, 13, 35, tzinfo=UTC)) == [iid]   # +3.1%: more than a day
    other = q.add("r1", date(2026, 11, 6), dict(kind="exit", rule_id="S12-R2", symbol="XOM", qty=1, price=100.0,
                                                preapproved=False, detail="x"), 100.0)
    assert q.recheck({"XOM": 102.5}, now=datetime(2026, 11, 9, 13, 35, tzinfo=UTC)) == [other]  # no stop: the flat 2%


def test_premarket_reminds_of_orders_that_expire_at_the_open(db):
    q = Queue(db)
    iid = q.add("r1", date(2026, 11, 6), _enter(), 265.81)                     # Friday night, valid to Monday's open
    bot = Bot()
    svc = {"quote": lambda s: (266.0, "fake"), "telegram": bot, "portfolio": {},
           "now": lambda: datetime(2026, 11, 9, 13, 35, tzinfo=UTC)}
    Runner(db, PIPELINES, svc).start("premarket", date(2026, 11, 9))
    text = bot.sent[0][1]
    assert "ממתינים לתשובה שלך עד הפתיחה (16:30 שעון ישראל)" in text and f"RGLD #{iid}" in text
    assert any(b and b[0][0]["callback_data"] == f"a:{iid}" for _, _, b in bot.sent)   # the buttons again


def test_a_gapped_fill_keeps_the_stop_width_from_the_fill():
    pf, what = B.apply_fill(dict(FLAT), "enter", "RGLD", 2, 282.0, _enter())
    pos = pf["positions"][0]
    assert pos["stop"] == round(282.0 * 236.73 / 265.81, 2) and "stop moved" in what     # 10.9% under the fill
    pf, what = B.apply_fill(dict(FLAT), "enter", "RGLD", 2, 267.0, _enter())              # inside one ADR: as approved
    assert pf["positions"][0]["stop"] == 236.73 and "stop moved" not in what
    pf, _ = B.apply_fill(dict(FLAT), "enter", "RGLD", 2, 250.0, _enter())                 # a cheaper fill: as approved
    assert pf["positions"][0]["stop"] == 236.73


# ------------------------------------------------------------------ the invalidation level

def test_the_invalidation_level_is_at_least_one_adr_away():
    assert _invalidation(235.98, 238.78, 213.9, 4.0, "long") == round(238.78 - (238.78 - 213.9) / 4, 2)   # RGLD: 232.56
    assert _invalidation(220.0, 238.78, 213.9, 4.0, "long") == 220.0           # a support further away stays
    assert _invalidation(101.0, 100.0, 108.0, 4.0, "short") == 102.0
    assert _invalidation(None, 100.0, 92.0, 4.0, "long") is None
    assert _invalidation(95.0, None, None, None, "long") == 95.0


# ------------------------------------------------------------------ intraday

def test_an_intraday_look_without_a_price_says_so_once(db):
    now = datetime(2023, 5, 31, 16, 30, tzinfo=UTC)

    def live(s):
        raise ConnectionError("HTTP 429")

    sent = []
    svc = {"prices": Prices(), "portfolio": FLAT, "watchlist": _wl(), "live": live, "notifier": sent.append,
           "now": lambda: now}
    Runner(db, PIPELINES, svc).start("intraday", AS_OF)
    assert "אין נתונים" in sent[0] and "BA" in sent[0] and "הטריגר לא נבדק" in sent[0]
    sent.clear()
    Runner(db, PIPELINES, svc).start("intraday", AS_OF)
    assert sent == []                                                          # once a day
