"""Local data store: HTTP response cache plus point-in-time snapshots.

Providers do not keep point-in-time analyst estimates (data-trial lesson 2) and FRED keeps only 3 years
of HY spread (lesson 3), so we snapshot those ourselves from day one.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import date
from pathlib import Path
from typing import Any

from qt.db import DB, dumps

SCHEMA = """
CREATE TABLE IF NOT EXISTS http_cache (
  key TEXT PRIMARY KEY, url TEXT NOT NULL, fetched_at {REAL} NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots (
  kind TEXT NOT NULL,          -- estimates | short_interest | hy_spread | zacks_screen | ...
  subject TEXT NOT NULL,       -- ticker or series id
  as_of TEXT NOT NULL,         -- date the values were known
  source TEXT NOT NULL,
  payload {JSON} NOT NULL,
  PRIMARY KEY (kind, subject, as_of, source));
"""


def default_path() -> Path:
    return Path(os.environ.get("QT_DATA_DIR", Path.home() / ".qt")) / "store.sqlite"


class Store:
    """On QT_DATABASE_URL when set (Postgres in the cloud), else a local SQLite file."""

    def __init__(self, path: str | Path | DB | None = None):
        if isinstance(path, DB):
            self.db = path
        else:
            self.db = DB(path if path is not None else os.environ.get("QT_DATABASE_URL", default_path()))
        self.db.ddl(SCHEMA)

    @staticmethod
    def _key(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()

    def cached(self, url: str, max_age_s: float) -> Any | None:
        row = self.db.one("SELECT fetched_at, body FROM http_cache WHERE key = ?", (self._key(url),))
        if row and time.time() - row["fetched_at"] <= max_age_s:
            return json.loads(row["body"])
        return None

    def put(self, url: str, body: Any) -> None:
        self.db.upsert("http_cache", dict(key=self._key(url), url=url, fetched_at=time.time(), body=json.dumps(body)),
                       ["key"])

    def snapshot(self, kind: str, subject: str, as_of: date, source: str, payload: Any) -> None:
        self.db.upsert("snapshots", dict(kind=kind, subject=subject, as_of=as_of.isoformat(), source=source,
                                         payload=dumps(payload)), ["kind", "subject", "as_of", "source"])

    def snapshot_as_of(self, kind: str, subject: str, as_of: date) -> tuple[str, str, Any] | None:
        """Latest snapshot known on `as_of` (no look-ahead)."""
        row = self.db.one(
            "SELECT as_of, source, payload FROM snapshots WHERE kind=? AND subject=? AND as_of<=? "
            "ORDER BY as_of DESC LIMIT 1", (kind, subject, as_of.isoformat()), ["payload"])
        return (row["as_of"], row["source"], row["payload"]) if row else None
