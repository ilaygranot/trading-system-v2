"""Earnings-call commentary sources (S07-R32: catalysts from announcements, reports and call transcripts).

FOOL_ALLOWED is False. The Motley Fool's "Fool Rules - Terms of Use" (last updated 29 Jan 2026) forbid using
"any automated means, including ... agents, robots, scripts, or spiders, to access, monitor, copy or harvest
data", and forbid making abstracts of or scraping their content; robots.txt does not override the terms.
So there is no Fool fetch code here.

roic.ai is the transcript source (Ilay 24.10: ROIC for transcripts; FMP and Alpha Vantage stay prices and stock data
only, never transcripts). RoicTranscripts returns the latest call before the run date with every turn: the speaker,
whether the speaker is management, an analyst or the operator, and whether the turn is in the prepared remarks or
the Q&A. The course reads the last call's transcript for the stocks that passed the screen (S07-R32, 075, 077);
qt.research.earnings_call asks it the course's questions. Without ROIC_API_KEY, documents["transcript"] stays None
and the SEC results commentary (EX-99.2 CFO commentary / IR data summary, or a 6-K MD&A outlook) stands in for the
prepared remarks. There is no Q&A from SEC filings.
"""
from __future__ import annotations

import os
import re
from datetime import date
from typing import Protocol, runtime_checkable

from qt.data.filings import SECFilings
from qt.data.http import ProviderError, get_json, redact
from qt.data.store import Store
from qt.data.textfetch import RateLimiter

FOOL_ALLOWED = False  # Fool Rules - Terms of Use forbid automated access (see module docstring)
COMMENTARY_CAP = 2500


@runtime_checkable
class TranscriptSource(Protocol):
    def latest(self, ticker: str, as_of: date) -> dict | None:
        """{date, source, url, prepared: [str], qa: [str], outlook: [str]} for the latest call, or None.
        RoicTranscripts adds turns: [{n, speaker, role, section, text}], year and quarter."""
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


# ---------------------------------------------------------------- roic.ai

_TURN = re.compile(r"^([A-Z][^:\n]{1,80}?):\s(.*)$")
_QA_START = re.compile(r"question|caller|\[Operator Instructions\]|Q&A|open (?:the|up the) (?:line|floor)", re.I)


def parse_turns(content: str) -> list[dict]:
    """The transcript's "Speaker: text" lines as turns. The prepared remarks run until the operator opens the Q&A;
    whoever speaks in them (not the operator) is management. In the Q&A the first speaker after each operator turn
    is the analyst the operator called (unless he spoke in the prepared remarks), and everyone else until the next
    operator turn answers him, so is management (a CEO or CFO who only answers questions). A line with no speaker
    is joined to the turn before."""
    turns: list[dict] = []
    for line in (content or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _TURN.match(line)
        if m and len(m.group(1).split()) <= 6:
            turns.append({"speaker": m.group(1).strip(), "text": m.group(2).strip()})
        elif turns:
            turns[-1]["text"] += " " + line
        else:
            turns.append({"speaker": "", "text": line})
    section, prepared, asking = "prepared", set(), None
    after_operator = False
    for i, t in enumerate(turns):
        sp = t["speaker"]
        if sp.lower() == "operator":
            t["role"] = "operator"
            if prepared and _QA_START.search(t["text"]):
                section = "qa"
            after_operator = section == "qa"
        elif section == "prepared":
            prepared.add(sp)
            t["role"] = "management"
        else:
            if after_operator and sp not in prepared:
                asking = sp
            t["role"] = "analyst" if sp == asking else "management"
            after_operator = False
        t["section"] = section
        t["n"] = i
    return turns


def _day(x: str) -> str:
    return str(x or "")[:10]


class RoicTranscripts:
    """Earnings-call transcripts from roic.ai (v2 API): the calls list, then the latest call on or before as_of."""
    BASE = "https://api.roic.ai/v2/company/earnings-calls"
    PAGE = "https://www.roic.ai/quote/{sym}/transcripts/{year}-year/{quarter}-quarter"

    MIN_INTERVAL_S = 12.5          # the free plan answers 5 requests a minute, then HTTP 429 (roic.ai, 24.10)

    def __init__(self, store: Store | None = None, key: str | None = None, limiter: RateLimiter | None = None):
        self.store, self.key = store, key or os.environ.get("ROIC_API_KEY")
        self.limiter = limiter or RateLimiter(self.MIN_INTERVAL_S)

    def ok(self) -> bool:
        return bool(self.key)

    def _get(self, url: str, max_age_s: float):
        if self.store is not None and self.store.cached(redact(url), max_age_s) is not None:
            return get_json(url, self.store, max_age_s=max_age_s)          # from the cache: no request, no wait
        self.limiter.wait()
        return get_json(url, self.store, max_age_s=max_age_s, retries=4)

    def calls(self, ticker: str) -> list[dict]:
        rows = self._get(f"{self.BASE}/list/{ticker.upper()}?apikey={self.key}", 12 * 3600)
        return sorted((r for r in rows or [] if isinstance(r, dict) and r.get("year") and r.get("quarter")),
                      key=lambda r: (_day(r.get("date")), r["year"], r["quarter"]), reverse=True)

    def transcript(self, ticker: str, year: int, quarter: int) -> dict:
        return self._get(f"{self.BASE}/transcript/{ticker.upper()}?year={year}&quarter={quarter}&apikey={self.key}",
                         30 * 86400)                              # a call's transcript does not change

    def latest(self, ticker: str, as_of: date) -> dict | None:
        if not self.ok():
            return None
        sym = ticker.upper()
        for c in self.calls(sym):
            if _day(c.get("date")) > as_of.isoformat():
                continue                                          # a call after the run date was not known then
            body = self.transcript(sym, c["year"], c["quarter"])
            turns = parse_turns((body or {}).get("content") or "")
            if not turns:
                raise ProviderError(f"roic {sym} {c['year']}Q{c['quarter']}: empty transcript")
            return {"date": _day(body.get("date") or c.get("date")), "year": c["year"], "quarter": c["quarter"],
                    "source": f"roic:earnings-call {sym} {c['year']}Q{c['quarter']}",
                    "url": self.PAGE.format(sym=sym, year=c["year"], quarter=c["quarter"]),
                    "turns": turns, "chars": sum(len(t["text"]) for t in turns),
                    "prepared": [], "qa": [], "outlook": []}
        return None
