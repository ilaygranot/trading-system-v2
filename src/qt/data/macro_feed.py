"""Inputs for qt.macro: FRED series as they were known on the run date (ALFRED vintages), plus the
manual monthly file for what FRED doesn't carry (ISM PMI/NMI since 2016, NFIB small business).

The manual file is YAML: {series_name: {"YYYY-MM": value, ...}}, filled by the macro agent from the
ISM report (architecture 9) and NFIB. Every value used by the stance keeps its source id.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

import yaml

FRED_SERIES = {
    "hy_oas": "BAMLH0A0HYM2",        # S01-R1 (ICE BofA US High Yield OAS; FRED keeps ~3 years)
    "ust10y": "DGS10",
    "ust3m": "DGS3MO",
    "ust20y": "DGS20",               # S01-R15
    "tips_breakeven10y": "T10YIE",   # S01-R19 stand-in for the TIPS / nominal ratio (not from course: the series)
    "real10y": "DFII10",
    "cpi": "CPIAUCSL",
    "core_cpi": "CPILFESL",
    "unemployment": "UNRATE",
    "initial_claims": "ICSA",
    "continuing_claims": "CCSA",
    "permits": "PERMIT",
    "michigan": "UMCSENT",
    "m2": "M2SL",
    "ffr": "FEDFUNDS",
    "fed_balance_sheet": "WALCL",
}
MANUAL_SERIES = ("ism_pmi", "ism_nmi", "small_business")


def _next_month(m: str) -> str:
    y, mo = int(m[:4]), int(m[5:7])
    return f"{y + mo // 12:04d}-{mo % 12 + 1:02d}"


def monthly_last(obs: Iterable[tuple[date, float]]) -> list[tuple[str, float]]:
    """Daily/weekly observations -> one value per month (the last of the month), with no missing months.

    Every consumer indexes by position (12 back = a year ago), so a month the agency never published (BLS
    skipped October 2025 in the shutdown) would turn a 12-month change into a 13-month one. A missing month
    is filled on the straight line between its neighbours (not from course)."""
    out: OrderedDict[str, float] = OrderedDict()
    for d, v in obs:
        out[f"{d.year:04d}-{d.month:02d}"] = v
    items = list(out.items())
    filled: list[tuple[str, float]] = items[:1]
    for m, v in items[1:]:
        gap = []
        nm = _next_month(filled[-1][0])
        while nm < m:
            gap.append(nm)
            nm = _next_month(nm)
        prev = filled[-1][1]
        for k, g in enumerate(gap, 1):
            filled.append((g, prev + (v - prev) * k / (len(gap) + 1)))
        filled.append((m, v))
    return filled


def missing_months(obs: Iterable[tuple[date, float]]) -> list[str]:
    """The months monthly_last filled in, to show next to the series."""
    have = {f"{d.year:04d}-{d.month:02d}" for d, _ in obs}
    return [m for m, _ in monthly_last(obs) if m not in have]


def yoy_pct(monthly: list[float]) -> list[float]:
    """12-month percent change (CPI YoY = inflation, S02-R28; M2 YoY, S02-R38)."""
    return [(monthly[i] / monthly[i - 12] - 1) * 100 for i in range(12, len(monthly))]


def load_manual(path: str | Path, as_of: date) -> dict[str, list[float]]:
    """Manual monthly series up to the month before `as_of` (a month's report is published the next month)."""
    return parse_manual(Path(path).read_text(encoding="utf-8"), as_of)


def parse_manual(text: str, as_of: date) -> dict[str, list[float]]:
    raw = yaml.safe_load(text) or {}
    cutoff = f"{as_of.year:04d}-{as_of.month:02d}"
    out = {}
    for name, rows in raw.items():
        if name == "naaim" or not isinstance(rows, dict) or not all(isinstance(v, (int, float)) for v in rows.values()):
            continue                     # the ISM tables (industries, short supply) are read by qt.monthly
        items = sorted((str(k), float(v)) for k, v in rows.items())
        out[name] = [v for k, v in items if k < cutoff]
    return out


def fetch_fred(fred, as_of: date, years: int = 12) -> tuple[dict[str, list[float]], dict[str, list[float]], dict[str, str]]:
    """Returns (monthly, daily, sources). Values are as known on `as_of` (no look-ahead from revisions)."""
    start = as_of - timedelta(days=365 * years)
    monthly, daily, src = {}, {}, {}
    for name, sid in FRED_SERIES.items():
        obs, s = fred.observations(sid, start, as_of, known_on=as_of)
        src[name] = s
        if name in ("hy_oas", "ust10y", "ust3m", "ust20y", "tips_breakeven10y", "real10y"):
            daily[name] = [v for _, v in obs]
        monthly[name] = [v for _, v in monthly_last(obs)]
    return monthly, daily, src
