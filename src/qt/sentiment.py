"""Sentiment for the monthly preparation (S13-R25; method S09-R31..R34, D-S09-8): a direction, an extreme, and for
some series the position against a long average. Sentiment changes risk and caution; it is never a trigger alone
(S09-J20).

COT (S09-R34): the CFTC Traders in Financial Futures report, futures and options combined, S&P 500. The course
uses the percent of open interest of the Asset Managers as an absolute index (< 0.2 extreme low, > 0.5 extreme high)
and the Smart Money Spread = Asset Managers - Non-Reportable (above its long moving average = positive). Both
are taken on the long side's percent of open interest; the course's spreadsheet is not in hand, so this reading
and the long average's length (52 reports) are not from the course.
NAAIM (S09-R33): the weekly exposure index. Since August 2026 its data is for subscribers (the public file lags
three months), so it comes from the monthly manual file (`naaim: {YYYY-MM-DD: value}`). Historical mean about 66;
the extreme is the mean +- one standard deviation computed from the history given (D-S09-8 (b)).
Every series is smoothed with a 4-period moving average (S09-R31).
"""
from __future__ import annotations

import csv
import io
from datetime import date
from statistics import mean, pstdev
from typing import Any

SMOOTH = 4                       # S09-R31
COT_LOW, COT_HIGH = 0.2, 0.5     # S09-R34
COT_LONG_MA = 52                 # not from course: the lecturer's long average was only shown on a chart
NAAIM_MEAN = 66.0                # S09-R33, the course's historical mean (the reference line)
NAAIM_MIN_HISTORY = 52           # D-S09-8 (b): a standard deviation needs a year of weeks (not from course)
SP500 = ("S&P 500 CONSOLIDATED", "E-MINI S&P 500")   # first found wins


def _col(header: list[str], *names: str) -> int:
    low = [h.strip().lower() for h in header]
    for n in names:
        if n.lower() in low:
            return low.index(n.lower())
    raise KeyError(names[0])


def parse_tff(text: str) -> list[dict[str, Any]]:
    """The S&P 500 rows of one yearly TFF file: date, open interest, asset managers and non-reportable long and
    short."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    h = rows[0]
    i_name = _col(h, "Market_and_Exchange_Names")
    i_date = _col(h, "Report_Date_as_YYYY-MM-DD")
    i_oi = _col(h, "Open_Interest_All")
    i_aml, i_ams = _col(h, "Asset_Mgr_Positions_Long_All"), _col(h, "Asset_Mgr_Positions_Short_All")
    i_nrl, i_nrs = _col(h, "NonRept_Positions_Long_All"), _col(h, "NonRept_Positions_Short_All")
    by_market: dict[str, list[dict]] = {}
    for r in rows[1:]:
        if len(r) <= max(i_name, i_date, i_oi, i_aml, i_ams, i_nrl, i_nrs):
            continue
        name = r[i_name].strip().upper()
        key = next((m for m in SP500 if name.startswith(m)), None)
        if key is None:
            continue
        try:
            by_market.setdefault(key, []).append(dict(
                date=r[i_date].strip()[:10], oi=float(r[i_oi]), am_long=float(r[i_aml]), am_short=float(r[i_ams]),
                nr_long=float(r[i_nrl]), nr_short=float(r[i_nrs]), market=name))
        except ValueError:
            continue
    for m in SP500:
        if by_market.get(m):
            return sorted(by_market[m], key=lambda x: x["date"])
    return []


def _ma(xs: list[float], n: int) -> list[float]:
    return [mean(xs[i - n + 1:i + 1]) for i in range(n - 1, len(xs))]


def _direction(smoothed: list[float]) -> str | None:
    if len(smoothed) < 2:
        return None
    return "up" if smoothed[-1] > smoothed[-2] else "down" if smoothed[-1] < smoothed[-2] else "flat"


def cot_reading(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(rows) < SMOOTH + 1:
        return None
    am = [r["am_long"] / r["oi"] for r in rows if r["oi"]]
    spread = [(r["am_long"] - r["nr_long"]) / r["oi"] for r in rows if r["oi"]]
    am_s, sp_s = _ma(am, SMOOTH), _ma(spread, SMOOTH)
    long_ma = mean(spread[-COT_LONG_MA:]) if len(spread) >= COT_LONG_MA else None
    level = am_s[-1]
    return dict(as_of=rows[-1]["date"], market=rows[-1]["market"], reports=len(rows),
                am_pct_oi=round(level, 3), am_direction=_direction(am_s),
                am_extreme="low" if level < COT_LOW else "high" if level > COT_HIGH else None,
                spread=round(sp_s[-1], 3), spread_direction=_direction(sp_s),
                spread_vs_long_ma=None if long_ma is None else ("above" if sp_s[-1] > long_ma else "below"))


def naaim_reading(raw: dict | None, as_of: date) -> dict[str, Any] | None:
    pts = sorted((str(k), float(v)) for k, v in (raw or {}).items() if str(k) <= as_of.isoformat())
    if len(pts) < SMOOTH + 1:
        return None
    vals = [v for _, v in pts]
    sm = _ma(vals, SMOOTH)
    out = dict(as_of=pts[-1][0], value=vals[-1], smoothed=round(sm[-1], 1), direction=_direction(sm),
               vs_mean="above" if sm[-1] > NAAIM_MEAN else "below", weeks=len(vals), extreme=None, band=None)
    if len(vals) >= NAAIM_MIN_HISTORY:
        m, sd = mean(vals), pstdev(vals)
        out["band"] = [round(m - sd, 1), round(m + sd, 1)]
        out["extreme"] = "high" if sm[-1] > m + sd else "low" if sm[-1] < m - sd else None
    return out


DIR_HE = {"up": "עולה", "down": "יורד", "flat": "שטוח", None: "?"}


def lines(cot: dict | None, naaim: dict | None, missing: list[str]) -> list[str]:
    out = []
    if cot:
        ext = {"low": " · קיצון נמוך", "high": " · קיצון גבוה"}.get(cot["am_extreme"] or "", "")
        ma = {"above": "מעל", "below": "מתחת"}.get(cot["spread_vs_long_ma"] or "", "")
        out.append(f"COT ({cot['as_of']}): מנהלי נכסים {cot['am_pct_oi']:.2f} מה-OI, {DIR_HE[cot['am_direction']]}{ext}"
                   f" · Smart Money Spread {cot['spread']:+.2f}, {DIR_HE[cot['spread_direction']]}"
                   + (f", {ma} לממוצע הארוך" if ma else ""))
    if naaim:
        ext = {"low": " · קיצון נמוך", "high": " · קיצון גבוה"}.get(naaim["extreme"] or "", "")
        out.append(f"NAAIM ({naaim['as_of']}): {naaim['smoothed']:.1f} בממוצע 4 שבועות, {DIR_HE[naaim['direction']]}"
                   f" · {'מעל' if naaim['vs_mean'] == 'above' else 'מתחת'} לממוצע ההיסטורי 66{ext}"
                   + ("" if naaim["band"] else " · אין עדיין שנה של נתונים לקיצון"))
    if missing:
        out.append("סנטימנט חסר: " + ", ".join(missing))
    return out
