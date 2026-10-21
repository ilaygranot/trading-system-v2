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


def test_missing_month_is_filled_so_yoy_stays_twelve_months():
    # BLS skipped October 2025; without the fill, Sep-26 vs "12 back" would compare with Aug-25
    obs = [(date(2025, m, 1), 100.0 + m) for m in range(1, 13) if m != 10] + \
          [(date(2026, m, 1), 110.0 + m) for m in range(1, 10)]
    ms = F.monthly_last(obs)
    assert [m for m, _ in ms][8:11] == ["2025-09", "2025-10", "2025-11"]
    assert dict(ms)["2025-10"] == 110.0                     # halfway between 109 and 111
    yoy = F.yoy_pct([v for _, v in ms])
    assert round(yoy[-1], 4) == round((119 / 109 - 1) * 100, 4)
    assert F.missing_months(obs) == ["2025-10"]
