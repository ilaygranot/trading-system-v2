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
