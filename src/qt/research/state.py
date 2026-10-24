"""What a weekly research has finished, in the database, so a run that stops (the agents' budget, a crash, a new
container) continues where it stopped instead of starting again (Ilay 24.10: "חיוני הכל מחדש?").

Keyed by the week (as_of), not the run: a new run for the same week continues too. The stages (the universe, the
screens, the traffic light) and each agent piece (A1, the DC-09 pick, each idea's cards, each article) are saved as
they finish. QT_RESEARCH_FRESH=1 starts the week from nothing.
"""
from __future__ import annotations

import base64
import json
import os
import pickle
import zlib
from datetime import date, datetime, timezone
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS research_state (
  as_of TEXT NOT NULL,
  key TEXT NOT NULL,
  content TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (as_of, key)
);
"""


class State:
    def __init__(self, db, as_of: date):
        self.db, self.as_of = db, as_of.isoformat()
        db.ddl(SCHEMA)

    def _raw(self, key: str) -> str | None:
        row = self.db.one("SELECT content FROM research_state WHERE as_of = ? AND key = ?", (self.as_of, key))
        return row["content"] if row else None

    def _put_raw(self, key: str, content: str) -> None:
        self.db.upsert("research_state", dict(as_of=self.as_of, key=key, content=content,
                                              updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds")),
                       ["as_of", "key"])

    def get(self, key: str) -> Any:
        raw = self._raw(key)
        return None if raw is None else json.loads(raw)

    def put(self, key: str, value: Any) -> None:
        self._put_raw(key, json.dumps(value, default=str, ensure_ascii=False))

    def get_blob(self, key: str) -> Any:
        raw = self._raw(key)
        return None if raw is None else pickle.loads(zlib.decompress(base64.b64decode(raw)))

    def put_blob(self, key: str, value: Any) -> None:
        self._put_raw(key, base64.b64encode(zlib.compress(pickle.dumps(value), 6)).decode())

    def keys(self) -> list[str]:
        return [r["key"] for r in self.db.all("SELECT key FROM research_state WHERE as_of = ? ORDER BY key", (self.as_of,))]

    def clear(self) -> None:
        self.db.execute("DELETE FROM research_state WHERE as_of = ?", (self.as_of,))


def fresh() -> bool:
    return os.environ.get("QT_RESEARCH_FRESH", "").strip().lower() in ("1", "true", "yes")


def seed_from_dossier(state: State, dossier: dict | None) -> list[str]:
    """A week's research that ran before this state existed (or whose state is gone) continues from its dossier:
    A1, the DC-09 pick and each idea's cards; what the budget cut (the ideas in errors, the articles) runs again.
    Returns the ideas taken over."""
    if not dossier or (dossier.get("meta") or {}).get("as_of") != state.as_of or state.keys():
        return []
    J = dossier.get("judgments") or {}
    if J.get("A1"):
        state.put("agents:A1", J["A1"])
    sel = dict(J.get("A3") or {})
    if sel.get("picked"):
        sel.pop("cut_by_budget", None)
        sel["skipped"] = {s: r for s, r in (sel.get("skipped") or {}).items() if s not in sel["picked"]}
        state.put("agents:A3", sel)
    done = []
    for sym, i in (dossier.get("ideas") or {}).items():
        j = dict(i.get("judgment") or {})
        if not j or sym in (J.get("errors") or {}):
            continue
        state.put(f"agents:idea:{sym}", {"j": j, "world": None})
        if j.get("article"):
            state.put(f"agents:article:{sym}", {"article": j["article"]})
        done.append(sym)
    return done
