"""The open positions as the dashboard shows them: the paper book (qt.book) marked to the latest price.

Read-only: nothing here changes the book or talks to a broker. Prices come from Yahoo's chart endpoint (no crumb),
cached for a minute so a dashboard that polls does not hammer it; a symbol without a price is shown at its entry
and flagged. Equity is cash plus the signed market value, as in qt.book.mark_equity.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

Prices = Callable[[list[str]], dict[str, float]]

PRICE_TTL_S = 60


def yahoo_prices(store: Any) -> Prices:
    """symbols -> last price (regular market, delayed), from the v8 chart meta, cached PRICE_TTL_S seconds."""
    from qt.data.http import get_json
    from qt.data.providers import BROWSER_UA, Yahoo

    def url(symbol: str) -> str:
        return f"{Yahoo.BASE}/v8/finance/chart/{symbol}?range=1d&interval=1m"

    def fetch(symbol: str) -> Any:
        try:
            return get_json(url(symbol), None, headers={"User-Agent": BROWSER_UA}, retries=1)
        except Exception:                                            # noqa: BLE001 - shown as "no price"
            return None

    def last(body: Any) -> float | None:
        try:
            v = body["chart"]["result"][0]["meta"].get("regularMarketPrice")
            return float(v) if v else None
        except (KeyError, IndexError, TypeError, ValueError):
            return None

    def prices(symbols: list[str]) -> dict[str, float]:
        # the cache is read and written on this thread (one DB connection); only the HTTP calls run in parallel
        bodies = {s: store.cached(url(s), PRICE_TTL_S) if store is not None else None for s in symbols}
        missing = [s for s, b in bodies.items() if b is None]
        if missing:
            with ThreadPoolExecutor(max_workers=min(8, len(missing))) as ex:
                for s, b in zip(missing, ex.map(fetch, missing)):
                    bodies[s] = b
                    if b is not None and store is not None:
                        store.put(url(s), b)
        got = {s: last(b) for s, b in bodies.items()}
        return {s: v for s, v in got.items() if v is not None}

    return prices


def snapshot(pf: dict | None, prices: dict[str, float], open_orders: list[dict] | None = None,
             now: datetime | None = None) -> dict[str, Any]:
    """The portfolio marked to `prices`: one row per position and the account totals."""
    pf = pf or {}
    rows, value, unreal = [], 0.0, 0.0
    for p in pf.get("positions", []):
        sg = 1 if p.get("direction", "long") == "long" else -1
        qty, entry = float(p.get("qty") or 0), float(p.get("entry") or 0)
        px = prices.get(p["symbol"])
        last = px if px is not None else entry
        mv = sg * qty * last
        pl = sg * (last - entry) * qty
        value += mv
        unreal += pl
        stop = p.get("stop")
        rows.append(dict(
            symbol=p["symbol"], direction=p.get("direction", "long"), sector=p.get("sector"), qty=qty, entry=entry,
            last=px, priced=px is not None, market_value=round(abs(mv), 2), pl=round(pl, 2),
            pl_pct=round(sg * (last / entry - 1) * 100, 2) if entry else None,
            stop=stop, stop_dist_pct=round(sg * (last - stop) / last * 100, 2) if stop and last else None,
            target=p.get("target"), opened=p.get("opened")))
    cash = pf.get("cash")
    equity = (float(cash) + value) if cash is not None else pf.get("equity")
    for r in rows:
        r["weight_pct"] = round(r["market_value"] / equity * 100, 2) if equity else None
    peak = pf.get("peak_equity")
    peak = max(peak, equity) if peak is not None and equity is not None else peak
    return {
        "as_of": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "has_portfolio": bool(pf),
        "positions": sorted(rows, key=lambda r: -r["market_value"]),
        "cash": cash, "equity": round(equity, 2) if equity is not None else None,
        "unrealized": round(unreal, 2),
        "long_value": round(sum(r["market_value"] for r in rows if r["direction"] == "long"), 2),
        "short_value": round(sum(r["market_value"] for r in rows if r["direction"] == "short"), 2),
        "peak_equity": peak,
        "drawdown_pct": round((equity / peak - 1) * 100, 2) if peak and equity is not None else None,
        "realized": round(sum(float(x.get("pnl") or 0) for x in pf.get("realized", [])), 2),
        "pending": [dict(symbol=o["symbol"], kind=o["kind"], qty=o["qty"], fill_on=str(o["fill_on"]))
                    for o in (open_orders or [])],
    }
