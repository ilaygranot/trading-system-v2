"""Fact sheet: every number an agent may cite, with id, source and as-of date (architecture 6, principle 4)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any


@dataclass(frozen=True)
class Fact:
    fact_id: str
    key: str            # e.g. "BA.eps.F1"
    value: Any
    source: str         # provider + endpoint, e.g. "yahoo:quoteSummary/earningsTrend"
    as_of: date         # the date the value describes / was known
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    url: str = ""       # the document a text fact is quoted from (the article's footnote links to it)

    def line(self) -> str:
        return f"{self.fact_id}: {self.key} = {self.value}, {self.source}, {self.as_of.isoformat()}"


class FactSheet:
    def __init__(self) -> None:
        self._facts: dict[str, Fact] = {}

    def add(self, key: str, value: Any, source: str, as_of: date, url: str = "") -> Fact:
        fid = f"F-{len(self._facts) + 1:03d}"
        fact = Fact(fid, key, value, source, as_of, url=url)
        self._facts[fid] = fact
        return fact

    def __len__(self) -> int:
        return len(self._facts)

    def get(self, fact_id: str) -> Fact:
        return self._facts[fact_id]

    def __contains__(self, fact_id: str) -> bool:
        return fact_id in self._facts

    def missing(self, fact_ids: list[str] | tuple[str, ...]) -> list[str]:
        """Ids an agent cited that are not on the sheet (the Critic rejects these)."""
        return [f for f in fact_ids if f not in self._facts]

    def render(self) -> str:
        return "\n".join(f.line() for f in self._facts.values())

    def to_list(self) -> list[dict]:
        return [{**asdict(f), "as_of": f.as_of.isoformat()} for f in self._facts.values()]
