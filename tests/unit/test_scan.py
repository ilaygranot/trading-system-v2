"""The nightly scan (qt.scan, Ilay 08.11): every sector, the macro every night, a diff with the last scan, info only."""
from datetime import date, datetime, timezone

import pytest

from qt.approvals import Queue
from qt.pipelines import PIPELINES
from qt.runs import Runner
from qt import scan as S

D1, D2 = date(2026, 11, 5), date(2026, 11, 6)


def _t(sym, etf, dr, light, rr=3.5):
    return dict(etf=etf, sym=sym, dir=dr, profile="growth", combined=light, stock=light, price=10.0, adr=0.3,
                rr_extreme=rr)


def test_classify_keeps_aligned_and_neutral_never_against_the_sector():
    timing = [_t("AAA", "XLI", "long", "green"), _t("BBB", "XLF", "long", "orange"), _t("CCC", "XLP", "long", "green"),
              _t("DDD", "XLP", "short", "orange"), _t("EEE", "XLI", "long", "red"), dict(etf="XLI", sym="ERR", error="x")]
    r = S.classify(timing, longs=["XLI"], shorts=["XLP"], on_list={"AAA"})
    assert [(x["sym"], x["fit"]) for x in r] == [("AAA", "aligned"), ("DDD", "aligned"), ("BBB", "watch_only")]
    assert r[0]["on_list"] and not r[2]["on_list"]                    # CCC: long in a short sector (S04-R13), out


def test_diff_names_new_upgraded_and_macro_changes():
    m1 = dict(zone="hot", bias="bullish", compass="goldilocks", phase="expansion", credit="calm", curve="normal",
              market="bull")
    prev = {"as_of": "2026-11-05", "macro": m1,
            "screen": {"sectors": {"long": ["XLI"], "short": ["XLP"], "neutral": ["XLF"]},
                       "radar": [dict(sym="AAA", dir="long", light="orange"), dict(sym="OLD", dir="long", light="green")]}}
    today = {"sectors": {"long": ["XLI", "XLF"], "short": ["XLP"], "neutral": []},
             "radar": [dict(sym="AAA", dir="long", light="green"), dict(sym="NEW", dir="long", light="orange")]}
    c = S.diff(m1 | {"credit": "rising_stress"}, today, prev)
    assert c["macro"] == ["מרווחי אשראי: calm → rising_stress"] and c["sectors"] == ["XLF: נייטרלי → לונג"]
    assert [r["sym"] for r in c["upgraded"]] == ["AAA"] and [r["sym"] for r in c["new"]] == ["NEW"] and c["dropped"] == 1
    assert S.diff(m1, today, None)["first"]


class Bot:
    def __init__(self):
        self.sent = []

    def send(self, text, buttons=None):
        self.sent.append(text)


def _fake_stages(monkeypatch, credit):
    from qt.research import macro_stage, sector_screen
    stance = dict(zone="hot", bias="bullish", compass_now="goldilocks", activity=dict(pct=72.0), flags=[])
    monkeypatch.setattr(macro_stage, "run", lambda fred, manual, spx, as_of, br: dict(
        stage1=dict(credit=dict(state=credit[as_of]), curve=dict(shape="normal")),
        stage2=dict(stance=stance, scores=[dict(name="ism_pmi", cycle_quadrant="expansion")]),
        stage3=dict(market=dict(regime="bull", drawdown_pct=2.04))))
    monkeypatch.setattr(sector_screen, "run", lambda y, rows, phase, flags, zone, as_of: dict(
        scorecard={"XLI": {}, "XLF": {}, "XLP": {}}, long=["XLI"], short=["XLP"]))
    seen = {}

    def screens(rows, longs, shorts):
        seen["longs"], seen["shorts"] = longs, shorts
        return {e: dict(long=[], short=[]) for e in longs + shorts}
    monkeypatch.setattr(sector_screen, "screens", screens)
    return seen


class Y:
    def bars(self, sym, start, end):
        return []


def test_scan_run_end_to_end_info_only(db, monkeypatch, tmp_path):
    monkeypatch.setenv("QT_WORK_DIR", str(tmp_path))
    seen = _fake_stages(monkeypatch, {D1: "calm", D2: "rising_stress"})
    radar = {D1: [_t("AAA", "XLI", "long", "orange")],
             D2: [_t("AAA", "XLI", "long", "green"), _t("BBB", "XLF", "long", "green")]}
    Queue(db).upload("manual_macro", "m.yaml", "ism_pmi: {}")
    bot = Bot()
    svc = dict(fred=object(), telegram=bot, research_yahoo=Y(), watchlist={"entries": [{"symbol": "AAA"}]},
               scan_universe={"quotes": {"AAA": dict(price=10, ma200=9)}, "summaries": {}},
               scan_timing=lambda y, screens, as_of: radar[as_of])
    r = Runner(db, PIPELINES, svc)
    r.start("scan", D1)
    assert seen == {"longs": ["XLI", "XLF"], "shorts": ["XLP", "XLF"]}   # every sector; neutral on both sides
    assert "סריקה ראשונה" in bot.sent[0] and "סקטורים: לונג XLI · שורט XLP" in bot.sent[0]
    r.start("scan", D2)
    msg = bot.sent[1]
    assert msg.startswith("⚠️ שינוי בתמונת המקרו: מרווחי אשראי: calm → rising_stress")
    assert "עלו לירוק: AAA (XLI לונג, ירוק, יחס לשיא 3.5, כבר ברשימה)" in msg
    assert "BBB (XLF לונג, ירוק, יחס לשיא 3.5, מעקב בלבד)" in msg
    assert db.one("SELECT COUNT(*) AS n FROM approval_queue")["n"] == 0                 # information only
    assert S.latest(db)["as_of"] == D2.isoformat()


def test_scan_without_fred_fails_with_what_is_missing(db, monkeypatch, tmp_path):
    monkeypatch.setenv("QT_WORK_DIR", str(tmp_path))
    r = Runner(db, PIPELINES, dict(research_yahoo=Y(), scan_universe={"quotes": {"A": dict(price=1, ma200=2)}, "summaries": {}}))
    with pytest.raises(S.ScanDataMissing, match="FRED_API_KEY"):
        r.start("scan", D1)


def test_scan_is_due_at_1730_new_york_on_trading_days():
    from qt.schedule import due
    assert due("scan", datetime(2026, 11, 6, 22, 35, tzinfo=timezone.utc)) == (True, D2)     # EST: 17:35
    assert not due("scan", datetime(2026, 11, 6, 21, 35, tzinfo=timezone.utc))[0]
    assert not due("scan", datetime(2026, 11, 7, 22, 35, tzinfo=timezone.utc))[0]          # Saturday
