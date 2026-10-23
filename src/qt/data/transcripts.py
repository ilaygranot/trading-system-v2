"""Earnings-call commentary sources (S07-R32: catalysts from announcements, reports and call transcripts).

FOOL_ALLOWED is False. The Motley Fool's "Fool Rules - Terms of Use" (last updated 29 Jan 2026) forbid using
"any automated means, including ... agents, robots, scripts, or spiders, to access, monitor, copy or harvest
data", and forbid making abstracts of or scraping their content; robots.txt does not override the terms.
So there is no Fool fetch code here. A licensed or permitted transcript source is added only after Ilay's
written decision, as another TranscriptSource; until then documents["transcript"] stays None and the SEC
results commentary (EX-99.2 CFO commentary / IR data summary, or a 6-K MD&A outlook) stands in for the
prepared remarks. There is no Q&A from SEC filings.
"""
from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from qt.data.filings import SECFilings

FOOL_ALLOWED = False  # Fool Rules - Terms of Use forbid automated access (see module docstring)
COMMENTARY_CAP = 2500


@runtime_checkable
class TranscriptSource(Protocol):
    def latest(self, ticker: str, as_of: date) -> dict | None:
        """{date, source, url, prepared: [str], qa: [str], outlook: [str]} for the latest call, or None."""
        ...


class SECResultsCommentary(SECFilings):
    """The results-commentary exhibit of the latest results filing, as outlook paragraphs only."""

    def latest(self, ticker: str, as_of: date) -> dict | None:
        if not self.ok():
            return None
        latest, _ = self.results(ticker, as_of)
        if not latest or not latest.get("commentary"):
            return None
        c = latest["commentary"]
        outlook, used = [], 0
        for p in c.get("outlook") or []:
            if used + len(p) > COMMENTARY_CAP:
                break
            outlook.append(p)
            used += len(p)
        form = "6-K" if latest["form"] == "6-K" else "8-K"
        return {"date": latest["date"], "source": f"sec:{form} {c.get('type') or 'EX-99.2'}", "url": c["url"],
                "prepared": [], "qa": [], "outlook": outlook}
