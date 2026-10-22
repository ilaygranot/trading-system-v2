"""Wiring for the cloud: the services a run needs, the scheduled entry point, and failure alerts.

Keys come only from the environment (runtime plan 9). The portfolio and the monthly manual file arrive as
uploads to the bot (a Render cron job has no disk), or from QT_PORTFOLIO for local runs.
"""
from __future__ import annotations

import os
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
import yaml

from qt.approvals import Queue
from qt.db import DB
from qt.runs import AlreadyRunning, Runner


def portfolio(db: DB, path: str | None = None) -> dict | None:
    """An explicit path wins; then the book the last daily run saved (qt.book); then QT_PORTFOLIO; then the last
    portfolio sent to the bot; then an all-cash book of QT_START_EQUITY. Sending a new portfolio file to the bot replaces the book."""
    if path and Path(path).exists():
        return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    up = Queue(db).latest_upload("portfolio")
    if up and up["name"].startswith("book-"):
        return yaml.safe_load(up["content"])
    env = os.environ.get("QT_PORTFOLIO")
    if env and Path(env).exists():
        return yaml.safe_load(Path(env).read_text(encoding="utf-8"))
    if up:
        return yaml.safe_load(up["content"])
    start = os.environ.get("QT_START_EQUITY")                  # nothing sent yet: an all-cash paper book (22.10)
    return start_book(float(start)) if start else None


def start_book(equity: float) -> dict:
    """The paper book before the first fill: all cash, no positions. Used only until a book or portfolio exists,
    so the first daily run can propose entries from the watchlist (22.10)."""
    return {"cash": equity, "peak_equity": equity, "portfolio_stop_triggered": False, "positions": []}


def watchlist(db: DB, path: str | None = None) -> dict | None:
    """The watchlist from the last research run: QT_WATCHLIST for local runs, else the last upload to the bot."""
    path = path or os.environ.get("QT_WATCHLIST")
    if path and Path(path).exists():
        return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    up = Queue(db).latest_upload("watchlist")
    return yaml.safe_load(up["content"]) if up else None


def services(db: DB, portfolio_path: str | None = None) -> dict[str, Any]:
    from qt.data.providers import CFTC, FMP, FRED, Yahoo
    from qt.data.store import Store
    from qt.telegram import Telegram
    store = Store(db)
    yahoo = Yahoo(store)
    s: dict[str, Any] = {"prices": yahoo, "prices_fallback": FMP(store), "quote": yahoo.quote, "cftc": CFTC()}
    pf = portfolio(db, portfolio_path)
    if pf is not None:
        s["portfolio"] = pf
    wl = watchlist(db)
    if wl is not None:
        s["watchlist"] = wl
    if os.environ.get("FRED_API_KEY"):
        s["fred"] = FRED(store)
    from qt.agents.cli_client import agent_client
    client = agent_client()
    if client is not None:
        from qt.agents import AgentRunner
        s["agents"] = AgentRunner(db, client)
    bot = Telegram.from_env()
    if bot is not None:
        s["telegram"] = bot
    return s


def runner(db: DB, svc: dict[str, Any] | None = None) -> Runner:
    from qt.pipelines import PIPELINES
    return Runner(db, PIPELINES, svc if svc is not None else services(db))


def alert_failure(svc: dict[str, Any], run_id: str, error: Exception) -> None:
    """5.2: the step, the error, and a "resume run" button. The monthly wait for the manual file is not a
    failure to alert: the reminder already went out."""
    from qt.pipelines import ManualFileMissing
    bot = svc.get("telegram")
    if bot is None or isinstance(error, ManualFileMissing):
        return
    bot.send(f"⚠️ ריצה {run_id} נכשלה: {type(error).__name__}: {str(error)[:300]}",
             [[{"text": "▶️ המשך ריצה", "callback_data": f"resume:{run_id}"}]])


def ping(kind: str) -> None:
    """Heartbeat (5.2): healthchecks.io alerts when a run did not report in time. Optional."""
    url = os.environ.get(f"HEALTHCHECK_{kind.upper()}")
    if url:
        try:
            requests.get(url, timeout=10)
        except requests.RequestException:
            pass


def scheduled(kind: str, db: DB, svc: dict[str, Any] | None = None, now: datetime | None = None) -> str:
    """The cron entry point. Each cron fires at both UTC candidates (daylight saving); the New York guard lets
    one through. The other firing resumes a failed run of the day once (5.2: one automatic resume)."""
    from qt.schedule import due
    ok, as_of = due(kind, now)
    if not ok:
        return _auto_resume(kind, db, svc, now)
    _schema(db)
    if db.one("SELECT id FROM runs WHERE kind = ? AND as_of = ? AND status = 'done'", (kind, as_of.isoformat())):
        return f"skip: {kind} {as_of} already done"
    svc = svc if svc is not None else services(db)
    r = runner(db, svc)
    try:
        rid = r.start(kind, as_of)
    except AlreadyRunning as e:
        return f"skip: {e}"
    except Exception as e:
        row = db.one("SELECT id FROM runs WHERE kind = ? AND as_of = ? ORDER BY started_at DESC LIMIT 1",
                     (kind, as_of.isoformat()))
        alert_failure(svc, row["id"] if row else kind, e)
        return f"failed: {type(e).__name__}: {e}"
    ping(kind)
    return rid


def _schema(db: DB) -> None:
    from qt.runs import SCHEMA
    db.ddl(SCHEMA)


def _auto_resume(kind: str, db: DB, svc: dict[str, Any] | None, now: datetime | None) -> str:
    _schema(db)
    since = ((now or datetime.now(timezone.utc)) - timedelta(hours=12)).isoformat(timespec="seconds")
    row = db.one("SELECT id FROM runs WHERE kind = ? AND status = 'failed' AND resumes = 0 AND started_at >= ? "
                 "ORDER BY started_at DESC LIMIT 1", (kind, since))
    if row is None:
        return f"not due: {kind}"
    svc = svc if svc is not None else services(db)
    try:
        rid = runner(db, svc).resume(row["id"])
    except AlreadyRunning as e:
        return f"skip: {e}"
    except Exception as e:
        alert_failure(svc, row["id"], e)
        return f"failed again: {type(e).__name__}: {e}"
    ping(kind)
    return f"resumed {rid}"


def resume_in_background(db_url: str, run_id: str) -> threading.Thread:
    """For the web service: the bot's "resume run" button and the upload of a missing file."""
    def go():
        db = DB(db_url)
        svc = services(db)
        try:
            runner(db, svc).resume(run_id)
        except AlreadyRunning:
            pass
        except Exception as e:                                      # noqa: BLE001 - reported to Ilay
            alert_failure(svc, run_id, e)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


def waiting_monthly(db: DB) -> str | None:
    _schema(db)
    row = db.one("SELECT id FROM runs WHERE kind = 'monthly' AND status = 'failed' ORDER BY started_at DESC LIMIT 1")
    return row["id"] if row else None


def as_of_today() -> date:
    from qt.schedule import ny
    return ny().date()
