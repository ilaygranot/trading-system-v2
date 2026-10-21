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
