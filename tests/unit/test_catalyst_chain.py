"""qt.research.catalyst_chain: the catalyst events, what code decides, and the gate and horizon derived from them
(S07-R13, S08-R13..R23, S08-J25, S10-R9, S10-R10, D-S10-5, S13-J7)."""
import copy
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from qt.agents.cards import load_cards
from qt.research import catalyst_chain as CC

FIX = json.loads((Path(__file__).parent.parent / "fixtures" / "research_dossier.json").read_text(encoding="utf-8"))
AS_OF = date(2026, 10, 20)


def _iso(days: int) -> str:
    return (AS_OF + timedelta(days=days)).isoformat()


def ev(days: int, cls="B", direction="for_idea", priced_in=False, persistence="none", kind="other_event", i=1,
       timing=None, **kw):
    return {"id": f"EV{i:02d}", "date": _iso(days), "timing": timing or ("expected" if days > 0 else "occurred"),
            "kind": kind, "what": f"אירוע {i}", "class": cls, "direction": direction, "priced_in": priced_in,
            "persistence": persistence, "flags": [], "final": [], **kw}


def idea(**over):
    d = copy.deepcopy(FIX["ideas"]["SHEL"])
    d.update(over)
    return d


def filing(items, days=-10, kind="unknown", form="8-K", acc="0001-26-000001", exhibits=None):
    return {"form": form, "date": _iso(days), "accession": acc, "items": items, "item_names": [], "kind": kind,
            "url": "https://www.sec.gov/x.htm", "index_url": f"https://www.sec.gov/{acc}-index.htm",
            "exhibits": exhibits or []}


def docs(current=(), results=None, results_prev=None, risk=None):
    return {"symbol": "SHEL", "cik": 1, "ciks": [1], "foreign": False, "as_of": AS_OF.isoformat(),
            "current": list(current), "results": results, "results_prev": results_prev, "risk_factors": risk,
            "transcript": None, "errors": []}


def results(low, high, period="FY", days=-10):
    return {"form": "8-K", "date": _iso(days), "url": f"https://www.sec.gov/ex99-{days}.htm", "source": "sec:8-K EX-99.1",
            "title": "Results", "lead": "lead", "outlook": [], "commentary": None,
            "guidance": [{"metric": "revenue", "period": period, "low": low, "high": high, "unit": "USD",
                          "sentence": "s"}]}


# ------------------------------------------------------------------ the horizon (S10-R9, S10-R10, D-S10-5)

def test_chain_extends_horizon():
    evs = [ev(137, i=1), ev(60, i=2), ev(90, cls="C", direction=None, kind="competitor_results", i=3)]
    h = CC.catalyst_horizon(2.0, evs, AS_OF)                       # two counted catalysts, the last at 4.5 months
    assert h["months"] == pytest.approx(4.6, abs=0.05) and h["basis"] == "catalyst" and h["expected"] is True
    assert h["chain"] and h["chain_events"] == ["EV02", "EV01"]    # the peer C is context, never a link (D-S08-2)
    assert h["chain"].startswith(_iso(60) + " B") and "context (C, not a link)" in h["chain"]


def test_peer_c_never_makes_a_chain():
    """S08-R18 / D-S08-2 / S10-R9: one counted catalyst plus a competitor report (C) or a macro lead is no chain."""
    for other in (ev(137, cls="C", direction=None, kind="competitor_results", i=2),
                  ev(137, cls="macro_lead", direction=None, i=2), ev(137, cls="B", direction=None, i=2)):
        h = CC.catalyst_horizon(2.0, [ev(60, i=1), other], AS_OF)
        assert h["basis"] == "sigma" and h["chain"] == "" and h["chain_events"] == [] and h["months"] <= 3.0


def test_cap_six_and_single_event_no_chain():
    h = CC.catalyst_horizon(2.0, [ev(182, i=1), ev(60, i=2)], AS_OF)
    assert h["months"] == 6.0 and h["basis"] == "catalyst"
    one = CC.catalyst_horizon(2.0, [ev(152, i=1)], AS_OF)          # one counted event at 5 months, no second
    assert one["months"] <= 3.0 and one["chain"] == "" and one["basis"] == "sigma"
    assert CC.catalyst_horizon(None, [ev(152)], AS_OF)["months"] is None


def test_never_below_sigma():
    for sig in (13.581062, 3.6601, 2.004, 4.444449):               # rounding never takes it below sigma (S10-R35)
        for evs in ([], [ev(60, i=1), ev(120, i=2)], [ev(9, kind="own_report")]):
            assert CC.catalyst_horizon(sig, evs, AS_OF)["months"] >= sig
    assert CC.catalyst_horizon(3.7, [ev(9, kind="own_report")], AS_OF)["months"] == pytest.approx(3.7)
    assert CC.catalyst_horizon(7.5, [ev(60, i=1), ev(120, i=2)], AS_OF)["months"] == pytest.approx(7.5)  # S10-R35's
    assert CC.catalyst_horizon(2.0, [], AS_OF)["months"] == 2.0                                          # flag, not ours


def test_wait_text():
    h = CC.catalyst_horizon(2.0, [ev(45)], AS_OF)
    assert h["wait"] == f"no catalyst in the next month; first on {_iso(45)}: extend the horizon or wait"
    assert CC.catalyst_horizon(2.0, [ev(10)], AS_OF)["wait"] is None
    r = CC.catalyst_horizon(2.0, [ev(9, kind="own_report", persistence="until_next_report")], AS_OF)
    assert r["wait"] == "no catalyst until the report: waiting is allowed (S09-R53)"


def test_recent_analyst_upgrade_alone_is_not_an_expected_catalyst():
    """S08-R20 / S09-R38 / S08-R21: an occurred +60% analyst B rides 1-3 days; it never opens a 1-3 month swing."""
    d = idea(direction="long", plan={"entry": 100}, documents=None, peers=[],
             catalysts={"next_earnings": None, "analyst_actions": [
                 {"date": _iso(-2), "firm": "X", "action": "up", "from_": "Hold", "to": "Buy", "target": 160,
                  "prior_target": 110}]})
    evs = CC.events(d, AS_OF)
    b = next(e for e in evs if e["kind"] == "analyst_target")
    assert b["class"] == "B" and CC.counts(b, AS_OF)               # still active today (adverse uses this) ...
    h = CC.catalyst_horizon(2.0, evs, AS_OF)
    assert h["expected"] is False and h["wait"] is None            # ... but it does not serve the horizon (S07-R13)
    assert not CC.serves_horizon(ev(-1, persistence="none"), AS_OF)


def test_occurred_long_lived_event_still_serves_the_horizon():
    g = ev(-10, cls="A", persistence="until_next_report")         # a guidance raise, next report unknown: ~81 days
    assert CC.catalyst_horizon(2.0, [g], AS_OF)["expected"] is True
    soon = CC.catalyst_horizon(2.0, [g], AS_OF, next_earnings=_iso(5))   # ends in 5 days: no longer a swing catalyst
    assert soon["expected"] is False
    assert CC.catalyst_horizon(2.0, [ev(-2, persistence="days_1_3"), ev(40, i=2)], AS_OF)["expected"] is True


def test_macro_and_cd_never_count():
    evs = [ev(20, cls=c, i=n) for n, c in enumerate(("C", "D", "macro_lead", "macro_noise"), 1)]
    evs.append(ev(30, priced_in=True, i=9))
    h = CC.catalyst_horizon(2.0, evs, AS_OF)
    assert h["expected"] is False and h["chain"] == ""
    assert not any(CC.counts(e) for e in evs)


def test_priced_in_a_does_not_count():
    assert not CC.counts(ev(20, cls="A", priced_in=True))
    assert not CC.counts(ev(20, cls="A", priced_in=None))          # undecided is priced in (S13-J7)
    assert CC.counts(ev(20, cls="A", priced_in=False))


def test_persistence_expiry():
    b = ev(-10, persistence="days_1_3")                             # analyst B from 10 days ago
    assert CC.active_until(b) == AS_OF - timedelta(days=7) and not CC.counts(b, AS_OF)
    assert CC.counts(ev(-2, persistence="days_1_3"), AS_OF)
    g = ev(-10, persistence="until_next_report")
    assert CC.active_until(g, _iso(9)) == AS_OF + timedelta(days=9)
    assert CC.active_until(g) == AS_OF + timedelta(days=81)        # next report unknown: one quarter
    assert CC.active_until(ev(-10, persistence="accumulating_2_6m")) == AS_OF + timedelta(days=173)
    assert CC.active_until(ev(-10, persistence="none")) == AS_OF - timedelta(days=10)


def test_against_prospect_does_not_count():
    e = ev(20, cls="A")
    e["flags"] = ["catalyst_against_prospect"]
    assert not CC.counts(e)
    d = idea(documents=docs(results=results(90, 92), results_prev=results(100, 100, days=-100)))   # a cut on a long
    g = next(x for x in CC.events(d, AS_OF) if x["kind"] == "guidance")
    assert g["class"] == "A" and g["direction"] == "against_idea" and "catalyst_against_prospect" in g["flags"]
    CC.apply_answer(g, {"option": "B", "direction": "for_idea", "priced_in": False})
    assert g["class"] == "A" and g["direction"] == "against_idea" and not CC.counts(g)
    assert CC.adverse([g], AS_OF)


# ------------------------------------------------------------------ what code decides

def test_governance_8k_is_d():
    d = idea(documents=docs([filing(["5.07"]), filing(["5.02", "9.01"], acc="0001-26-000002"),
                             filing(["2.02", "9.01"], acc="0001-26-000003")]))
    evs = {e["accession"]: e for e in CC.events(d, AS_OF) if e.get("accession")}
    g = evs["0001-26-000001"]
    assert g["class"] == "D" and g["decided_by"] == "code" and not g["needs_agent"] and g["kind"] == "governance"
    assert evs["0001-26-000002"]["kind"] == "officer" and evs["0001-26-000002"]["needs_agent"]
    assert evs["0001-26-000003"]["kind"] == "results" and evs["0001-26-000003"]["needs_agent"]


def test_analyst_target_bands():
    acts = [{"date": _iso(-5), "firm": "Big", "action": "up", "from_": "Hold", "to": "Buy", "target": 155.0},
            {"date": _iso(-5), "firm": "Up8", "action": "up", "from_": "Hold", "to": "Buy", "target": 108.0},
            {"date": _iso(-5), "firm": "Main8", "action": "main", "from_": "Buy", "to": "Buy", "target": 108.0},
            {"date": _iso(-5), "firm": "Mid", "action": "up", "from_": "Hold", "to": "Buy", "target": 130.0},
            {"date": _iso(-120), "firm": "Old", "action": "up", "target": 200.0}]          # outside 90 days
    d = idea(catalysts={**FIX["ideas"]["SHEL"]["catalysts"], "analyst_actions": acts},
             plan={**FIX["ideas"]["SHEL"]["plan"], "entry": 100.0})
    by = {e["what"].split(":")[0]: e for e in CC.events(d, AS_OF) if e["kind"] == "analyst_target"}
    assert set(by) == {"Big", "Up8", "Main8", "Mid"}
    assert by["Big"]["class"] == "B" and by["Big"]["persistence"] == "days_1_3" and by["Big"]["direction"] == "for_idea"
    assert by["Up8"]["class"] == "C" and by["Main8"]["class"] == "D"
    assert by["Mid"]["class"] is None and by["Mid"]["needs_agent"]
    assert CC.counts(by["Big"]) and not CC.counts(by["Big"], AS_OF)   # 5 days ago, works 1-3 days: expired


def test_fy_guidance_threshold():
    up = CC.events(idea(documents=docs(results=results(108, 112), results_prev=results(98, 102, days=-100))), AS_OF)
    g = next(e for e in up if e["kind"] == "guidance")
    assert g["class"] == "A" and g["facts"]["guidance_change_pct"] == pytest.approx(10.0) and g["needs_agent"]
    CC.apply_answer(g, {"option": "C", "direction": "for_idea", "priced_in": False})
    assert g["class"] == "A" and g["priced_in"] is False and g["persistence"] == "until_next_report"
    small = CC.events(idea(documents=docs(results=results(103, 103), results_prev=results(100, 100, days=-100))), AS_OF)
    g = next(e for e in small if e["kind"] == "guidance")
    assert g["class"] == "D" and not g["needs_agent"] and "below" in g["code_reason"]
    q = idea(documents=docs([filing(["2.02", "9.01"])], results=results(150, 160, period="Q"),
                            results_prev=results(100, 100, period="Q", days=-100)))
    evs = CC.events(q, AS_OF)
    assert not any(e["kind"] == "guidance" for e in evs)
    r = next(e for e in evs if e["kind"] == "results")
    assert r["code_reason"] == "quarterly guidance is not an A (S08-R19)"
    CC.apply_answer(r, {"option": "A", "direction": "for_idea", "priced_in": False})
    assert r["class"] == "B"                                        # never an A


def test_peer_report_is_c():
    d = idea(peers=[{"sym": "BP", "etf": "XLE", "same": "industry", "on_list": True, "next_earnings": _iso(15),
                     "last_results_8k": None},
                    {"sym": "XOM", "etf": "XLE", "same": "etf", "on_list": False, "next_earnings": _iso(400)}])
    p = [e for e in CC.events(d, AS_OF) if e["kind"] == "competitor_results"]
    assert len(p) == 1 and p[0]["class"] == "C" and p[0]["timing"] == "expected" and not p[0]["needs_agent"]
    assert "BP" in p[0]["what"] and not CC.counts(p[0])


def test_event_price_and_count_facts():
    ch = FIX["ideas"]["SHEL"]["chart"]
    day = ch["dates"][-25]
    d = idea(documents=docs([filing(["8.01"], days=(date.fromisoformat(day) - AS_OF).days)]))
    e = next(x for x in CC.events(d, AS_OF) if x.get("accession"))
    i = ch["dates"].index(day)
    assert e["facts"]["pre_move_20d_pct"] == pytest.approx(round((ch["close"][i] / ch["close"][i - 20] - 1) * 100, 2))
    assert e["facts"]["since_event_pct"] == pytest.approx(round((ch["close"][-1] / ch["close"][i] - 1) * 100, 2))
    own = next(x for x in CC.events(idea(), AS_OF) if x["kind"] == "own_report")
    assert own["facts"]["since_event_pct"] is None and own["facts"]["prior_items_30d"] >= 5   # the Yahoo headlines


def test_cap_overflow_is_lower_class():
    d = idea(documents=docs([filing(["8.01"], acc=f"0001-26-{n:06d}") for n in range(8)]))
    evs = CC.events(d, AS_OF)
    picked = CC.select_for_agent(evs)
    assert len(picked) == CC.MAX_AGENT_EVENTS == 5 and picked[0]["kind"] == "own_report"
    rest = [e for e in evs if e["code_reason"] == "cap"]
    assert rest and all(e["class"] == "D" and e["priced_in"] is True for e in rest)
    e = picked[1]
    CC.agent_error(e, RuntimeError("budget"))
    assert e["class"] == "D" and e["code_reason"].startswith("agent_error")
    u = picked[2]
    CC.apply_answer(u, {"option": "lower_class"})                   # the runner's undecided fallback
    assert u["class"] == "D" and u["priced_in"] is True and u["direction"] is None


def test_adverse():
    a = ev(-3, cls="A", direction="against_idea", persistence="until_next_report")
    assert CC.adverse([a], AS_OF)
    assert not CC.adverse([dict(a, priced_in=True)], AS_OF)
    assert not CC.adverse([dict(a, persistence="days_1_3", date=_iso(-10))], AS_OF)   # expired


def test_dc14_schema_fields():
    s = load_cards()["DC-14"].schema()
    for k in ("priced_in", "direction", "level", "timing", "expected_date", "persistence", "follow_on_expected",
              "strength", "flow_technical", "peers_affected", "what"):
        assert k in s["required"], k
    assert s["properties"]["level"]["enum"] == ["market_macro", "industry", "company"]          # S08-R13
    assert s["properties"]["persistence"]["enum"] == list(CC.PERSISTENCE)                       # S08-R20
    e = ev(20, cls=None)
    CC.apply_answer(e, {"option": "B", "level": "industry", "strength": "חזקה", "flow_technical": True,
                        "peers_affected": ["BP"], "persistence": "accumulating_2_6m", "follow_on_expected": True,
                        "what": "שורה", "priced_in": False, "direction": "for_idea"})
    r = CC.judgment_rows([e])[0]
    assert r["level"] == "industry" and r["flow_technical"] is True and r["peers_affected"] == ["BP"]
    assert r["what"] == "שורה" and r["decided_by"] == "DC-14" and r["active_until"] == _iso(203)
    assert "needs_agent" not in r and "final" not in r


def test_sources_from_documents():
    rf = {"form": "10-K", "filed": "2026-02-01", "url": "https://www.sec.gov/10k.htm", "chars_total": 1, "headlines": [],
          "excerpt": "", "delta": None}
    s = CC.sources({"documents": docs([filing(["2.02"])], results=results(1, 2), risk=rf)})
    assert [x["url"] for x in s] == ["https://www.sec.gov/0001-26-000001-index.htm", "https://www.sec.gov/ex99--10.htm",
                                     "https://www.sec.gov/10k.htm"]
    assert CC.sources({}) == []


def test_small_fy_guidance_caps_the_release_below_a():
    """S08-R19: an annual change under 8% is no A, so DC-14 cannot make the same release an A on it."""
    d = idea(documents=docs([filing(["2.02", "9.01"])], results=results(103, 103),
                            results_prev=results(100, 100, days=-100)))
    r = next(e for e in CC.events(d, AS_OF) if e["kind"] == "results")
    assert "below the threshold" in r["code_reason"]
    CC.apply_answer(r, {"option": "A", "direction": "for_idea", "priced_in": False})
    assert r["class"] == "B"


def test_short_mirror_of_not_renewed_is_c():
    """S08-R17 / S08-R18 mirrored: on a short, a continuing downgrade with a small lower target is C, not D."""
    acts = [{"date": _iso(-5), "firm": "Dn8", "action": "down", "from_": "Hold", "to": "Sell", "target": 92.0},
            {"date": _iso(-5), "firm": "Up8", "action": "up", "from_": "Hold", "to": "Buy", "target": 108.0}]
    d = idea(direction="short", catalysts={**FIX["ideas"]["SHEL"]["catalysts"], "analyst_actions": acts},
             plan={**FIX["ideas"]["SHEL"]["plan"], "entry": 100.0})
    by = {e["what"].split(":")[0]: e for e in CC.events(d, AS_OF) if e["kind"] == "analyst_target"}
    assert by["Dn8"]["class"] == "C" and by["Up8"]["class"] == "D"


@pytest.mark.parametrize("status", ["critic_rejected", "agent_undecided"])
def test_unclear_dc14_answer_never_counts(status):
    """S13-J7: a rejected or undecided answer is the lower class and priced in, never a counted catalyst."""
    e = ev(20, cls=None, direction=None, priced_in=None)
    CC.apply_answer(e, {"option": "B", "direction": "for_idea", "priced_in": False, "status": status})
    assert e["class"] == "D" and e["priced_in"] is True and not CC.counts(e) and status in e["code_reason"]
    ok = ev(20, cls=None, direction=None, priced_in=None)
    CC.apply_answer(ok, {"option": "B", "direction": "for_idea", "priced_in": False, "status": "decided"})
    assert CC.counts(ok)


def test_rows_without_a_url_carry_no_url_key():
    rows = CC.judgment_rows([ev(20, url=""), ev(-3, i=2, url="https://www.sec.gov/x-index.htm")])
    assert "url" not in rows[0] and rows[1]["url"].startswith("https://")
