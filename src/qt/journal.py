"""Decision journal (architecture 7.1). Append-only, on Postgres or SQLite (qt.db).

Every decision, by code or agent, records: what was decided, on what basis (fact sheet, playbook and
prompt versions, model), why (reasoning, cited rules, confidence, deviations), what the Critic said,
what Ilay decided, and a measurable prediction with a check date. Outcomes are written later by code.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from qt.db import DB, dumps

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
  id {ID},
  created_at {TS} NOT NULL,
  decided_by TEXT NOT NULL,              -- code | agent:A6 | ilay
  card_id TEXT,                          -- decision card (DC-xx) or rule id
  subject TEXT NOT NULL,                 -- e.g. BA, macro, portfolio
  action TEXT,                           -- enter | add | exit | reduce | classify | ...
  choice TEXT NOT NULL,
  options_considered {JSON},
  status TEXT,                           -- engine verdict status
  findings {JSON},
  facts {JSON},                          -- fact sheet snapshot
  reasoning TEXT,
  cited_rules {JSON},
  confidence TEXT,                       -- low | medium | high
  deviations {JSON},                     -- overrides (deviation from default)
  playbook_version TEXT NOT NULL,
  prompt_version TEXT,
  model TEXT,
  critic TEXT,
  prediction TEXT,                       -- measurable statement
  check_on TEXT,                         -- ISO date to measure the prediction
  run_id TEXT                            -- the run that made it (runtime plan 5)
);
CREATE TABLE IF NOT EXISTS ilay_reviews (
  id {ID},
  decision_id BIGINT NOT NULL REFERENCES decisions(id),
  created_at {TS} NOT NULL,
  verdict TEXT NOT NULL CHECK (verdict IN ('approved', 'modified', 'rejected', 'not_reviewed', 'changed')),
  note TEXT
);
CREATE TABLE IF NOT EXISTS outcomes (
  id {ID},
  decision_id BIGINT NOT NULL REFERENCES decisions(id),
  measured_at {TS} NOT NULL,
  result {JSON} NOT NULL,                -- hit/miss, R multiple, MFE/MAE, ...
  correct INTEGER                        -- 1 / 0 / NULL (undetermined)
);
"""

APPEND_ONLY = {
    "sqlite": """
CREATE TRIGGER IF NOT EXISTS decisions_no_update BEFORE UPDATE ON decisions
BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_delete BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;
""",
    "postgres": """
CREATE OR REPLACE FUNCTION qt_append_only() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'journal is append-only'; END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS decisions_append_only ON decisions;
CREATE TRIGGER decisions_append_only BEFORE UPDATE OR DELETE ON decisions
FOR EACH ROW EXECUTE FUNCTION qt_append_only();
""",
}

JSON_COLS = ("options_considered", "findings", "facts", "cited_rules", "deviations")


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
    """Append-only on both backends: `db` is a qt.db.DB, a URL or a SQLite path (default in-memory)."""

    def __init__(self, db: DB | str | Path | None = ":memory:"):
        self.db = db if isinstance(db, DB) else DB(db)
        self.db.ddl(SCHEMA)
        self.db.ddl(APPEND_ONLY[self.db.dialect])

    def record(self, d: Decision, run_id: str | None = None) -> int:
        row = dict(
            created_at=_now(), decided_by=d.decided_by, card_id=d.card_id, subject=d.subject, action=d.action,
            choice=d.choice, options_considered=dumps(d.options_considered), status=d.status,
            findings=dumps(d.findings), facts=dumps(d.facts), reasoning=d.reasoning,
            cited_rules=dumps(d.cited_rules), confidence=d.confidence, deviations=dumps(d.deviations),
            playbook_version=d.playbook_version, prompt_version=d.prompt_version, model=d.model,
            critic=d.critic, prediction=d.prediction, check_on=d.check_on.isoformat() if d.check_on else None,
            run_id=run_id)
        return self.db.insert("decisions", row)

    def review(self, decision_id: int, verdict: str, note: str = "") -> None:
        self.db.insert("ilay_reviews", dict(decision_id=decision_id, created_at=_now(), verdict=verdict, note=note))

    def record_outcome(self, decision_id: int, result: dict[str, Any], correct: bool | None) -> None:
        self.db.insert("outcomes", dict(decision_id=decision_id, measured_at=_now(), result=dumps(result),
                                        correct=None if correct is None else int(correct)))

    def due_predictions(self, on: date) -> list[dict]:
        """Decisions whose prediction check date has arrived and that have no outcome yet."""
        return self.db.all(
            "SELECT * FROM decisions d WHERE prediction IS NOT NULL AND check_on <= ? "
            "AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.decision_id = d.id) ORDER BY check_on",
            (on.isoformat(),), JSON_COLS)

    def get(self, decision_id: int) -> dict | None:
        return self.db.one("SELECT * FROM decisions WHERE id = ?", (decision_id,), JSON_COLS)

    def deviation_success(self) -> list[dict]:
        """Per-rule hit rate of deviations from default: the metric that shows if flexibility works (7.3)."""
        if self.db.pg:
            q = ("SELECT dev->>'rule_id' AS rule_id, COUNT(*) AS n, SUM(o.correct) AS correct "
                 "FROM decisions d CROSS JOIN LATERAL jsonb_array_elements(d.deviations) dev "
                 "JOIN outcomes o ON o.decision_id = d.id GROUP BY 1")
        else:
            q = ("SELECT json_extract(dev.value, '$.rule_id') AS rule_id, COUNT(*) AS n, "
                 "SUM(o.correct) AS correct FROM decisions d, json_each(d.deviations) dev "
                 "JOIN outcomes o ON o.decision_id = d.id GROUP BY rule_id")
        return self.db.all(q)


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
