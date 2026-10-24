"""The last earnings call, asked the course's questions (DC-26; S07-R32, lessons 075-077).

The course reads the last call's transcript for every stock that passed the screen, and reads it for specific things:
what management expects and how it feels about the business (075), what drives growth in its own words and the KPIs
it stresses (076, S07-R27), pipeline, launches and scheduled events as catalysts (077: TDOC's pipeline, "the
questions the analysts ask and the CEO's and CFO's answers"), costs and margins (S07-R9/R10), capital and dilution
(S07-R6, S07-R37), macro and input exposure and headwinds (S07-R3). QUESTIONS holds them, each with what to look for.

The agent reads the whole call (every turn is a fact: speaker, management or analyst, prepared remarks or Q&A) and
answers each question with verbatim excerpts. Code keeps an excerpt only when it appears word for word in the turn
it names (qt.research.article.quote_ok); a question left with no excerpt is "not found". The kept excerpts become
facts with the call as their source and link, so the deep-dive cards and the article (DC-25) cite the call like any
SEC document; the agent's Hebrew summary is a fact of the agent, which supports words, never a number.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from qt.facts import FactSheet
from qt.research import article as AR

# (id, the question in Hebrew, what to look for in the call, the course, the article sections it feeds)
QUESTIONS: tuple[tuple[str, str, str, str, tuple[str, ...]], ...] = (
    ("guidance", "מה התחזית שההנהלה נתנה או שינתה, והאם הטווח רחב?",
     "Guidance or outlook with numbers (revenue, earnings, margins, production, capex) for the next quarter or year; "
     "raised, kept or cut against before; how wide the range is", "S07-R34, S07-J13, 075, 077", ("management", "reports_trend")),
    ("growth_drivers", "מה מניע את הצמיחה לדעת ההנהלה, ואילו KPI היא מדגישה?",
     "What management says drives revenue and earnings, in its own words, and the KPIs it reports or stresses, with "
     "their values", "076, S07-R27, S07-R11, S07-J31", ("growth_engines",)),
    ("demand", "מה ההנהלה אומרת על הביקוש: הזמנות, צבר, פייפליין, לקוחות, נפחים?",
     "Demand: orders, backlog, pipeline, bookings, customers, volumes, pricing power", "077, S07-R33",
     ("growth_engines", "catalysts")),
    ("margins_costs", "מה קורה למרווחים ולהוצאות: התייעלות, עלויות, מינוף תפעולי?",
     "Margins and costs: cost programs and their targets, opex, input costs, operating leverage, whether profit grows "
     "from revenue or from cuts", "S07-R9, S07-R10, S07-J6, S07-J34, 075", ("reports_trend",)),
    ("new_products", "יש השקה, שוק חדש, פרויקט או אתר חדש בדרך, ומתי?",
     "Launches of products or services, new markets, new projects, plants or sites, and when", "077, S07-J30",
     ("catalysts", "growth_engines")),
    ("events_ahead", "אילו אירועים קבועים בלוח הזמנים: יום משקיעים, סגירת עסקה, החלטה, כנס?",
     "Scheduled events: investor or capital markets day, a deal closing, a regulatory or final investment decision, "
     "a conference, a contract renewal, with the date", "077, S08", ("catalysts",)),
    ("competition", "מה ההנהלה אומרת על התחרות ועל היתרון שלה?",
     "Management on competition, market share and what sets it apart", "077, S07-R3 שאלה 4", ("advantage", "vs_sector")),
    ("capital", "מה עם הקצאת ההון: באייבק, דיבידנד, חוב, גיוס, השקעות?",
     "Capital allocation: buybacks, dividends, debt, equity raises (dilution), capex", "S07-R6, S07-R37",
     ("reports_trend", "what_changes")),
    ("macro_exposure", "איך המאקרו, התשומות, המטבע או המכסים משפיעים, לדברי ההנהלה?",
     "Exposure to the macro, commodity and input prices, currency, rates, tariffs and geopolitics as management "
     "describes it", "S07-R3 (חשיפה למקרו ולתשומות)", ("big_picture", "risks_10k")),
    ("headwinds", "באילו רוחות נגד וסיכונים ההנהלה מודה?",
     "Headwinds and risks management admits: lower volumes, outages, weaker segments, disposals, legal",
     "S07-R3 (רוחות נגד), S07-R29", ("what_changes", "risks_10k")),
    ("analyst_pushback", "על מה האנליסטים לחצו, ומה ההנהלה ענתה?",
     "What the analysts pressed on in the Q&A, and management's answer: one excerpt of the question and one of the "
     "answer", "077", ("management", "what_changes")),
    ("tone", "מה הטון של ההנהלה על הרבעון והשנה הבאים: בטוחה, זהירה, מתחמקת?",
     "Management's tone about the next quarter and year (confident, cautious, evasive), in the words that show it",
     "075", ("management",)),
)
QUESTION_IDS = tuple(q for q, *_ in QUESTIONS)
QUESTION_HE = {q: he for q, he, *_ in QUESTIONS}
MAX_EXCERPTS = 3
ROLE_HE = {"management": "הנהלה", "analyst": "אנליסט", "operator": "מנחה"}
SECTION_HE = {"prepared": "דברי הפתיחה", "qa": "שאלות ותשובות"}

_EXCERPT = {"type": "object", "properties": {
    "fact_id": {"type": "string", "description": "the turn the excerpt is copied from"},
    "quote": {"type": "string", "description": "English, word for word from that turn: one to three whole sentences"}},
    "required": ["fact_id", "quote"], "additionalProperties": False}


def schema_extra() -> dict:
    """DC-26's own fields (cards.yaml points here)."""
    return {"answers": {"type": "array", "description": "one entry per question, in the order of world.questions",
                        "items": {"type": "object", "properties": {
                            "question": {"type": "string", "enum": list(QUESTION_IDS)},
                            "found": {"type": "boolean"},
                            "he": {"type": "string", "description": "Hebrew, one or two sentences: the answer; numbers "
                                   "only as they are in the excerpts; empty when not found"},
                            "excerpts": {"type": "array", "items": _EXCERPT, "description": "at most 3"}},
                            "required": ["question", "found", "he", "excerpts"], "additionalProperties": False}}}


def _label(t: dict) -> str:
    return f'{t.get("speaker") or "?"} ({ROLE_HE.get(t.get("role"), t.get("role") or "")}, ' \
           f'{SECTION_HE.get(t.get("section"), t.get("section") or "")})'


def call_sheet(sym: str, tr: dict, as_of: date) -> tuple[FactSheet, dict[str, dict]]:
    """Every turn of the call as a fact, and the turn behind each fact id."""
    fs, by_id = FactSheet(), {}
    day = _day(tr.get("date"), as_of)
    fs.add(f"{sym}.call", f'{tr.get("year")}Q{tr.get("quarter")} earnings call, {tr.get("date")}', tr["source"], day,
           url=tr.get("url") or "")
    for t in tr.get("turns") or []:
        if t.get("role") == "operator":
            continue
        f = fs.add(f'{sym}.call.turn[{t["n"]}]', f'{_label(t)}: {t["text"]}', f'{tr["source"]} {t.get("speaker", "")}'.strip(),
                   day, url=tr.get("url") or "")
        by_id[f.fact_id] = t
    return fs, by_id


def world(sym: str, tr: dict) -> dict:
    return {"symbol": sym, "call": f'{tr.get("year")}Q{tr.get("quarter")} ({tr.get("date")})',
            "write_in": "Hebrew in he; excerpts in English, word for word from one turn",
            "questions": [dict(id=q, question=he, look_for=look, rules=rules) for q, he, look, rules, _ in QUESTIONS]}


def _numbers_ok(text: str, pool_texts: list[str]) -> bool:
    """Every number of the summary is in the excerpts, in their scale or as written ("2.6 to 2.8 million")."""
    pool = [v for s in pool_texts for v in AR._walk(s)]
    pool += [float(x.replace(",", "")) for s in pool_texts for x in re.findall(r"\d[\d,]*(?:\.\d+)?", s)]
    for v, tol, pct, tok in AR.numbers_in_text(text):
        if not pct and v <= AR.SMALL_COUNT and tok.strip().isdigit():
            continue
        if not AR.number_ok(v, tol, pct, pool):
            return False
    return True


def verify(out: dict, by_id: dict[str, dict], tr: dict) -> dict:
    """The answers as the report keeps them: each excerpt checked word for word against its turn."""
    got = {a.get("question"): a for a in out.get("answers") or [] if isinstance(a, dict)}
    answers, dropped = [], []
    for q in QUESTION_IDS:
        a = got.get(q) or {}
        kept = []
        for x in (a.get("excerpts") or [])[:MAX_EXCERPTS]:
            t = by_id.get(x.get("fact_id") or "")
            quote = (x.get("quote") or "").strip()
            why = ("not a turn of the call" if t is None else
                   None if AR.quote_ok(quote, t["text"]) else "not word for word in the turn")
            if why:
                dropped.append({"question": q, "quote": quote, "why": why})
                continue
            kept.append({"n": t["n"], "speaker": t.get("speaker", ""), "role": t.get("role", ""),
                         "section": t.get("section", ""), "text": quote})
        he = (a.get("he") or "").strip() if kept else ""
        if he and not _numbers_ok(he, [k["text"] for k in kept]):
            dropped.append({"question": q, "quote": he, "why": "a number in the summary is not in the excerpts"})
            he = ""
        answers.append({"q": q, "question": QUESTION_HE[q], "found": bool(kept), "he": he, "excerpts": kept})
    return {k: tr.get(k) for k in ("date", "year", "quarter", "source", "url")} | {
        "answers": answers, "dropped": dropped, "found": sum(a["found"] for a in answers), "asked": len(QUESTIONS)}


def call_facts(fs: FactSheet, sym: str, call: dict | None, as_of: date, only: tuple[str, ...] | None = None) -> None:
    """The kept excerpts as facts of the call (source and link: the transcript), the summaries as the agent's."""
    if not call:
        return
    day = _day(call.get("date"), as_of)
    tag = f'{call.get("year")}Q{call.get("quarter")}'
    unanswered = []
    for a in call.get("answers") or []:
        if only and a["q"] not in only:
            continue
        if not a.get("excerpts"):
            unanswered.append(a["question"])
            continue
        for k, x in enumerate(a["excerpts"]):
            fs.add(f'{sym}.call.{a["q"]}[{k}]', f'[{tag} call {call.get("date")} · {_label(x)}] {x["text"]}',
                   f'roic:earnings-call {tag} {x.get("speaker", "")}'.strip(), day, url=call.get("url") or "")
        if a.get("he"):
            fs.add(f'{sym}.call.{a["q"]}.summary', a["he"], "agent:DC-26", day)
    if unanswered:
        fs.add(f"{sym}.call.not_found", unanswered, f"code:earnings_call {tag}", day)


def _day(x: Any, default: date) -> date:
    try:
        return date.fromisoformat(str(x)[:10])
    except ValueError:
        return default
