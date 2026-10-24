"""Rule tiers from the Playbook (playbook/tiers/*.csv).

Every R rule has a tier: hard (code blocks), default (code flags), judgment (agent decides)
or out-of-v1. All J rules are judgment. Rules missing from the CSVs are `default` (architecture 3.4).
_fixes.csv overrides the chapter files. _scope.csv holds the v1 scope rule (decision 1); it lives in this repo.
"""
from __future__ import annotations

import csv
import hashlib
import os
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path

def _default_tiers_dir() -> Path:
    """The snapshot ships inside the package (qt/playbook_snapshot, package data), so an editable install, a regular
    install (Render) and any working directory read the same files (24.10: it was found from the checkout only, and
    on Render it loaded empty)."""
    return Path(__file__).resolve().parent / "playbook_snapshot"


DEFAULT_TIERS_DIR = _default_tiers_dir()


class Tier(str, Enum):
    HARD = "hard"
    DEFAULT = "default"
    JUDGMENT = "judgment"
    OUT_OF_V1 = "out-of-v1"


@dataclass(frozen=True)
class Rule:
    rule_id: str
    tier: Tier
    reason: str = ""
    course_wording: str = ""


class Playbook:
    def __init__(self, rules: dict[str, Rule], version: str):
        self.rules = rules
        self.version = version

    def tier(self, rule_id: str) -> Tier:
        if "-J" in rule_id:
            return Tier.JUDGMENT
        rule = self.rules.get(rule_id)
        return rule.tier if rule else Tier.DEFAULT

    def is_hard(self, rule_id: str) -> bool:
        return self.tier(rule_id) is Tier.HARD

    def hard_rules(self) -> list[str]:
        return sorted(r for r, rule in self.rules.items() if rule.tier is Tier.HARD)


def _tiers_dir() -> Path:
    return Path(os.environ.get("QT_TIERS_DIR", DEFAULT_TIERS_DIR))


@lru_cache(maxsize=None)
def load_playbook(tiers_dir: str | None = None) -> Playbook:
    d = Path(tiers_dir) if tiers_dir else _tiers_dir()
    files = sorted(d.glob("S*.csv")) + [d / "_fixes.csv", d / "_scope.csv"]
    if not d.is_dir() or not any(d.glob("S*.csv")):
        raise FileNotFoundError(f"no Playbook snapshot at {d}: set QT_TIERS_DIR or run from the repo checkout")
    rules: dict[str, Rule] = {}
    digest = hashlib.sha256()
    for path in files:
        if not path.exists():
            continue
        raw = path.read_bytes()
        digest.update(raw)
        for row in csv.DictReader(raw.decode("utf-8").splitlines()):
            rid = row["rule_id"].strip()
            rules[rid] = Rule(rid, Tier(row["tier"].strip()), row.get("reason", ""), row.get("course_wording", ""))
    return Playbook(rules, version=digest.hexdigest()[:12])
