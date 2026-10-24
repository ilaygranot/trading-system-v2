"""The deep-dive article (DC-25): the course's ten questions (S07-R3, lesson 074) answered in prose, every sentence
standing on facts from the fact sheet.

The agent writes; code decides what survives. A sentence is kept only when
  1. it cites at least one fact id, and every id is on the sheet;
  2. every number in it is a number in one of the facts it cites (at the precision it is written, in any scale:
     units, thousands, millions, billions, or a fraction written as a percent), and every date is a date in them.
     A fact an agent wrote (an earlier card) supports words, never a number. Counts up to 12 written as plain
     integers ("two segments", "3 of 4 quarters") and ids of rules, forms and indicator periods are not checked;
  3. every quote it carries appears, word for word, in the text of the fact it names (an ellipsis may join pieces
     that appear in that order).
A sentence that fails is dropped and listed with the reason; a section left with nothing says "no source found"
instead of a guess. The footnotes are numbered in the order the facts are first cited.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Iterable

from qt.facts import FactSheet

# (key, the question as the course asks it, the course rules, is it one of the ten questions)
SECTIONS: tuple[tuple[str, str, str, bool], ...] = (
    ("big_picture", "הסקטור נתמך מניתוח המאקרו?", "S07-R3 שאלה 1, S01–S04", True),
    ("business", "מה החברה עושה וממה היא מרוויחה?", "S07-R2", False),
    ("vs_sector", "החברה בולטת מול הסקטור?", "S07-R3 שאלה 2, S07-R12", True),
    ("advantage", "מה היתרון היחסי, והאם הוא קיים?", "S07-R3 שאלה 4, D-S07-8", True),
    ("growth_engines", "למה הצמיחה עולה על הסקטור, ומה המנועים?", "S07-R3 שאלה 3, S07-R27", True),
    ("reports_trend", "מה מגמת הדוחות, והאם תימשך?", "S07-R3 שאלה 6, S07-R5..R11, S07-R31", True),
    ("management", "מה ההנהלה אומרת?", "075, S07-R32, S07-R34", False),
    ("market_agrees", "האם השוק מסכים?", "S07-R3 שאלה 7, S07-R28", True),
    ("we_agree", "האם אנחנו מסכימים עם השוק?", "S07-R3 שאלה 8, S07-R19", True),
    ("catalysts", "אילו זרזים צפויים מעבר ליומן הדוחות?", "S07-R3 שאלה 5, S08", True),
    ("chart", "מה הגרף אומר: אינטרוול גבוה, רמות, מומנטום, תנודתיות, שוק וסקטור?", "S09", False),
    ("risks_10k", "מה הסיכונים לפי ה-10-K?", "S07-R3 שאלה 9, S07-R30", True),
    ("what_changes", "מה ישנה את הסיפור?", "S07-R3 שאלה 10, S07-R29", True),
)
SECTION_KEYS = tuple(k for k, *_ in SECTIONS)
ANSWERS = ("yes", "partial", "no", "no_source", "not_a_question")
ANSWER_HE = {"yes": "כן", "partial": "חלקית", "no": "לא", "no_source": "אין מקור"}

_QUOTE = {"type": "object", "properties": {
    "fact_id": {"type": "string", "description": "the text fact the quote is copied from"},
    "text": {"type": "string", "description": "English, word for word from that fact's text"},
    "he": {"type": "string", "description": "a short Hebrew translation"}},
    "required": ["fact_id", "text", "he"], "additionalProperties": False}
SENTENCE = {"type": "object", "properties": {
    "text": {"type": "string", "description": "one Hebrew sentence; every number in it is in a fact it cites"},
    "fact_ids": {"type": "array", "items": {"type": "string"}},
    "quotes": {"type": "array", "items": _QUOTE, "description": "at most one quote, or none"}},
    "required": ["text", "fact_ids", "quotes"], "additionalProperties": False}
SECTION = {"type": "object", "properties": {
    "answer": {"type": "string", "enum": list(ANSWERS)},
    "paragraphs": {"type": "array", "items": {"type": "object", "properties": {
        "sentences": {"type": "array", "items": SENTENCE}}, "required": ["sentences"], "additionalProperties": False}}},
    "required": ["answer", "paragraphs"], "additionalProperties": False}


def schema_extra() -> dict:
    """The card's own fields (cards.yaml points here: the schema is too long for one yaml line)."""
    return {"headline": SENTENCE, "dek": SENTENCE,
            "sections": {"type": "object", "properties": {k: SECTION for k in SECTION_KEYS},
                         "required": list(SECTION_KEYS), "additionalProperties": False}}


# ---------------------------------------------------------------- numbers

_SCALE_WORDS = {"טריליון": 1e12, "trillion": 1e12, "T": 1e12, "מיליארד": 1e9, "billion": 1e9, "bn": 1e9, "B": 1e9,
                "מיליון": 1e6, "million": 1e6, "M": 1e6, "mn": 1e6, "אלף": 1e3, "thousand": 1e3, "K": 1e3}
_NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?:\s?(%|טריליון|trillion|מיליארד|billion|bn|מיליון|million|mn|"
                  r"אלף|thousand)|(?<=\d)([TBMK])(?![A-Za-z]))?")
_EXEMPT = re.compile(
    r"\b(?:S\d\d-[RJX]\d+|D-S\d\d-\d+|L\d{3}(?:-[RJ]\d+)?|DC-\d\d|F-\d{3,}|S\d\d)\b"      # course and card ids
    r"|\b(?:10-K|10-Q|8-K|6-K|20-F|40-F|S&P\s?500|Item\s+\d+[A-Z]?(?:\.\d+)?|EX-99(?:\.\d)?)"  # forms
    r"|\b(?:RSI|MACD|ADR|ATR|SMA|EMA|MA)\s?\(?\d+(?:\s?/\s?\d+)*\)?"                      # indicator periods
    r"|ממוצע(?:ים)?\s\d+(?:\s?/\s?\d+)*|\bF[0-2]\b|\bQ[1-4](?:/\d\d)?\b|\b[1-4]Q\b|\bH[12]\b", re.IGNORECASE)
SMALL_COUNT = 12


def numbers_in_text(text: str) -> list[tuple[float, float, bool, str]]:
    """(value, tolerance, written as a percent, the token) for each number a reader sees in the sentence. The
    tolerance is half a unit of the last digit shown, in the number's own scale ("4.5 מיליארד": 0.05 billion)."""
    t = _EXEMPT.sub(" ", text.replace("−", "-"))
    out = []
    for m in _NUM.finditer(t):
        whole, frac, word, letter = m.group(1), m.group(2) or "", m.group(3), m.group(4)
        v = float(whole.replace(",", "") + frac)
        dec = len(frac) - 1 if frac else 0
        unit = word or letter
        pct = unit == "%"
        scale = 1.0 if pct or not unit else _SCALE_WORDS.get(unit, _SCALE_WORDS.get(unit.lower(), 1.0))
        out.append((v * scale, 0.5 * 10 ** -dec * scale + 1e-9, pct, m.group(0)))
    return out


def _walk(x: Any) -> Iterable[float]:
    if isinstance(x, bool) or x is None:
        return
    if isinstance(x, (int, float)):
        yield abs(float(x))
    elif isinstance(x, str):
        for v, _, _, _ in numbers_in_text(x.replace("$", " ")):
            yield v
    elif isinstance(x, dict):
        for k, v in x.items():
            yield from _walk(k)
            yield from _walk(v)
    elif isinstance(x, (list, tuple)):
        for v in x:
            yield from _walk(v)


_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_DM = re.compile(r"(?<![\d.,])(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2}))?(?!\d|[.,]\d)")


def fact_numbers(facts: FactSheet, ids: Iterable[str]) -> list[float]:
    """The numbers in the cited facts. A fact the agent wrote (an earlier card's reasoning) is a source for words,
    never for a number: its numbers are not in the pool, so a number reaches the article only from code or a
    document."""
    out: list[float] = []
    for fid in ids:
        f = facts.get(fid)
        if not str(f.source).startswith("agent:"):
            out += list(_walk(f.value))
    return out


def fact_dates(facts: FactSheet, ids: Iterable[str]) -> set[tuple[int, int, int]]:
    """(year, month, day) of every date in the cited facts: their as-of dates and the ISO dates in their text."""
    out: set[tuple[int, int, int]] = set()
    for fid in ids:
        f = facts.get(fid)
        out.add((f.as_of.year, f.as_of.month, f.as_of.day))
        for m in _ISO.finditer(str(f.value)):
            out.add((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    return out


def strip_dates(text: str, dates: set[tuple[int, int, int]]) -> str:
    """Remove the dates the cited facts hold ("29.10", "29.10.2026", "2026-10-29", a year "2026") from the text. A
    date-like token that matches none stays, and is then checked as a number ("8.4" is a multiple, not a date)."""
    years = {y for y, _, _ in dates}
    dm = {(d, m) for _, m, d in dates}

    def iso(m: re.Match) -> str:
        return " " if (int(m.group(1)), int(m.group(2)), int(m.group(3))) in dates else m.group(0)

    def short(m: re.Match) -> str:
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        if (d, mo) in dm and (not y or int(y) % 100 in {x % 100 for x in years}):
            return " "
        return m.group(0)
    t = _DM.sub(short, _ISO.sub(iso, text))
    return re.sub(r"(?<!\d)(19|20)\d\d(?![\d%.,])", lambda m: " " if int(m.group(0)) in years else m.group(0), t)


def number_ok(value: float, tol: float, pct: bool, pool: list[float]) -> bool:
    """The number at the precision written against the pool, also a fraction written as a percent. A sign is the
    sentence's wording (up/down, profit/loss), so sizes are compared."""
    v = abs(value)
    return any(abs(f - v) <= tol or (pct and abs(f * 100 - v) <= tol) for f in pool)


def _norm(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace(" ", " ").replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_ok(quote: str, source_text: str) -> bool:
    """Word for word (case, curly quotes and spacing aside); "…" or "..." may join pieces that appear in order."""
    src = _norm(source_text)
    pieces = [p.strip(" \"'") for p in re.split(r"\s*(?:…|\.\.\.)\s*", _norm(quote)) if p.strip(" \"'")]
    if not pieces or sum(len(p.split()) for p in pieces) < 3:
        return False
    at = 0
    for p in pieces:
        i = src.find(p, at)
        if i < 0:
            return False
        at = i + len(p)
    return True


# ---------------------------------------------------------------- the check

def check_sentence(s: dict, facts: FactSheet) -> str | None:
    """None when the sentence stands on its facts, else why it does not."""
    text = (s.get("text") or "").strip()
    ids = [i for i in s.get("fact_ids") or [] if i]
    if not text:
        return "empty"
    if not ids:
        return "no fact cited"
    missing = facts.missing(ids)
    if missing:
        return f"facts not on the sheet: {', '.join(missing)}"
    pool = fact_numbers(facts, ids)
    rest = strip_dates(text, fact_dates(facts, ids))
    for v, tol, pct, tok in numbers_in_text(rest):
        if not pct and v <= SMALL_COUNT and tok.strip().isdigit():
            continue
        if not number_ok(v, tol, pct, pool):
            return f"the number {tok.strip()} is not in the cited facts"
    for q in s.get("quotes") or []:
        fid = q.get("fact_id") or ""
        if fid not in facts:
            return f"quote from {fid}, which is not on the sheet"
        if fid not in ids:
            return f"quote from {fid}, which the sentence does not cite"
        if not quote_ok(q.get("text") or "", str(facts.get(fid).value)):
            return f"the quote does not appear in {fid}"
    return None


SOURCE_HE = (("sec:", "SEC"), ("roic:", "roic.ai"), ("yahoo:", "Yahoo"), ("agent:", "שיקול הדעת של הסוכן"), ("code:", "חישוב הקוד"))


def source_label(source: str) -> str:
    """'sec:10-K Item 1 Competition https://...' -> 'SEC · 10-K Item 1 Competition'."""
    s = re.sub(r"\s*https?://\S+", "", source or "").strip()
    for pre, he in SOURCE_HE:
        if s.startswith(pre):
            rest = s[len(pre):].strip()
            return f"{he} · {rest}" if rest else he
    return s


def build(out: dict, facts: FactSheet, as_of: date | None = None) -> dict:
    """The article as the report shows it: kept sentences with footnote numbers, the notes, and what was dropped."""
    notes: dict[str, int] = {}
    dropped: list[dict] = []
    kept = 0

    def sentence(s: dict, where: str) -> dict | None:
        nonlocal kept
        why = check_sentence(s, facts)
        if why:
            dropped.append({"section": where, "text": s.get("text", ""), "why": why})
            return None
        kept += 1
        for fid in dict.fromkeys(s["fact_ids"]):
            notes.setdefault(fid, len(notes) + 1)
        return {"text": s["text"].strip(), "notes": [notes[f] for f in dict.fromkeys(s["fact_ids"])],
                "quotes": [{"text": q["text"].strip(), "he": (q.get("he") or "").strip(), "note": notes[q["fact_id"]]}
                           for q in s.get("quotes") or []]}

    art: dict[str, Any] = {"headline": sentence(out.get("headline") or {}, "headline"),
                           "dek": sentence(out.get("dek") or {}, "dek"), "sections": {}}
    secs = out.get("sections") or {}
    for key, *_ in SECTIONS:
        sec = secs.get(key) or {}
        paras = []
        for p in sec.get("paragraphs") or []:
            ss = [x for x in (sentence(s, key) for s in p.get("sentences") or []) if x]
            if ss:
                paras.append(ss)
        answer = sec.get("answer") if sec.get("answer") in ANSWERS else "no_source"
        art["sections"][key] = {"answer": answer if paras else "no_source", "paragraphs": paras}
    art["notes"] = [dict(n=n, fact_id=fid, key=facts.get(fid).key, value=_short(facts.get(fid).value),
                         source=source_label(facts.get(fid).source), as_of=facts.get(fid).as_of.isoformat(),
                         url=facts.get(fid).url or _url(facts.get(fid).source))
                    for fid, n in sorted(notes.items(), key=lambda x: x[1])]
    art["dropped"] = dropped
    art["stats"] = {"kept": kept, "dropped": len(dropped), "facts": len(facts)}
    if as_of:
        art["as_of"] = as_of.isoformat()
    return art


def _url(source: str) -> str:
    m = re.search(r"https?://\S+", source or "")
    return m.group(0) if m else ""


def _short(v: Any, n: int = 600) -> str:
    s = v if isinstance(v, str) else ", ".join(f"{k}: {x}" for k, x in v.items()) if isinstance(v, dict) else str(v)
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"
