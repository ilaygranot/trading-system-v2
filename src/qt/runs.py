"""Runs and steps with resume (runtime plan 5).

A run has an id, a kind (daily / weekly / monthly) and an as-of date, and is a list of steps. Three rules
make it resumable:
1. every step saves its output before the next one starts; a finished step never runs again on resume;
2. data is fetched once per run: the first step saves a snapshot and every later step reads it;
3. every step is safe to run again (unique key run + step + subject): no duplicate queue item or decision.
Only one run of each kind at a time: a second start exits at once (the lock).
"""
from __future__ import annotations

import json
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Sequence

from qt.db import DB, dumps

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                    -- daily | weekly | monthly | premarket
  as_of TEXT NOT NULL,
  status TEXT NOT NULL,                  -- running | failed | done
  started_at {TS} NOT NULL,
  finished_at {TS},
  error TEXT,
  resumes INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS run_steps (
  run_id TEXT NOT NULL REFERENCES runs(id),
  step TEXT NOT NULL,
  subject TEXT NOT NULL DEFAULT '',      -- ticker / position, '' for a whole-run step
  seq INTEGER NOT NULL,
  status TEXT NOT NULL,                  -- running | failed | done
  started_at {TS},
  finished_at {TS},
  attempts INTEGER NOT NULL DEFAULT 0,
  output {JSON},
  error TEXT,
  PRIMARY KEY (run_id, step, subject)
);
CREATE TABLE IF NOT EXISTS run_events (
  id {ID},
  run_id TEXT NOT NULL,
  at {TS} NOT NULL,
  level TEXT NOT NULL,                   -- info | warn | error
  step TEXT,
  message TEXT NOT NULL,
  data {JSON}
);
CREATE TABLE IF NOT EXISTS agent_calls (
  id {ID},
  run_id TEXT,
  step TEXT,
  subject TEXT,
  agent TEXT NOT NULL,                   -- A1..A8, C, L
  model TEXT NOT NULL,
  playbook_version TEXT,
  prompt_version TEXT,
  input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
  cost_usd {REAL},
  duration_ms INTEGER,
  stop_reason TEXT,
  output {JSON},
  valid INTEGER,                         -- output passed the card schema
  critic TEXT,
  created_at {TS} NOT NULL
);
CREATE TABLE IF NOT EXISTS run_locks (
  kind TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  acquired_at {TS} NOT NULL
);
"""

LOCK_STALE = timedelta(hours=6)       # a lock older than this is a crashed run (not from course)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AlreadyRunning(RuntimeError):
    pass


@dataclass
class Step:
    name: str
    fn: Callable[["RunContext", str], Any]                       # (ctx, subject) -> JSON-able output
    subjects: Callable[["RunContext"], Sequence[str]] | None = None   # one sub-step per subject (e.g. per ticker)


@dataclass
class RunContext:
    db: DB
    run_id: str
    kind: str
    as_of: date
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)   # step -> subject -> output
    services: dict[str, Any] = field(default_factory=dict)              # providers, clients (not persisted)

    def out(self, step: str, subject: str = "") -> Any:
        return self.outputs.get(step, {}).get(subject)

    def event(self, message: str, level: str = "info", step: str | None = None, data: Any = None) -> None:
        self.db.insert("run_events", dict(run_id=self.run_id, at=_now(), level=level, step=step, message=message,
                                          data=dumps(data)))


class Runner:
    def __init__(self, db: DB, pipelines: dict[str, Sequence[Step]], services: dict[str, Any] | None = None):
        self.db = db
        self.db.ddl(SCHEMA)
        self.pipelines = pipelines
        self.services = services or {}

    # ------------------------------------------------------------ lock
    def _lock(self, kind: str, run_id: str) -> None:
        row = self.db.one("SELECT run_id, acquired_at FROM run_locks WHERE kind = ?", (kind,))
        if row:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(row["acquired_at"])
            if age < LOCK_STALE and row["run_id"] != run_id:
                raise AlreadyRunning(f"{kind} run {row['run_id']} is still running")
            self.db.execute("DELETE FROM run_locks WHERE kind = ?", (kind,))
        if not self.db.insert_new("run_locks", dict(kind=kind, run_id=run_id, acquired_at=_now()), ["kind"]):
            raise AlreadyRunning(f"{kind} run started concurrently")

    def _unlock(self, kind: str, run_id: str) -> None:
        self.db.execute("DELETE FROM run_locks WHERE kind = ? AND run_id = ?", (kind, run_id))

    # ------------------------------------------------------------ public
    def start(self, kind: str, as_of: date) -> str:
        """Run (or run again) a pipeline for a day. Returns the run id; raises on failure after recording it."""
        run_id = f"{kind}-{as_of.isoformat()}-{uuid.uuid4().hex[:6]}"
        self._lock(kind, run_id)
        self.db.insert("runs", dict(id=run_id, kind=kind, as_of=as_of.isoformat(), status="running",
                                    started_at=_now()), returning=None)
        return self._execute(run_id)

    def resume(self, run_id: str) -> str:
        run = self.db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if run is None:
            raise KeyError(run_id)
        if run["status"] == "done":
            return run_id
        self._lock(run["kind"], run_id)
        self.db.execute("UPDATE runs SET status='running', error=NULL, resumes=resumes+1 WHERE id = ?", (run_id,))
        return self._execute(run_id)

    def status(self, limit: int = 10) -> list[dict]:
        runs = self.db.all("SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT ?", (limit,))
        for r in runs:
            r["steps"] = self.db.all("SELECT step, subject, status, attempts, error FROM run_steps WHERE run_id = ? "
                                     "ORDER BY seq, subject", (r["id"],))
        return runs

    # ------------------------------------------------------------ the loop
    def _execute(self, run_id: str) -> str:
        run = self.db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        ctx = RunContext(self.db, run_id, run["kind"], date.fromisoformat(run["as_of"]), services=self.services)
        for r in self.db.all("SELECT step, subject, output FROM run_steps WHERE run_id = ? AND status = 'done'",
                             (run_id,), ["output"]):
            ctx.outputs.setdefault(r["step"], {})[r["subject"]] = r["output"]
        try:
            for seq, step in enumerate(self.pipelines[run["kind"]]):
                subjects = list(step.subjects(ctx)) if step.subjects else [""]
                for subject in subjects:
                    if subject in ctx.outputs.get(step.name, {}):
                        continue                                      # rule 1: done steps never run again
                    self._run_step(ctx, seq, step, subject)
        except Exception as e:
            self.db.execute("UPDATE runs SET status='failed', error=? WHERE id = ?", (f"{type(e).__name__}: {e}", run_id))
            ctx.event(f"run failed: {e}", "error")
            self._unlock(run["kind"], run_id)
            raise
        self.db.execute("UPDATE runs SET status='done', finished_at=? WHERE id = ?", (_now(), run_id))
        ctx.event("run done")
        self._unlock(run["kind"], run_id)
        return run_id

    def _run_step(self, ctx: RunContext, seq: int, step: Step, subject: str) -> None:
        key = (ctx.run_id, step.name, subject)
        self.db.insert_new("run_steps", dict(run_id=ctx.run_id, step=step.name, subject=subject, seq=seq,
                                             status="running"), ["run_id", "step", "subject"])
        self.db.execute("UPDATE run_steps SET status='running', started_at=?, attempts=attempts+1, error=NULL "
                        "WHERE run_id=? AND step=? AND subject=?", (_now(), *key))
        t0 = time.monotonic()
        try:
            out = step.fn(ctx, subject)
            json.dumps(out, default=str)                              # must be storable before we go on
        except Exception as e:
            self.db.execute("UPDATE run_steps SET status='failed', error=? WHERE run_id=? AND step=? AND subject=?",
                            (f"{type(e).__name__}: {e}", *key))
            ctx.event(f"step {step.name} failed", "error", step.name,
                      {"subject": subject, "trace": traceback.format_exc(limit=5)})
            raise
        self.db.execute("UPDATE run_steps SET status='done', finished_at=?, output=? WHERE run_id=? AND step=? AND subject=?",
                        (_now(), dumps(out), *key))
        ctx.outputs.setdefault(step.name, {})[subject] = json.loads(dumps(out)) if out is not None else None
        ctx.event(f"step {step.name} done", step=step.name,
                  data={"subject": subject, "ms": int((time.monotonic() - t0) * 1000)})
