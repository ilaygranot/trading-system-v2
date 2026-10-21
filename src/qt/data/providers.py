"""Data providers. Each returns values together with a source string so they can enter the fact sheet.

Roles (architecture 9.2, updated 20.10):
- Yahoo Finance (unofficial query1/query2): primary for prices, F1/F2 estimates and revisions, short float, earnings dates.
- FMP (free plan): profile, sample-symbol fundamentals; cross-check.
- FRED/ALFRED: macro, always with realtime_start/realtime_end so history shows what was known then.
- SEC EDGAR: filings and XBRL company facts (needs a User-Agent with contact); the source of truth for reported numbers.
- Alpha Vantage (free key, 25 calls a day): a second daily close when FMP is out of reach.
- Zacks: Ilay uploads a monthly screen export (see zacks.py).
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import Any

import requests

from qt.calc.volatility import Bar, parse_fmp_prices, parse_yahoo_chart
from qt.data.http import ProviderError, get_json
from qt.data.store import Store

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"


def _key(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise ProviderError(f"missing environment variable {name}")
    return v


class Yahoo:
    BASE = "https://query2.finance.yahoo.com"
    MODULES = ("earningsTrend,earningsHistory,calendarEvents,defaultKeyStatistics,financialData,"
               "upgradeDowngradeHistory,recommendationTrend,price,assetProfile")

    def __init__(self, store: Store | None = None):
        self.store = store
        self.session = requests.Session()
        self.session.headers["User-Agent"] = BROWSER_UA
        self._crumb: str | None = None

    def _crumb_value(self) -> str:
        if self._crumb is None:
            self.session.get("https://fc.yahoo.com/", timeout=30)
            r = self.session.get(f"{self.BASE}/v1/test/getcrumb", timeout=30)
            if r.status_code != 200 or not r.text:
                raise ProviderError(f"yahoo crumb: HTTP {r.status_code}")
            self._crumb = r.text.strip()
        return self._crumb

    def daily_bars(self, symbol: str, start: date, end: date) -> tuple[list[Bar], str]:
        p1 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
        p2 = int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp()) + 86400
        url = f"{self.BASE}/v8/finance/chart/{symbol}?period1={p1}&period2={p2}&interval=1d"
        return parse_yahoo_chart(get_json(url, self.store, session=self.session)), "yahoo:v8/chart"

    def summary(self, symbol: str) -> tuple[dict[str, Any], str]:
        url = f"{self.BASE}/v10/finance/quoteSummary/{symbol}?modules={self.MODULES}&crumb={self._crumb_value()}"
        body = get_json(url, self.store, session=self.session)
        return body["quoteSummary"]["result"][0], "yahoo:v10/quoteSummary"

    def quote(self, symbol: str) -> tuple[float | None, str]:
        """The latest price before the open: the pre-market price when Yahoo has one, else the last close."""
        pr = self.summary(symbol)[0].get("price", {})
        for k in ("preMarketPrice", "regularMarketPrice"):
            v = (pr.get(k) or {}).get("raw") if isinstance(pr.get(k), dict) else pr.get(k)
            if v:
                return float(v), f"yahoo:price.{k}"
        return None, "yahoo:price"

    def estimates(self, symbol: str) -> tuple[dict[str, Any], str]:
        """F1/F2 EPS and revenue estimates, 90-day EPS trend, revisions, short interest, next earnings."""
        s, src = self.summary(symbol)

        def raw(d: dict, k: str) -> Any:
            v = (d or {}).get(k)
            return v.get("raw") if isinstance(v, dict) else v

        out: dict[str, Any] = {}
        for tr in s.get("earningsTrend", {}).get("trend", []):
            tag = {"0y": "F1", "+1y": "F2"}.get(tr.get("period"))
            if not tag:
                continue
            out[tag] = dict(end=tr.get("endDate"), eps=raw(tr.get("earningsEstimate"), "avg"),
                            eps_year_ago=raw(tr.get("earningsEstimate"), "yearAgoEps"),
                            revenue=raw(tr.get("revenueEstimate"), "avg"),
                            eps_90d_ago=raw(tr.get("epsTrend"), "90daysAgo"),
                            eps_30d_ago=raw(tr.get("epsTrend"), "30daysAgo"),
                            up_30d=raw(tr.get("epsRevisions"), "upLast30days"),
                            down_30d=raw(tr.get("epsRevisions"), "downLast30days"))
        ks = s.get("defaultKeyStatistics", {})
        out["short_pct_float"] = raw(ks, "shortPercentOfFloat")
        out["days_to_cover"] = raw(ks, "shortRatio")
        out["market_cap"] = raw(s.get("price", {}), "marketCap")
        out["sector"] = s.get("assetProfile", {}).get("sector")
        out["industry"] = s.get("assetProfile", {}).get("industry")
        dates = s.get("calendarEvents", {}).get("earnings", {}).get("earningsDate", [])
        out["next_earnings"] = dates[0].get("fmt") if dates else None
        return out, src

    def snapshot_estimates(self, symbol: str, as_of: date | None = None) -> None:
        """Weekly point-in-time snapshot (providers don't keep estimate history)."""
        if self.store is None:
            raise ProviderError("snapshots need a store")
        data, src = self.estimates(symbol)
        self.store.snapshot("estimates", symbol, as_of or date.today(), src, data)


class FMP:
    BASE = "https://financialmodelingprep.com/stable"

    def __init__(self, store: Store | None = None):
        self.store = store

    def _get(self, path: str, **params: Any) -> Any:
        q = "&".join(f"{k}={v}" for k, v in params.items())
        return get_json(f"{self.BASE}/{path}?{q}&apikey={_key('FMP_API_KEY')}", self.store)

    def profile(self, symbol: str) -> tuple[dict, str]:
        return self._get("profile", symbol=symbol)[0], "fmp:stable/profile"

    def daily_bars(self, symbol: str, start: date, end: date) -> tuple[list[Bar], str]:
        rows = self._get("historical-price-eod/full", symbol=symbol, **{"from": start, "to": end})
        return parse_fmp_prices(rows), "fmp:stable/historical-price-eod"


class AlphaVantage:
    BASE = "https://www.alphavantage.co/query"

    def __init__(self, store: Store | None = None):
        self.store = store

    def daily_closes(self, symbol: str) -> tuple[dict[str, float], str]:
        """Last ~100 daily closes by ISO date. The free plan answers over-limit calls with a note, not an error."""
        key = _key("ALPHAVANTAGE_API_KEY")
        # no cache: an over-limit answer is HTTP 200 and must not stick for the rest of the day
        body = get_json(f"{self.BASE}?function=TIME_SERIES_DAILY&symbol={symbol}&apikey={key}")
        rows = body.get("Time Series (Daily)")
        if not rows:
            msg = str(body.get("Information") or body.get("Note") or body).replace(key, "***")
            raise ProviderError(f"alphavantage {symbol}: {'daily limit reached' if 'rate limit' in msg else msg[:120]}")
        return {d: float(v["4. close"]) for d, v in rows.items()}, "alphavantage:TIME_SERIES_DAILY"


class FRED:
    BASE = "https://api.stlouisfed.org/fred"

    def __init__(self, store: Store | None = None):
        self.store = store

    def observations(self, series_id: str, start: date, end: date, known_on: date | None = None) -> tuple[list[tuple[date, float]], str]:
        """Observations as they were known on `known_on` (ALFRED vintage). Without it: today's revised data."""
        url = (f"{self.BASE}/series/observations?series_id={series_id}&observation_start={start}"
               f"&observation_end={end}&file_type=json&api_key={_key('FRED_API_KEY')}")
        if known_on:
            url += f"&realtime_start={known_on}&realtime_end={known_on}"
        body = get_json(url, self.store, max_age_s=24 * 3600)
        obs = [(date.fromisoformat(o["date"]), float(o["value"])) for o in body["observations"] if o["value"] != "."]
        return obs, f"fred:{series_id}" + (f"@{known_on}" if known_on else "")


class EDGAR:
    BASE = "https://data.sec.gov"

    def __init__(self, store: Store | None = None, contact: str | None = None):
        self.store = store
        self.headers = {"User-Agent": f"QuantumTrader research {contact or os.environ.get('SEC_CONTACT', 'contact@example.com')}"}

    def cik(self, ticker: str) -> int | None:
        body = get_json("https://www.sec.gov/files/company_tickers.json", self.store, max_age_s=7 * 24 * 3600,
                        headers=self.headers)
        return next((int(v["cik_str"]) for v in body.values() if v["ticker"] == ticker.upper()), None)

    def company_facts(self, cik: int) -> tuple[dict, str]:
        return get_json(f"{self.BASE}/api/xbrl/companyfacts/CIK{cik:010d}.json", self.store,
                        max_age_s=24 * 3600, headers=self.headers), "sec:companyfacts"

    def annual_revenue(self, cik: int) -> tuple[list[tuple[str, float, str]], str]:
        """(fiscal period end, revenue, filed date) from 10-K XBRL; filed date avoids look-ahead."""
        facts, src = self.company_facts(cik)
        gaap = facts.get("facts", {}).get("us-gaap", {})
        for tag in ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"):
            units = gaap.get(tag, {}).get("units", {}).get("USD")
            if units:
                rows = {(u["end"], u["val"], u["filed"]) for u in units if u.get("form") == "10-K" and u.get("fp") == "FY"}
                return sorted(rows), f"{src}/{tag}"
        return [], src
