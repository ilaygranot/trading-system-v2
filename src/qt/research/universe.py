"""Stage 0 of the research run: the universe (Yahoo screener, US listed, NYSE/NASDAQ, market cap above the
lowest floor) and one StockRow per stock from the analyst consensus (S06 inputs)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from qt.policy import load_policy
from qt.research.yahoo import EXCHANGES, SECTORS, Yahoo, raw
from qt.screening import StockRow


def fetch(y: Yahoo, threads: int = 8) -> dict[str, Any]:
    floor = min(load_policy().floors.long_min_market_cap, load_policy().floors.short_min_market_cap)
    quotes = {}
    for sector, etf in SECTORS.items():
        for q in y.universe(sector, floor):
            if q.get("quoteType") != "EQUITY" or q["symbol"] in quotes:
                continue
            quotes[q["symbol"]] = dict(symbol=q["symbol"], name=q.get("longName") or q.get("shortName"), sector=sector,
                                       etf=etf, exchange=EXCHANGES.get(q.get("exchange"), q.get("exchange")),
                                       market_cap=q.get("marketCap"), price=q.get("regularMarketPrice"),
                                       ma200=q.get("twoHundredDayAverage"), ma50=q.get("fiftyDayAverage"),
                                       w52_change=q.get("fiftyTwoWeekChangePercent"))

    def one(sym: str):
        try:
            return sym, y.summary(sym)
        except Exception as e:                                    # noqa: BLE001 - reported per stock
            return sym, {"error": str(e)}
    with ThreadPoolExecutor(threads) as ex:
        summaries = dict(ex.map(one, list(quotes)))
    return {"quotes": quotes, "summaries": summaries}


def trend(summary: dict, period: str) -> dict:
    return next((t for t in summary.get("earningsTrend", {}).get("trend", []) if t.get("period") == period), {})


def row(q: dict, s: dict) -> StockRow:
    t0, t1 = trend(s, "0y"), trend(s, "+1y")
    ks = s.get("defaultKeyStatistics", {})
    spf = raw(ks, "shortPercentOfFloat")
    return StockRow(
        ticker=q["symbol"], exchange=q["exchange"] or "", market_cap=q["market_cap"], price=q["price"],
        sales=raw(t0.get("revenueEstimate"), "yearAgoRevenue"), sales_f1=raw(t0.get("revenueEstimate"), "avg"),
        f0=raw(t0.get("earningsEstimate"), "yearAgoEps"), f1=raw(t0.get("earningsEstimate"), "avg"),
        f2=raw(t1.get("earningsEstimate"), "avg"), short_float=spf * 100 if spf is not None else None,
        days_to_cover=raw(ks, "shortRatio"), industry=s.get("assetProfile", {}).get("industry", ""))


def breadth_pct(quotes: dict) -> float:
    xs = [q for q in quotes.values() if q.get("price") and q.get("ma200")]
    return 100.0 * sum(q["price"] > q["ma200"] for q in xs) / len(xs)
