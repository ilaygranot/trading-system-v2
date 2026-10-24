"""Yahoo Finance endpoints the research run needs beyond qt.data.providers: the equity screener (the
universe), quoteSummary per stock (estimates, profile, key statistics, earnings history, analyst actions),
fundamentals time series (statements), the option chain and news headlines. A file cache keeps one run
reproducible and lets the fetch run in threads."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests

from qt.calc.volatility import Bar, parse_yahoo_chart
from qt.data.providers import BROWSER_UA

BASE = "https://query2.finance.yahoo.com"
SECTORS = {                      # Yahoo sector -> SPDR sector ETF (S04-R9; Yahoo's map, not GICS: a judgment)
    "Technology": "XLK", "Consumer Cyclical": "XLY", "Industrials": "XLI", "Energy": "XLE",
    "Real Estate": "XLRE", "Communication Services": "XLC", "Financial Services": "XLF",
    "Healthcare": "XLV", "Utilities": "XLU", "Basic Materials": "XLB", "Consumer Defensive": "XLP",
}
EXCHANGES = {"NMS": "NASDAQ", "NGM": "NASDAQ", "NCM": "NASDAQ", "NYQ": "NYSE"}
MODULES = ("earningsTrend,defaultKeyStatistics,assetProfile,price,financialData,calendarEvents,earningsHistory,"
           "upgradeDowngradeHistory,recommendationTrend,summaryDetail")
TS_TYPES = ("TotalRevenue,CostOfRevenue,GrossProfit,OperatingIncome,NetIncome,DilutedEPS,OperatingCashFlow,"
            "CapitalExpenditure,FreeCashFlow,TotalDebt,CashAndCashEquivalents,CurrentAssets,CurrentLiabilities,"
            "TotalLiabilitiesNetMinorityInterest,DilutedAverageShares,InterestExpense,EBITDA,"
            "CashCashEquivalentsAndShortTermInvestments")


class Yahoo:
    def __init__(self, cache_dir: str | Path, max_age_s: float = 20 * 3600):
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.max_age = max_age_s
        self.s = requests.Session()
        self.s.headers["User-Agent"] = BROWSER_UA
        self._crumb: str | None = None
        self._lock = threading.Lock()

    WAITS_429 = (5, 15, 45, 120, 300)              # 24.10: after the universe, Yahoo's limit took minutes to pass
    CRUMB_WAITS = (15, 30, 60, 120, 240)          # 429 on the first request (24.10): Yahoo's limit passes in minutes

    def crumb(self) -> str:
        with self._lock:
            for wait in (*self.CRUMB_WAITS, None):
                if self._crumb is not None:
                    break
                self.s.get("https://fc.yahoo.com/", timeout=30)
                r = self.s.get(f"{BASE}/v1/test/getcrumb", timeout=30)
                if r.status_code in (429, 500, 502, 503) and wait is not None:
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                self._crumb = r.text.strip()
            return self._crumb

    def _get(self, url: str, key: str | None = None, post: dict | None = None) -> Any:
        k = hashlib.sha256((key or url + json.dumps(post or {})).encode()).hexdigest()
        f = self.cache / f"{k}.json"
        if f.exists() and time.time() - f.stat().st_mtime < self.max_age:
            return json.loads(f.read_text())
        last = None
        for attempt in range(len(self.WAITS_429) + 1):
            sep = "&" if "?" in url else "?"
            full = f"{url}{sep}crumb={self.crumb()}"
            try:
                r = self.s.post(full, json=post, timeout=30) if post else self.s.get(full, timeout=30)
            except requests.RequestException as e:
                last = type(e).__name__
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 500, 502, 503):
                last = f"HTTP {r.status_code}"
                if attempt < len(self.WAITS_429):
                    time.sleep(self.WAITS_429[attempt] if r.status_code == 429 else 2 ** (attempt + 1))
                continue
            if r.status_code == 401:
                self._crumb = None
                last = "HTTP 401"
                continue
            if r.status_code != 200:
                raise RuntimeError(f"yahoo {url.split('?')[0]}: HTTP {r.status_code}")
            body = r.json()
            f.write_text(json.dumps(body))
            return body
        raise RuntimeError(f"yahoo {url.split('?')[0]}: {last}")

    # ------------------------------------------------------------ universe
    def universe(self, sector: str, min_cap: float) -> list[dict]:
        out, offset = [], 0
        while True:
            body = {"size": 250, "offset": offset, "sortField": "intradaymarketcap", "sortType": "DESC",
                    "quoteType": "EQUITY", "query": {"operator": "AND", "operands": [
                        {"operator": "eq", "operands": ["region", "us"]},
                        {"operator": "eq", "operands": ["sector", sector]},
                        {"operator": "gt", "operands": ["intradaymarketcap", min_cap]},
                        {"operator": "or", "operands": [{"operator": "eq", "operands": ["exchange", x]}
                                                        for x in EXCHANGES]}]},
                    "userId": "", "userIdType": "guid"}
            res = self._get(f"{BASE}/v1/finance/screener", key=f"screener|{sector}|{min_cap}|{offset}",
                            post=body)["finance"]["result"][0]
            out += res["quotes"]
            offset += 250
            if offset >= res["total"] or not res["quotes"]:
                return out

    # ------------------------------------------------------------ per stock
    def summary(self, sym: str) -> dict:
        r = self._get(f"{BASE}/v10/finance/quoteSummary/{sym}?modules={MODULES}")
        return (r.get("quoteSummary", {}).get("result") or [{}])[0]

    def fundamentals(self, sym: str) -> dict[str, list[tuple[str, float]]]:
        """{'quarterlyTotalRevenue': [(date, value), ...], 'annualTotalRevenue': ...}, oldest first."""
        types = ",".join(f"{p}{t}" for p in ("quarterly", "annual") for t in TS_TYPES.split(","))
        r = self._get(f"{BASE}/ws/fundamentals-timeseries/v1/finance/timeseries/{sym}?type={types}"
                      f"&period1=1420070400&period2={int(time.time())}")
        out = {}
        for s in r["timeseries"]["result"]:
            t = s["meta"]["type"][0]
            vals = [(v["asOfDate"], v["reportedValue"]["raw"]) for v in s.get(t, []) if v and v.get("reportedValue")]
            if vals:
                out[t] = sorted(vals)
        return out

    def bars(self, sym: str, start: date, end: date) -> list[Bar]:
        p1 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
        p2 = int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp()) + 86400
        return [b for b in parse_yahoo_chart(self._get(f"{BASE}/v8/finance/chart/{sym}?period1={p1}&period2={p2}"
                                                       f"&interval=1d")) if b.day <= end]

    def options(self, sym: str, expiry: int | None = None) -> dict:
        url = f"{BASE}/v7/finance/options/{sym}" + (f"?date={expiry}" if expiry else "")
        return self._get(url)["optionChain"]["result"][0]

    def news(self, sym: str, n: int = 8) -> list[dict]:
        r = self._get(f"{BASE}/v1/finance/search?q={sym}&newsCount={n}&quotesCount=0")
        return [dict(title=x.get("title"), publisher=x.get("publisher"), link=x.get("link"),
                     time=datetime.fromtimestamp(x.get("providerPublishTime", 0), timezone.utc).date().isoformat())
                for x in r.get("news", [])]


def raw(d: dict | None, k: str) -> Any:
    v = (d or {}).get(k)
    return v.get("raw") if isinstance(v, dict) else v
