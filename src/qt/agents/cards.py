"""Decision cards: the closed options per card and the output schema built from them."""
from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

HERE = Path(__file__).parent
CONFIDENCE = ["low", "medium", "high"]


@dataclass(frozen=True)
class Card:
    card_id: str
    agent: str
    options: tuple[str, ...]
    undecided: str
    approval: str                      # required | flag | journal
    extra: dict = field(default_factory=dict, hash=False)
    text: str = ""                     # the full card from decision_cards.md

    def schema(self) -> dict:
        """JSON schema for the structured output (runtime plan 8.4: a card becomes a schema)."""
        props = {
            "option": {"type": "string", "enum": list(self.options)},
            "confidence": {"type": "string", "enum": CONFIDENCE},
            "reasoning": {"type": "string", "description": "3-6 sentences"},
            "fact_ids": {"type": "array", "items": {"type": "string"}},
            "cited_rules": {"type": "array", "items": {"type": "string"}},
            "deviations": {"type": "array", "items": {
                "type": "object",
                "properties": {"rule_id": {"type": "string"}, "principle": {"type": "string"},
                               "fact_ids": {"type": "array", "items": {"type": "string"}},
                               "reason": {"type": "string"}},
                "required": ["rule_id", "principle", "fact_ids", "reason"], "additionalProperties": False}},
            "missing_facts": {"type": "array", "items": {"type": "string"}},
            "not_from_course": {"type": "array", "items": {"type": "string"}},
        }
        props.update(self.extra)
        return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _full_texts(md: str) -> dict[str, str]:
    parts = re.split(r"^#### כרטיס מלא · (DC-\d\d)[^\n]*$", md, flags=re.M)
    out = {}
    for i in range(1, len(parts) - 1, 2):
        body = re.split(r"^#{2,3} ", parts[i + 1], flags=re.M)[0]
        out[parts[i]] = body.strip()
    return out


@lru_cache(maxsize=None)
def load_cards() -> dict[str, Card]:
    spec = yaml.safe_load((HERE / "cards.yaml").read_text(encoding="utf-8"))
    texts = _full_texts((HERE / "decision_cards.md").read_text(encoding="utf-8"))
    return {cid: Card(cid, c["agent"], tuple(str(o) for o in c["options"]), str(c["undecided"]), c["approval"],
                      _extra(c), texts.get(cid, ""))
            for cid, c in spec.items()}


def _extra(c: dict) -> dict:
    """The card's own fields; extra_from names a module whose schema_extra() holds a schema too long for yaml."""
    if c.get("extra_from"):
        return importlib.import_module(c["extra_from"]).schema_extra()
    return c.get("extra", {})
