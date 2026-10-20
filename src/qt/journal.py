"""Decision journal (architecture 7.1). Append-only SQLite.

Every decision, by code or agent, records: what was decided, on what basis (fact sheet, playbook and
prompt versions, model), why (reasoning, cited rules, confidence, deviations), what the Critic said,
what Ilay decided, and a measurable prediction with a check date. Outcomes are written later by code.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  decided_by TEXT NOT NULL,              -- code | agent:A6 | ilay
  card_id TEXT,                          -- decision card (DC-xx) or rule id
  subject TEXT NOT NULL,                 -- e.g. BA, macro, portfolio
  action TEXT,                           -- enter | add | exit | reduce | classify | ...
  choice TEXT NOT NULL,
  options_considered TEXT,               -- JSON list
  status TEXT,                           -- engine verdict status
  findings TEXT,                         -- JSON list of findings
  facts TEXT,                            -- JSON fact sheet snapshot
  reasoning TEXT,
  cited_rules TEXT,                      -- JSON list
  confidence TEXT,                       -- low | medium | high
  deviations TEXT,                       -- JSON list of overrides (deviation from default)
  playbook_version TEXT NOT NULL,
  prompt_version TEXT,
  model TEXT,
  critic TEXT,
  prediction TEXT,                       -- measurable statement
  check_on TEXT                          -- ISO date to measure the prediction
);
CREATE TABLE IF NOT EXISTS ilay_reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  decision_id INTEGER NOT NULL REFERENCES decisions(id),
  created_at TEXT NOT NULL,
  verdict TEXT NOT NULL CHECK (verdict IN ('approved', 'modified', 'rejected')),
  note TEXT
);
CREATE TABLE IF NOT EXISTS outcomes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  decision_id INTEGER NOT NULL REFERENCES decisions(id),
  measured_at TEXT NOT NULL,
  result TEXT NOT NULL,                  -- JSON: hit/miss, R multiple, MFE/MAE, ...
  correct INTEGER                        -- 1 / 0 / NULL (undetermined)
);
CREATE TRIGGER IF NOT EXISTS decisions_no_update BEFORE UPDATE ON decisions
BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_delete BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;
"""


@dataclass
class Decision:
    decided_by: str
    subject: str
    choice: str
    playbook_version: str
    card_id: str | None = None
    action: str | None = None
    options_considered: list[str] = field(default_factory=list)
    status: str | None = None
    findings: list[dict] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)
    reasoning: str = ""
    cited_rules: list[str] = field(default_factory=list)
    confidence: str | None = None
    deviations: list[dict] = field(default_factory=list)
    prompt_version: str | None = None
    model: str | None = None
    critic: str | None = None
    prediction: str | None = None
    check_on: date | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Journal:
    def __init__(self, path: str | Path = ":memory:"):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def record(self, d: Decision) -> int:
        row = dict(
            created_at=_now(), decided_by=d.decided_by, card_id=d.card_id, subject=d.subject, action=d.action,
            choice=d.choice, options_considered=json.dumps(d.options_considered, ensure_ascii=False),
            status=d.status, findings=json.dumps(d.findings, ensure_ascii=False, default=str),
            facts=json.dumps(d.facts, ensure_ascii=False, default=str), reasoning=d.reasoning,
            cited_rules=json.dumps(d.cited_rules), confidence=d.confidence,
            deviations=json.dumps(d.deviations, ensure_ascii=False, default=str),
            playbook_version=d.playbook_version, prompt_version=d.prompt_version, model=d.model,
            critic=d.critic, prediction=d.prediction, check_on=d.check_on.isoformat() if d.check_on else None)
        cur = self.db.execute(f"INSERT INTO decisions ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                              list(row.values()))
        self.db.commit()
        return int(cur.lastrowid)

    def review(self, decision_id: int, verdict: str, note: str = "") -> None:
        self.db.execute("INSERT INTO ilay_reviews (decision_id, created_at, verdict, note) VALUES (?,?,?,?)",
                        (decision_id, _now(), verdict, note))
        self.db.commit()

    def record_outcome(self, decision_id: int, result: dict[str, Any], correct: bool | None) -> None:
        self.db.execute("INSERT INTO outcomes (decision_id, measured_at, result, correct) VALUES (?,?,?,?)",
                        (decision_id, _now(), json.dumps(result, default=str), None if correct is None else int(correct)))
        self.db.commit()

    def due_predictions(self, on: date) -> list[sqlite3.Row]:
        """Decisions whose prediction check date has arrived and that have no outcome yet."""
        return self.db.execute(
            "SELECT * FROM decisions d WHERE prediction IS NOT NULL AND check_on <= ? "
            "AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.decision_id = d.id) ORDER BY check_on",
            (on.isoformat(),)).fetchall()

    def get(self, decision_id: int) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()

    def deviation_success(self) -> list[sqlite3.Row]:
        """Per-rule hit rate of deviations from default: the metric that shows if flexibility works (7.3)."""
        return self.db.execute(
            "SELECT json_extract(dev.value, '$.rule_id') AS rule_id, COUNT(*) AS n, "
            "SUM(o.correct) AS correct FROM decisions d, json_each(d.deviations) dev "
            "JOIN outcomes o ON o.decision_id = d.id GROUP BY rule_id").fetchall()


def decision_from_verdict(verdict, subject: str, playbook_version: str, decided_by: str = "code",
                          facts: list[dict] | None = None, **kw: Any) -> Decision:
    """Journal entry for a rules-engine verdict."""
    findings = [dict(rule_id=f.rule_id, severity=f.severity.value, message=f.message, data=f.data)
                for f in verdict.findings]
    deviations = [dict(rule_id=f.rule_id, principle=f.override.principle, fact_ids=list(f.override.fact_ids),
                       reason=f.override.reason) for f in verdict.deviations]
    return Decision(decided_by=decided_by, subject=subject, choice=verdict.status.value, action=verdict.action,
                    status=verdict.status.value, findings=findings, deviations=deviations,
                    cited_rules=sorted(verdict.rule_ids()), facts=facts or [], playbook_version=playbook_version, **kw)
