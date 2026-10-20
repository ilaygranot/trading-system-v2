"""Acceptance tests, stage 13 (AT-S13-*)."""
import pytest

from qt.calc.portfolio import Portfolio
from qt.rules.checks import Context, Readiness, TradeProposal, evaluate
from qt.rules.engine import Override, Severity, Status


def entry(**kw):
    base = dict(entry=10, stop=8.8, target=14, target_source="technical_level", horizon_months=2, adr_pct=3,
                requested_pct=6)
    base.update(kw)
    return TradeProposal("X", "long", **base)


@pytest.mark.parametrize("readiness,blocked", [
    (Readiness(slept=False), True),
    (Readiness(), False),
    (Readiness(personal_event_today=True), True),   # AT-S13-2: wedding at 18:30
])
def test_at_s13_1_2_readiness(readiness, blocked):
    v = evaluate(entry(), Context(Portfolio(100_000), readiness=readiness))
    assert ("S13-R13" in v.rule_ids(Severity.BLOCK)) is blocked


def test_at_s13_1_exit_allowed_when_not_ready():
    v = evaluate(TradeProposal("X", "long", action="exit"), Context(Portfolio(100_000), readiness=Readiness(slept=False)))
    assert v.status is Status.READY_FOR_APPROVAL


def test_personal_crisis_pauses():
    v = evaluate(entry(), Context(Portfolio(100_000), readiness=Readiness(personal_crisis=True)))
    assert "S13-R14" in v.rule_ids(Severity.BLOCK)


def test_at_s13_3_fomo_is_default_rule_needs_override():
    v = evaluate(entry(move_pct_2w=40, idea_listed_before_move=False), Context(Portfolio(100_000)))
    assert "S13-R5" in v.rule_ids(Severity.NEEDS_OVERRIDE)
    assert v.status is Status.NEEDS_OVERRIDE
    # an override without a course principle and a fact does not count
    bad = Override("S13-R5", "because", (), "it will run")
    assert evaluate(entry(move_pct_2w=40, idea_listed_before_move=False), Context(Portfolio(100_000)),
                    overrides=(bad,)).status is Status.NEEDS_OVERRIDE


def test_at_s13_4_third_entry_after_two_stops():
    v = evaluate(entry(stops_last_30d=2), Context(Portfolio(100_000)))
    assert "S13-R6" in v.rule_ids(Severity.NEEDS_OVERRIDE)


def test_valid_override_becomes_marked_deviation():
    o = Override("S13-R6", "L085-J03", ("F-004",), "new catalyst changed the thesis")
    v = evaluate(entry(stops_last_30d=2), Context(Portfolio(100_000)), overrides=(o,))
    assert v.status is Status.READY_FOR_APPROVAL
    assert [d.rule_id for d in v.deviations] == ["S13-R6"]


def test_no_journal_no_entry():
    assert "S13-R29" in evaluate(entry(journaled=False), Context(Portfolio(100_000))).rule_ids(Severity.BLOCK)


def test_watchlist_and_process_gates():
    v = evaluate(entry(on_watchlist=False, process_complete=False), Context(Portfolio(100_000)))
    assert {"S08-R2", "S13-R1"} <= v.rule_ids(Severity.BLOCK)
