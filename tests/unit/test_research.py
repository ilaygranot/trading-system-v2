import json
from datetime import date, timedelta
from pathlib import Path

from qt.calc.volatility import Bar
from qt.research import idea, report, run

FIX = Path(__file__).parent.parent / "fixtures" / "research_dossier.json"


def _bars(prices: dict[str, float]) -> list[Bar]:
    """Daily bars from 2022-01-03 with a flat close per year (the value at each year-end matters)."""
    out, d = [], date(2022, 1, 3)
    while d <= date(2025, 12, 31):
        p = prices[str(d.year)]
        out.append(Bar(d, p, p * 1.01, p * 0.99, p, 1))
        d += timedelta(days=1)
    return out


def _fund(eps: list[float]) -> dict:
    return {"annualDilutedEPS": [(f"{y}-12-31", e) for y, e in zip((2023, 2024, 2025), eps)],
            "annualTotalRevenue": [], "annualDilutedAverageShares": []}


def test_own_multiple_target_from_three_years():
    bars = _bars({"2022": 10, "2023": 100, "2024": 110, "2025": 120})
    v = idea.valuation(bars, _fund([10, 10, 10]), {"f0": 10, "f1": 11, "f2": 12}, {"pe1": 15})
    assert [round(m) for m in v["hist_multiples"]] == [10, 11, 12]
    assert v["target"]["multiple_source"] == "hist_3y"
    assert round(v["target"]["price"], 2) == 132.0                     # mean 11 x F2 12
    assert not v["flags"]


def test_outlier_multiple_is_dropped_and_flagged():
    bars = _bars({"2022": 10, "2023": 100, "2024": 110, "2025": 120})
    v = idea.valuation(bars, _fund([10, 10, 0.5]), {"f0": 10, "f1": 11, "f2": 12}, {"pe1": 15})
    assert v["dropped_multiples"] == [240.0]                           # 120 / 0.5
    assert "hist_multiple_outlier_dropped" in v["flags"]
    assert v["target"]["multiple_source"] == "hist_trimmed"
    assert round(v["target"]["price"], 2) == 126.0                     # mean(10, 11) x 12


def test_eps_basis_mismatch_marks_target_unreliable():
    # an ADR worth two shares with EPS in another currency: the statements show half the consensus figure
    bars = _bars({"2022": 10, "2023": 100, "2024": 110, "2025": 120})
    v = idea.valuation(bars, _fund([5, 5, 5]), {"f0": 10, "f1": 11, "f2": 12}, {"pe1": 15})
    assert v["eps_basis_mismatch"]
    assert "unreliable_eps_basis" in v["target"]["flags"]
    opts = run._target_options(120, "long", 150, v, {})
    own = next(o for o in opts if o["source"] == "own_multiple_target")
    assert own["unreliable"]


def test_target_options_keep_only_the_right_side():
    v = {"target": {"price": 90, "method": "pe", "multiple": 9, "multiple_source": "hist_3y", "flags": []}}
    opts = run._target_options(100, "long", 130, v, {"nearest_resistance": 104, "nearest_support": 95})
    assert [o["source"] for o in opts] == ["technical_level", "all_time_high_target"]
    short = run._target_options(100, "short", 70, v, {"nearest_resistance": 104, "nearest_support": 95})
    assert [o["source"] for o in short] == ["technical_level", "own_multiple_target", "all_time_high_target"]


def _idea(color, rr, status="ready_for_approval", decision=None):
    return {"timing": {"combined": color}, "plan": {"rr": rr, "verdict": {"status": status, "findings": []}},
            "judgment": {"decision": decision} if decision else {}}


def test_classify_buckets():
    assert run.classify(_idea("green", 4))[0] == "enter"
    assert run.classify(_idea("orange", 4))[0] == "conditional"
    assert run.classify(_idea("green", 2))[0] == "watch"
    assert run.classify(_idea("red", 5))[0] == "watch"
    assert run.classify(_idea("green", 4, "blocked"))[0] == "reject"
    assert run.classify(_idea("green", 4, decision="watch_only"))[0] == "watch"


def test_report_renders_every_section():
    d = json.loads(FIX.read_text(encoding="utf-8"))
    h = report.render(d)
    assert h.startswith("<title>")
    for sec in ("summary", "funnel", "macro", "sectors", "screen", "ideas", "method"):
        assert f'id="{sec}"' in h
    for sym in d["ideas"]:
        assert f'id="idea-{sym}"' in h
    assert h.count("<svg") > 10
    assert "Claude" in h                                               # who wrote the judgment is stated


def test_exit_plan_from_trigger_with_earnings_before_expiry():
    p = {"entry": 100.0, "stop": 90.0, "target": 160.0, "horizon_months": 4}
    j = {"trigger": {"level": 105.0}}
    x = run.exit_plan(p, "long", "conditional", date(2026, 10, 20), "2026-11-03", j)
    assert (x["entry"], x["stop"], x["r"]) == (105.0, 95.0, 10.0)        # same stop width, from the trigger
    assert (x["trail_from"], x["trail_stop"]) == (135.0, 123.0)         # 3R; keeps 60% of 30
    assert x["time_stop_date"] == "2026-11-17" and x["earnings_before_expiry"]
    now = run.exit_plan(p, "long", "enter", date(2026, 10, 20), None, j)
    assert now["entry"] == 100.0 and now["time_stop_date"] is None


def test_corr_on_month_keys():
    a = {f"2025-{m:02d}": m * 0.01 for m in range(1, 13)}
    assert round(run._corr(a, {k: -v for k, v in a.items()}, 12), 6) == -1.0
    assert run._corr(a, {"2025-01": 0.1}, 12) is None


def test_event_markers_land_on_the_right_point():
    dates = [f"2026-{m:02d}" for m in range(1, 10)]
    svg = report.line_chart(dates, [("x", list(range(9)), "main")], markers=[("2026-03-02", "1"), ("2024-01", "2")])
    assert svg.count('class="ev"') == 1 and ">1</text>" in svg


def test_report_shows_events_fed_exit_and_fit():
    d = json.loads(FIX.read_text(encoding="utf-8"))
    d["judgments"]["A1"]["events"] = [{"date": "2026-03-02", "title": "אירוע", "text": "t", "charts": ["ffr"],
                                       "sources": [{"title": "s", "url": "https://example.com"}]}]
    d["judgments"]["A1"]["fed"] = {"headline": "הפד העלה"}
    sym = next(iter(d["ideas"]))
    d["ideas"][sym]["judgment"]["tldr"] = {"line": "שורה לחבר", "macro": "מאקרו"}
    d["ideas"][sym]["bucket"] = "enter"
    d["ideas"][sym]["exit"] = run.exit_plan(d["ideas"][sym]["plan"], "long", "enter", date(2026, 10, 20), "2026-10-29", {})
    d["portfolio"] = {"syms": [sym], "refs": ["SPY"], "pairs": [], "strong": 0.5, "sectors": {"XLE": 1},
                      "vs_refs": {sym: {"SPY": {"c12": 0.6, "c36": None}}}, "total_size_pct": 6, "total_risk_pct": 0.5}
    h = report.render(d)
    for s in ('class="events"', "הפד העלה", 'class="tldr"', "שורה לחבר", 'class="fed-box"', 'class="exit"', 'class="fit"', 'class="ev"'):
        assert s in h


def _wave(n: int, base: float, amp: float, period: int) -> list[Bar]:
    import math
    out, d = [], date(2023, 1, 2)
    for k in range(n):
        p = base + amp * math.sin(k / period)
        out.append(Bar(d, p, p * 1.012, p * 0.988, p * (1.002 if k % 3 else 0.998), 1))
        d += timedelta(days=1)
    return out


def test_tech_series_follow_the_course_periods_and_relative_strength():
    st, sec, spy = _wave(900, 100, 20, 40), _wave(900, 50, 5, 60), _wave(900, 400, 10, 90)
    t = idea.tech(st, sec, spy)
    assert len(t["dates"]) == len(t["rsi"]) == len(t["rs_sector"]) == 260
    assert t["periods"]["rsi"] == 20 and t["periods"]["macd"] == [20, 60, 10]
    assert t["rs_sector"][0] == 100.0 and t["rs_spy"][0] == 100.0           # both rebased to the first day
    assert 0.5 < t["adr_ratio"][-1] < 2
    assert len(t["weekly"]["close"]) == 129                                  # 900 days: every week held, up to 156


def test_idea_renders_the_charts_with_their_computed_captions():
    d = json.loads(FIX.read_text(encoding="utf-8"))
    i = next(iter(d["ideas"].values()))
    h0 = report._idea(i)
    assert "הגרף בתמונות" not in h0                                         # an older dossier without the series
    i["tech"] = idea.tech(_wave(900, 100, 20, 40), _wave(900, 50, 5, 60), _wave(900, 400, 10, 90))
    h = report._idea(i)
    assert "הגרף בתמונות (S09)" in h and "הדוחות בתמונות (S07)" in h
    for t in ("RSI(20)", "MACD (20/60/10)", "ADR20 חלקי ADR60", "חוזק יחסי", "שלוש שנים, נר שבועי"):
        assert t in h
    assert "(S09-R19)" in h and "(S09-R21)" in h


def test_the_card_under_the_article_leaves_out_what_the_article_shows():
    d = json.loads(FIX.read_text(encoding="utf-8"))
    i = next(iter(d["ideas"].values()))
    i["tech"] = idea.tech(_wave(900, 100, 20, 40), _wave(900, 50, 5, 60), _wave(900, 400, 10, 90))
    full, short = report._idea(i), report._idea(i, appendix=True)
    for t in ("הגרף בתמונות (S09)", "הדוחות בתמונות (S07)", "class=\"pchart\"", "class=\"rrbar\""):
        assert t in full and t not in short
    for t in ("תכנון עסקה (S10–S11)", "מנוע הכללים", "ADR60", "גודל ראשון"):          # what only the card has
        assert t in short
    assert short.count("<div") == short.count("</div")


def test_eps_basis_uses_the_same_range_as_the_data_check():
    # FLOC 31.10: ratio 0.76 failed the check (0.8-1.25) but passed the valuation's own 0.67-1.5
    bars = _bars({"2022": 10, "2023": 100, "2024": 110, "2025": 120})
    v = idea.valuation(bars, _fund([7.6, 7.6, 7.6]), {"f0": 10, "f1": 11, "f2": 12}, {"pe1": 15})
    assert v["eps_basis_mismatch"]


def test_agent_pick_of_an_unreliable_own_multiple_is_overridden():
    targets = [dict(source="own_multiple_target", price=150, unreliable=True, multiple_source="hist_3y", rr=5.0),
               dict(source="technical_level", price=130, rr=3.2),
               dict(source="all_time_high_target", price=140, rr=4.0)]
    j = {"target_source": "own_multiple_target"}
    chosen = run._choose_target(targets, j)
    assert chosen["source"] == "technical_level"               # 3.11: the nearest level that meets 1:3 wins, not the best R:R
    assert j["code_overrides"][-1]["from"] == "own_multiple_target"
    assert j["code_overrides"][-1]["to"] == "technical_level"
    j2 = {"target_source": "technical_level"}
    assert run._choose_target(targets, j2)["source"] == "technical_level" and "code_overrides" not in j2
    targets[1]["rr"] = 2.0                                     # the level is too close: the best usable R:R, the extreme
    assert run._choose_target(targets, {"target_source": "own_multiple_target"})["source"] == "all_time_high_target"


def test_nearest_level_meeting_the_minimum_beats_a_farther_one():
    targets = [dict(source="technical_level", price=104, rank=1, rr=0.4),
               dict(source="technical_level", price=135, rank=2, rr=3.5),
               dict(source="technical_level", price=160, rank=3, rr=6.0),
               dict(source="all_time_high_target", price=190, rr=9.0)]
    assert run._choose_target(targets, {})["price"] == 135


def test_target_beyond_the_horizon_is_never_chosen():
    # TALO 31.10: the 8-year high at +90% needed 9.6 months by the monthly sigma; the plan then has no target
    v = {"target": {"price": 17.0, "method": "pe", "multiple": 12, "multiple_source": "sector", "flags": ["unreliable_eps_basis"]}}
    opts = run._target_options(16.46, "long", 31.34, v, {"nearest_resistance": 16.49}, monthly_sigma_pct=9.4)
    ath = next(o for o in opts if o["source"] == "all_time_high_target")
    assert ath["beyond_horizon"] and ath["horizon_months"] > 6
    for o in opts:
        o["rr"] = {"technical_level": 0.01, "own_multiple_target": 0.24, "all_time_high_target": 6.26}[o["source"]]
    j = {"target_source": "all_time_high_target"}
    chosen = run._choose_target(opts, j)                      # what is left is the honest nearest level: R:R 0.01, a watch entry
    assert chosen["source"] == "technical_level" and chosen["price"] == 16.49
    assert j["code_overrides"][-1]["rule_id"] == "horizon.extended_max_months" and j["code_overrides"][-1]["to"] == "technical_level"
    only = [o for o in opts if o["source"] != "technical_level"]
    j2 = {"target_source": "all_time_high_target"}
    assert run._choose_target(only, j2) is None and j2["code_overrides"][-1]["to"] == "—"


def test_target_options_offer_the_three_nearest_levels():
    lv = {"nearest_resistance": 104, "resistances": [104, 112, 125], "supports": [95, 90]}
    opts = run._target_options(100, "long", 130, {}, lv)
    assert [(o["source"], o["price"], o.get("rank")) for o in opts][:3] == [("technical_level", 104, 1), ("technical_level", 112, 2),
                                                                         ("technical_level", 125, 3)]
    short = run._target_options(100, "short", 70, {}, lv)
    assert [o["price"] for o in short if o["source"] == "technical_level"] == [95, 90]


def test_open_skies_drops_the_extreme_and_keeps_the_own_multiple():
    # CVX 31.10: at its high, the extreme gave R:R 0.06; decision 1 (20.10): the fundamental target is the target
    assert run.open_skies(235.5, 236.0, 2.0) and not run.open_skies(200.0, 236.0, 2.0)
    bars = _bars({"2022": 10, "2023": 100, "2024": 110, "2025": 120})
    v = idea.valuation(bars, _fund([10, 10, 10]), {"f0": 10, "f1": 11, "f2": 12}, {"pe1": 15})
    opts = run._target_options(120, "long", 120.5, v, {}, sky=True)
    assert [o["source"] for o in opts] == ["own_multiple_target"] and "שמיים פתוחים" in opts[0]["note"]
    assert "all_time_high_target" in [o["source"] for o in run._target_options(100, "long", 150, v, {})]


def test_open_skies_ranks_as_meeting_the_minimum_not_as_zero():
    from qt.research.judge import rank_rr
    assert rank_rr({"open_skies": True, "rr_extreme": None}) == 3.0
    assert rank_rr({"rr_extreme": 0.06}) == 0.06


def test_report_opens_with_the_week_in_words_and_lists_the_critic_rejections():
    # Ilay 2.11: "I read it and have no idea what happened in the market this week"
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    from dossiers import complete

    from qt.research import report as R
    d = complete()
    d["judgments"]["agent_log"] = [{"card": "DC-14", "subject": "TALO", "status": "critic_rejected",
                                    "why": "wrong_rule: the company's own report is not C"}]
    nav, body = R.sections(d)
    assert nav[0] == ("week", "השבוע") and body.index('id="week"') < body.index('id="summary"')
    w = R._week(d)
    assert "בשבוע" in w and "סקטורים" in w and "own report is not C" in w
    assert R.plain("תיקון (S03-J5, S02-J5) והרוחב 47% (F-116, F-117).") == "תיקון והרוחב 47%."


def test_historical_multiple_outlier_at_one_and_a_half_times_the_median():
    # GRNT 31.10: P/E 9.9, 46.1, 26.1; at 2x the median the 46 stayed and the target came out +424%
    bars = _bars({"2022": 10, "2023": 99, "2024": 461, "2025": 261})
    v = idea.valuation(bars, _fund([10, 10, 10]), {"f0": 10, "f1": 11, "f2": 12}, {"pe1": 15})
    assert v["dropped_multiples"] == [46.1] and v["target"]["multiple_source"] == "hist_trimmed"
    assert round(v["target"]["multiple"], 2) == 18.0


def test_an_unreliable_own_multiple_is_the_target_of_last_resort_with_a_data_gap():
    # Ilay 09.11: the idea comes to the table with the caveat, not dropped
    targets = [dict(source="own_multiple_target", price=150, unreliable=True, multiple_source="hist_3y", rr=5.0),
               dict(source="all_time_high_target", price=190, rr=9.0, beyond_horizon=True)]
    j = {"target_source": "own_multiple_target"}
    chosen = run._choose_target(targets, j)
    assert chosen["source"] == "own_multiple_target" and "פער נתונים" in chosen["data_gap"]
    assert j["code_overrides"][-1]["to"] == "own_multiple_target"
    sector = [dict(targets[0], multiple_source="sector")]                # a sector multiple is still never the target
    assert run._choose_target(sector, {}) is None
    d = {"plan": {"verdict": {"status": "ok"}, "rr": 5.0, "data_gap": chosen["data_gap"]}, "timing": {"combined": "orange"},
         "judgment": {}}
    bucket, why = run.classify(d)
    assert bucket == "conditional" and "פער נתונים" in why


def test_a_green_neutral_sector_goes_on_the_watchlist_watch_only(monkeypatch):
    # Ilay 09.11 (funnel audit 3.11, option b): seen, not entered, not deep-dived
    from types import SimpleNamespace as NS
    from qt.watch import from_dossier, load
    light = {"XLF": "green", "XLY": "red"}
    monkeypatch.setattr(run, "snapshot", lambda bars, direction: NS(overall=light[bars]))
    monkeypatch.setattr(run.sector_screen, "screens", lambda rows, longs, shorts: {e: {} for e in longs})

    def timing(y, screens, as_of):
        assert list(screens) == ["XLF"]
        return [dict(etf="XLF", sym=f"F{n}", dir="long", combined=c, rr_extreme=rr, price=10.0, adr=0.2, extreme=14.0)
                for n, (c, rr) in enumerate([("orange", 2), ("green", 1), ("red", 9), ("orange", 5), ("orange", 4),
                                             ("orange", 3), ("orange", 1)])] + [dict(etf="XLF", sym="ERR", error="x")]
    monkeypatch.setattr(run, "stage_timing", timing)
    y = NS(bars=lambda sym, start, end: sym)
    w = run.watch_sectors(y, {"neutral": ["XLF", "XLY"]}, {}, date(2026, 11, 7))
    assert w["etfs"] == ["XLF"] and [t["sym"] for t in w["timing"]] == ["F1", "F3", "F4", "F5", "F0"]   # green first, then R:R
    d = {"meta": {"as_of": "2026-11-07"}, "ideas": {}, "timing": [], "watch_sectors": w}
    entries = load(from_dossier(d))
    assert [e.symbol for e in entries] == ["F1", "F3", "F4", "F5", "F0"]
    assert all(e.bucket == "watch" and e.watch_sector and "מעקב בלבד" in e.why_not for e in entries)
    assert run.watch_sectors(y, {"neutral": ["XLY"]}, {}, date(2026, 11, 7)) == {"etfs": [], "timing": []}
