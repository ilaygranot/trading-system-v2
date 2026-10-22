"""The monthly preparation (qt.monthly, S13-R25): candles, growth expectations, ISM tables, calendar, seasonality,
exposure against the caps, A1 on the stance, and the full monthly run."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace as NS

from qt import monthly as MO
from qt.approvals import Queue
from qt.db import dumps
from qt.calc.volatility import Bar
from qt.pipelines import PIPELINES
from qt.runs import Runner

AS_OF = date(2026, 10, 9)


def _series(growth, end=AS_OF, years=16):
    """Weekday bars with a constant daily growth (close = open, a 1% range); April always falls 3%."""
    out, d, px = [], end - timedelta(days=365 * years), 100.0
    while d <= end:
        if d.weekday() < 5:
            px *= 1 + growth - (0.0015 if d.month == 4 else 0)
            out.append(Bar(d, px, px * 1.005, px * 0.995, px, 1))
        d += timedelta(days=1)
    return out


class Prices:
    def daily_bars(self, symbol, start, end):
        g = {"XLK": 0.001, "XLU": -0.0005}.get(symbol, 0.0003)
        return [b for b in _series(g, end=end) if b.day >= start], "fake"


class Fred:
    def observations(self, sid, start, end, known_on=None):
        months = [date(2014 + i // 12, i % 12 + 1, 28) for i in range(150)]
        return [(m, 100 + i * (0.3 if sid != "FEDFUNDS" else -0.01)) for i, m in enumerate(months)], f"fake:{sid}"

    def release_dates(self, rid, start, end):
        return ([date(2026, 10, 15)] if rid == 10 else [date(2026, 11, 6)] if rid == 50 else []), "fake"


class Bot:
    chat_id = "42"

    def __init__(self):
        self.sent = []

    def send(self, text, buttons=None, force_reply=False):
        self.sent.append(text)
        return len(self.sent)


class Agents:
    def __init__(self):
        self.cards = []

    def decide(self, agent, card, fs, world, **kw):
        self.cards.append((agent, card, kw.get("step")))
        assert "macro.stance.zone" in fs.render()
        opt = {"DC-01": "hot", "DC-02": "growth+_inflation+", "DC-03": "aligned", "DC-04": "medium"}[card]
        return NS(option=opt, output={"option": opt, "reasoning": "r"}, status="decided", cost_usd=0.01)


# ------------------------------------------------------------------ pieces

def test_candle_shapes_and_breaks():
    prev = ["2026-08", 100, 110, 95, 105]
    assert MO.candle(prev, ["2026-09", 105, 120, 104, 119])["shape"] == "trend_up"
    c = MO.candle(prev, ["2026-09", 100, 101, 90, 93])
    assert c["shape"] == "trend_down" and c["breaks"] == "low" and c["closed_below_prev_low"]
    assert MO.candle(prev, ["2026-09", 100, 101, 90, 100.5])["shape"] == "hammer"
    assert MO.candle(prev, ["2026-09", 100, 110, 99.5, 100.5])["shape"] == "inverted_hammer"
    assert MO.candle(prev, ["2026-09", 100, 104, 96, 100.2])["shape"] == "doji"
    assert MO.candle(prev, ["2026-09", 100, 104, 96, 100.2])["relation"] == "inside"


def test_to_monthly_and_seasonality():
    m = MO.to_monthly(_series(0.0003, years=5))
    assert m[-1][0] == "2026-10" and all(len(r) == 5 for r in m)
    apr = MO.seasonality(m, 4)
    assert apr["avg_pct"] < 0 and apr["up_pct"] == 0 and apr["years"] >= 4
    assert MO.seasonality(m, 5)["up_pct"] == 100


def test_growth_change_and_ism_tables():
    prev = {"XLK": dict(sg1=8, eg1=10, eg2=9, n=60), "XLE": dict(sg1=2, eg1=3, eg2=2, n=20)}
    now = {"XLK": dict(sg1=9, eg1=12, eg2=9, n=60, bucket="long"), "XLE": dict(sg1=1, eg1=2, eg2=2, n=20)}
    ch = MO.growth_change(prev, now)
    assert ch["XLK"]["change"] == "improving" and ch["XLE"]["change"] == "weakening"
    raw = {"ism_growing": {"2026-07": ["Machinery", "Chemicals"], "2026-08": ["Machinery"],
                           "2026-09": ["Machinery", "Paper"], "2026-10": ["Future"]},
           "ism_short_supply": {"2026-09": {"Copper": 4, "Steel": 1}}}
    t = MO.ism_tables(raw, AS_OF)
    assert {m["name"]: m["months_in_row"] for m in t["growing"]} == {"Machinery": 3, "Paper": 1}
    assert [m["name"] for m in t["growing"] if m["trend"]] == ["Machinery"] and t["short_supply"][0]["name"] == "Copper"
    assert MO.ism_tables({}, AS_OF) == {"available": False}


def test_exposure_against_caps():
    pf = {"cash": 50_000, "caps": {"long_max": 0.8, "net_max": 0.4},
          "positions": [{"symbol": "A", "direction": "long", "qty": 100, "entry": 400.0},
                        {"symbol": "B", "direction": "short", "qty": 100, "entry": 100.0}]}
    ex = MO.exposure_now(pf, {"A": 500.0})
    assert ex["equity"] == 90_000 and ex["long"] == round(50_000 / 90_000, 3) and ex["over"] == ["net"]
    assert MO.exposure_now({"cash": 0, "positions": []}, {}) == {"equity": 0}


def test_daily_flags_a_cap_breach(db):
    from test_runs_daily import AS_OF as D, PF, Prices as DP
    Runner(db, PIPELINES, {"prices": DP(), "portfolio": {**PF, "caps": {"long_max": 0.1}}}).start("daily", D)
    flags = [i for i in Queue(db).for_day(D) if i["rule_id"] == "D-S11-7"]
    assert len(flags) == 1 and flags[0]["payload"]["detail"].startswith("long exposure")


def test_caps_default_to_policy():
    """Ilay's caps (22.10) apply when the portfolio file has none, e.g. the all-cash start book."""
    from qt.policy import load_policy
    assert dict(load_policy().exposure.caps) == {"long_max": 1.0, "short_max": 0.3, "net_max": 0.9}
    pf = {"cash": -2_000, "positions": [{"symbol": "A", "direction": "long", "qty": 100, "entry": 100.0}]}
    ex = MO.exposure_now(pf, {"A": 100.0})
    assert ex["caps"]["long_max"] == 1.0 and ex["over"] == ["long", "net"]


# ------------------------------------------------------------------ the run

def _manual():
    months = [f"{2025 + (i + 8) // 12}-{(i + 8) % 12 + 1:02d}" for i in range(13)]            # 2025-09 .. 2026-09
    pmi = "ism_pmi: {" + ", ".join(f"'{m}': {48 + i * 0.3:.1f}" for i, m in enumerate(months)) + "}\n"
    return pmi + "ism_growing: {'2026-08': [Machinery], '2026-09': [Machinery, Paper]}\n"


def _weekly_research(db, as_of, growth):
    rid = f"weekly-{as_of}"
    now = datetime.now(timezone.utc).isoformat()
    db.insert("runs", dict(id=rid, kind="weekly", as_of=as_of, status="done", started_at=now), returning=None)
    db.insert("run_steps", dict(run_id=rid, step="research", subject="", seq=7, status="done",
                                output=dumps({"sector_growth": growth})), returning=None)


def test_monthly_run_end_to_end(db):
    bot, ag = Bot(), Agents()
    Runner(db, PIPELINES, {})                                                  # the run tables
    Queue(db).upload("manual_macro", "m.yaml", _manual())
    _weekly_research(db, "2026-09-05", {"XLK": dict(sg1=8, eg1=10, eg2=9, n=60)})
    _weekly_research(db, "2026-10-03", {"XLK": dict(sg1=9, eg1=12, eg2=9, n=60, bucket="long")})
    wl = {"entries": [dict(symbol="ABC", direction="long", etf="XLK", bucket="conditional", added="2026-10-01",
                           trigger_kind="close_above", trigger_level=10.0, k=4.0, target=20.0, target_source="t",
                           next_earnings="2026-10-29")]}
    pf = {"cash": 100_000, "positions": []}
    Runner(db, PIPELINES, {"telegram": bot, "fred": Fred(), "prices": Prices(), "agents": ag, "watchlist": wl,
                           "portfolio": pf}).start("monthly", AS_OF)
    assert [c[1] for c in ag.cards] == ["DC-01", "DC-02", "DC-03", "DC-04"] and ag.cards[0][2] == "a1"
    item = Queue(db).for_day(AS_OF)[0]
    assert item["rule_id"] == "S02-STANCE" and "A1: DC-01 hot" in item["payload"]["detail"]
    prep = bot.sent[-2]
    for s in ("הכנה חודשית", "A1 על המקרו: DC-01 hot", "XLK משתפרות", "נרות החודש", "XLK", "ISM: 2 תעשיות צומחות",
              "עונתיות החודש", "10-15 CPI", "10-28 החלטת ריבית (FOMC)", "10-29 דוח ABC", "11-06 תעסוקה",
              "נטו 0.00/0.9", "NAAIM"):
        assert s in prep, s
    assert len(prep) < 4096 and len(bot.sent[-2]) < 4096


# ------------------------------------------------------------------ sentiment

from qt import sentiment as SE  # noqa: E402

HEADER = ("Market_and_Exchange_Names,As_of_Date_In_Form_YYMMDD,Report_Date_as_YYYY-MM-DD,CFTC_Contract_Market_Code,"
          "Open_Interest_All,Dealer_Positions_Long_All,Asset_Mgr_Positions_Long_All,Asset_Mgr_Positions_Short_All,"
          "NonRept_Positions_Long_All,NonRept_Positions_Short_All")


def tff_text(n=60, am_end=0.55, start=date(2025, 10, 7)):
    """n weekly S&P 500 rows with the asset managers' share rising from 0.3 to am_end, plus a market to ignore."""
    lines = [HEADER]
    for i in range(n):
        d = start + timedelta(days=7 * i)
        am = 0.3 + (am_end - 0.3) * i / (n - 1)
        lines.append(f'"E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE",{d:%y%m%d},{d},13874A,1000000,1,'
                     f"{am * 1e6:.0f},100000,{0.1 * 1e6:.0f},50000")
        lines.append(f'"NASDAQ MINI - CHICAGO MERCANTILE EXCHANGE",{d:%y%m%d},{d},209742,5,1,4,1,1,1')
    return "\n".join(lines) + "\n"


def test_cot_reading_flags_the_extreme_and_the_direction():
    rows = SE.parse_tff(tff_text())
    assert len(rows) == 60 and rows[0]["market"].startswith("E-MINI S&P 500")
    r = SE.cot_reading(rows)
    assert r["am_extreme"] == "high" and r["am_direction"] == "up" and r["spread_direction"] == "up"
    assert r["spread_vs_long_ma"] == "above" and r["am_pct_oi"] > 0.5
    assert SE.parse_tff("") == [] and SE.cot_reading(rows[:3]) is None


def test_naaim_reading_uses_the_history_for_the_extreme():
    weeks = {(date(2025, 1, 1) + timedelta(days=7 * i)).isoformat(): 60 + (i % 10) for i in range(60)}
    weeks["2026-02-18"] = weeks["2026-02-25"] = weeks["2026-03-04"] = weeks["2026-03-11"] = 100
    r = SE.naaim_reading(weeks, date(2026, 3, 12))
    assert r["extreme"] == "high" and r["band"] and r["vs_mean"] == "above" and r["smoothed"] == 100.0
    short = SE.naaim_reading(dict(list(weeks.items())[:8]), date(2026, 3, 12))
    assert short["band"] is None and short["extreme"] is None
    assert SE.naaim_reading(None, date(2026, 3, 12)) is None


def test_monthly_sentiment_step_with_cot_and_naaim(db):
    class Cftc:
        def tff_year(self, y):
            if y == AS_OF.year - 1:
                raise ConnectionError("down")
            return tff_text(n=40, start=date(2026, 1, 6)), "fake"
    bot = Bot()
    naaim = "naaim: {" + ", ".join(f"'2026-09-{d:02d}': {70 + d}" for d in (2, 9, 16, 23, 30)) + "}\n"
    Queue(db).upload("manual_macro", "m.yaml", _manual() + naaim)
    Runner(db, PIPELINES, {"telegram": bot, "fred": Fred(), "prices": Prices(), "cftc": Cftc(),
                           "portfolio": {"cash": 100_000, "positions": []}}).start("monthly", AS_OF)
    prep = bot.sent[-2]
    assert "COT (" in prep and "NAAIM (2026-09-30)" in prep and "סנטימנט חסר" not in prep
