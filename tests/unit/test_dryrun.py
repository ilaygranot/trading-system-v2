"""The dry run (qt.dryrun) and the pre-flight check (qt.preflight): the Friday rehearsal of Saturday's run."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from qt import dryrun, preflight
from qt.approvals import Queue
from qt.db import DB
from dossiers import complete, research_services
from test_weekly import Prices

AS_OF = date(2026, 10, 23)


def _ok():
    return [preflight.Result("Yahoo", True, "ok")]


def _dry(prod: DB) -> DB:
    return DB(prod.url if prod.pg else ":memory:", schema=dryrun.DRY_SCHEMA)


def _services(seen: dict, universe_errors: dict | None = None):
    def build(db):
        def research_run(as_of, judge, manual_text, fred):
            seen.update(max_deep=judge.max_deep, max_rescue=judge.max_rescue, db=db)
            judge.state.put_blob("stage:universe.pkl", {"quotes": [1, 2, 3]})
            d = complete()
            d["meta"]["universe"] = {"n": 3, "errors": universe_errors or {}}
            return d
        pf = {"cash": 10_000, "peak_equity": 10_000, "positions": []}
        return {"prices": Prices(), "portfolio": pf, "watchlist": None,
                "now": lambda: datetime(2026, 10, 23, 22, 0, tzinfo=timezone.utc),
                **research_services(db), "research_run": research_run}
    return build


def test_a_dry_run_is_the_whole_weekly_run_in_its_own_schema(db):
    Queue(db).upload("watchlist", "watchlist-2026-10-16.yaml", "entries: []\n")
    seen: dict = {}
    dry = _dry(db)
    res = dryrun.run(db, AS_OF, dry=dry, services=_services(seen), checks=_ok)
    assert res["ok"], dryrun.text(res)
    assert seen["max_deep"] == 2 and seen["max_rescue"] == 2 and seen["db"] is dry
    assert [s["step"] for s in res["steps"]][-3:] == ["research", "learn", "notify"]
    assert all(s["status"] == "done" for s in res["steps"])
    assert res["copied"] == ["watchlist: watchlist-2026-10-16.yaml"]
    assert res["messages"], "the weekly summary is kept, not sent"
    # nothing of the run is in production: no run, no report; the report is copied as a dry report only
    from qt.runs import SCHEMA
    db.ddl(SCHEMA)
    assert db.all("SELECT id FROM runs") == []
    assert Queue(db).latest_upload("report") is None and Queue(db).latest_upload("dossier") is None
    assert Queue(db).latest_upload("dry_report")["id"] == res["report_id"]
    # the universe every stock answered is handed to Saturday's research of the same week
    from qt.research.state import State
    assert State(db, AS_OF).get_blob("stage:universe.pkl") == {"quotes": [1, 2, 3]}
    t = dryrun.text(res)
    assert t.startswith("🧪 ריצת ניסיון 2026-10-23: ✅") and "✅ דוח מלא" in t and "הועבר" in t


def test_a_universe_with_failed_requests_is_not_handed_on(db):
    res = dryrun.run(db, AS_OF, dry=_dry(db), services=_services({}, {"AAA": "yahoo: HTTP 429", "BBB": "HTTP 404"}),
                     checks=_ok)
    assert not res["ok"] and "1 מניות לא ענו" in res["shared"]
    from qt.research.state import State
    assert State(db, AS_OF).get_blob("stage:universe.pkl") is None
    assert "❌ דוח חלקי" in dryrun.text(res)


def test_a_failed_check_or_step_fails_the_dry_run(db):
    bad = lambda: [preflight.Result("Yahoo", False, "HTTP 429")]           # noqa: E731
    res = dryrun.run(db, AS_OF, dry=_dry(db), services=_services({}), checks=bad)
    assert not res["ok"] and "✗ Yahoo: HTTP 429" in dryrun.text(res)

    def no_fred(db):
        s = _services({})(db)
        s.pop("fred")
        return s
    res = dryrun.run(db, AS_OF, dry=_dry(db), services=no_fred, checks=_ok)
    t = dryrun.text(res)
    assert not res["ok"] and "✗ research: DataMissing" in t and "FRED_API_KEY" in t


def test_the_dry_schema_is_emptied_each_time(db):
    dry = _dry(db)
    dryrun.run(db, AS_OF, dry=dry, services=_services({}), checks=_ok)
    dryrun.run(db, AS_OF, dry=dry, services=_services({}), checks=_ok)
    assert len(dry.all("SELECT id FROM runs")) == 1


def test_a_dry_run_never_takes_the_production_tables():
    with pytest.raises(ValueError):
        DB(":memory:", schema="public")
    with pytest.raises(ValueError):
        DB(":memory:").drop_schema()
    with pytest.raises(ValueError):
        dryrun.prepare(DB(":memory:"), DB(":memory:"))


def test_the_dry_run_takes_the_as_of_of_the_coming_saturday():
    assert dryrun._friday(date(2026, 10, 23)) == date(2026, 10, 23)        # Friday evening
    assert dryrun._friday(date(2026, 10, 21)) == date(2026, 10, 23)
    assert dryrun._friday(date(2026, 11, 27)) == date(2026, 11, 27)        # the half day after Thanksgiving trades


def test_preflight_offline_passes_from_the_installed_package():
    res = preflight.run(offline=True)
    assert all(r.ok for r in res), preflight.text(res)
    assert {r.name for r in res} == {"Playbook", "כרטיסים", "policy"}


def test_preflight_reports_each_failure_and_exits_1(monkeypatch):
    monkeypatch.setattr(preflight, "OFFLINE", [("a", lambda: "fine"), ("b", lambda: 1 / 0)])
    res = preflight.run(offline=True)
    assert [r.ok for r in res] == [True, False]
    assert "❌ בדיקת מוכנות: 1 בדיקות נכשלו" in preflight.text(res) and "ZeroDivisionError" in preflight.text(res)
    assert preflight.main(offline=True) == 1


def test_preflight_keys_names_what_is_missing(monkeypatch):
    for k in preflight.WEEKLY_KEYS:
        monkeypatch.setenv(k, "x")
    monkeypatch.delenv("ROIC_API_KEY")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    r = preflight._run("keys", preflight.keys)
    assert not r.ok and "ROIC_API_KEY" in r.detail


def test_completeness_cli(tmp_path):
    import json
    from qt.cli import main
    p = tmp_path / "d.json"
    p.write_text(json.dumps(complete()), encoding="utf-8")
    assert main(["completeness", str(p)]) == 0
    from dossiers import fixture
    p.write_text(json.dumps(fixture()), encoding="utf-8")
    assert main(["completeness", str(p)]) == 1


def test_preflight_fails_when_the_run_would_outgrow_the_subscription(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("QT_DATABASE_URL", raising=False)
    r = preflight._run("עומס מנוי", preflight.subscription)              # 12 deep dives at the first estimate
    assert not r.ok and "המנוי הפסיק לענות" in r.detail
    monkeypatch.setattr(preflight, "_estimate", lambda: (30.0, "x"))
    assert preflight._run("עומס מנוי", preflight.subscription).ok
    monkeypatch.setattr(preflight, "_estimate", lambda: (300.0, "x"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert preflight._run("עומס מנוי", preflight.subscription).ok          # an API key pays per call
