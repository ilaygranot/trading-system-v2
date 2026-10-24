"""The monthly macro file without a person: ISM PMI / Services PMI, the ISM industry tables, NFIB optimism and
NAAIM, fetched from public pages and merged into the `manual_macro` upload the runs already read.

Start: the latest manual_macro upload, else the seed history in macro_seed.yaml. A month already in the file is
never overwritten (the seed note: originals kept, later revisions ignored); only new months and new NAAIM weeks are
added. Every value passes three checks before it is added (not from course: the bounds):
  range    ISM 25..75, NFIB 75..115, NAAIM -200..200
  jump     at most 8 points from the month before for ISM, 10 for NFIB
  month    a report month before the run's month, and not more than 3 months after the last month known
Each series has a primary source and a second one; when both answer for the same month and differ by more than
0.15 the value is not added and the disagreement is flagged. When every source fails the file stays as it was and
the series is reported stale: the runs go on with the last months known and say so.

Sources (ISM's own pages are behind Cloudflare, so the press releases come first):
  ISM      PR Newswire's ISM newsroom (the release title carries value, month and year; the release body carries
           the industries and Short Supply), then ismworld.org's report page, then Google News RSS titles
  NFIB     the SBET report PDF on nfib.com, then Google News RSS titles
  NAAIM    the exposure index page on naaim.org (since August 2026 the current number may be for subscribers
           only: then NAAIM stays missing in the sentiment, as before)
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus

import yaml

MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december")
_MON = "(" + "|".join(m.capitalize() for m in MONTHS) + ")"
BOUNDS = {"ism_pmi": (25.0, 75.0), "ism_nmi": (25.0, 75.0), "small_business": (75.0, 115.0)}
MAX_JUMP = {"ism_pmi": 8.0, "ism_nmi": 8.0, "small_business": 10.0}
NAAIM_BOUNDS = (-200.0, 200.0)
AGREE = 0.15                     # two sources for the same month must agree within this
MAX_AHEAD = 3                    # months after the last month known
SEED = Path(__file__).with_name("macro_seed.yaml")
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

PRN_ISM = "https://www.prnewswire.com/news/institute-for-supply-management/"
ISM_PAGE = {"ism_pmi": "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-report-on-business/pmi/{month}/",
            "ism_nmi": "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-report-on-business/services/{month}/"}
GNEWS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
NFIB_PDF = ("https://www.nfib.com/wp-content/uploads/{py}/{pm:02d}/NFIB-SBET-Report-{Month}-{y}.pdf",
            "https://www.nfib.com/wp-content/uploads/{py}/{pm:02d}/NFIB-SBET-Report-{Mon}.-{y}.pdf",
            "https://www.nfib.com/wp-content/uploads/{py}/{pm:02d}/SBET-{Month}-{y}.pdf")
NAAIM_PAGE = "https://naaim.org/programs/naaim-exposure-index/"
KIND = {"ism_pmi": "Manufacturing", "ism_nmi": "Services"}


def ym(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def add_months(m: str, k: int) -> str:
    n = int(m[:4]) * 12 + int(m[5:7]) - 1 + k
    return f"{n // 12:04d}-{n % 12 + 1:02d}"


def _month_of(name: str, year: str | int) -> str:
    return f"{int(year):04d}-{MONTHS.index(name.lower()) + 1:02d}"


@dataclass
class Reading:
    series: str
    month: str                   # YYYY-MM for the monthly series, YYYY-MM-DD for NAAIM
    value: float
    url: str
    quote: str
    extra: dict[str, Any] = field(default_factory=dict)   # ISM tables: growing, contracting, short_supply


# ------------------------------------------------------------------------------------------ parsers (pure)

def _text(html: str) -> str:
    from qt.data.textfetch import html_to_text
    return html_to_text(html)


def _clean(s: str) -> str:
    return re.sub(r"[®™*]", "", s).replace("—", "-").replace("–", "-")


_PRN_SLUG = re.compile(r"/news-releases/(manufacturing|services)-pmi-at-(\d{2})(?:-(\d))?-([a-z]+)-(\d{4})-[a-z0-9-]*?\d{6,}\.html")


def prn_releases(html: str) -> list[tuple[str, str, float, str]]:
    """(series, month, value, absolute url) for every ISM PMI release linked on the newsroom page."""
    out, seen = [], set()
    for m in _PRN_SLUG.finditer(html):
        kind, whole, dec, mon, year = m.groups()
        if mon not in MONTHS:
            continue
        url = "https://www.prnewswire.com" + m.group(0)
        if url in seen:
            continue
        seen.add(url)
        out.append(("ism_pmi" if kind == "manufacturing" else "ism_nmi", _month_of(mon, year),
                    float(f"{whole}.{dec or 0}"), url))
    return out


def ism_headline(text: str, series: str) -> tuple[str, float, str] | None:
    """'The Manufacturing PMI registered 54.5 percent in September' + the report's year -> (month, value, quote)."""
    t = _clean(text)
    m = re.search(rf"(?:The\s+)?{KIND[series]}\s+PMI\s+registered\s+(\d{{2}}(?:\.\d)?)\s+percent\s+in\s+{_MON}", t)
    if not m:
        return None
    mon = m.group(2)
    y = re.search(rf"{mon}\s+(\d{{4}})\s+(?:ISM\s+)?(?:{KIND[series]}\s+(?:PMI\s+)?ISM\s+)?(?:Report|{KIND[series]})", t) \
        or re.search(rf"{mon}\s+(\d{{4}})", t)
    if not y:
        return None
    return _month_of(mon, y.group(1)), float(m.group(1)), m.group(0)


def _names(s: str) -> list[str]:
    s = re.sub(r"\s+", " ", s).strip().rstrip(".")
    parts = [p.strip() for p in s.split(";")]
    return [re.sub(r"^and\s+", "", p).strip() for p in parts if p.strip()]


def ism_tables(text: str) -> dict[str, Any]:
    """The manufacturing report's industries growing / contracting (S05-R6) and Commodities in Short Supply with
    the report's month count (S05-R9). Missing parts are left out."""
    t = _clean(text).replace("\n", " ")
    out: dict[str, Any] = {}
    for key, word in (("growing", "growth"), ("contracting", "contraction")):
        m = re.search(rf"industries reporting (?:an? )?{word} in {_MON}[^:]{{0,80}}?(?:are|is)\s*:\s*(.+?)\.(?=\s+[A-Z]|\s*$)", t)
        if m:
            out[key] = _names(m.group(2))
    m = re.search(r"Commodities in Short Supply\s*(.+?)(?:Note\s*:|Buying Policy|The number of consecutive months|$)", t)
    if m:
        sup = {re.sub(r"\s+", " ", n).strip(): int(k)
               for n, k in re.findall(r"([A-Z][A-Za-z0-9&,'/\- ]{1,60}?)\s*\((\d{1,3})\)", m.group(1))}
        if sup or re.search(r"\bNo\s+commodit", m.group(1)):
            out["short_supply"] = sup
    return out


_GN_ITEM = re.compile(r"<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>.*?<pubDate>(.*?)</pubDate>", re.S)


def gnews_items(xml: str) -> list[tuple[str, str, str]]:
    import html as H
    return [(H.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", t)).strip(), l.strip(), p.strip())
            for t, l, p in _GN_ITEM.findall(xml)]


def gnews_ism(xml: str, series: str) -> list[tuple[str, float, str, str]]:
    """'Manufacturing PMI® at 54.5%; September 2026 ISM® Manufacturing PMI® Report' -> (month, value, title, link)."""
    out = []
    for title, link, _ in gnews_items(xml):
        t = _clean(title)
        m = re.search(rf"^{KIND[series]}\s+PMI\s+at\s+(\d{{2}}(?:\.\d)?)%\W+{_MON}\s+(\d{{4}})\s+ISM", t)
        if m:
            out.append((_month_of(m.group(2), m.group(3)), float(m.group(1)), title, link))
    return out


def _pub_month(pub: str) -> str | None:
    from email.utils import parsedate_to_datetime
    try:
        return ym(parsedate_to_datetime(pub).date())
    except (TypeError, ValueError):
        return None


def gnews_nfib(xml: str) -> list[tuple[str, float, str, str]]:
    """'NFIB Small Business Optimism Index Rises to 98.2 in October' -> (month, value, title, link). The year comes
    from the publication date (the report for month M is published in M+1)."""
    out = []
    for title, link, pub in gnews_items(xml):
        m = re.search(rf"Optimism(?:\s+Index)?\D{{0,40}}?(?:to|at|of)\s+(\d{{2,3}}\.\d)\s+in\s+{_MON}", title, re.I)
        pm = _pub_month(pub)
        if not m or not pm:
            continue
        mon = MONTHS.index(m.group(2).lower()) + 1
        y = int(pm[:4]) - (1 if mon > int(pm[5:7]) else 0)
        out.append((f"{y:04d}-{mon:02d}", float(m.group(1)), title, link))
    return out


def nfib_pdf_value(text: str, month: str) -> tuple[float, str] | None:
    """The SBET report's first page: 'The NFIB Small Business Optimism Index fell 0.8 points in September to 97.9'."""
    t = re.sub(r"\s+", " ", text[:6000])
    name = MONTHS[int(month[5:7]) - 1].capitalize()
    pats = (rf"Optimism Index.{{0,120}}?\bin {name}\b.{{0,40}}?\bto (\d{{2,3}}\.\d)",
            rf"Optimism Index.{{0,120}}?\bto (\d{{2,3}}\.\d).{{0,40}}?\bin {name}\b",
            rf"Optimism Index.{{0,60}}?\b(?:at|was) (\d{{2,3}}\.\d)",
            rf"\b(\d{{2,3}}\.\d) in {name}\b")
    for p in pats:
        m = re.search(p, t)
        if m:
            return float(m.group(1)), m.group(0)
    return None


def naaim_value(text: str) -> tuple[float, str] | None:
    t = re.sub(r"\s+", " ", text)
    m = re.search(r"NAAIM Exposure Index (?:number )?(?:is|this week|\(this week\))\s*:?\s*(-?\d{1,3}(?:\.\d+)?)", t, re.I)
    return (float(m.group(1)), m.group(0)) if m else None


def last_wednesday(d: date) -> date:
    """NAAIM surveys its members on Wednesday and publishes Thursday: the reading's date is that Wednesday."""
    back = (d.weekday() - 2) % 7
    if back == 0 and d.weekday() == 2:
        back = 7                                  # Wednesday: this week's number is not out until Thursday
    return d - timedelta(days=back)


# ------------------------------------------------------------------------------------------ fetching

class Http:
    """GET with a browser User-Agent; raises on anything but 200."""

    def __init__(self, timeout: float = 30):
        import requests
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9",
                               "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
        self.timeout = timeout

    def bytes(self, url: str, retries: int = 3) -> bytes:
        """A 429 or 5xx is asked again after 2, 4 s (Google News answered 503 to the dry run of 24.10)."""
        import time
        for attempt in range(retries):
            r = self.s.get(url, timeout=self.timeout)
            if r.status_code == 200:
                return r.content
            if r.status_code != 429 and r.status_code < 500 or attempt == retries - 1:
                break
            time.sleep(2 ** (attempt + 1))
        raise RuntimeError(f"HTTP {r.status_code}")

    def text(self, url: str) -> str:
        return self.bytes(url).decode("utf-8", errors="replace")


def _pdf_text(blob: bytes) -> str:
    from pypdf import PdfReader
    r = PdfReader(io.BytesIO(blob))
    return "\n".join((p.extract_text() or "") for p in r.pages[:3])


class Sources:
    """Each method returns the readings it found and records what failed in `errors`."""

    def __init__(self, http: Http | None = None):
        self.http = http or Http()
        self.errors: dict[str, list[str]] = {}

    def _err(self, series: str, where: str, e: Exception | str) -> None:
        self.errors.setdefault(series, []).append(f"{where}: {e if isinstance(e, str) else type(e).__name__ + ': ' + str(e)[:120]}")

    def _try(self, series: str, where: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as e:                                       # noqa: BLE001 - every source is optional
            self._err(series, where, e)
            return None

    # ---- ISM
    def ism_prn(self, wanted: set[str]) -> list[Reading]:
        html = self._try("ism", "prnewswire", lambda: self.http.text(PRN_ISM))
        if not html:
            return []
        out = []
        rel = prn_releases(html)
        if not rel:
            self._err("ism", "prnewswire", "no ISM release links on the newsroom page")
        for series, month, value, url in rel:
            if (series, month) not in wanted:
                continue
            extra, quote = {}, f"{KIND[series]} PMI at {value}%; {MONTHS[int(month[5:]) - 1].capitalize()} {month[:4]}"
            body = self._try(series, "prnewswire release", lambda u=url: _text(self.http.text(u)))
            if body:
                h = ism_headline(body, series)
                if h and h[0] == month:
                    if abs(h[1] - value) > 1e-9:
                        self._err(series, "prnewswire", f"title {value} but body {h[1]} for {month}")
                        continue
                    quote = h[2]
                if series == "ism_pmi":
                    extra = ism_tables(body)
            out.append(Reading(series, month, value, url, quote, extra))
        return out

    def ism_site(self, series: str, month: str) -> Reading | None:
        url = ISM_PAGE[series].format(month=MONTHS[int(month[5:]) - 1])
        html = self._try(series, "ismworld", lambda: self.http.text(url))
        if not html:
            return None
        body = _text(html)
        h = ism_headline(body, series)
        if not h or h[0] != month:
            self._err(series, "ismworld", f"no {month} headline on the page")
            return None
        return Reading(series, month, h[1], url, h[2], ism_tables(body) if series == "ism_pmi" else {})

    def ism_gnews(self, series: str, month: str) -> Reading | None:
        name = MONTHS[int(month[5:]) - 1].capitalize()
        url = GNEWS.format(q=quote_plus(f'"{KIND[series]} PMI" ISM {name} {month[:4]}'))
        xml = self._try(series, "google news", lambda: self.http.text(url))
        for m, v, title, link in gnews_ism(xml or "", series):
            if m == month:
                return Reading(series, month, v, link, title)
        if xml is not None:
            self._err(series, "google news", f"no {month} title")
        return None

    # ---- NFIB
    def nfib_pdf(self, month: str) -> Reading | None:
        y, mo = int(month[:4]), int(month[5:])
        pub = date(y + mo // 12, mo % 12 + 1, 1)
        name = MONTHS[mo - 1].capitalize()
        for pat in NFIB_PDF:
            url = pat.format(py=pub.year, pm=pub.month, Month=name, Mon=name[:4] if name == "September" else name[:3], y=y)
            try:
                txt = _pdf_text(self.http.bytes(url))
            except Exception as e:                                   # noqa: BLE001 - try the next name
                self._err("small_business", "nfib pdf", f"{url.rsplit('/', 1)[-1]}: {type(e).__name__}: {str(e)[:60]}")
                continue
            v = nfib_pdf_value(txt, month)
            if v:
                return Reading("small_business", month, v[0], url, v[1])
            self._err("small_business", "nfib pdf", f"{url.rsplit('/', 1)[-1]}: no index value in the text")
        return None

    def nfib_gnews(self, month: str) -> Reading | None:
        name = MONTHS[int(month[5:]) - 1].capitalize()
        url = GNEWS.format(q=quote_plus(f'NFIB "Small Business Optimism" {name} {month[:4]}'))
        xml = self._try("small_business", "google news", lambda: self.http.text(url))
        for m, v, title, link in gnews_nfib(xml or ""):
            if m == month:
                return Reading("small_business", month, v, link, title)
        if xml is not None:
            self._err("small_business", "google news", f"no {month} title with a value")
        return None

    # ---- NAAIM
    def naaim(self, today: date) -> Reading | None:
        html = self._try("naaim", "naaim.org", lambda: self.http.text(NAAIM_PAGE))
        if not html:
            return None
        v = naaim_value(_text(html))
        if not v:
            self._err("naaim", "naaim.org", "no current number on the page (subscribers only?)")
            return None
        return Reading("naaim", last_wednesday(today).isoformat(), v[0], NAAIM_PAGE, v[1])


# ------------------------------------------------------------------------------------------ merge + checks

def base_file(latest: dict | None) -> dict:
    """The latest upload over the seed: the seed fills only months the upload does not have."""
    seed = yaml.safe_load(SEED.read_text(encoding="utf-8")) or {}
    try:
        up = (yaml.safe_load(latest["content"]) or {}) if latest else {}
    except yaml.YAMLError:
        up = {}
    if not isinstance(up, dict):
        up = {}
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in up.items()}
    for k, v in seed.items():
        if k == "sources":
            src = out.setdefault("sources", {})
            for s, lst in v.items():
                src.setdefault(s, list(lst))
        elif isinstance(v, dict):
            cur = {str(m): x for m, x in (out.get(k) or {}).items()}
            out[k] = {**{str(m): x for m, x in v.items()}, **cur}
    return out


def check(series: str, month: str, value: float, known: dict, as_of: date) -> str | None:
    """None when the value may be added, else why not."""
    if series == "naaim":
        lo, hi = NAAIM_BOUNDS
        if not lo <= value <= hi:
            return f"{value} outside {lo}..{hi}"
        return "date after the run" if month > as_of.isoformat() else None
    lo, hi = BOUNDS[series]
    if not lo <= value <= hi:
        return f"{value} outside {lo}..{hi}"
    if month >= ym(as_of):
        return f"{month} is not before the run's month"
    prev = sorted(m for m in known if m < month)
    if prev:
        if month > add_months(max(known), MAX_AHEAD):
            return f"{month} is more than {MAX_AHEAD} months after the last month known ({max(known)})"
        jump = abs(value - float(known[prev[-1]]))
        if jump > MAX_JUMP[series]:
            return f"{value} is {jump:.1f} from {prev[-1]} ({known[prev[-1]]})"
    return None


def expected_month(series: str, as_of: date) -> str:
    """The last month whose report is out by `as_of`: ISM on the first business days of the next month (allow 5
    days), NFIB on the second Tuesday (allow up to the 16th). Not from course: the release calendar."""
    lag_day = 6 if series.startswith("ism") else 16
    back = 1 if as_of.day >= lag_day else 2
    return add_months(ym(as_of), -back)


def refresh(latest: dict | None, as_of: date, sources: Sources | None = None,
            now: datetime | None = None) -> dict[str, Any]:
    """The merged file and a report: {"content", "changed", "added", "rejected", "errors", "stale", "latest"}.
    `content` is the YAML to upload when `changed`."""
    src = sources or Sources()
    f = base_file(latest)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    added: dict[str, list[str]] = {}
    rejected: dict[str, list[str]] = {}
    srcs = f.setdefault("sources", {})

    def months_needed(series: str) -> list[str]:
        known = {str(m) for m in (f.get(series) or {})}
        last = max(known) if known else add_months(ym(as_of), -13)
        want, m = [], add_months(last, 1)
        while m <= expected_month(series, as_of):
            want.append(m)
            m = add_months(m, 1)
        return want

    def accept(r: Reading, second: Reading | None) -> None:
        known = {str(m): v for m, v in (f.get(r.series) or {}).items()}
        why = check(r.series, r.month, r.value, known, as_of)
        if why is None and second is not None and abs(second.value - r.value) > AGREE:
            why = f"sources disagree: {r.value} ({r.url}) vs {second.value} ({second.url})"
        if why:
            rejected.setdefault(r.series, []).append(f"{r.month}: {why}")
            return
        f.setdefault(r.series, {})[r.month] = r.value
        added.setdefault(r.series, []).append(r.month)
        entry = {"months": r.month, "url": r.url, "quote": r.quote, "note": f"auto {stamp}"}
        if second is not None:
            entry["checked"] = second.url
        srcs.setdefault(r.series, []).append(entry)
        if r.series == "ism_pmi":
            for key, field_ in (("growing", "ism_growing"), ("contracting", "ism_contracting"),
                                ("short_supply", "ism_short_supply")):
                if key in r.extra and r.month not in {str(m) for m in (f.get(field_) or {})}:
                    f.setdefault(field_, {})[r.month] = r.extra[key]
                    added.setdefault(field_, []).append(r.month)

    # ISM: one newsroom page answers every month; the ISM site and Google News check it or stand in for it
    need = {s: months_needed(s) for s in ("ism_pmi", "ism_nmi")}
    prn = {(r.series, r.month): r for r in src.ism_prn({(s, m) for s, ms in need.items() for m in ms})} \
        if any(need.values()) else {}
    for s, months in need.items():
        for m in months:
            first = prn.get((s, m)) or src.ism_site(s, m)
            second = src.ism_gnews(s, m)
            if first is None and second is None:
                continue
            accept(first or second, second if first is not None else None)
    for m in months_needed("small_business"):
        first, second = src.nfib_pdf(m), src.nfib_gnews(m)
        if first is None and second is None:
            continue
        accept(first or second, second if first is not None else None)
    nr = src.naaim(as_of)
    if nr is not None and nr.month not in {str(k) for k in (f.get("naaim") or {})}:
        accept(nr, None)

    latest_m = {s: max((str(m) for m in (f.get(s) or {})), default=None) for s in ("ism_pmi", "ism_nmi", "small_business")}
    stale = {s: dict(have=m, expected=expected_month(s, as_of)) for s, m in latest_m.items()
             if m is None or m < expected_month(s, as_of)}
    changed = bool(added) or latest is None
    head = (f"# The macro file built by qt.data.macro_auto on {stamp} (run {as_of.isoformat()}): the seed history plus\n"
            "# every month fetched since; each value's source is in `sources`.\n")
    content = head + yaml.safe_dump(f, allow_unicode=True, sort_keys=False, width=120)
    return dict(content=content, changed=changed, added=added, rejected=rejected, errors=src.errors, stale=stale,
                latest=latest_m)


def lines(rep: dict | None) -> list[str]:
    """For the Telegram summaries: what was added, and what is stale or was rejected."""
    if not rep:
        return []
    he = {"ism_pmi": "ISM תעשייה", "ism_nmi": "ISM שירותים", "small_business": "NFIB", "naaim": "NAAIM",
          "ism_growing": "תעשיות ISM", "ism_contracting": "תעשיות מתכווצות", "ism_short_supply": "Short Supply"}
    out = []
    if rep.get("error"):
        return [f"⚠️ איסוף המקרו האוטומטי נכשל ({rep['error']}): הריצה ממשיכה עם הקובץ הקודם"]
    if rep.get("added"):
        out.append("מקרו אוטומטי: נוספו " + " · ".join(f"{he.get(k, k)} {', '.join(v)}" for k, v in rep["added"].items()))
    for s, v in (rep.get("stale") or {}).items():
        errs = [e[:70] for e in ((rep.get("errors") or {}).get(s, []) + (rep.get("errors") or {}).get("ism", [])
                                 if s.startswith("ism") else (rep.get("errors") or {}).get(s, []))[:2]]
        out.append(f"⚠️ {he.get(s, s)}: הנתון האחרון {v['have'] or 'אין'}, מצופה {v['expected']}. לא נמצא אוטומטית"
                   + (f" ({'; '.join(errs)})" if errs else "") + ": הריצה משתמשת בחודש האחרון הידוע")
    for s, v in (rep.get("rejected") or {}).items():
        out.append(f"⚠️ {he.get(s, s)} נדחה בבדיקה: " + "; ".join(v))
    return out


def run(db, as_of: date, sources: Sources | None = None) -> dict[str, Any]:
    """Refresh the manual_macro upload in the database. Returns the report (without the content)."""
    from qt.approvals import Queue
    q = Queue(db)
    rep = refresh(q.latest_upload("manual_macro"), as_of, sources)
    if rep["changed"]:
        rep["upload_id"] = q.upload("manual_macro", f"auto-{as_of.isoformat()}.yaml", rep["content"])
    rep.pop("content")
    return rep
