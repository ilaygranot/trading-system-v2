"""SEC EDGAR document extracts for a deep-dive stock: recent 8-K/6-K events, the latest results release and the
annual-report risk factors (S07-R30, S07-R32). No LLM here; the output is the `documents` contract that the
research path attaches to the deep-dive dict (see SECFilings.documents).

Politeness: every request carries "QuantumTrader research <SEC_CONTACT>" (SEC answers 403 without a contact)
and never a browser User-Agent; requests are spaced by a RateLimiter (~7/s, SEC's limit is 10/s). Filings are
immutable, so document extracts are cached for a year; only the small transformed text is stored.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import date, timedelta
from typing import Any

import requests

from qt.data.http import ProviderError, get_json, redact
from qt.data.providers import EDGAR
from qt.data.store import Store
from qt.data.textfetch import RateLimiter, get_text, html_to_text

ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
CURRENT_FORMS = ("8-K", "8-K/A", "6-K")
ANNUAL_FORMS = ("10-K", "20-F", "40-F")
YEAR_S = 365 * 86400  # filings never change once accepted
SUBMISSIONS_MAX_AGE_S = 6 * 3600
MAX_CHARS = 22_000  # total size of documents() output, 6,000 of it the business section (not from course: LLM budget)
MAX_RESULT_CANDIDATES = 6  # results filings opened per stock (not from course: request budget)
MAX_RESULT_AGE_DAYS = 200  # "latest" results release: one half-year plus a margin (not from course; as results_prev)
MAX_EXHIBIT_BYTES = 4_000_000  # skip larger EX-99 files in the current-report list (not from course)
MAX_SEC_FAILURES = 4  # consecutive failed SEC requests that stop SEC for the rest of the run (not from course)
STOCK_DEADLINE_S = 180.0  # wall-clock budget for one stock's documents() (not from course: run time)

# The holding-company reorganisation in 2026 moved XOM's ticker to a new CIK; the old one keeps the 10-K.
ALIAS: dict[str, list[int]] = {"XOM": [34088]}

ITEM_KIND = {"2.02": "results", "1.01": "agreement", "2.01": "acquisition", "2.05": "restructuring",
             "2.06": "impairment", "5.02": "officer", "7.01": "reg_fd", "8.01": "other_event"}
GOVERNANCE_ITEMS = {"5.03", "5.05", "5.07", "5.08", "9.01"}
ITEM_NAMES = {
    "1.01": "Entry into a Material Definitive Agreement", "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership", "1.05": "Material Cybersecurity Incidents",
    "2.01": "Completion of Acquisition or Disposition of Assets", "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation", "2.04": "Triggering Events That Accelerate an Obligation",
    "2.05": "Costs Associated with Exit or Disposal Activities", "2.06": "Material Impairments",
    "3.01": "Notice of Delisting or Failure to Satisfy a Listing Rule", "3.02": "Unregistered Sales of Equity Securities",
    "3.03": "Material Modification to Rights of Security Holders", "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements", "5.01": "Changes in Control of Registrant",
    "5.02": "Departure/Election of Directors or Officers; Compensatory Arrangements",
    "5.03": "Amendments to Articles of Incorporation or Bylaws; Change in Fiscal Year",
    "5.05": "Amendments to the Code of Ethics", "5.07": "Submission of Matters to a Vote of Security Holders",
    "5.08": "Shareholder Director Nominations", "7.01": "Regulation FD Disclosure", "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}

# 6-K has no item codes: classify by exhibit/document title words.
_6K_RESULTS = re.compile(r"results|quarter|half[- ]year|interim|annual results|financial report|"
                         r"(?:three|six|nine|twelve) month periods? ended", re.IGNORECASE)
_6K_DIVIDEND = re.compile(r"dividend", re.IGNORECASE)
_6K_GOVERNANCE = re.compile(r"transaction in own shares|pdmr|total voting rights|voting results|annual general "
                            r"meeting|director declaration|block listing|form 8\.3|notice of meeting|"
                            r"information circular|directorate change|board committee", re.IGNORECASE)
_6K_ACQ = re.compile(r"acqui|merger|takeover|disposal|divest", re.IGNORECASE)
_6K_AGREEMENT = re.compile(r"agreement|contract", re.IGNORECASE)


def _acc_path(cik: int, accession: str) -> str:
    return f"{ARCHIVES}/{int(cik)}/{accession.replace('-', '')}"


def _cap(s: str | None, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _cap_list(items: list[str], total: int, each: int | None = None) -> list[str]:
    out, used = [], 0
    for it in items:
        it = _cap(it, each) if each else it
        if used + len(it) > total:
            break
        out.append(it)
        used += len(it)
    return out


def _iso(d: Any) -> str:
    return d.isoformat() if isinstance(d, date) else str(d)


# ---------------------------------------------------------------- pure parsers

def parse_index(html: str) -> list[dict]:
    """Rows of a filing's "<acc>-index.htm" document table: seq, description, document, type, size, url."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for table in soup.select("table.tableFile"):
        for tr in table.select("tr"):
            cells = tr.find_all("td")
            if len(cells) < 4:
                continue
            a = tr.find("a")
            href = (a.get("href") or "") if a else ""
            href = href.replace("/ix?doc=", "")
            if href.startswith("/"):
                href = "https://www.sec.gov" + href
            doc = cells[2].get_text(" ", strip=True).replace(" iXBRL", "").strip()
            size = cells[4].get_text(strip=True) if len(cells) > 4 else ""
            rows.append({"seq": cells[0].get_text(strip=True), "description": cells[1].get_text(" ", strip=True),
                         "document": doc, "type": cells[3].get_text(strip=True),
                         "size": int(size) if size.isdigit() else None, "url": href})
    return rows


def ex99(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["type"].upper().startswith("EX-99")]


_RF_10K_START = re.compile(r"^\s*item\s*1a\.?\s*[-–—:.]?\s*risk\s+factors\.?\s*$", re.IGNORECASE | re.MULTILINE)
_RF_10K_END = re.compile(r"^\s*item\s*(1b|1c|2|3|4|5|6)\s*[.:\-–—]?\s*[A-Za-z][^\n]{3,80}$", re.IGNORECASE | re.MULTILINE)
_RF_20F_START = re.compile(r"^\s*risk factors and risk management\s*\n(?:\s*\n)*\s*risk factors\s*$", re.IGNORECASE | re.MULTILINE)
_RF_20F_END = re.compile(r"^\s*Risk management\s*$", re.MULTILINE)
_RF_20F_STD_START = re.compile(r"^\s*(?:item\s*3\.?\s*)?D\.\s*risk\s+factors\s*$", re.IGNORECASE | re.MULTILINE)
_RF_20F_STD_END = re.compile(r"^\s*item\s*4\.?\s*[-–—:.]?\s*[A-Za-z][^\n]{3,80}$", re.IGNORECASE | re.MULTILINE)
_RF_40F_START = re.compile(r"^\s*D\.\s+RISK FACTORS\s*$", re.IGNORECASE | re.MULTILINE)
_RF_40F_END = re.compile(r"Form 51-101F1|Statement of Reserves Data", re.IGNORECASE)


def _longest(text: str, start: re.Pattern, end: re.Pattern) -> str:
    best = ""
    for m in start.finditer(text):
        e = end.search(text, m.end())
        span = text[m.start(): e.start() if e else len(text)] if e else ""
        if len(span) > len(best):
            best = span
    return best.strip()


def cut_risk_factors(text: str, form: str) -> str:
    """The risk-factor section of an annual/quarterly report's text, or "" when it is not found.

    The longest START..END span wins, which skips the table of contents; END needs a title after the item
    number, so a bare "Item 1C" reference line inside the section does not end it.
    """
    form = (form or "").upper()
    if form.startswith("10-"):
        return _longest(text, _RF_10K_START, _RF_10K_END)
    if form.startswith("20-F"):
        return _longest(text, _RF_20F_START, _RF_20F_END) or _longest(text, _RF_20F_STD_START, _RF_20F_STD_END)
    if form.startswith("40-F"):
        return _longest(text, _RF_40F_START, _RF_40F_END)
    return ""


_HEADLINE_SKIP = re.compile(r"^(see |if |includes |for example|the following|such as|in addition|note )", re.IGNORECASE)


def _title_cased(line: str) -> bool:
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'’\-]*", line) if len(w) > 3]
    return bool(words) and sum(w[0].isupper() for w in words) >= 0.7 * len(words)


_BULLET = re.compile(r"^[\u2022\u25aa\u25cf\u25a0\uffee\-–•▪]+\s*")
_RUN_IN = re.compile(r"^([A-Z][A-Za-z ,'’/&\-]{6,58})\.\s+[A-Z]")


def risk_headlines(text: str, cap: int = 25) -> list[str]:
    """Heading-like lines of a risk-factor section, at most `cap`, in document order.

    A heading is a 40-200 character line without a final period followed by a longer line, a title-cased
    short line ("Supply and Demand"), a one-sentence line followed by a long paragraph (the 10-K italic
    heading style), a line after a bare number ("1." / "Portfolio risks"), or a run-in heading that starts
    a long paragraph ("Economic conditions. The demand ..."). Lines repeated more than twice are page headers.
    """
    lines = [_BULLET.sub("", ln.strip()) for ln in text.split("\n") if ln.strip()]
    lines = [ln for ln in lines if ln]
    counts: dict[str, int] = {}
    for ln in lines:
        counts[ln] = counts.get(ln, 0) + 1
    out: list[str] = []
    for i, ln in enumerate(lines[1:], start=1):
        prev, nxt = lines[i - 1], (lines[i + 1] if i + 1 < len(lines) else "")
        head = None
        m = _RUN_IN.match(ln)
        if len(ln) > 200 and m and len(m.group(1).split()) <= 8:
            head = m.group(1)
        elif 12 <= len(ln) <= 200 and counts[ln] <= 2 and (ln[0].isupper() or ln[0].isdigit()) \
                and ln[-1] not in ";:,(" and not _HEADLINE_SKIP.match(ln) \
                and sum(c.isdigit() for c in ln) <= len(ln) * 0.15:
            one_sentence = ln.count(". ") == 0
            if (re.fullmatch(r"\d{1,2}\.?|[a-z]\.", prev) and len(ln) >= 10) or (_title_cased(ln) and len(nxt) > 80) \
                    or (len(ln) >= 40 and ln[-1] not in ".!?" and len(nxt) > len(ln)) \
                    or (len(ln) >= 40 and one_sentence and len(nxt) > 200):
                head = ln
        if head and head not in out:
            out.append(_cap(head, 200))
        if len(out) >= cap:
            break
    return out


_OUTLOOK = re.compile(r"\b(outlook|guidance|expects?|expected to|forecast(?:s|ed)?|full[- ]year|fiscal \d{4})\b", re.IGNORECASE)
_FOOTNOTE = re.compile(r"^(\(?\d{1,2}\)?\s+|\*\s*)?(source|note)s?:", re.IGNORECASE)
_BOILER = re.compile(r"forward[- ]looking statements|private securities litigation|safe harbor", re.IGNORECASE)


def paragraphs(text: str) -> list[str]:
    """Lines merged into paragraphs where a line break cut a sentence (next line starts in lower case)."""
    out: list[str] = []
    for ln in (x.strip() for x in text.split("\n")):
        if not ln:
            continue
        if out and out[-1][-1:] not in ".!?:;”\"" and (ln[0].islower() or ln[0] in ",;)%"):
            out[-1] = f"{out[-1]} {ln}"
        else:
            out.append(ln)
    return out


def outlook_paragraphs(text: str, cap: int = 4000) -> list[str]:
    """Paragraphs that talk about the outlook/guidance and carry a number, up to `cap` characters in total."""
    hits = [p for p in paragraphs(text)
            if 40 <= len(p) <= 1500 and _OUTLOOK.search(p) and re.search(r"\d", p) and not _BOILER.search(p)
            and not _FOOTNOTE.match(p)]
    return _cap_list(hits, cap)


_NUM = r"(\d[\d,]*(?:\.\d+)?)"
_SCALE = {"billion": 1e9, "million": 1e6}
_G_EXPECTED = re.compile(r"revenues?\b[^.;]{0,80}?\b(?:is |are )?expected to be (?:approximately |about )?\$" + _NUM
                         + r" ?(billion|million)(?:,? plus or minus " + _NUM + r" ?%| to \$" + _NUM
                         + r" ?(billion|million)?)?", re.IGNORECASE)
_G_FULLYEAR = re.compile(r"full[- ]year (?:\d{4} )?revenues? (?:guidance )?(?:of|to) \$" + _NUM
                         + r" ?(billion|million)?(?: to \$" + _NUM + r" ?(billion|million))", re.IGNORECASE)
_FY_WORDS = re.compile(r"full[- ]year|fiscal[- ]year|\bannual\b|for the year", re.IGNORECASE)
_Q_WORDS = re.compile(r"\bquarter\b|\bQ[1-4]\b", re.IGNORECASE)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z(])|\n", text) if s.strip()]


def _f(x: str) -> float:
    return float(x.replace(",", ""))


def guidance_ranges(text: str) -> list[dict]:
    """Revenue guidance ranges stated in plain words; [] when unsure (code never guesses a guidance number).

    period is "FY" only with full-year / fiscal-year / annual wording in the sentence, "Q" with quarter
    wording in the sentence or the lines just before it, else "unknown".
    """
    out: list[dict] = []
    sents = _sentences(text)
    for i, s in enumerate(sents):
        if _BOILER.search(s):
            continue  # forward-looking-statement boilerplate is not guidance
        context = " ".join(sents[max(0, i - 2): i])
        m = _G_FULLYEAR.search(s)
        if m:
            unit = (m.group(4) or m.group(2) or "").lower()
            if not unit:
                continue
            lo, hi = _f(m.group(1)) * _SCALE[(m.group(2) or unit).lower()], _f(m.group(3)) * _SCALE[unit]
            period = "FY"
        else:
            m = _G_EXPECTED.search(s)
            if not m:
                continue
            mid = _f(m.group(1)) * _SCALE[m.group(2).lower()]
            if m.group(3):
                p = _f(m.group(3)) / 100
                lo, hi = mid * (1 - p), mid * (1 + p)
            elif m.group(4):
                lo, hi = mid, _f(m.group(4)) * _SCALE[(m.group(5) or m.group(2)).lower()]
            else:
                lo = hi = mid
            period = ("FY" if _FY_WORDS.search(s) else
                      "Q" if _Q_WORDS.search(s) or _Q_WORDS.search(context) else "unknown")
        if lo > hi or lo <= 0:
            continue
        out.append({"metric": "revenue", "period": period, "low": round(lo, 2), "high": round(hi, 2), "unit": "USD",
                    "sentence": _cap(s, 300)})
    return out


def no_material_change(text: str) -> bool:
    return bool(re.search(r"\bno material changes?\b", text or "", re.IGNORECASE))


_TOP_BOILER = re.compile(r"^(ex-\d+(\.\d+)?|\d+|[\w\-.]+\.html?|document|exhibit\s*\d+(\.\d+)?|press release|news "
                         r"release|edgarfiling|live .*|for immediate release|regulatory release|form 6-k|6-k|"
                         r"\(?exhibit.*\)?)$", re.IGNORECASE)


def _title(lines: list[str]) -> str:
    for i, ln in enumerate(lines[:60]):
        if _TOP_BOILER.match(ln) or len(ln) < 12 or len(re.findall(r"[A-Za-z]{2,}", ln)) < 2:
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if ln.isupper() and nxt.isupper() and len(ln) + len(nxt) < 200:
            ln = f"{ln} {nxt}"  # a capitalised headline broken over two lines
        return _cap(ln, 200)
    return ""


def _after_cover(lines: list[str]) -> list[str]:
    """Drop the 6-K / 8-K cover page (registrant check boxes) when present."""
    marks = [i for i, ln in enumerate(lines[:120]) if re.search(r"Form 40-F|Emerging growth company", ln, re.IGNORECASE)]
    return lines[marks[-1] + 1:] if marks else lines


def extract_text(text: str, outlook_cap: int = 4000) -> dict:
    """title, lead, outlook paragraphs and guidance ranges of a release/exhibit text."""
    lines = _after_cover([ln for ln in text.split("\n") if ln.strip()])
    title = _title(lines)
    first = title.split(" ")[0] if title else ""
    start = next((i for i, ln in enumerate(lines) if first and title.startswith(ln[:60]) and ln.startswith(first)), -1)
    body = "\n".join(lines[start + 1:])
    lead: list[str] = []
    for p in paragraphs(body):
        if len(p) < 80 or sum(c.isdigit() for c in p) > len(p) * 0.3 or _BOILER.search(p):
            continue
        lead.append(p)
        if sum(map(len, lead)) >= 1200:
            break
    return {"title": title, "lead": _cap(" ".join(lead), 1500), "outlook": outlook_paragraphs(body, outlook_cap),
            "guidance": guidance_ranges(body), "chars": len(text)}


def risk_extract(html: str, form: str) -> dict:
    text = html_to_text(html)
    sec = cut_risk_factors(text, form)
    return {"chars_total": len(sec), "headlines": risk_headlines(sec), "excerpt": _cap(sec, 4000),
            "no_material_change": no_material_change(sec[:5000])}


# ---- the business section (S07-R2, S07-R3 questions 3-4, S07-R27): what the company does, its segments, its
# competition and the metrics it reports itself. Paragraphs are kept verbatim, so a quote can be checked against them.

_BIZ_10K_START = re.compile(r"^\s*item\s*1\s*[.:\-–—]?\s*business\.?\s*$", re.IGNORECASE | re.MULTILINE)
_BIZ_10K_END = re.compile(r"^\s*item\s*1a\s*[.:\-–—]?\s*risk\s+factors\.?\s*$", re.IGNORECASE | re.MULTILINE)
_BIZ_20F_START = re.compile(r"^\s*(?:item\s*4\.?\s*)?B\.\s*business\s+overview\.?\s*$", re.IGNORECASE | re.MULTILINE)
_BIZ_20F_END = re.compile(r"^\s*(?:item\s*4\.?\s*)?C\.\s*organi[sz]ational\s+structure\.?\s*$", re.IGNORECASE | re.MULTILINE)
_NOISE = re.compile(r"^(table of contents|index|item\s*\d+[a-z]?\.?(\s.{0,60})?|part\s+[ivx]+\.?|page \d+|\d{1,3}|"
                    r"[\u2022\u25aa\u25cf\u25a0\-–•▪]+)$", re.IGNORECASE)
_H_COMPETITION = re.compile(r"(?:our\s+)?(?:competition|competitive\s+(?:environment|landscape|conditions|position)|"
                            r"competitors)\.?", re.IGNORECASE)
_H_SEGMENTS = re.compile(r"(?:our\s+)?(?:(?:operating|reportable|business)\s+)?segments?(?:\s+information|\s+overview)?\.?|"
                         r"(?:our\s+)?(?:businesses|reportable\s+segments)\.?", re.IGNORECASE)
_H_OVERVIEW = re.compile(r"(?:general|overview|our\s+company|company\s+overview|business\s+overview)\.?", re.IGNORECASE)
_KPI = re.compile(r"\bkey\s+(?:business|operating|performance|financial)?\s*(?:metrics|indicators|measures)\b", re.IGNORECASE)
_H_KPI = re.compile(r"(?:key\s+)?(?:(?:business|operating|performance|financial)\s+)?(?:metrics|statistics|indicators)"
                    r"(?:\s+and\s+\w+)?\.?", re.IGNORECASE)
_KPI_INTRO = re.compile(r"\bwe\b.{0,80}\b(?:use|discuss|monitor|review|track|believe)\b", re.IGNORECASE)


_SEE = re.compile(r"^see (?:pages?|note|item)\b", re.IGNORECASE)
_H_NEXT = re.compile(r"(?:our\s+)?(?:competition|competitors|human\s+capital|employees|properties|(?:government\s+)?regulation|"
                     r"intellectual\s+property|seasonality|available\s+information|sustainability|research\s+and\s+development|"
                     r"manufacturing|sales\s+and\s+marketing|customers|suppliers|strategy|risk\s+factors)\b.*", re.IGNORECASE)
_H_20F_BUSINESS = re.compile(r"(?:our\s+businesses|business\s+overview|reporting\s+segments)\.?", re.IGNORECASE)


def _lines(text: str) -> list[str]:
    return [ln for ln in (x.strip() for x in text.split("\n")) if ln and not _NOISE.match(ln)]


def _heading(ln: str) -> bool:
    return len(ln) <= 70 and ln[-1] not in ".:;,)" and not _BULLET.match(ln) and len(ln.split()) <= 9


def _take(lines: list[str], cap: int, max_n: int = 6, through: bool = False) -> list[str]:
    """The paragraphs after a heading, verbatim, until the next heading (through: past sub-headings, for a list of
    segments); at most `cap` characters in all. A line that starts in lower case continues the paragraph a page
    break cut; a bullet list stays with the line that opened it; "See pages 45-51" cross-references are skipped."""
    out: list[str] = []
    for ln in lines:
        if _SEE.match(ln):
            continue
        if _heading(ln) and not ln[0].islower():
            if out and (not through or _H_NEXT.fullmatch(ln)):
                break
            continue                                          # a sub-heading right under the heading
        if out and (ln[0].islower() or _BULLET.match(ln) or out[-1].endswith(":")):
            out[-1] = f"{out[-1]} {_BULLET.sub('', ln)}"
        else:
            out.append(ln)
        if sum(map(len, out)) >= cap or len(out) >= max_n:
            break
    return _cap_list([_cap(x, 1200) for x in out], cap)


def _block(lines: list[str], cap: int) -> list[str]:
    """A metrics block, tables included: short lines (a table's cells) join the chunk before them, verbatim."""
    out: list[str] = []
    for ln in lines:
        if out and (len(ln) < 80 or ln[0].islower()):
            out[-1] = f"{out[-1]} {ln}"
        else:
            out.append(ln)
        if sum(map(len, out)) >= cap or len(out) >= 4:
            break
    return _cap_list([_cap(x, 1000) for x in out], cap)


def _subsection(lines: list[str], head: re.Pattern, cap: int, through: bool = False) -> list[str]:
    for i, ln in enumerate(lines):
        if len(ln) <= 70 and head.fullmatch(ln):
            got = _take(lines[i + 1:], cap, through=through)
            if got and sum(map(len, got)) >= 150:
                return got
    return []


def cut_business(text: str, form: str) -> str:
    """The business section of an annual report's text (10-K Item 1, 20-F Item 4.B), or "" when it is not found."""
    form = (form or "").upper()
    if form.startswith("10-K"):
        return _longest(text, _BIZ_10K_START, _BIZ_10K_END)
    if form.startswith("20-F"):
        return _longest(text, _BIZ_20F_START, _BIZ_20F_END)
    return ""


def business_extract(text: str, form: str) -> dict:
    """overview, segments and competition from the business section, and the company's own key metrics from
    anywhere after it (MD&A). Each value is a list of verbatim paragraphs; [] when not found (code never guesses)."""
    biz = cut_business(text, form)
    lines = _lines(biz)
    overview = _subsection(lines, _H_OVERVIEW, 1500) or _take(lines[1:], 1500, 3)
    segments = _subsection(lines, _H_SEGMENTS, 1500, through=True)
    if not segments and (form or "").upper().startswith(("20-F", "40-F")):
        # an annual report in the company's own layout (Shell): its "Our businesses" pages, by heading
        segments = _subsection(_lines(text), _H_20F_BUSINESS, 1500, through=True)
    kpi: list[str] = []
    after = text[text.find(biz) + len(biz):] if biz else text
    al = _lines(after)
    for i, ln in enumerate(al):
        if len(ln) <= 70 and _H_KPI.fullmatch(ln):
            kpi = _block(al[i + 1:], 1500)
        elif _KPI.search(ln) and (ln.endswith(":") or _KPI_INTRO.search(ln)):
            kpi = _block(al[i:], 1500)
        if sum(map(len, kpi)) >= 150 and sum(c.isalpha() for c in "".join(kpi)) >= 0.4 * len("".join(kpi)):
            break
        kpi = []
    return {"chars_total": len(biz), "overview": overview, "segments": segments,
            "competition": _subsection(lines, _H_COMPETITION, 1500), "kpi": kpi}


def annual_extract(html: str, form: str) -> dict:
    """One pass over an annual report: the risk factors and the business section."""
    text = html_to_text(html)
    sec = cut_risk_factors(text, form)
    return {"risk": {"chars_total": len(sec), "headlines": risk_headlines(sec), "excerpt": _cap(sec, 4000),
                     "no_material_change": no_material_change(sec[:5000])},
            "business": business_extract(text, form)}


def results_kind(*titles: str) -> str:
    """Kind of a 6-K from its exhibit and document titles."""
    t = " | ".join(x for x in titles if x)
    if _6K_RESULTS.search(t) and not _6K_GOVERNANCE.search(t) and \
            not (_6K_DIVIDEND.search(t) and not re.search(r"results|financial report", t, re.IGNORECASE)):
        return "results"
    if _6K_GOVERNANCE.search(t):
        return "governance"
    if _6K_ACQ.search(t):
        return "acquisition"
    if _6K_AGREEMENT.search(t):
        return "agreement"
    return "other_event" if t else "unknown"


def item_kind(items: list[str]) -> str:
    if items and set(items) <= GOVERNANCE_ITEMS:
        return "governance"
    for it in items:
        if it != "9.01" and it in ITEM_KIND:
            return ITEM_KIND[it]
    return "other_event" if items else "unknown"


def _period_end(d: str) -> bool:
    return d[5:] in ("03-31", "06-30", "09-30", "12-31")


# ---------------------------------------------------------------- the fetcher

class SECFilings:
    """SEC EDGAR filings for one ticker at a time. Every network failure is caught by documents()."""

    def __init__(self, store: Store | None = None, contact: str | None = None,
                 session: requests.Session | None = None, edgar: EDGAR | None = None,
                 limiter: RateLimiter | None = None):
        self.store = store
        self.contact = (contact if contact is not None else os.environ.get("SEC_CONTACT") or "").strip()
        self.session = session
        self.edgar = edgar or EDGAR(store, self.contact or None)
        self.headers = {"User-Agent": f"QuantumTrader research {self.contact}"}
        self.limiter = limiter or RateLimiter(0.15)
        self.failures = 0             # consecutive failed requests (a 404 is an answer, not a failure)
        self.down = False             # circuit breaker: SEC unreachable for the rest of this run
        self.deadline: float | None = None

    def ok(self) -> bool:
        return bool(self.contact)

    # ---- low level
    def _guarded(self, fetch) -> Any:
        if self.down:
            raise ProviderError(f"SEC unreachable: stopped after {MAX_SEC_FAILURES} consecutive failures")
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise ProviderError(f"SEC stock deadline of {STOCK_DEADLINE_S:.0f}s reached")
        try:
            out = fetch()
        except ProviderError as e:
            if "HTTP 404" not in str(e):
                self.failures += 1
                if self.failures >= MAX_SEC_FAILURES:
                    self.down = True
            raise
        self.failures = 0
        return out

    def _json(self, url: str, max_age_s: float) -> Any:
        if self.store is not None:
            hit = self.store.cached(redact(url), max_age_s)
            if hit is not None:
                return hit

        def fetch():
            self.limiter.wait()
            return get_json(url, self.store, max_age_s=max_age_s, headers=self.headers, session=self.session)
        return self._guarded(fetch)

    def _text(self, url: str, transform=None, key: str = "", max_age_s: float = YEAR_S) -> Any:
        return self._guarded(lambda: get_text(url, self.store, max_age_s=max_age_s, headers=self.headers,
                                              session=self.session, transform=transform, key=key,
                                              limiter=self.limiter))

    # ---- identity
    def cik(self, ticker: str) -> int | None:
        body = self._json(TICKERS_URL, 7 * 86400)
        want = {ticker.upper(), ticker.upper().replace(".", "-")}       # BRK.B is "BRK-B" at SEC
        return next((int(v["cik_str"]) for v in body.values() if v["ticker"] in want), None)

    def submissions(self, cik: int) -> list[dict]:
        j = self._json(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", SUBMISSIONS_MAX_AGE_S)
        recent = (j.get("filings") or {}).get("recent") or {}
        cols = list(recent)
        rows = [dict(zip(cols, vals)) for vals in zip(*(recent[c] for c in cols))]
        for r in rows:
            r["cik"] = int(cik)
        return rows

    def ciks(self, ticker: str) -> list[int]:
        """Ticker CIK first, then a predecessor that still holds the annual report (accession prefixes)."""
        first = self.cik(ticker)
        if first is None:
            return list(ALIAS.get(ticker.upper(), []))
        out = [first]
        rows = self.submissions(first)
        if not any(r.get("form") in ANNUAL_FORMS for r in rows):
            prefixes: list[int] = []
            for r in rows:
                if r.get("form") in ANNUAL_FORMS + ("10-Q", "8-K", "6-K"):
                    p = int(str(r.get("accessionNumber", "0-")).split("-")[0] or 0)
                    if p and p != first and p not in prefixes:
                        prefixes.append(p)
            for p in prefixes[:3]:
                try:
                    if any(x.get("form") in ANNUAL_FORMS for x in self.submissions(p)):
                        out.append(p)
                except ProviderError:
                    continue
            for a in ALIAS.get(ticker.upper(), []):
                if a not in out:
                    out.append(a)
        return out

    def _rows(self, ciks: list[int]) -> list[dict]:
        seen, rows = set(), []
        for c in ciks:
            for r in self.submissions(c):
                if r["accessionNumber"] not in seen:
                    seen.add(r["accessionNumber"])
                    rows.append(r)
        rows.sort(key=lambda r: (r.get("filingDate", ""), r.get("acceptanceDateTime", "")), reverse=True)
        return rows

    # ---- documents
    def filing_exhibits(self, cik: int, accession: str) -> list[dict]:
        """EX-99 rows of a filing index ("/ix?doc=" and " iXBRL" stripped)."""
        return ex99(self._index(cik, accession))

    def _index(self, cik: int, accession: str) -> list[dict]:
        url = f"{_acc_path(cik, accession)}/{accession}-index.htm"
        return self._text(url, transform=parse_index, key="index")

    def exhibit_extract(self, url: str, outlook_cap: int = 4000) -> dict:
        return self._text(url, transform=lambda h: extract_text(html_to_text(h), outlook_cap), key=f"x{outlook_cap}")

    def current_reports(self, ticker: str, as_of: date, days: int = 90, max_n: int = 8,
                        ciks: list[int] | None = None, errors: list[str] | None = None) -> list[dict]:
        """8-K, 8-K/A and 6-K filed in [as_of - days, as_of], newest first (no look-ahead)."""
        ciks = ciks or self.ciks(ticker)
        lo, hi = (as_of - timedelta(days=days)).isoformat(), as_of.isoformat()
        rows = [r for r in self._rows(ciks) if r.get("form") in CURRENT_FORMS and lo <= r.get("filingDate", "") <= hi]
        out = []
        for r in rows[:max_n]:
            acc, cik = r["accessionNumber"], r["cik"]
            items = [x.strip() for x in (r.get("items") or "").split(",") if x.strip()]
            base = _acc_path(cik, acc)
            row = {"form": r["form"], "date": r["filingDate"], "accession": acc, "items": items,
                   "item_names": [ITEM_NAMES.get(i, i) for i in items], "kind": item_kind(items),
                   "url": f"{base}/{r.get('primaryDocument', '')}", "index_url": f"{base}/{acc}-index.htm",
                   "exhibits": []}
            try:
                for x in ex99(self._index(cik, acc))[:3]:
                    ext = {"title": "", "lead": ""}
                    if not x["size"] or x["size"] <= MAX_EXHIBIT_BYTES:
                        ext = self.exhibit_extract(x["url"])
                    row["exhibits"].append({"type": x["type"], "url": x["url"],
                                            "title": _cap(ext.get("title") or x["description"], 200),
                                            "lead": _cap(ext.get("lead"), 1200)})
                if r["form"] == "6-K":
                    titles = [e["title"] for e in row["exhibits"]]
                    if (not row["exhibits"] or not any(titles)) and (r.get("size") or 0) <= MAX_EXHIBIT_BYTES:
                        titles.append(self.exhibit_extract(row["url"]).get("title", ""))
                    row["title"] = _cap(next((t for t in titles if t), ""), 200)
                    kind = results_kind(*titles)
                    if kind in ("other_event", "unknown") and _period_end(r.get("reportDate") or "") \
                            and r.get("reportDate") != r.get("filingDate"):
                        kind = "results"  # a 6-K dated to a quarter end and filed later is a periodic report
                    row["kind"] = kind
            except Exception as e:  # noqa: BLE001 - one filing's failure (HTTP or parse) loses only that row's extract
                if errors is not None:
                    errors.append(f"{acc}: {redact(str(e))[:300]}")
            out.append(row)
        return out

    def _results_candidates(self, rows: list[dict], as_of: date, max_age_days: int | None = MAX_RESULT_AGE_DAYS
                            ) -> list[dict]:
        """Results filings with as_of - max_age_days <= filingDate <= as_of (None: no lower bound), newest first."""
        hi = as_of.isoformat()
        lo = (as_of - timedelta(days=max_age_days)).isoformat() if max_age_days is not None else ""
        out = []
        for r in rows:
            if r.get("filingDate", "") > hi or r.get("filingDate", "") < lo:
                continue
            items = (r.get("items") or "").split(",")
            if r.get("form") in ("8-K", "8-K/A") and "2.02" in items or r.get("form") == "6-K" and _period_end(r.get("reportDate") or "") and \
                    r.get("reportDate") != r.get("filingDate") and (r.get("size") or 0) < 20_000_000:
                out.append(r)
        return out

    def _release(self, r: dict) -> dict | None:
        cik, acc = r["cik"], r["accessionNumber"]
        idx = self._index(cik, acc)
        exs = ex99(idx)
        main = next((x for x in exs if x["type"].upper().startswith("EX-99.1")), exs[0] if exs else None)
        url = main["url"] if main else f"{_acc_path(cik, acc)}/{r.get('primaryDocument', '')}"
        ext = self.exhibit_extract(url)
        typ = main["type"] if main else "primary"
        titles = (ext.get("title", ""), ext.get("lead", "")[:300], *(x["description"] for x in exs))
        rep = r.get("reportDate") or ""
        # an XBRL financial report 6-K titled by its period end ("TotalEnergies SE_2026-06-30") is a results release
        dated = _period_end(rep) and any(rep in (t or "") for t in titles)
        if r["form"] == "6-K" and not dated and results_kind(*titles) != "results":
            return None
        form = "6-K" if r["form"] == "6-K" else "8-K"
        out = {"form": r["form"], "date": r["filingDate"], "url": url, "source": f"sec:{form} {typ}",
               "title": _cap(ext.get("title"), 200), "lead": _cap(ext.get("lead"), 1500),
               "outlook": _cap_list(ext.get("outlook") or [], 4000), "guidance": ext.get("guidance") or [],
               "commentary": None}
        second = next((x for x in exs if x is not main and x["type"].upper().startswith("EX-99.2")), None)
        if second:
            c = self.exhibit_extract(second["url"], outlook_cap=2500)
            out["commentary"] = {"type": second["type"], "url": second["url"],
                                 "outlook": _cap_list(c.get("outlook") or [], 2500)}
            if not out["guidance"]:
                out["guidance"] = c.get("guidance") or []
            if len(out["lead"]) < 300 and len(c.get("lead") or "") > len(out["lead"]):
                out["lead"] = _cap(c["lead"], 1500)  # a cover-note EX-99.1 (Shell): the report itself is EX-99.2
        return out

    def results(self, ticker: str, as_of: date, ciks: list[int] | None = None,
                errors: list[str] | None = None) -> tuple[dict | None, dict | None]:
        """(latest results release, the one before it within 200 days), each from 8-K Item 2.02 or a results 6-K."""
        ciks = ciks or self.ciks(ticker)
        found: list[dict] = []
        rows = self._rows(ciks)
        for r in self._results_candidates(rows, as_of)[:MAX_RESULT_CANDIDATES]:
            try:
                rel = self._release(r)
            except ProviderError as e:
                if errors is not None:
                    errors.append(f"results {r['accessionNumber']}: {e}")
                continue
            if rel is None:
                continue
            if found and date.fromisoformat(found[0]["date"]) - date.fromisoformat(rel["date"]) > timedelta(days=200):
                break
            found.append(rel)
            if len(found) == 2:
                break
        if not found and errors is not None:
            older = [r for r in self._results_candidates(rows, as_of, None)
                     if r.get("filingDate", "") < (as_of - timedelta(days=MAX_RESULT_AGE_DAYS)).isoformat()]
            errors.append(f"no results release (8-K 2.02 / results 6-K) in the last {MAX_RESULT_AGE_DAYS} days"
                          + (f" (newest older candidate: {older[0]['filingDate']})" if older else " found"))
        return (found[0] if found else None), (found[1] if len(found) > 1 else None)

    def _risk(self, url: str, form: str) -> dict:
        return self._text(url, transform=lambda h: risk_extract(h, form), key=f"rf:{form}")

    def _annual(self, url: str, form: str) -> dict:
        return self._text(url, transform=lambda h: annual_extract(h, form), key=f"annual:{form}")

    def risk_factors(self, ticker: str, as_of: date, ciks: list[int] | None = None,
                     errors: list[str] | None = None) -> dict | None:
        return self.annual_report(ticker, as_of, ciks, errors)[0]

    def annual_report(self, ticker: str, as_of: date, ciks: list[int] | None = None,
                      errors: list[str] | None = None) -> tuple[dict | None, dict | None]:
        """(risk factors, business section) of the latest annual report filed by as_of, from one download."""
        ciks = ciks or self.ciks(ticker)
        rows = [r for r in self._rows(ciks) if r.get("filingDate", "") <= as_of.isoformat()]
        annual = next((r for r in rows if r.get("form") in ANNUAL_FORMS), None)
        if annual is None:
            if errors is not None:
                errors.append(f"no annual report (10-K / 20-F / 40-F) before {as_of.isoformat()}")
            return None, None
        base = _acc_path(annual["cik"], annual["accessionNumber"])
        url = f"{base}/{annual.get('primaryDocument', '')}"
        ax = self._annual(url, annual["form"])
        rf, bz, burl = ax["risk"], ax["business"], url
        if not rf["chars_total"] and annual["form"] in ("40-F", "20-F"):
            for x in ex99(self._index(annual["cik"], annual["accessionNumber"]))[:2]:
                alt = self._annual(x["url"], annual["form"])
                if alt["risk"]["chars_total"]:
                    rf, url = alt["risk"], x["url"]
                    if not bz["chars_total"]:
                        bz, burl = alt["business"], x["url"]
                    break
        business = None
        if any(bz.get(k) for k in ("overview", "segments", "competition", "kpi")):
            business = {"form": annual["form"], "filed": annual["filingDate"], "url": burl,
                        **{k: bz.get(k) or [] for k in ("overview", "segments", "competition", "kpi")}}
        elif errors is not None:
            errors.append(f"business section not found in {annual['form']} {burl}")
        if not rf["chars_total"]:
            if errors is not None:
                errors.append(f"risk factors not found in {annual['form']} {url}")
            return None, business
        out = {"form": annual["form"], "filed": annual["filingDate"], "url": url, "chars_total": rf["chars_total"],
               "headlines": [_cap(h, 200) for h in rf["headlines"][:25]], "excerpt": _cap(rf["excerpt"], 4000),
               "delta": None}
        if annual["form"] == "10-K":
            q = next((r for r in rows if r.get("form") == "10-Q" and r["filingDate"] > annual["filingDate"]), None)
            if q:
                qurl = f"{_acc_path(q['cik'], q['accessionNumber'])}/{q.get('primaryDocument', '')}"
                try:
                    qrf = self._risk(qurl, "10-Q")
                    if qrf["chars_total"]:  # a 10-Q without Item 1A falls back to the 10-K alone
                        out["delta"] = {"form": "10-Q", "filed": q["filingDate"], "url": qurl,
                                        "no_material_change": qrf["no_material_change"],
                                        "excerpt": _cap(qrf["excerpt"], 2500)}
                except ProviderError as e:
                    if errors is not None:
                        errors.append(f"10-Q {q['accessionNumber']}: {e}")
        return out, business

    def documents(self, ticker: str, as_of: date) -> dict:
        """The `documents` contract for one stock. Never raises: each failure is appended to "errors"."""
        out: dict[str, Any] = {"symbol": ticker.upper(), "cik": None, "ciks": [], "foreign": False,
                               "as_of": _iso(as_of), "current": [], "results": None, "results_prev": None,
                               "risk_factors": None, "business": None, "transcript": None, "errors": []}
        errs: list[str] = out["errors"]
        if not self.ok():
            errs.append("SEC_CONTACT not set")
            return out
        if self.down:
            errs.append(f"SEC unreachable: stopped after {MAX_SEC_FAILURES} consecutive failures")
            return out
        self.deadline = time.monotonic() + STOCK_DEADLINE_S
        try:
            return self._documents(ticker, as_of, out)
        finally:
            self.deadline = None

    def _documents(self, ticker: str, as_of: date, out: dict[str, Any]) -> dict:
        errs: list[str] = out["errors"]
        try:
            ciks = self.ciks(ticker)
        except Exception as e:  # noqa: BLE001 - documents() never raises; the run continues on Yahoo events
            errs.append(f"cik: {redact(str(e))[:300]}")
            return out
        if not ciks:
            errs.append(f"no SEC CIK for {ticker}")
            return out
        out["ciks"] = ciks
        try:
            rows = self._rows(ciks)
            annual = next((r for r in rows if r.get("form") in ANNUAL_FORMS), None)
            out["cik"] = annual["cik"] if annual else ciks[0]
            out["foreign"] = bool(annual and annual["form"] in ("20-F", "40-F")) or (
                not any(r.get("form") in ("10-K", "10-Q") for r in rows) and any(r.get("form") == "6-K" for r in rows))
        except Exception as e:  # noqa: BLE001
            errs.append(f"submissions: {redact(str(e))[:300]}")
            return out
        for name, fn in (("current", lambda: self.current_reports(ticker, as_of, ciks=ciks, errors=errs)),
                         ("results", lambda: self.results(ticker, as_of, ciks=ciks, errors=errs)),
                         ("annual", lambda: self.annual_report(ticker, as_of, ciks=ciks, errors=errs))):
            try:
                val = fn()
            except Exception as e:  # noqa: BLE001
                errs.append(f"{name}: {redact(str(e))[:300]}")
                continue
            if name == "results":
                out["results"], out["results_prev"] = val
            elif name == "annual":
                out["risk_factors"], out["business"] = val
            else:
                out[name] = val
        return shrink(out)


def size(d: dict) -> int:
    return len(json.dumps(d, ensure_ascii=False))


def shrink(out: dict, limit: int = MAX_CHARS) -> dict:
    """Trim the longest free-text fields until the dict is about `limit` characters."""
    steps = [
        lambda: _trim(out.get("results_prev"), lead=400, outlook=800),
        lambda: _trim_business(out.get("business"), 1000),
        lambda: [x.update(lead=_cap(x["lead"], 500)) for c in out["current"] for x in c["exhibits"]],
        lambda: out["risk_factors"] and out["risk_factors"].update(excerpt=_cap(out["risk_factors"]["excerpt"], 2500)),
        lambda: _trim(out.get("results"), lead=1000, outlook=2500),
        lambda: out["risk_factors"] and out["risk_factors"].get("delta") and out["risk_factors"]["delta"].update(
            excerpt=_cap(out["risk_factors"]["delta"]["excerpt"], 1200)),
        lambda: [x.update(lead=_cap(x["lead"], 200)) for c in out["current"] for x in c["exhibits"]],
        lambda: _trim(out.get("results_prev"), lead=0, outlook=0),
        lambda: out["risk_factors"] and out["risk_factors"].update(excerpt=_cap(out["risk_factors"]["excerpt"], 1200)),
        lambda: _trim(out.get("results"), lead=600, outlook=1200),
        lambda: _trim_business(out.get("business"), 600),
    ]
    for step in steps:
        if size(out) <= limit:
            break
        step()
    return out


def _trim(r: dict | None, lead: int, outlook: int) -> None:
    if not r:
        return
    r["lead"] = _cap(r.get("lead"), lead) if lead else ""
    r["outlook"] = _cap_list(r.get("outlook") or [], outlook)
    if r.get("commentary"):
        r["commentary"]["outlook"] = _cap_list(r["commentary"].get("outlook") or [], max(outlook // 2, 0))


def _trim_business(b: dict | None, each: int) -> None:
    """Fewer whole paragraphs, never a cut one: a quote is checked against the paragraph as filed."""
    if b:
        for k in ("overview", "segments", "competition", "kpi"):
            b[k] = _cap_list(b.get(k) or [], each) or (b.get(k) or [])[:1]
