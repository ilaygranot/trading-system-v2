"""One database layer for both backends: Postgres in the cloud (runtime plan 3.3), SQLite for tests and
local work. QT_DATABASE_URL picks the backend: postgresql://... or a SQLite path (default in-memory).

Queries are written with `?` placeholders; schema templates use {ID} (auto id), {JSON} (TEXT / JSONB),
{REAL} and {TS}. JSON columns come back decoded on both backends.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

TOKENS = {
    "sqlite": {"{ID}": "INTEGER PRIMARY KEY AUTOINCREMENT", "{JSON}": "TEXT", "{REAL}": "REAL", "{TS}": "TEXT"},
    "postgres": {"{ID}": "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY", "{JSON}": "JSONB",
                 "{REAL}": "DOUBLE PRECISION", "{TS}": "TEXT"},
}


def is_postgres(url: str) -> bool:
    return url.startswith(("postgres://", "postgresql://"))


class DB:
    def __init__(self, url: str | Path | None = None):
        url = str(url if url is not None else os.environ.get("QT_DATABASE_URL", ":memory:"))
        self.url = url
        if is_postgres(url):
            import psycopg
            from psycopg.rows import dict_row
            self._connect = lambda: psycopg.connect(url, row_factory=dict_row, autocommit=True, keepalives=1,
                                                    keepalives_idle=30, keepalives_interval=10, keepalives_count=5)
            self.conn = self._connect()
            self.dialect = "postgres"
            self.error: tuple[type[Exception], ...] = (psycopg.Error,)
        else:
            path = url.removeprefix("sqlite:///").removeprefix("sqlite://")
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(path)
            self.conn.row_factory = sqlite3.Row
            self.dialect = "sqlite"
            self.error = (sqlite3.DatabaseError,)

    @property
    def pg(self) -> bool:
        return self.dialect == "postgres"

    def ddl(self, template: str) -> None:
        for k, v in TOKENS[self.dialect].items():
            template = template.replace(k, v)
        if self.pg:
            self.conn.execute(template)
        else:
            self.conn.executescript(template)

    def _sql(self, q: str) -> str:
        return q.replace("?", "%s") if self.pg else q

    def execute(self, q: str, params: Sequence[Any] = ()) -> Any:
        try:
            cur = self.conn.execute(self._sql(q), tuple(params))
        except self.error:
            if not (self.pg and self.conn.closed):
                raise
            self.conn = self._connect()               # 24.10: the server dropped an idle connection mid-run
            cur = self.conn.execute(self._sql(q), tuple(params))
        if not self.pg:
            self.conn.commit()
        return cur

    def all(self, q: str, params: Sequence[Any] = (), json_cols: Iterable[str] = ()) -> list[dict]:
        return [decode(dict(r), json_cols) for r in self.execute(q, params).fetchall()]

    def one(self, q: str, params: Sequence[Any] = (), json_cols: Iterable[str] = ()) -> dict | None:
        r = self.execute(q, params).fetchone()
        return decode(dict(r), json_cols) if r is not None else None

    def insert(self, table: str, row: Mapping[str, Any], returning: str | None = "id") -> int | None:
        cols = ",".join(row)
        q = f"INSERT INTO {table} ({cols}) VALUES ({','.join('?' * len(row))})"
        if self.pg and returning:
            return self.execute(q + f" RETURNING {returning}", list(row.values())).fetchone()[returning]
        cur = self.execute(q, list(row.values()))
        return int(cur.lastrowid) if returning else None

    def upsert(self, table: str, row: Mapping[str, Any], keys: Sequence[str]) -> None:
        cols = ",".join(row)
        sets = ",".join(f"{c}=excluded.{c}" for c in row if c not in keys) or f"{keys[0]}=excluded.{keys[0]}"
        self.execute(f"INSERT INTO {table} ({cols}) VALUES ({','.join('?' * len(row))}) "
                     f"ON CONFLICT ({','.join(keys)}) DO UPDATE SET {sets}", list(row.values()))

    def insert_new(self, table: str, row: Mapping[str, Any], keys: Sequence[str]) -> bool:
        """Insert unless the key exists (idempotent steps, runtime plan 5.1 rule 3). True if inserted."""
        cols = ",".join(row)
        cur = self.execute(f"INSERT INTO {table} ({cols}) VALUES ({','.join('?' * len(row))}) "
                           f"ON CONFLICT ({','.join(keys)}) DO NOTHING", list(row.values()))
        return cur.rowcount == 1


def dumps(v: Any) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False, default=str)


def decode(row: dict, json_cols: Iterable[str]) -> dict:
    for c in json_cols:
        if isinstance(row.get(c), str):
            row[c] = json.loads(row[c])
    return row
