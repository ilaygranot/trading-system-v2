import os
import json
from pathlib import Path

import pytest

from qt.calc.volatility import Bar, parse_fmp_prices, parse_yahoo_chart

FIX = Path(__file__).parent / "fixtures" / "prices"


def load_bars(name: str) -> list[Bar]:
    payload = json.loads((FIX / name).read_text())
    return parse_yahoo_chart(payload) if isinstance(payload, dict) else parse_fmp_prices(payload)


@pytest.fixture
def bars():
    return load_bars


@pytest.fixture(autouse=True)
def _offline_transcripts(monkeypatch):
    """No test reaches roic.ai: a test that wants transcripts passes its own source."""
    monkeypatch.delenv("ROIC_API_KEY", raising=False)


PG_URL = os.environ.get("QT_TEST_DATABASE_URL")


@pytest.fixture(params=["sqlite"] + (["postgres"] if PG_URL else []))
def db(request):
    """A fresh database on each backend. Postgres runs when QT_TEST_DATABASE_URL is set (CI service)."""
    from qt.db import DB
    if request.param == "sqlite":
        yield DB(":memory:")
        return
    d = DB(PG_URL)
    d.conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    yield d
    d.conn.close()
