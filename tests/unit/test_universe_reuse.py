"""Audit 09.11: the evening's research runs (midweek, the dry run, Saturday) take the nightly scan's universe instead of
asking Yahoo for every stock again."""
from datetime import date

from qt import scan
from qt.db import DB
from qt.research import universe as U
from qt.research.state import SCAN_TAG, UNIVERSE, State, copy_universe, universe_from_scan, universe_ok
from qt.runs import RunContext

WED = date(2026, 11, 11)
GOOD = {"quotes": {"AAA": {"price": 10}}, "summaries": {"AAA": {"sector": "Energy"}, "OLD": {"error": "HTTP 404"}}}
BAD = {"quotes": {"AAA": {"price": 10}}, "summaries": {"AAA": {"error": "HTTP 429 Too Many Requests"}}}


def _scan_ctx(db, monkeypatch, tmp_path, u):
    monkeypatch.setenv("QT_WORK_DIR", str(tmp_path))
    calls = []
    monkeypatch.setattr(U, "fetch", lambda y: calls.append(1) or u)
    return RunContext(db=db, run_id="r1", kind="scan", as_of=WED, services={"research_yahoo": object()}), calls


def test_universe_ok_ignores_delisted_symbols_only():
    assert universe_ok(GOOD)
    assert not universe_ok(BAD)
    assert not universe_ok(None)


def test_the_scan_keeps_a_whole_universe_in_the_state(db, monkeypatch, tmp_path):
    ctx, calls = _scan_ctx(db, monkeypatch, tmp_path, GOOD)
    assert scan._universe(ctx) == GOOD
    assert State(db, WED, tag=SCAN_TAG).get_blob(UNIVERSE) == GOOD
    (tmp_path / "scan" / WED.isoformat() / "universe.pkl").unlink()       # a resume in a new container
    assert scan._universe(ctx) == GOOD and len(calls) == 1


def test_the_scan_keeps_only_the_last_few_nights(db, monkeypatch, tmp_path):
    State(db, date(2026, 11, 2), tag=SCAN_TAG).put_blob(UNIVERSE, GOOD)
    State(db, date(2026, 11, 10), tag=SCAN_TAG).put_blob(UNIVERSE, GOOD)
    ctx, _ = _scan_ctx(db, monkeypatch, tmp_path, GOOD)
    scan._universe(ctx)
    assert State(db, date(2026, 11, 2), tag=SCAN_TAG).get(UNIVERSE) is None
    assert State(db, date(2026, 11, 10), tag=SCAN_TAG).get(UNIVERSE) is not None


def test_the_scan_does_not_hand_on_a_universe_with_failed_requests(db, monkeypatch, tmp_path):
    ctx, _ = _scan_ctx(db, monkeypatch, tmp_path, BAD)
    scan._universe(ctx)
    assert State(db, WED, tag=SCAN_TAG).get(UNIVERSE) is None


def test_a_research_state_takes_the_scan_universe_of_its_date_once(db):
    State(db, WED, tag=SCAN_TAG).put_blob(UNIVERSE, GOOD)
    mid = State(db, WED, tag="midweek")
    assert universe_from_scan(db, WED, mid)
    assert mid.get_blob(UNIVERSE) == GOOD
    assert not universe_from_scan(db, WED, mid)                           # already there: nothing copied
    other = State(db, date(2026, 11, 12))
    assert not universe_from_scan(db, date(2026, 11, 12), other)          # no scan that day: the run fetches


def test_copy_universe_crosses_schemas_with_every_part(monkeypatch):
    import qt.research.state as S
    monkeypatch.setattr(S, "BLOB_PART", 50)                               # several parts
    src, dst = DB(":memory:"), DB(":memory:")
    big = {"quotes": {f"S{i}": {"price": i} for i in range(200)}, "summaries": {}}
    State(src, WED, tag=SCAN_TAG).put_blob(UNIVERSE, big)
    parts = copy_universe(src, f"{WED.isoformat()}:{SCAN_TAG}", dst, WED.isoformat())
    assert parts and parts > 1
    assert State(dst, WED).get_blob(UNIVERSE) == big
    assert copy_universe(src, "2026-01-01", dst, "2026-01-01") is None


def test_the_dry_run_starts_from_the_evening_scan_universe():
    from qt import dryrun
    prod, dry = DB(":memory:"), DB(":memory:")
    dry.schema = "qt_dry"                                                  # prepare() wants a schema of its own
    dry.drop_schema = lambda: None
    fri = date(2026, 11, 13)
    State(prod, fri, tag=SCAN_TAG).put_blob(UNIVERSE, GOOD)
    copied = dryrun.prepare(prod, dry, fri)
    assert any("היקום מהסריקה" in c for c in copied)
    assert State(dry, fri).get_blob(UNIVERSE) == GOOD


def test_preflight_fails_when_the_nyse_holidays_run_out():
    import pytest
    from qt import preflight
    assert "2028-12-25" in preflight.policy(date(2026, 11, 10))
    with pytest.raises(RuntimeError):
        preflight.policy(date(2028, 11, 1))


def test_the_monthly_run_is_due_before_the_weekly_one():
    from datetime import datetime, timezone
    from qt.schedule import due
    utc = timezone.utc
    assert due("monthly", datetime(2026, 11, 14, 12, 35, tzinfo=utc))[0]          # 07:35 New York (EST)
    assert not due("monthly", datetime(2026, 11, 14, 15, 5, tzinfo=utc))[0]       # 10:05: the weekly run's slot
    assert due("weekly", datetime(2026, 11, 14, 15, 5, tzinfo=utc))[0]
