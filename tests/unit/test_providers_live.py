"""Live provider smoke tests. Skipped by default; run with: pytest -m network"""
from datetime import date

import pytest

from qt.data.providers import EDGAR, FRED, Yahoo

pytestmark = pytest.mark.network


def test_yahoo_prices_and_estimates():
    y = Yahoo()
    bars, src = y.daily_bars("BA", date(2023, 3, 1), date(2023, 5, 31))
    assert len(bars) > 50 and src.startswith("yahoo")
    est, _ = y.estimates("BA")
    assert "F1" in est and "F2" in est


def test_fred_vintage_permits_feb_2023():
    obs, _ = FRED().observations("PERMIT", date(2023, 2, 1), date(2023, 2, 1), known_on=date(2023, 3, 20))
    assert obs[0][1] == 1524


def test_edgar_revenue():
    rows, _ = EDGAR(contact="research@example.com").annual_revenue(12927)  # Boeing
    assert rows
