"""Transcript sources: SEC results commentary only; Motley Fool is off by its terms of use."""
import inspect
from datetime import date

from qt.data import transcripts
from qt.data.textfetch import RateLimiter
from qt.data.transcripts import FOOL_ALLOWED, SECResultsCommentary, TranscriptSource
from tests.unit.test_filings import FakeSession, nvda_routes


def test_sec_commentary_is_returned_as_outlook():
    s = FakeSession(nvda_routes())
    src = SECResultsCommentary(None, contact="research@example.com", session=s, limiter=RateLimiter(0))
    assert isinstance(src, TranscriptSource)
    t = src.latest("NVDA", date(2026, 10, 23))
    assert t["source"] == "sec:8-K EX-99.2" and t["date"] == "2026-08-26"
    assert t["url"].endswith("q2fy27cfocommentary.htm")
    assert t["qa"] == [] and t["prepared"] == []
    assert any("Revenue is expected to be $108.0 billion" in p for p in t["outlook"])
    assert sum(map(len, t["outlook"])) <= 2500


def test_no_contact_returns_none(monkeypatch):
    monkeypatch.delenv("SEC_CONTACT", raising=False)
    s = FakeSession(nvda_routes())
    assert SECResultsCommentary(None, session=s).latest("NVDA", date(2026, 10, 23)) is None and s.calls == []


def test_fool_is_not_allowed_and_not_fetched():
    assert FOOL_ALLOWED is False
    src = inspect.getsource(transcripts)
    assert "Fool Rules" in src and "fool.com" not in src
