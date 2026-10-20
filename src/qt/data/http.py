"""Small HTTP helper: caching, polite retry, and never leaking API keys into errors or the cache key."""
from __future__ import annotations

import re
import time
from typing import Any

import requests

from qt.data.store import Store

UA = "QuantumTrader/0.1 research (contact: owner)"
_SECRET = re.compile(r"(api_?key|apikey|token)=[^&]+", re.I)


def redact(url: str) -> str:
    return _SECRET.sub(r"\1=***", url)


class ProviderError(RuntimeError):
    pass


def get_json(url: str, store: Store | None = None, max_age_s: float = 6 * 3600, headers: dict | None = None,
             session: requests.Session | None = None, retries: int = 3) -> Any:
    safe = redact(url)
    if store is not None:
        hit = store.cached(safe, max_age_s)
        if hit is not None:
            return hit
    s = session or requests
    last = None
    for attempt in range(retries):
        try:
            r = s.get(url, headers={"User-Agent": UA, **(headers or {})}, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                time.sleep(2 ** (attempt + 1))
                continue
            if r.status_code != 200:
                raise ProviderError(f"{safe}: HTTP {r.status_code} {redact(r.text[:200])}")
            body = r.json()
            if store is not None:
                store.put(safe, body)
            return body
        except requests.RequestException as e:
            last = type(e).__name__
            time.sleep(2 ** (attempt + 1))
    raise ProviderError(f"{safe}: {last}")
