"""Fetch an HTML/text document, turn it into plain text, and cache only the transformed result.

Mirrors http.get_json (polite retry, redacted URLs in errors and cache keys). Filings can be up to ~10MB of
HTML, so the cache holds the text (or whatever `transform` returns), never the raw page.
"""
from __future__ import annotations

import re
import threading
import time
import warnings
from collections.abc import Callable
from typing import Any

import requests

from qt.data.http import UA, ProviderError, redact
from qt.data.store import Store

_SPACES = re.compile(r"[ \t\r\f\v\u00a0\u2002\u2009\u200b]+")
_BLANKS = re.compile(r"\n\s*\n\s*\n+")


class RateLimiter:
    """At most one request per `min_interval_s` (SEC asks for <= 10 requests a second; 0.15s keeps us near 7)."""

    def __init__(self, min_interval_s: float = 0.15):
        self.min_interval_s = min_interval_s
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            delay = self._last + self.min_interval_s - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


_BLOCKS = ("p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol",
           "section", "article", "header", "footer", "blockquote", "pre", "title", "dt", "dd", "center")


def html_to_text(html: str) -> str:
    """Visible text, one line per block element, runs of spaces collapsed; drops script, style and ix:header.

    Inline elements (span, b, a, ix:nonFraction ...) are joined without a break, the way a browser renders
    them, so a heading split over spans ("Item 1A." + "Risk Factors") stays on one line.
    """
    from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(lambda t: t.name in ("script", "style") or t.name.lower() == "ix:header"):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(_BLOCKS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    text = soup.get_text("")
    lines = [_SPACES.sub(" ", ln).strip() for ln in text.split("\n")]
    return _BLANKS.sub("\n\n", "\n".join(lines)).strip()


def get_text(url: str, store: Store | None = None, max_age_s: float = 30 * 86400, headers: dict | None = None,
             session: requests.Session | None = None, retries: int = 3,
             transform: Callable[[str], Any] | None = None, key: str = "",
             limiter: RateLimiter | None = None) -> Any:
    """GET `url`, return transform(text) (default: the raw response text). The transformed value is cached."""
    safe = redact(url)
    ck = f"text:{key}:{safe}"
    if store is not None:
        hit = store.cached(ck, max_age_s)
        if hit is not None:
            return hit
    s = session or requests
    last = None
    for attempt in range(retries):
        try:
            if limiter is not None:
                limiter.wait()
            r = s.get(url, headers={"User-Agent": UA, **(headers or {})}, timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                time.sleep(2 ** (attempt + 1))
                continue
            if r.status_code != 200:
                raise ProviderError(f"{safe}: HTTP {r.status_code}")
            out = transform(r.text) if transform else r.text
            if store is not None:
                store.put(ck, out)
            return out
        except requests.RequestException as e:
            last = type(e).__name__
            time.sleep(2 ** (attempt + 1))
    raise ProviderError(f"{safe}: {last}")
