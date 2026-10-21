"""Data verification (peer-review plan, stage 1): every number a decision leans on is checked in code
before anyone reads the dossier. No model calls, so a run costs nothing extra.

Four kinds of check, each with a stated source of truth:
- cross-source: the vendor's number against a second source. Reported revenue, EPS and shares against the
  filing itself (SEC XBRL); the daily close against FMP or Alpha Vantage;
- sanity: ranges that catch unit, currency and ADR mistakes (forward P/E, sales jump, EPS basis);
- freshness: the last bar and the latest macro value against as_of;
- sources: every link behind a judgment needs a quote copied from the opened page, so a search snippet
  cannot pass as a fact. With fetch on, the quote must appear on the page.

Each check ends in one status: ok (✓, two sources agree or a primary source quoted), single (◐, one
source, or nothing to compare against), bad (✗, sources disagree, out of range, stale, or a quote that
is not on its page). Nothing here blocks a trade: a ✗ is a flag for Ilay and the Critic.
"""
from __future__ import annotations

import html as H
import re
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from qt.policy import load_policy

BADGE = {"ok": "✓", "single": "◐", "bad": "✗"}
REVENUE_TAGS = (("us-gaap", "Revenues"), ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
                ("us-gaap", "SalesRevenueNet"), ("ifrs-full", "Revenue"))
EPS_TAGS = (("us-gaap", "EarningsPerShareDiluted"), ("ifrs-full", "DilutedEarningsLossPerShare"),
            ("ifrs-full", "DilutedEarningsLossPerShareFromContinuingOperations"))
ANNUAL_FORMS = ("10-K", "20-F", "40-F", "10-K/A", "20-F/A")


@dataclass
class Check:
    subject: str                 # ticker, "macro" or "sources"
    field: str                   # revenue_fy, eps_fy, shares, close, pe1, bars_age, ism_pmi, source ...
    label: str                   # Hebrew, for the report
    status: str                  # ok | single | bad
    value: Any = None
    source: str = ""
    second: Any = None
    second_source: str = ""
    diff_pct: float | None = None
    note: str = ""


def _v():
    return load_policy().verification


def diff_pct(a: float, b: float) -> float:
    return abs(a - b) / max(abs(b), 1e-12) * 100


def adr_ratio(a: float, b: float) -> int | None:
    """k when a/b (or b/a) is one of the usual ADR ratios, within the statement tolerance."""
    if not a or not b or (a > 0) != (b > 0):
        return None
    r = max(a / b, b / a)
    return next((k for k in _v().adr_ratios if abs(r - k) / k * 100 <= _v().statement_tol_pct), None)


# ------------------------------------------------------------------ SEC
def sec_annual(facts: dict, tags: Iterable[tuple[str, str]], per_share: bool = False) -> dict[str, tuple[float, str, str]]:
    """{fiscal year end: (value, unit, tag)} from annual filings, first tag that has data wins per year."""
    out: dict[str, tuple[float, str, str]] = {}
    f = facts.get("facts", {})
    for tax, tag in tags:
        for unit, rows in (f.get(tax, {}).get(tag, {}).get("units") or {}).items():
            if per_share != ("/shares" in unit):
                continue
            for r in rows:
                if r.get("fp") == "FY" and r.get("form") in ANNUAL_FORMS and _full_year(r):
                    out.setdefault(r["end"], (float(r["val"]), unit.split("/")[0], tag))
    return out


def _full_year(r: dict) -> bool:
    if "start" not in r:
        return True
    return (date.fromisoformat(r["end"]) - date.fromisoformat(r["start"])).days > 300


def sec_shares(facts: dict) -> tuple[float, str] | None:
    rows = facts.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding", {}).get("units", {}).get("shares")
    if not rows:
        return None
    r = max(rows, key=lambda x: (x["end"], x.get("filed", "")))
    return float(r["val"]), r["end"]


def sec_annual_all(facts: dict, tags: Iterable[tuple[str, str]], per_share: bool) -> list[dict[str, tuple[float, str, str]]]:
    """One series per tag: companies report revenue under more than one tag, and vendors pick different ones."""
    return [s for s in (sec_annual(facts, [t], per_share) for t in tags) if s]


def _match_year(series: dict[str, tuple], end: str) -> tuple | None:
    """The filing value for the same fiscal year end (±10 days: 52/53-week years)."""
    e = date.fromisoformat(end[:10])
    return next((v for k, v in series.items() if abs((date.fromisoformat(k) - e).days) <= 10), None)


def statement_checks(sym: str, idea: dict, facts: dict | None, sec_src: str) -> list[Check]:
    ann = idea["fundamentals"].get("annual", {})
    out: list[Check] = []
    tol = _v().statement_tol_pct
    for field, label, key, tags, per_share in (
            ("revenue_fy", "הכנסות שנתיות אחרונות", "TotalRevenue", REVENUE_TAGS, False),
            ("eps_fy", "EPS מדולל שנתי אחרון", "DilutedEPS", EPS_TAGS, True)):
        rows = ann.get(key) or []
        if not rows:
            continue
        end, val = rows[-1][0][:10], rows[-1][1]
        c = Check(sym, field, label, "single", val, "yahoo:fundamentals", note=f"שנה שמסתיימת ב-{end}")
        if facts is None:
            c.note += " · אין דוח ב-SEC להשוואה"
            out.append(c)
            continue
        hits = [h for h in (_match_year(x, end) for x in sec_annual_all(facts, tags, per_share)) if h]
        hit = min(hits, key=lambda h: diff_pct(val, h[0])) if hits else None
        if hit is None:
            c.note += " · השנה הזו לא נמצאה בדוחות ב-SEC"
        else:
            sv, unit, tag = hit
            c.second, c.second_source, c.diff_pct = sv, f"{sec_src}/{tag}", diff_pct(val, sv)
            k = adr_ratio(val, sv) if per_share else None
            if unit != "USD":
                c.status, c.note = "bad", c.note + f" · הדוח ב-SEC במטבע {unit}, והמחיר בדולר: לבדוק בסיס מטבע לפני כל מכפיל"
            elif c.diff_pct <= tol:
                c.status = "ok"
            elif k:
                c.status, c.note = "single", c.note + f" · פער של פי {k}: כנראה בסיס ADR (ADR אחד = {k} מניות). לאמת מול יחס ה-ADR"
            else:
                c.status, c.note = "bad", c.note + f" · פער של {c.diff_pct:.1f}% מהדוח"
        out.append(c)
    return out


def market_cap_check(sym: str, idea: dict, facts: dict | None, sec_src: str) -> Check | None:
    mc, price = idea["quote"].get("market_cap"), idea["plan"]["entry"]
    sh = sec_shares(facts) if facts else None
    if not mc or not price:
        return None
    c = Check(sym, "market_cap", "שווי שוק", "single", mc, "yahoo:quote")
    if sh is None:
        c.note = "אין מספר מניות ב-SEC להשוואה"
        return c
    implied = price * sh[0]
    c.second, c.second_source, c.diff_pct = implied, f"{sec_src}/dei:EntityCommonStockSharesOutstanding@{sh[1]}", diff_pct(mc, implied)
    k = adr_ratio(mc, implied)
    if c.diff_pct <= _v().market_cap_tol_pct:
        c.status, c.note = "ok", "מחיר × מספר המניות בדוח האחרון"
    elif k:
        c.status, c.note = "single", f"פי {k} ממחיר × מניות: בסיס ADR (ADR אחד = {k} מניות)"
    else:
        c.status, c.note = "bad", f"פער של {c.diff_pct:.0f}% ממחיר × מספר המניות בדוח {sh[1]}"
    return c


# ------------------------------------------------------------------ price
def close_check(sym: str, idea: dict, as_of: date, second_close: Callable[[str, date], tuple[float, str]] | None) -> Check:
    close = idea["plan"]["entry"]
    last = idea["chart"]["dates"][-1]
    c = Check(sym, "close", f"סגירה ב-{last}", "single", close, "yahoo:chart")
    if second_close is None:
        c.note = "אין מקור שני למחיר בסביבה הזו"
        return c
    try:
        v, src = second_close(sym, date.fromisoformat(last))
    except Exception as ex:                                              # noqa: BLE001 - reported on the check
        c.note = f"המקור השני לא זמין: {str(ex)[:80]}"
        return c
    c.second, c.second_source, c.diff_pct = v, src, diff_pct(close, v)
    c.status = "ok" if c.diff_pct <= _v().price_tol_pct else "bad"
    if c.status == "bad":
        c.note = f"פער של {c.diff_pct:.2f}% בין המקורות"
    return c


# ------------------------------------------------------------------ sanity and freshness
def sanity_checks(sym: str, idea: dict) -> list[Check]:
    v, row, out = _v(), idea["row"], []
    pe1 = row.get("pe1")
    if pe1 is not None and idea["direction"] == "long":
        lo, hi = v.pe_range
        bad = not (lo < pe1 <= hi)
        out.append(Check(sym, "pe1", "מכפיל צפוי (F1)", "bad" if bad else "ok", pe1, "code: price / F1",
                         note=f"מחוץ לטווח {lo}–{hi}: לבדוק יחידות, מטבע ו-ADR" if bad else f"בטווח {lo}–{hi}"))
    s0, s1 = row.get("sales"), row.get("sales_f1")
    if s0 and s1:
        r = s1 / s0
        bad = r > v.sales_f1_max_ratio or r <= 0
        out.append(Check(sym, "sales_f1", "תחזית הכנסות מול השנה שעברה", "bad" if bad else "ok", s1, "yahoo:earningsTrend",
                         second=s0, note=f"יחס {r:.2f}" + (": קפיצה לא סבירה, לבדוק בסיס ומטבע" if bad else "")))
    ratio = idea["valuation"].get("eps_basis_ratio")
    if ratio is not None:
        lo, hi = v.eps_basis_range
        off = not (lo <= ratio <= hi)
        # by risk: a basis gap matters when the chosen target is the own multiple, which stands on that EPS
        rests = idea["plan"].get("target_source") == "own_multiple_target"
        status = "ok" if not off else "bad" if rests else "single"
        note = f"יחס {ratio:.2f}"
        if off:
            note += f", מחוץ ל-{lo}–{hi}: בסיס שונה (ADR, מטבע, GAAP מול מתואם)"
            note += ". היעד שנבחר נשען על המכפיל הזה" if rests else ". היעד שנבחר לא נשען עליו"
        out.append(Check(sym, "eps_basis", "EPS בדוחות מול F0 של האנליסטים", status, ratio, "code", note=note))
    return out


def bars_age_check(sym: str, idea: dict, as_of: date) -> Check:
    last = date.fromisoformat(idea["chart"]["dates"][-1])
    age = (as_of - last).days
    ok = age <= _v().bars_max_age_days
    return Check(sym, "bars_age", "עדכניות המחיר", "ok" if ok else "bad", last.isoformat(), "yahoo:chart",
                 note=f"{age} ימים לפני {as_of}" + ("" if ok else ": מחיר ישן"))


def _months_between(a: str, as_of: date) -> int:
    y, m = int(a[:4]), int(a[5:7])
    return (as_of.year - y) * 12 + as_of.month - m


def macro_checks(macro: dict, as_of: date) -> list[Check]:
    v, out = _v(), []
    for name, ch in (macro.get("charts") or {}).items():
        dates = ch.get("dates") or []
        if not dates:
            continue
        last = dates[-1]
        if len(last) == 10:
            age = (as_of - date.fromisoformat(last)).days
            ok, note = age <= v.daily_max_age_days, f"{age} ימים לפני {as_of}"
        else:
            lag, allowed = _months_between(last, as_of), (v.monthly_lag_overrides or {}).get(name, v.monthly_max_lag)
            ok, note = lag <= allowed, f"{lag} חודשים לפני {as_of:%Y-%m} (מותר {allowed})"
        out.append(Check("macro", name, f"עדכניות: {name}", "ok" if ok else "bad", last,
                         (macro.get("sources") or {}).get(name, "manual" if name in (macro.get("manual_sources") or {}) else ""),
                         note=note + ("" if ok else ": ערך ישן")))
    for name, months in (macro.get("filled_months") or {}).items():
        out.append(Check("macro", f"{name}_gap", f"חודש חסר: {name}", "single", ", ".join(months), "fred",
                         note="חודש חסר במקור; הקוד מילא אותו (מסומן)"))
    return out


# ------------------------------------------------------------------ sources behind judgments
def _domain(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


def _in(domain: str, domains: Iterable[str]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in domains)


def _norm(t: str) -> str:
    t = H.unescape(re.sub(r"<[^>]+>", " ", t))
    t = t.replace("’", "'").replace("“", '"').replace("”", '"').replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", t).strip().lower()


def quote_on_page(quote: str, page: str) -> bool:
    """Every piece of a quote (split on '...') appears on the page, ignoring markup, case and spacing."""
    p = _norm(page)
    return all(_norm(part) in p for part in re.split(r"\.\.\.|…", quote) if _norm(part))


def collect_sources(judgments: dict, ideas: dict, macro: dict) -> list[tuple[str, dict]]:
    """(subject, {title, url, quote?}) for every link in the judgments, the ideas and the manual macro."""
    out: list[tuple[str, dict]] = []

    def walk(subject: str, x: Any) -> None:
        if isinstance(x, dict):
            if isinstance(x.get("url"), str):
                out.append((subject, x))
            for k, v in x.items():
                if k != "ideas":
                    walk(subject, v)
        elif isinstance(x, list):
            for v in x:
                walk(subject, v)
    walk("macro", judgments)
    walk("macro", macro.get("manual_sources") or {})
    for sym, i in ideas.items():
        walk(sym, i.get("judgment") or {})
    seen, uniq = set(), []
    for s, x in out:
        if (s, x["url"]) not in seen:
            seen.add((s, x["url"]))
            uniq.append((s, x))
    return uniq


def source_check(subject: str, src: dict, company_domain: str | None, fetch: Callable[[str], str] | None) -> Check:
    v = _v()
    url, quote = src["url"], (src.get("quote") or "").strip()
    d = _domain(url)
    primary = _in(d, v.primary_domains) or (company_domain and _in(d, [company_domain]))
    kind = "מקור ראשוני" if primary else "אתר מצרף" if _in(d, v.aggregator_domains) else "מקור משני"
    c = Check(subject, "source", src.get("title") or d, "single", url, d, note=kind)
    if not quote:
        c.note += " · אין ציטוט מהעמוד: ייתכן שנלקח מתקציר חיפוש בלי לפתוח"
        return c
    if fetch is None:
        c.note += " · יש ציטוט, העמוד לא נבדק"
        c.status = "ok" if primary else "single"
        return c
    try:
        page = fetch(url)
    except Exception as ex:                                              # noqa: BLE001 - reported on the check
        c.note += f" · יש ציטוט, העמוד לא נפתח ({str(ex)[:60]})"
        c.status = "ok" if primary else "single"
        return c
    if quote_on_page(quote, page):
        c.status, c.note = "ok", c.note + " · הציטוט נמצא בעמוד"
    else:
        c.status, c.note = "bad", c.note + " · הציטוט לא נמצא בעמוד"
    return c


# ------------------------------------------------------------------ the stage
def _company_domain(idea: dict) -> str | None:
    w = (idea.get("profile") or {}).get("website")
    return _domain(w) if w else None


def run(d: dict, *, sec: Any = None, second_close: Callable[[str, date], tuple[float, str]] | None = None,
        fetch: Callable[[str], str] | None = None) -> dict[str, Any]:
    """Checks for a dossier. sec: an object with cik(ticker) and company_facts(cik) (qt.data.providers.EDGAR),
    or None when SEC is out of reach. Every network failure becomes a ◐ on its check, never an exception."""
    as_of = date.fromisoformat(d["meta"]["as_of"])
    checks: list[Check] = []
    for sym, i in d["ideas"].items():
        facts, src = None, "sec:companyfacts"
        if sec is not None:
            try:
                cik = sec.cik(sym)
                if cik:
                    facts, src = sec.company_facts(cik)
            except Exception:                                            # noqa: BLE001 - becomes ◐ below
                facts = None
        checks += statement_checks(sym, i, facts, src)
        mc = market_cap_check(sym, i, facts, src)
        if mc:
            checks.append(mc)
        checks.append(close_check(sym, i, as_of, second_close))
        checks.append(bars_age_check(sym, i, as_of))
        checks += sanity_checks(sym, i)
    checks += macro_checks(d.get("macro") or {}, as_of)
    for subject, s in collect_sources(d.get("judgments") or {}, d["ideas"], d.get("macro") or {}):
        cd = _company_domain(d["ideas"][subject]) if subject in d["ideas"] else None
        checks.append(source_check(subject, s, cd, fetch))
    rows = [asdict(c) for c in checks]
    return dict(checks=rows, summary=summarize(rows), sec=sec is not None, second_price=second_close is not None,
                fetched_pages=fetch is not None)


def summarize(rows: list[dict]) -> dict[str, Any]:
    by: dict[str, dict[str, int]] = {}
    for r in rows:
        s = by.setdefault(r["subject"], {"ok": 0, "single": 0, "bad": 0})
        s[r["status"]] += 1
    total = {k: sum(s[k] for s in by.values()) for k in ("ok", "single", "bad")}
    return dict(total=total, by_subject=by,
                worst={k: ("bad" if s["bad"] else "single" if s["single"] else "ok") for k, s in by.items()})


# ------------------------------------------------------------------ live sources
def default_sources(store: Any = None) -> dict[str, Any]:
    """EDGAR, a second close (FMP, then Alpha Vantage) and a page fetcher, each only if it answers here."""
    import os

    import requests

    from qt.data.providers import EDGAR, FMP, AlphaVantage

    out: dict[str, Any] = {"sec": EDGAR(store, contact=os.environ.get("SEC_CONTACT"))}
    try:
        out["sec"].cik("AAPL")
    except Exception:                                                    # noqa: BLE001 - SEC blocked
        out["sec"] = None
    av_cache: dict[str, dict[str, float]] = {}

    def second_close(sym: str, day: date) -> tuple[float, str]:
        errors = []
        if os.environ.get("FMP_API_KEY"):
            try:
                bars, src = FMP(store).daily_bars(sym, day - timedelta(days=7), day)
                b = next((b for b in bars if b.day == day), None)
                if b:
                    return b.close, src
                errors.append("fmp: no bar")
            except Exception as ex:                                      # noqa: BLE001 - try the next source
                errors.append(f"fmp: {str(ex)[:60]}")
        if os.environ.get("ALPHAVANTAGE_API_KEY"):
            if sym not in av_cache:
                av_cache[sym] = AlphaVantage(store).daily_closes(sym)[0]
            if day.isoformat() in av_cache[sym]:
                return av_cache[sym][day.isoformat()], "alphavantage:TIME_SERIES_DAILY"
            errors.append("alphavantage: no bar")
        raise RuntimeError("; ".join(errors) or "no second price source configured")

    out["second_close"] = second_close if (os.environ.get("FMP_API_KEY") or os.environ.get("ALPHAVANTAGE_API_KEY")) else None

    def fetch(url: str) -> str:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (QuantumTrader verification)"}, timeout=20)
        r.raise_for_status()
        if "pdf" in r.headers.get("content-type", "") or url.lower().endswith(".pdf"):
            raise RuntimeError("PDF: not read in code")
        return r.text

    out["fetch"] = fetch
    return out
