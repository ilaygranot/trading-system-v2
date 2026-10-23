"""The deep dive as a magazine article (Ilay 23.10): a headline that makes the claim, the course's questions as
chapters, the charts and the peers table where they answer a question, numbered footnotes with the source, date and
link, and the trade card as an appendix. The text is DC-25's (qt.research.article), after code checked every sentence;
everything else on the page is code. Without an article the page falls back to the trade card alone."""
from __future__ import annotations

from typing import Any

from qt.research import article as AR
from qt.research import report as R

E = R.E

# The chapters, in the course's order from the macro to the trade. Each holds article sections and, after the
# text, what code shows for it: charts by name (report.fund_figs / tech_figs) and tables.
CHAPTERS: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("picture", "התמונה הגדולה", ("big_picture",), ("rs",)),
    ("business", "העסק", ("business",), ()),
    ("peers", "מול המתחרים", ("vs_sector", "advantage"), ("peers_table",)),
    ("numbers", "המספרים", ("growth_engines", "reports_trend"), ("quarters", "margins", "cash", "surprises")),
    ("management", "מה ההנהלה אומרת", ("management",), ("guidance",)),
    ("market", "האם השוק מסכים, ואנחנו", ("market_agrees", "we_agree"), ("multiple", "targets")),
    ("catalysts", "מה יזיז את המניה", ("catalysts",), ("timeline",)),
    ("chart", "הגרף", ("chart",), ("weekly", "rsi", "macd", "adr", "lights")),
    ("risks", "מה יכול להשתבש", ("risks_10k", "what_changes"), ("breakers",)),
)
QUESTION = {k: (q, rules, is_q) for k, q, rules, is_q in AR.SECTIONS}
QNUM = {"big_picture": 1, "vs_sector": 2, "growth_engines": 3, "advantage": 4, "catalysts": 5, "reports_trend": 6,
        "market_agrees": 7, "we_agree": 8, "risks_10k": 9, "what_changes": 10}

# Terms a reader meets in the article, each as the course defines it (the rule id says where).
GLOSSARY: tuple[tuple[str, str, str], ...] = (
    ("מינוף תפעולי", "הכנסות שגדלות מהר יותר מעלות המכר, ולכן המרווחים עולים. ההפך הוא דגל אדום.", "S07-R9"),
    ("פרמיה מול הסקטור", "המכפיל הצפוי של המניה חלקי המכפיל הצפוי של הסקטור, פחות 1. פרמיה צריכה הצדקה מהדוחות.", "S07-R19"),
    ("השוק מסכים", "מכפילים גבוהים מול הסקטור ומגמה טכנית בכיוון. מכפיל נמוך: השוק עדיין לא מזהה, או לא מסכים.", "S07-R28"),
    ("חוב נטו ל-EBITDA", "כל החוב וההתחייבויות פחות המזומן והנכסים הנזילים, חלקי הרווח התפעולי. עלייה לאורך שנים היא דגל.", "S07-R24"),
    ("הפתעה", "הפער בין הרווח בפועל לתחזית האנליסטים, באחוזים: כיוון, תדירות ועוצמה לאורך רבעונים.", "S07-R31"),
    ("RSI(20)", "מומנטום, ברמות 30/50/70. לא נכנסים ללונג כשהוא יורד; שהייה מעל 70 לבדה אינה איתות היפוך.", "S09-R19"),
    ("MACD 20/60/10", "ממוצע נע מעריכי 20 פחות 60, וקו איתות 10. ההיסטוגרמה היא הפער ביניהם.", "S09-R21"),
    ("ADR20 / ADR60", "הטווח היומי הממוצע של 20 יום חלקי זה של 60. גבוה בהרבה מ-1: ממתינים שבוע-שבועיים.", "S09-R52"),
    ("יחס סיכוי-סיכון", "המרחק ליעד חלקי המרחק לעצירה. המינימום בקורס הוא 3.", "S10-R3"),
    ("עצירה", "k כפול הטווח היומי הממוצע של 60 יום, מהכניסה, עם k של 4 לפחות כברירת מחדל.", "S11-R11"),
)


def has_article(i: dict) -> bool:
    return bool(((i.get("judgment") or {}).get("article") or {}).get("sections"))


def _tip(n: dict) -> str:
    v = n.get("value") or ""
    return f'{n.get("source", "")} · {n.get("as_of", "")}: {v[:220]}{"…" if len(v) > 220 else ""}'


def _notes(nums: list[int], sym: str, notes: dict[int, dict]) -> str:
    if not nums:
        return ""
    links = ",".join(f'<a href="#src-{E(sym)}-{n}" data-tip="{E(_tip(notes.get(n, {})))}">{n}</a>' for n in nums)
    return f'<sup class="fn" dir="ltr">{links}</sup>'                 # 1,2,3: one marker, not 123


def _sentence(s: dict | None, sym: str, notes: dict[int, dict]) -> str:
    if not s:
        return ""
    return f'{E(s["text"])}{_notes(s["notes"], sym, notes)}'


def _section(key: str, sec: dict, sym: str, notes: dict[int, dict]) -> str:
    q, rules, is_q = QUESTION[key]
    ans = sec.get("answer")
    chip = (f'<span class="ans ans-{E(ans)}">{E(AR.ANSWER_HE.get(ans, ""))}</span>'
            if is_q and ans in AR.ANSWER_HE else "")
    num = f'<span class="qn">שאלה {QNUM[key]}</span>' if key in QNUM else ""
    out = [f'<section class="q" id="q-{E(sym)}-{E(key)}"><h3>{num}{E(q)} {chip}</h3>']
    if not sec.get("paragraphs"):
        out.append('<p class="nosrc">לא מצאנו מקור שעונה על השאלה הזו, ולכן אין כאן תשובה.</p>')
    pulls = []
    for para in sec.get("paragraphs") or []:
        out.append("<p>" + " ".join(_sentence(s, sym, notes) for s in para) + "</p>")
        pulls += [q for s in para for q in s.get("quotes") or []]
    for qt in pulls[:2]:
        out.append(f'<blockquote class="pull"><p dir="ltr" lang="en">“{E(qt["text"])}”</p>'
                   f'<footer>{E(qt.get("he") or "")}{_notes([qt["note"]], sym, notes)}</footer></blockquote>')
    out.append(f'<p class="rules-ref">לפי הקורס: {E(rules)}</p></section>')
    return "".join(out)


def peers_table(i: dict) -> str:
    """S07-R3 question 2 in numbers: the stock, its peers from the sector's screen (same industry first) and the
    sector average. Code only; '—' where the screen has no number."""
    row, avg = i.get("row") or {}, i.get("sector_avg") or {}
    peers = [p for p in i.get("peers") or [] if any(p.get(k) is not None for k in ("eg1", "sg1", "pe1"))]
    if not peers:
        return ""

    def tr(sym: str, name: str, ind: str, cap: Any, r: dict, cls: str = "") -> str:
        return (f'<tr class="{cls}"><td><b>{E(sym)}</b> <small>{E(name or "")}</small></td><td>{E(ind or "")}</td>'
                f'<td class="n">{R.money(cap)}</td><td class="n">{R.num(r.get("sg1"), 0, True, True)}</td>'
                f'<td class="n">{R.num(r.get("eg1"), 0, True, True)}</td><td class="n">{R.num(r.get("eg2"), 0, True, True)}</td>'
                f'<td class="n">{R.num(r.get("pe1"), 1)}</td><td class="n">{R.num(r.get("ps1"), 1)}</td></tr>')
    rows = [tr(i["sym"], i["profile"].get("name"), i["profile"].get("industry"), row.get("market_cap"), row, "self")]
    rows += [tr(p["sym"], p.get("name"), p.get("industry"), p.get("market_cap"), p) for p in peers]
    rows.append(tr(f'ממוצע {i["etf"]}', "", "", None, {"sg1": avg.get("sg1"), "eg1": avg.get("eg1"), "eg2": avg.get("eg2"),
                                                        "pe1": avg.get("pe"), "ps1": avg.get("ps")}, "avg"))
    return ('<figure class="peers"><figcaption><b>המניה, המתחרים מהסינון וממוצע הסקטור</b></figcaption><div class="tbl">'
            '<table class="compact"><thead><tr><th>מניה</th><th>תעשייה</th><th>שווי $</th><th>צמיחת מכירות השנה</th>'
            '<th>צמיחת רווח השנה</th><th>שנה הבאה</th><th>P/E צפוי</th><th>P/S</th></tr></thead><tbody>'
            + "".join(rows) + '</tbody></table></div><p class="cap">מסינון S06 של הסקטור, אותה תעשייה קודם. '
            'צמיחה ומכפילים לפי תחזית האנליסטים לשנה הנוכחית (F1) והבאה (F2).</p></figure>')


def _extras(i: dict, names: tuple[str, ...], figs: dict[str, str]) -> str:
    j, p = i["judgment"], i["plan"]
    out = []
    for n in names:
        if n in figs and figs[n]:
            out.append(figs[n])
        elif n == "peers_table":
            out.append(peers_table(i))
        elif n == "guidance":
            res = (i.get("documents") or {}).get("results") or {}
            for g in res.get("guidance") or []:
                out.append(f'<p class="small">תחזית {E(str(g.get("metric")))} ({E(str(g.get("period")))}): '
                           f'{R.num(g.get("low"), 0)}–{R.num(g.get("high"), 0)} {E(str(g.get("unit") or ""))} · '
                           f'{R.src_link(res.get("url"), res.get("form") or "")} {E(res.get("date") or "")}</p>')
        elif n == "targets" and i.get("targets"):
            out.append('<div class="tbl"><table class="compact"><thead><tr><th>מקור יעד (S10-R3)</th><th>מחיר</th>'
                       '<th>תזוזה</th><th>R:R</th></tr></thead><tbody>' + "".join(
                           f'<tr class="{"chosen" if t["source"] == p.get("target_source") else ""}">'
                           f'<td>{E(R.TARGET_HE.get(t["source"], t["source"]))}</td><td class="n">{R.px(t["price"])}</td>'
                           f'<td class="n">{R.num(t["move_pct"], 1, True, True)}</td><td class="n">{R.num(t["rr"], 1)}</td></tr>'
                           for t in i["targets"]) + "</tbody></table></div>")
        elif n == "timeline" and j.get("catalysts"):
            out.append('<ul class="timeline">' + "".join(R._catalyst_li(x) for x in j["catalysts"]) + "</ul>")
        elif n == "lights":
            tl = i["timing"]
            out.append(f'<p class="small">רמזור: מניה {R.pill(tl["stock"]["overall"])} · סקטור {R.pill(tl["sector"]["overall"])}'
                       f' · שוק {R.pill(tl["market"]["overall"])} → {R.pill(tl["combined"])}</p>'
                       + R.light_grid(tl["stock"]["sources"]))
        elif n == "breakers" and j.get("breakers"):
            out.append('<div class="breakers"><b>שוברי התזה שנרשמו מראש (DC-12)</b><ul>' + "".join(
                f'<li>{E(b["text"])} <small>· מה בודקים: {E(b["check"])}</small></li>' for b in j["breakers"]) + "</ul></div>")
    out = [x for x in out if x]
    return f'<div class="mag-figs">{"".join(out)}</div>' if out else ""


def _scorecard(sym: str, secs: dict) -> str:
    cells = []
    for key, n in sorted(QNUM.items(), key=lambda x: x[1]):
        a = (secs.get(key) or {}).get("answer", "no_source")
        cells.append(f'<a href="#q-{E(sym)}-{E(key)}" class="sc sc-{E(a)}"><b>{n}</b><span>{E(QUESTION[key][0])}</span>'
                     f'<i>{E(AR.ANSWER_HE.get(a, "—"))}</i></a>')
    return f'<nav class="scorecard" aria-label="עשר השאלות">{"".join(cells)}</nav>'


def _stats(i: dict) -> str:
    p, j, tl = i["plan"], i["judgment"], i["timing"]
    cells = [("מחיר", R.px(p["entry"]), ""), ("יעד", R.px(p.get("target")), R.num(p.get("move_pct"), 1, True, True)),
             ("עצירה", R.px(p["stop"]), R.num(-p["stop_pct"], 1, True)), ("R:R", R.num(p.get("rr"), 1), "מינימום 3"),
             ("רמזור", R.COLOR_HE.get(tl["combined"], "—"), ""),
             ("הסיפור", R.STORY_HE.get(j.get("story_type", ""), j.get("story_type") or "—"), R.BUCKET_HE.get(i["bucket"], ""))]
    return '<div class="mag-stats">' + "".join(f'<div><label>{E(a)}</label><b>{E(b)}</b><small>{E(c)}</small></div>'
                                               for a, b, c in cells) + "</div>"


def _sources(sym: str, art: dict) -> str:
    items = []
    for n in art.get("notes") or []:
        link = R.src_link(n.get("url"), n.get("source") or "") if n.get("url") else E(n.get("source") or "")
        items.append(f'<li id="src-{E(sym)}-{n["n"]}"><span class="src-h">{link} · {E(n.get("as_of") or "")} · '
                     f'<code>{E(n.get("fact_id") or "")}</code> <small>{E(n.get("key") or "")}</small></span>'
                     f'<q dir="auto">{E(n.get("value") or "")}</q></li>')
    return f'<ol class="srcs">{"".join(items)}</ol>' if items else ""


def article_html(i: dict, checks: list[dict] | None = None, as_of: str = "") -> str:
    """The whole page for one stock: the article, then the appendix (sources, what code dropped, the glossary and
    the trade card with every number). Falls back to the trade card when the run wrote no article."""
    if not has_article(i):
        return R._idea(i, checks)
    sym, j, art = i["sym"], i["judgment"], i["judgment"]["article"]
    notes = {n["n"]: n for n in art.get("notes") or []}
    secs = art["sections"]
    figs = {**R.fund_figs(i), **R.tech_figs(i)}
    pr = i["profile"]
    st = art.get("stats") or {}
    head = _sentence(art.get("headline"), sym, notes) or E(f'{pr.get("name") or sym}: {R.STORY_HE.get(j.get("story_type", ""), "")}')
    out = [f'<article class="mag" id="idea-{E(sym)}">',
           f'<header class="mag-head"><p class="kicker">{E(pr.get("industry") or "")} · {E(i["etf"])} · '
           f'{E(R.DIR_HE[i["direction"]])} · ניתוח עומק {E(art.get("as_of") or as_of)}</p>'
           f'<h2 class="headline"><span class="sym">{E(sym)}</span> {head}</h2>'
           + (f'<p class="dek">{_sentence(art.get("dek"), sym, notes)}</p>' if art.get("dek") else "")
           + f'<p class="byline">{E(pr.get("name") or "")} · נכתב על ידי A4 ונבדק בקוד: {st.get("kept", 0)} משפטים, '
           f'כל אחד עם מקור; {st.get("dropped", 0)} נמחקו בבדיקה · {len(notes)} מקורות</p></header>',
           _stats(i)]
    if j.get("tldr"):
        out.append(R._tldr(i))
    out.append(_scorecard(sym, secs))
    for cid, title, keys, extra in CHAPTERS:
        body = "".join(_section(k, secs.get(k) or {}, sym, notes) for k in keys)
        out.append(f'<section class="chapter" id="ch-{E(sym)}-{cid}"><h2 class="ch">{E(title)}</h2>{body}'
                   f'{_extras(i, extra, figs)}</section>')
    out.append('<section class="chapter" id="ch-' + E(sym) + '-trade"><h2 class="ch">העסקה</h2>'
               f'<figure class="pchart"><figcaption>מחיר, שנה אחרונה: סגירה, ממוצעים 21/63/252 וקווי התוכנית</figcaption>'
               f'{R.price_chart(i["chart"], i["plan"], i["levels"], i["direction"])}</figure>'
               f'{R.rr_bar(i["plan"]["entry"], i["plan"]["stop"], i["plan"].get("target"), i["direction"])}'
               + (R._exit(i) if i.get("exit") else "") + "</section>")
    out.append(f'<section class="appendix"><h2 class="ch">מקורות</h2><p class="note">כל מספר הערה במאמר מוביל לכאן: '
               f'מאיפה העובדה, מאיזה תאריך, וקישור למסמך. ציטוטים מופיעים כאן מילה במילה.</p>{_sources(sym, art)}')
    if art.get("dropped"):
        out.append(f'<details><summary>מה נמחק בבדיקת הקוד ({len(art["dropped"])})</summary><ul class="small">' + "".join(
            f'<li><b>{E(QUESTION.get(x["section"], (x["section"],))[0])}</b>: {E(x["text"])} <small>({E(x["why"])})</small></li>'
            for x in art["dropped"]) + "</ul></details>")
    out.append('<details><summary>מילון מושגים (מהקורס)</summary><dl class="glossary">' + "".join(
        f'<dt>{E(t)}</dt><dd>{E(d)} <small>{E(r)}</small></dd>' for t, d, r in GLOSSARY) + "</dl></details>")
    card = R._idea(i, checks).replace(f'id="idea-{E(sym)}"', f'id="card-{E(sym)}"', 1)
    out.append(f'<details><summary>כרטיס העסקה וכל הנתונים</summary>{card}</details></section></article>')
    return "".join(out)


CSS = """
.mag{background:var(--panel);border:1px solid var(--line);border-radius:4px;padding:28px clamp(16px,4vw,48px);margin:16px 0}
.mag-head{border-bottom:2px solid var(--ink);padding-bottom:14px;margin-bottom:12px}
.kicker{font-size:.78rem;letter-spacing:.06em;color:var(--accent);font-weight:700;margin:0 0 .4rem}
.headline{font-family:var(--display);font-size:clamp(1.6rem,3.4vw,2.4rem);line-height:1.2;margin:0;border:0;padding:0}
.headline .sym{font-family:var(--mono);font-size:.6em;color:var(--muted);vertical-align:.3em;margin-inline-end:.3em}
.dek{font-family:var(--display);font-size:1.2rem;color:var(--muted);margin:.6rem 0 .4rem;line-height:1.45}
.byline{font-size:.8rem;color:var(--muted);margin:.3rem 0 0}
.mag-stats{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:1px;background:var(--line);border:1px solid var(--line);margin:12px 0}
.mag-stats div{background:var(--panel);padding:8px 10px;display:flex;flex-direction:column;min-width:0}
.mag-stats label{font-size:.72rem;color:var(--muted)} .mag-stats b{font-family:var(--mono);font-size:1.05rem}
.mag-stats small{font-size:.74rem;color:var(--muted)}
.scorecard{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:6px;margin:16px 0}
.sc{display:flex;flex-direction:column;gap:2px;text-decoration:none;color:var(--ink);border:1px solid var(--line);
  border-top:3px solid var(--muted);padding:6px 8px;border-radius:3px;font-size:.78rem;min-width:0}
.sc b{font-family:var(--mono)} .sc span{line-height:1.3} .sc i{font-style:normal;font-weight:700}
.sc-yes{border-top-color:var(--g)} .sc-yes i{color:var(--g)} .sc-partial{border-top-color:var(--o)} .sc-partial i{color:var(--o)}
.sc-no{border-top-color:var(--r)} .sc-no i{color:var(--r)} .sc-no_source i{color:var(--muted)}
.chapter{max-width:720px;margin-inline:auto}
.chapter .mag-figs{max-width:none}
h2.ch{font-family:var(--display);font-size:1.5rem;border-top:1px solid var(--ink);padding-top:.8rem;margin:2.2rem 0 .6rem}
.q h3{font-family:var(--display);font-size:1.15rem;margin:1.4rem 0 .4rem;display:flex;flex-wrap:wrap;gap:8px;align-items:baseline}
.q .qn{font-family:var(--body);font-size:.72rem;font-weight:700;color:var(--accent);letter-spacing:.04em}
.q p{font-family:var(--display);font-size:1.08rem;line-height:1.75;margin:.5rem 0}
.q p.rules-ref{font-family:var(--body);font-size:.75rem;color:var(--muted)}
.q p.nosrc{font-family:var(--body);font-size:.92rem;color:var(--muted);border-inline-start:3px solid var(--line);padding-inline-start:10px}
.ans{font-family:var(--body);font-size:.72rem;font-weight:700;border-radius:3px;padding:1px 8px}
.ans-yes{background:var(--g-bg);color:var(--g)} .ans-partial{background:var(--o-bg);color:var(--o)}
.ans-no{background:var(--r-bg);color:var(--r)} .ans-no_source{background:var(--grid);color:var(--muted)}
sup.fn{font-family:var(--mono);font-size:.62em;line-height:0;margin-inline-start:2px;unicode-bidi:isolate} sup.fn a{text-decoration:none;padding:0 1px;position:relative}
sup.fn a::after{content:attr(data-tip);position:absolute;bottom:1.8em;inset-inline-start:-8px;width:min(320px,70vw);
  background:var(--ink);color:var(--panel);font:400 .74rem/1.45 var(--body);padding:8px 10px;border-radius:4px;
  opacity:0;pointer-events:none;transition:opacity .12s;z-index:20;white-space:normal;text-align:start}
sup.fn a:hover::after,sup.fn a:focus-visible::after{opacity:1}
blockquote.pull{margin:1rem 0;padding:6px 18px;border-inline-start:4px solid var(--accent)}
blockquote.pull p{font-family:Georgia,var(--display);font-size:1.15rem;font-style:italic;margin:0;text-align:left}
blockquote.pull footer{font-size:.85rem;color:var(--muted);margin-top:4px}
.mag-figs{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px 24px;margin:1rem 0}
.mag-figs>.peers,.mag-figs>.tbl,.mag-figs>.timeline,.mag-figs>.breakers,.mag-figs>p,.mag-figs>.lights{grid-column:1/-1}
tr.self td{background:var(--accent-soft);font-weight:600} tr.avg td{color:var(--muted);font-style:italic}
.breakers{border:1px solid var(--line);border-radius:4px;padding:8px 14px;font-size:.9rem}
.appendix{max-width:820px;margin-inline:auto}
ol.srcs{font-size:.82rem;padding-inline-start:1.6rem} ol.srcs li{margin:.5rem 0;scroll-margin-top:60px}
ol.srcs li:target{background:var(--accent-soft)}
ol.srcs .src-h{display:block;color:var(--muted)} ol.srcs q{display:block;color:var(--ink);quotes:none;word-break:break-word}
dl.glossary{display:grid;grid-template-columns:max-content 1fr;gap:6px 14px;font-size:.88rem}
dl.glossary dt{font-weight:700} dl.glossary dd{margin:0}
@media (max-width:760px){.mag-stats{grid-template-columns:repeat(3,minmax(0,1fr))} .scorecard{grid-template-columns:repeat(2,minmax(0,1fr))}
  .mag-figs{grid-template-columns:minmax(0,1fr)} dl.glossary{grid-template-columns:1fr}}
@media print{.rail{display:none} .mag{border:0;padding:0} .chapter,.q,figure,blockquote{break-inside:avoid} sup.fn a::after{display:none}
  a{color:inherit;text-decoration:none}}
"""
