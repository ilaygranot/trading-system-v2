from datetime import date

import pytest
from pathlib import Path

from qt.calc.portfolio import Portfolio
from qt.data.zacks import parse
from qt.facts import FactSheet
from qt.journal import Journal, decision_from_verdict
from qt.playbook import Tier, load_playbook
from qt.rules.checks import Context, TradeProposal, evaluate
from qt.rules.engine import Override


def test_playbook_has_the_41_approved_hard_rules():
    pb = load_playbook()
    assert len([r for r in pb.hard_rules() if r.startswith("S")]) == 41
    assert pb.tier("V1-SCOPE") is Tier.HARD              # decision 1, kept in the repo (_scope.csv)
    assert pb.tier("S12-R24") is Tier.DEFAULT           # VIX jump: flag only
    assert pb.tier("S12-R25") is Tier.HARD              # ATR 20-60 regime switch
    assert pb.tier("S06-J6") is Tier.JUDGMENT
    assert pb.tier("S99-R1") is Tier.DEFAULT            # unknown R => default (safe direction)


def test_journal_records_verdict_and_is_append_only(db):
    j = Journal(db)
    facts = FactSheet()
    f = facts.add("X.adr60", 3.0, "yahoo:v8/chart", date(2023, 5, 1))
    p = TradeProposal("X", "long", entry=10, stop=8.8, target=14, target_source="technical_level", horizon_months=2,
                      adr_pct=3, requested_pct=6, stops_last_30d=2)
    o = Override("S13-R6", "L085-J03", (f.fact_id,), "new catalyst")
    v = evaluate(p, Context(Portfolio(100_000)), overrides=(o,))
    did = j.record(decision_from_verdict(v, "X", load_playbook().version, facts=facts.to_list(),
                                         prediction="reaches 14 within 60 days", check_on=date(2023, 7, 1)))
    row = j.get(did)
    assert row["status"] == "ready_for_approval" and row["deviations"][0]["rule_id"] == "S13-R6"
    with pytest.raises(db.error, match="append-only"):
        j.db.execute("UPDATE decisions SET choice='x' WHERE id=?", (did,))
    assert [r["id"] for r in j.due_predictions(date(2023, 7, 1))] == [did]
    j.review(did, "approved")
    j.record_outcome(did, {"hit": True, "r": 3.1}, True)
    assert j.due_predictions(date(2023, 7, 2)) == []
    stats = j.deviation_success()
    assert stats[0]["rule_id"] == "S13-R6" and stats[0]["correct"] == 1


def test_fact_sheet_catches_invented_facts():
    fs = FactSheet()
    fs.add("BA.eps.F1", -1.12, "fmp", date(2023, 4, 18))
    assert fs.missing(["F-001", "F-009"]) == ["F-009"]


def test_zacks_import_maps_known_columns_and_reports_the_rest():
    text = "Ticker,Company Name,Sector,Market Cap (mil),F1 Consensus Est.,F2 Consensus Est.,Weird Col\nBA,Boeing,Aerospace,120000,-1.10,3.76,1\n"
    imp = parse(text, date(2026, 11, 1))
    assert imp.rows[0]["ticker"] == "BA" and imp.rows[0]["eps_f2"] == 3.76
    assert imp.rows[0]["market_cap_mil"] == 120000
    assert "Weird Col" in imp.unmapped_headers


def test_playbook_ships_in_the_package_and_never_loads_empty(tmp_path, monkeypatch):
    """24.10: on Render (`pip install .`) the snapshot was looked up next to src/ and loaded empty. It is package
    data now: found from the package itself, whatever the working directory."""
    import pytest
    from qt import playbook as P
    monkeypatch.chdir(tmp_path)
    d = P._default_tiers_dir()
    assert d == Path(P.__file__).resolve().parent / "playbook_snapshot" and (d / "chapters" / "decisions.md").exists()
    pb = P.load_playbook(str(d))
    assert len(pb.rules) > 400 and len(pb.hard_rules()) >= 40
    with pytest.raises(FileNotFoundError):
        P.load_playbook(str(tmp_path))                           # no snapshot: loud, never an empty rule set
