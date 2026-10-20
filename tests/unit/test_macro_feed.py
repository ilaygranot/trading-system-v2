from datetime import date

import pytest

from qt.data import macro_feed as F


def test_monthly_last_and_yoy():
    obs = [(date(2023, 1, 3), 1.0), (date(2023, 1, 31), 2.0), (date(2023, 2, 1), 3.0)]
    assert F.monthly_last(obs) == [("2023-01", 2.0), ("2023-02", 3.0)]
    assert F.yoy_pct([100.0] * 12 + [106.0]) == [pytest.approx(6.0)]


def test_manual_file_has_no_look_ahead(tmp_path):
    p = tmp_path / "manual.yaml"
    p.write_text("ism_pmi: {'2023-01': 47.4, '2023-02': 47.7, '2023-03': 46.3}\n")
    assert F.load_manual(p, date(2023, 3, 10)) == {"ism_pmi": [47.4, 47.7]}


def test_fetch_fred_uses_vintages():
    calls = []

    class FakeFred:
        def observations(self, sid, start, end, known_on=None):
            calls.append((sid, known_on))
            return [(date(2023, 1, 31), 1.0), (date(2023, 2, 28), 2.0)], f"fred:{sid}@{known_on}"

    monthly, daily, src = F.fetch_fred(FakeFred(), date(2023, 3, 1))
    assert all(k == date(2023, 3, 1) for _, k in calls)
    assert monthly["cpi"] == [1.0, 2.0] and "hy_oas" in daily and src["cpi"].endswith("@2023-03-01")
