"""Local data store: HTTP response cache plus point-in-time snapshots.

Providers do not keep point-in-time analyst estimates (data-trial lesson 2) and FRED keeps only 3 years
of HY spread (lesson 3), so we snapshot those ourselves from day one.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS http_cache (
  key TEXT PRIMARY KEY, url TEXT NOT NULL, fetched_at REAL NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots (
  kind TEXT NOT NULL,          -- estimates | short_interest | hy_spread | zacks_screen | ...
  subject TEXT NOT NULL,       -- ticker or series id
  as_of TEXT NOT NULL,         -- date the values were known
  source TEXT NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (kind, subject, as_of, source));
"""


def default_path() -> Path:
    return Path(os.environ.get("QT_DATA_DIR", Path.home() / ".qt")) / "store.sqlite"


class Store:
    def __init__(self, path: str | Path | None = None):
        p = Path(path) if path else default_path()
        if str(p) != ":memory:":
            p.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(p))
        self.db.executescript(SCHEMA)

    @staticmethod
    def _key(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()

    def cached(self, url: str, max_age_s: float) -> Any | None:
        row = self.db.execute("SELECT fetched_at, body FROM http_cache WHERE key = ?", (self._key(url),)).fetchone()
        if row and time.time() - row[0] <= max_age_s:
            return json.loads(row[1])
        return None

    def put(self, url: str, body: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO http_cache VALUES (?,?,?,?)",
                        (self._key(url), url, time.time(), json.dumps(body)))
        self.db.commit()

    def snapshot(self, kind: str, subject: str, as_of: date, source: str, payload: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?)",
                        (kind, subject, as_of.isoformat(), source, json.dumps(payload, default=str)))
        self.db.commit()

    def snapshot_as_of(self, kind: str, subject: str, as_of: date) -> tuple[str, str, Any] | None:
        """Latest snapshot known on `as_of` (no look-ahead)."""
        row = self.db.execute(
            "SELECT as_of, source, payload FROM snapshots WHERE kind=? AND subject=? AND as_of<=? "
            "ORDER BY as_of DESC LIMIT 1", (kind, subject, as_of.isoformat())).fetchone()
        return (row[0], row[1], json.loads(row[2])) if row else None
