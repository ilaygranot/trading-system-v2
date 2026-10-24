"""What a finished weekly report is (Ilay 24.10: "ריצה כמו היום לא יכולה לקרות"), checked in code on the dossier.

The 23.10 report went out with 4 of 12 picked stocks, no article, and the market story copied from 20.10, and
every test passed: the tests checked the parts, never the report. This is the contract of the whole report. It
runs in the weekly step before the report is saved (the report opens with its verdict, and a report with a
block fails the step), in the dry run (qt.dryrun) and in `qt verify-report` on any dossier.

block: the report is not finished; the weekly step fails after the partial report, so a resume can finish it.
flag:  the report is finished but something in it is weaker than it should be; it is shown in red at the top
       of the report and in the run's message, never left to a line at the bottom.
"""
from __future__ import annotations

from typing import Any

NARRATIVE = ("story", "fed", "events")             # A1's market story (report.macro_section)
# The market story (A1, DC-27) blocks like any other missing part (Ilay 24.10: nothing goes on without its data).
NARRATIVE_REQUIRED = True
DECISIONS = ("long", "short", "watch_only", "no")
BUCKETS = ("enter", "conditional", "watch", "reject")
HEAT = ("hot", "cold", "no")
MIN_ANSWERED_SECTIONS = 7                          # of 13 article sections with kept text (not from course)
CONFIG_EVENTS = ("SEC_CONTACT not set", "ROIC_API_KEY not set")


def _item(code: str, where: str, text: str) -> dict[str, str]:
    return {"code": code, "where": where, "text": text}


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def _text(v: Any) -> bool:
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, tuple)):
        return any(_text(x) for x in v)
    if isinstance(v, dict):
        return any(_text(x) for x in v.values())
    return False


def _plan(sym: str, i: dict) -> list[dict]:
    p, dr = i.get("plan") or {}, i.get("direction")
    out = []
    missing = [k for k in ("entry", "stop", "target", "rr") if not _num(p.get(k))]
    if missing:
        return [_item("plan_missing", sym, f"{sym}: חסר בתוכנית העסקה: {', '.join(missing)}")]
    sg = 1 if dr == "long" else -1 if dr == "short" else 0
    if not sg:
        return [_item("direction_missing", sym, f"{sym}: אין כיוון (לונג / שורט)")]
    if sg * (p["entry"] - p["stop"]) <= 0:
        out.append(_item("stop_side", sym, f"{sym}: הסטופ {p['stop']:g} בצד הלא נכון של הכניסה {p['entry']:g}"))
    if sg * (p["target"] - p["entry"]) <= 0:
        out.append(_item("target_side", sym, f"{sym}: היעד {p['target']:g} בצד הלא נכון של הכניסה {p['entry']:g}"))
    if p["rr"] <= 0:
        out.append(_item("rr", sym, f"{sym}: יחס סיכון-סיכוי {p['rr']:g}"))
    return out


def _article(sym: str, j: dict) -> tuple[list[dict], list[dict]]:
    a = j.get("article")
    if not a:
        why = f" ({j['article_error']})" if j.get("article_error") else ""
        return [_item("article_missing", sym, f"{sym}: אין מאמר{why}")], []
    secs = a.get("sections") or {}
    answered = [k for k, s in secs.items() if (s or {}).get("paragraphs")]
    if not (a.get("headline") or {}).get("text") or not answered:
        return [_item("article_empty", sym, f"{sym}: המאמר ריק (אין כותרת או אף פסקה שעברה את בדיקת הקוד)")], []
    if len(answered) < MIN_ANSWERED_SECTIONS:
        return [], [_item("article_thin", sym, f"{sym}: המאמר עונה רק על {len(answered)} מתוך {len(secs)} השאלות")]
    return [], []


def _idea(sym: str, i: dict) -> tuple[list[dict], list[dict]]:
    blocks, flags = [], []
    j = i.get("judgment") or {}
    if j.get("decision") not in DECISIONS:
        blocks.append(_item("decision", sym, f"{sym}: אין החלטה של הסוכנים"))
    if not _text(j.get("story")):
        blocks.append(_item("story", sym, f"{sym}: אין סיפור (DC-10)"))
    if not _text(j.get("thesis")):
        blocks.append(_item("thesis", sym, f"{sym}: אין תזה (DC-11)"))
    if not j.get("breakers"):
        blocks.append(_item("breakers", sym, f"{sym}: אין שוברי תזה (DC-12)"))
    if j.get("heat") not in HEAT:
        blocks.append(_item("heat", sym, f"{sym}: אין החלטת חום (DC-13)"))
    if j.get("decision") in ("long", "short"):
        for k, card in (("trigger", "DC-17"), ("k", "DC-19"), ("sizing", "DC-20")):
            if not j.get(k):
                blocks.append(_item(k, sym, f"{sym}: רעיון חם בלי {card}"))
    if i.get("bucket") not in BUCKETS:
        blocks.append(_item("bucket", sym, f"{sym}: אין סיווג (כניסה / מותנה / מעקב / נפסל)"))
    blocks += _plan(sym, i)
    b, f = _article(sym, j)
    blocks += b
    flags += f
    if j.get("call_error"):
        blocks.append(_item("call_error", sym, f"{sym}: שיחת הוועידה לא נותחה ({j['call_error'][:120]})"))
    errs = (i.get("documents") or {}).get("errors") or []
    if errs:
        blocks.append(_item("documents", sym, f"{sym}: מסמכי SEC או התמלול לא הגיעו ({'; '.join(map(str, errs))[:160]})"))
    if (i.get("valuation") or {}).get("target_error"):
        blocks.append(_item("target_error", sym, f"{sym}: יעד המכפיל לא חושב ({i['valuation']['target_error'][:120]})"))
    for c in j.get("catalysts") or []:
        if str(c.get("code_reason") or "").startswith("agent_error"):
            blocks.append(_item("dc14_error", sym, f"{sym}: A5 לא סיווג את האירוע {str(c.get('what') or c.get('date'))[:60]} "
                                                  f"({c['code_reason']})"))
    if any(isinstance(o, dict) and o.get("error") for o in i.get("options") or []):
        flags.append(_item("options", sym, f"{sym}: שרשרת האופציות לא נטענה (גרסה 1 סוחרת במניות בלבד)"))
    return blocks, flags


def _upstream(d: dict) -> list[dict]:
    """The stages before the agents: a stock that dropped out because a request failed, not because of a rule."""
    out = []
    uerr = ((d.get("meta") or {}).get("universe") or {}).get("errors") or {}
    hard = {s: e for s, e in uerr.items() if "HTTP 404" not in str(e)}
    if hard:
        out.append(_item("universe", "universe", f"{len(hard)} מניות נפלו מהיקום בגלל שגיאת נתונים (לא בגלל כלל): "
                         + ", ".join(sorted(hard)[:15]) + f" · {str(next(iter(hard.values())))[:80]}"))
    bad = [t for t in d.get("timing") or [] if t.get("error") and "daily bars" not in str(t["error"])]
    if bad:
        out.append(_item("timing", "timing", f"הרמזור לא חושב ל-{len(bad)} מניות: "
                         + ", ".join(f"{t['sym']} ({str(t['error'])[:50]})" for t in bad[:8])))
    for etf, s in (d.get("screens") or {}).items():
        for sym, x in ((s or {}).get("dc08") or {}).items():
            if x.get("status") == "error":
                out.append(_item("dc08_error", sym, f"{sym}: DC-08 לא נשאל עד הסוף ({str(x.get('reason'))[:100]})"))
    return out


def _verification(v: Any) -> tuple[list[dict], list[dict]]:
    if not isinstance(v, dict) or "checks" not in v:
        return [_item("verification", "verify", "בדיקת הנתונים (SEC, מחיר שני, טריות) לא רצה")], []
    blocks = []
    if not v.get("sec"):
        blocks.append(_item("verify_sec", "verify", "בדיקת הנתונים רצה בלי SEC (לא זמין או חסום)"))
    if not v.get("second_price"):
        blocks.append(_item("verify_price", "verify", "בדיקת הנתונים רצה בלי מקור שני למחיר (FMP / Alpha Vantage)"))
    down = [c for c in v["checks"] if c.get("field") == "close" and "לא זמין" in str(c.get("note"))]
    if down:
        blocks.append(_item("verify_price_down", "verify", "המקור השני למחיר לא ענה ל-" + ", ".join(c["subject"] for c in down)))
    bad = ((v.get("summary") or {}).get("total") or {}).get("bad", 0)
    flags = [_item("verification_bad", "verify", f"{bad} בדיקות נתונים נכשלו (✗): מקורות לא מסכימים")] if bad else []
    return blocks, flags


def check(d: dict) -> dict[str, Any]:
    """The verdict on one dossier: complete or partial, every block and flag with where it is, and the counts."""
    blocks: list[dict] = []
    flags: list[dict] = []
    meta, J = d.get("meta") or {}, d.get("judgments") or {}
    ideas = d.get("ideas") or {}

    from qt.research.judge import JUDGED_BY
    if meta.get("judged_by") != JUDGED_BY:
        blocks.append(_item("judged_by", "meta", f"השיפוט לא נעשה על ידי הסוכנים: {meta.get('judged_by', 'חסר')}"))
    for ev in meta.get("events") or []:
        if any(c in str(ev.get("text", "")) for c in CONFIG_EVENTS):
            blocks.append(_item("config", "meta", f"הגדרה חסרה בשרת: {ev['text'][:120]}"))

    st = ((d.get("macro") or {}).get("stage2") or {}).get("stance") or {}
    if not st.get("zone"):
        blocks.append(_item("stance", "macro", "אין עמדת מקרו (אזור)"))
    a1 = J.get("A1") or {}
    for card in ("DC-01", "DC-02", "DC-03", "DC-04"):
        if not (a1.get(card) or {}).get("choice"):
            blocks.append(_item("a1", "macro", f"A1 לא החליט ב-{card}"))
    carried = (J.get("carried") or {})
    copied = [k for k in NARRATIVE if f"A1.{k}" in (carried.get("what") or [])]
    absent = [k for k in NARRATIVE if not _text(a1.get(k))]
    if copied or absent:
        names = {"story": "הסיפור", "fed": "הפד", "events": "האירועים"}
        text = "סיפור השוק לא נכתב השבוע: " + ", ".join(
            [f"{names[k]} הועתק מ-{carried.get('from')}" for k in copied] + [f"{names[k]} חסר" for k in absent if k not in copied])
        (blocks if NARRATIVE_REQUIRED else flags).append(_item("narrative", "macro", text))

    sel = J.get("A3") or {}
    picked = list(sel.get("picked") or [])
    covered = set(J.get("errors") or {}) | set(sel.get("cut_by_budget") or [])
    for s in J.get("incomplete") or []:                # what a resume can still ask for; the rest is listed below
        if s not in covered and not str(s).endswith(":article"):
            blocks.append(_item("incomplete", s, f"{s}: לא הושלם (נחתך או נכשל)"))
    for s, e in (J.get("errors") or {}).items():
        blocks.append(_item("error", s, f"{s}: הניתוח נכשל: {str(e)[:160]}"))
    for s in sel.get("cut_by_budget") or []:
        blocks.append(_item("cut_by_budget", s, f"{s}: נחתך בתקרת העלות"))
    for s in picked:
        if s not in ideas:
            blocks.append(_item("picked_missing", s, f"{s}: נבחרה לניתוח עומק ואין לה ניתוח בדוח"))
    if not picked and sel.get("candidates"):
        blocks.append(_item("no_pick", "A3", f"{sel['candidates']} מועמדות ואף מניה לא נבחרה"))
    dd = next((f.get("n") for f in d.get("funnel") or [] if f.get("stage") == "deep_dive"), None)
    if dd is not None and dd != len(ideas):
        blocks.append(_item("funnel", "funnel", f"המשפך אומר {dd} ניתוחי עומק, בדוח יש {len(ideas)}"))

    for sym, i in ideas.items():
        b, f = _idea(sym, i)
        blocks += b
        flags += f

    blocks += _upstream(d)
    b, f = _verification(d.get("verification"))
    blocks += b
    flags += f
    shaky = [x for x in J.get("agent_log") or [] if x.get("status") not in (None, "decided")]
    if shaky:
        flags.append(_item("agents_undecided", "agents", f"{len(shaky)} החלטות של הסוכנים לא הוכרעו או נדחו: "
                           + ", ".join(sorted({f"{x.get('card')} {x.get('subject')}" for x in shaky}))[:200]))

    with_article = sum(1 for i in ideas.values() if (i.get("judgment") or {}).get("article"))
    return {"status": "partial" if blocks else "complete", "blocks": blocks, "flags": flags,
            "counts": {"picked": len(picked), "analysed": len(ideas), "articles": with_article}}


def line(c: dict) -> str:
    """One line for Telegram and the dashboard."""
    n = c["counts"]
    head = (f"דוח מלא: {n['analysed']}/{n['picked']} מניות, {n['articles']} מאמרים" if c["status"] == "complete"
            else f"דוח חלקי: {len(c['blocks'])} חוסרים · {n['analysed']}/{n['picked']} מניות, {n['articles']} מאמרים")
    return head + (f" · {len(c['flags'])} אזהרות" if c["flags"] else "")


def lines(c: dict, limit: int = 12) -> list[str]:
    out = [("✅ " if c["status"] == "complete" else "❌ ") + line(c)]
    out += [f"• {x['text']}" for x in c["blocks"][:limit]]
    if len(c["blocks"]) > limit:
        out.append(f"• ועוד {len(c['blocks']) - limit}")
    out += [f"⚠️ {x['text']}" for x in c["flags"][:limit]]
    return out
