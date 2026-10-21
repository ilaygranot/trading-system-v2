"""The research run's dossier as one readable HTML page (Hebrew, right to left, inline SVG charts).

Built for a person who reads the reasoning, not only the result: the trade list comes first, then every stage
that led to it with its numbers, charts, sources and the judgment written on top of them. Code facts and
judgment are marked apart, and so is everything the course does not specify.
"""
from __future__ import annotations

import html
import math
from datetime import date
from typing import Any, Iterable, Sequence

E = html.escape

COLOR_HE = {"green": "ירוק", "orange": "כתום", "red": "אדום"}
BUCKET_HE = {"enter": "כניסה", "conditional": "מותנה בטריגר", "watch": "מעקב", "reject": "נדחה"}
DIR_HE = {"long": "לונג", "short": "שורט"}
TARGET_HE = {"technical_level": "רמה טכנית", "own_multiple_target": "מכפיל עצמי", "all_time_high_target": "שיא/שפל"}
STRUCT_HE = {"stock": "מניה", "single_option": "אופציה בודדת", "call_spread": "מרווח קול", "put_spread": "מרווח פוט",
             "none_suitable": "אין מבנה מתאים"}
SOURCE_HE = {"levels": "רמות", "price_behavior": "התנהגות מחיר", "sma": "ממוצעים", "rsi": "RSI", "macd": "MACD",
             "psar": "PSAR", "volatility_stop": "עצירת תנודתיות"}
STORY_HE = {"momentum_long": "מומנטום לונג", "momentum_short": "מומנטום שורט", "growth_to_profit": "מצמיחה לרווח",
            "turnaround": "תפנית", "value_trap": "מלכודת ערך", "bubble": "בועה", "stagnation": "קיפאון", "unclear": "לא ברור"}
CAT_HE = {"A": "A (חזק)", "B": "B", "C": "C", "D": "D (חלש)", "macro_lead": "מקרו מוביל", "macro_noise": "רעש מקרו"}
FLAG_HE = {
    "acceleration_find_the_factor": "האצה בצמיחה: למצוא את הגורם (S07-R11)",
    "negative_operating_leverage": "מינוף תפעולי שלילי (S07-R9)",
    "pricing_risk_gap_narrowing": "פער הצמיחה מול הסקטור מצטמצם (S07-R12)",
    "net_debt_ebitda_rising": "חוב נטו/EBITDA עולה (S07-R24)",
    "dilution": "דילול מניות",
    "weak_liquidity_long_waits": "נזילות חלשה: הלונג ממתין",
    "weak_liquidity_supports_short": "נזילות חלשה: תומך בשורט",
    "verify_extreme_growth": "צמיחה קיצונית: לאמת",
    "premium_needs_justification": "פרמיה דורשת הצדקה (S07-R19)",
    "no_3y_history_sector_multiple": "אין 3 שנות מכפיל: מכפיל הסקטור",
    "hist_multiple_outlier_dropped": "מכפיל חריג הושמט (לא מהקורס)",
    "not_from_course_trimmed_mean": "ממוצע ללא חריג (לא מהקורס)",
    "unreliable_eps_basis": "בסיס EPS לא תואם: יעד לא אמין (לא מהקורס)",
    "extended": "מתוח מעל הממוצע",
    "vol_stop_quarter_multiplier_unverified": "מכפיל עצירת התנודתיות הרבעונית לא אומת (D-S09-5)",
    "levels_pending": "צבע רמות ממתין לשיקול דעת",
    "atr20_over_atr60_wait": "ATR20/ATR60 גבוה: להמתין",
    "permits_phase_recovery_differs": "היתרי הבנייה מצביעים על התאוששות, לא התרחבות",
}
RULE_HE = {"S06-R22": "פרופיל צמיחה/תמחור לא עומד", "S06-R10": "הכנסות מתחת לרצפה", "S06-R9": "מחיר מתחת לרצפה",
           "S06-R6": "שווי שוק מתחת לרצפה", "S06-R8": "שווי שוק לשורט מתחת לרצפה", "no_profile": "לא התאים לאף פרופיל"}
TERM_HE = {"hot": "חם", "extreme_heat": "חם קיצוני", "cold": "קר", "extreme_cold": "קר קיצוני", "grey": "אפור",
           "bullish": "שורית", "bearish": "דובית", "growth+_inflation+": "צמיחה+ אינפלציה+",
           "growth+_inflation-": "צמיחה+ אינפלציה−", "growth-_inflation-": "צמיחה− אינפלציה−",
           "growth-_inflation+": "צמיחה− אינפלציה+", "easing": "הקלה", "tightening": "הידוק",
           "acting_reversing": "פועל/מתהפך", "up": "עולה", "down": "יורדת", "above": "מעל", "below": "מתחת",
           "within": "בתוך", "panic": "פאניקה", "calm": "רגוע", "normal": "רגיל", "inverted": "הפוך", "flat": "שטוח",
           "bull": "שוק שורי", "bear": "שוק דובי", "correction": "תיקון", "expansion": "התרחבות", "recovery": "התאוששות",
           "slowdown": "האטה", "contraction": "התכווצות", "cool": "יקרר", "heat": "יחמם"}


def T(x: Any) -> str:
    return TERM_HE.get(str(x), str(x))


INDICATOR_HE = {"ism_pmi": "ISM תעשייה", "ism_nmi": "ISM שירותים", "michigan": "אמון צרכנים (מישיגן)",
                "permits": "היתרי בנייה", "small_business": "NFIB עסקים קטנים"}


# ---------------------------------------------------------------- formatting

def num(x: Any, d: int = 1, pct: bool = False, sign: bool = False) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    s = f"{x:+,.{d}f}" if sign else f"{x:,.{d}f}"
    return s + ("%" if pct else "")


def money(x: Any) -> str:
    if x is None:
        return "—"
    a = abs(x)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if a >= div:
            return f"{x / div:,.1f}{suf}"
    return f"{x:,.0f}"


def px(x: Any) -> str:
    if x is None:
        return "—"
    return f"{x:,.2f}" if abs(x) < 1000 else f"{x:,.0f}"


def pill(color: str | None, text: str | None = None) -> str:
    c = color or "none"
    return f'<span class="pill pill-{E(c)}">{E(text or COLOR_HE.get(c, c))}</span>'


def bucket_pill(b: str) -> str:
    return f'<span class="bucket bucket-{E(b)}">{E(BUCKET_HE.get(b, b))}</span>'


def flag_chips(flags: Iterable[str]) -> str:
    return "".join(f'<span class="chip" title="{E(f)}">{E(FLAG_HE.get(f, f))}</span>' for f in flags)


def src_link(url: str | None, title: str) -> str:
    if not url:
        return E(title)
    return f'<a href="{E(url)}" target="_blank" rel="noopener">{E(title)}</a>'


# ---------------------------------------------------------------- charts (inline SVG, themed through CSS classes)

def _scale(lo: float, hi: float, a: float, b: float):
    span = (hi - lo) or 1.0
    return lambda v: a + (v - lo) / span * (b - a)


def _nice_ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    span = hi - lo or 1.0
    step = 10 ** math.floor(math.log10(span / n))
    for m in (1, 2, 2.5, 5, 10):
        if span / (step * m) <= n:
            step *= m
            break
    t = math.ceil(lo / step) * step
    out = []
    while t <= hi + 1e-9:
        out.append(round(t, 10))
        t += step
    return out


def line_chart(dates: Sequence[str], series: list[tuple[str, Sequence[float | None], str]], *, w: int = 640, h: int = 220,
               hlines: Sequence[tuple[float, str, str]] = (), zero: bool = False, fmt=lambda v: f"{v:,.1f}",
               label: str = "", log: bool = False) -> str:
    """series: (name, values, css class). hlines: (value, label, css class)."""
    vals = [v for _, s, _ in series for v in s if v is not None] + [v for v, _, _ in hlines]
    if not vals or not dates:
        return ""
    lo, hi = min(vals), max(vals)
    if zero:
        lo, hi = min(lo, 0), max(hi, 0)
    pad = (hi - lo) * 0.06 or 1
    lo, hi = lo - pad, hi + pad
    L, R, T, B = 8, 56, 10, 22
    x = _scale(0, max(len(dates) - 1, 1), L, w - R)
    y = _scale(lo, hi, h - B, T)
    parts = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="{E(label)}" preserveAspectRatio="none">']
    for t in _nice_ticks(lo, hi):
        parts.append(f'<line class="grid" x1="{L}" x2="{w - R}" y1="{y(t):.1f}" y2="{y(t):.1f}"/>'
                     f'<text class="tick" x="{w - R + 4}" y="{y(t) + 3:.1f}">{E(fmt(t))}</text>')
    if zero and lo < 0 < hi:
        parts.append(f'<line class="zero" x1="{L}" x2="{w - R}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>')
    # date ticks: first of each year (or quarter for short spans)
    last_x = -1e9
    seen, step_key = set(), (lambda d: d[:4]) if len(dates) > 400 else (lambda d: d[:7] if d[5:7] in ("01", "04", "07", "10") else None)
    for i, d in enumerate(dates):
        k = step_key(d)
        if k and k not in seen:
            seen.add(k)
            if i > len(dates) * 0.03 and x(i) - last_x >= 56:
                last_x = x(i)
                parts.append(f'<line class="grid" x1="{x(i):.1f}" x2="{x(i):.1f}" y1="{T}" y2="{h - B}"/>'
                             f'<text class="tick" x="{x(i):.1f}" y="{h - 6}" text-anchor="middle">{E(k)}</text>')
    for k, (v, lab, cls) in enumerate(hlines):
        lx, anchor = ((L + 4, "start"), (w - R - 4, "end"), ((L + w - R) / 2, "middle"))[k % 3]
        parts.append(f'<line class="hl {cls}" x1="{L}" x2="{w - R}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
                     f'<text class="hl-label {cls}" x="{lx:.1f}" y="{y(v) - 4:.1f}" text-anchor="{anchor}">{E(lab)}</text>')
    for name, s, cls in series:
        pts, seg = [], []
        for i, v in enumerate(s):
            if v is None:
                if seg:
                    pts.append(seg)
                seg = []
            else:
                seg.append(f"{x(i):.1f},{y(v):.1f}")
        if seg:
            pts.append(seg)
        for sg in pts:
            parts.append(f'<polyline class="ln {cls}" points="{" ".join(sg)}"><title>{E(name)}</title></polyline>')
        last = next((v for v in reversed(s) if v is not None), None)
        if last is not None and cls.endswith("main"):
            parts.append(f'<circle class="dot {cls}" cx="{x(len(s) - 1):.1f}" cy="{y(last):.1f}" r="3"/>')
    parts.append("</svg>")
    return "".join(parts)


def bars_chart(labels: Sequence[str], values: Sequence[float | None], *, w: int = 640, h: int = 180,
               fmt=lambda v: f"{v:,.0f}", classes: Sequence[str] | None = None, label: str = "") -> str:
    vals = [v for v in values if v is not None]
    if not vals:
        return ""
    lo, hi = min(0, min(vals)), max(0, max(vals))
    L, R, T, B = 8, 8, 18, 22
    y = _scale(lo, hi, h - B, T)
    n = len(labels)
    bw = (w - L - R) / n
    parts = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="{E(label)}">',
             f'<line class="zero" x1="{L}" x2="{w - R}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>']
    for i, (lab, v) in enumerate(zip(labels, values)):
        cx = L + bw * i + bw / 2
        parts.append(f'<text class="tick" x="{cx:.1f}" y="{h - 6}" text-anchor="middle">{E(lab)}</text>')
        if v is None:
            continue
        top, bot = sorted((y(v), y(0)))
        cls = (classes[i] if classes else "") or ("neg" if v < 0 else "pos")
        parts.append(f'<rect class="bar {cls}" x="{cx - bw * 0.32:.1f}" y="{top:.1f}" width="{bw * 0.64:.1f}" '
                     f'height="{max(bot - top, 1):.1f}"><title>{E(lab)}: {E(fmt(v))}</title></rect>'
                     f'<text class="val" x="{cx:.1f}" y="{(top - 4) if v >= 0 else (bot + 12):.1f}" '
                     f'text-anchor="middle">{E(fmt(v))}</text>')
    parts.append("</svg>")
    return "".join(parts)


def price_chart(c: dict, plan: dict, levels: dict, direction: str, n: int = 260) -> str:
    dates = c["dates"][-n:]
    close = c["close"][-n:]
    ma = c["ma"]
    hl = [(plan["entry"], f"כניסה {px(plan['entry'])}", "entry"), (plan["stop"], f"עצירה {px(plan['stop'])}", "stop")]
    if plan.get("target") and abs(plan["target"] / plan["entry"] - 1) < 0.9:
        hl.append((plan["target"], f"יעד {px(plan['target'])}", "target"))
    for k, lab in (("nearest_resistance", "התנגדות"), ("nearest_support", "תמיכה")):
        v = levels.get(k)
        if v and abs(v / plan["entry"] - 1) > 0.004:
            hl.append((v, f"{lab} {px(v)}", "level"))
    return line_chart(dates, [("ממוצע 252", ma["252"][-n:], "ma252"), ("ממוצע 63", ma["63"][-n:], "ma63"),
                              ("ממוצע 21", ma["21"][-n:], "ma21"), ("סגירה", close, "main")],
                      hlines=hl, fmt=px, label="מחיר שנה אחרונה", h=260)


def funnel_svg(funnel: list[dict]) -> str:
    w, row = 640, 46
    h = row * len(funnel) + 8
    mx = max(f["n"] for f in funnel) or 1
    parts = [f'<svg viewBox="0 0 {w} {h}" class="chart funnel" role="img" aria-label="משפך">']
    for i, f in enumerate(funnel):
        frac = max(math.log10(f["n"] + 1) / math.log10(mx + 1), 0.04)
        bw = (w - 20) * frac
        x0 = (w - bw) / 2
        y0 = 4 + i * row
        parts.append(f'<rect class="fbar f{i}" x="{x0:.1f}" y="{y0}" width="{bw:.1f}" height="{row - 10}" rx="3"/>'
                     f'<text class="fnum" x="{w / 2}" y="{y0 + row / 2:.1f}" text-anchor="middle">{f["n"]:,}</text>')
    parts.append("</svg>")
    return "".join(parts)


def light_grid(sources: dict) -> str:
    return '<div class="lights">' + "".join(
        f'<div class="light"><span class="lamp lamp-{E(v)}"></span><span>{E(SOURCE_HE.get(k, k))}</span></div>'
        for k, v in sources.items()) + "</div>"


def rr_bar(entry: float, stop: float, target: float | None, direction: str) -> str:
    """Risk vs reward drawn to one scale: the stop distance on one side, the target on the other."""
    if not target:
        return ""
    risk = abs(entry - stop)
    reward = abs(target - entry)
    tot = risk + reward
    rp = risk / tot * 100
    return (f'<div class="rrbar" aria-label="סיכון מול סיכוי"><div class="rr-risk" style="width:{rp:.1f}%">'
            f'<span>סיכון {num(risk / entry * 100, 1, True)}</span></div><div class="rr-reward" style="width:{100 - rp:.1f}%">'
            f'<span>סיכוי {num(reward / entry * 100, 1, True)}</span></div></div>')


# ---------------------------------------------------------------- sections

def _summary(d: dict) -> str:
    ideas = d["ideas"]
    order = {"enter": 0, "conditional": 1, "watch": 2, "reject": 3}
    rows = sorted(ideas.values(), key=lambda i: (order.get(i["bucket"], 9), -(i["plan"].get("rr") or 0)))
    st = d["macro"]["stage2"]["stance"]
    J = d["judgments"].get("A1", {})
    trades = [i for i in rows if i["bucket"] in ("enter", "conditional")]
    out = ['<section id="summary"><h2>השורה התחתונה</h2>']
    out.append('<div class="lede">')
    n_enter = sum(i["bucket"] == "enter" for i in rows)
    n_cond = sum(i["bucket"] == "conditional" for i in rows)
    out.append(f'<p class="big">מתוך {d["funnel"][0]["n"]:,} מניות, <b>{n_enter}</b> עסקה מוכנה לכניסה '
               f'ו-<b>{n_cond}</b> ממתינות לטריגר. כל השאר במעקב או נדחו, וכל אחת מהן מנומקת למטה.</p>')
    out.append(f'<p>מקרו: אזור <b>{E(T(st["zone"]))}</b>, הטיה <b>{E(T(st["bias"] or "grey"))}</b>, מצפן '
               f'<b>{E(T(st["compass_now"] or "—"))}</b>. {E(J.get("DC-03", {}).get("why", ""))}</p>')
    out.append("</div>")
    for i in trades:
        p, j = i["plan"], i["judgment"]
        trig = j.get("trigger", {})
        out.append(f'''<a class="trade-card" href="#idea-{E(i["sym"])}">
  <div class="tc-head">{bucket_pill(i["bucket"])}<span class="tc-sym">{E(i["sym"])}</span>
    <span class="tc-name">{E(i["profile"]["name"])}</span>{pill(i["timing"]["combined"])}</div>
  <div class="tc-nums">
    <div><label>{"כניסה" if i["bucket"] == "enter" else "טריגר"}</label><b>{px(trig.get("level") or p["entry"])}</b></div>
    <div><label>עצירה</label><b>{px(p["stop"])}</b><small>{num(-p["stop_pct"], 1, True)}</small></div>
    <div><label>יעד</label><b>{px(p.get("target"))}</b><small>{num(p.get("move_pct"), 1, True, True)}</small></div>
    <div><label>R:R</label><b>{num(p.get("rr"), 1)}</b></div>
    <div><label>אופק</label><b>{num(p.get("horizon_months"), 1)}</b><small>חודשים</small></div>
    <div><label>גודל ראשון</label><b>{num(p["sizing"]["allowed_pct"], 0, True)}</b><small>{p["sizing"]["quantity"]:,} יח'</small></div>
  </div>
  {rr_bar(p["entry"], p["stop"], p.get("target"), i["direction"])}
  <p class="tc-why">{E(trig.get("text", ""))}</p>
</a>''')
    out.append('<h3>כל הרעיונות שנבדקו לעומק</h3><div class="tbl"><table><thead><tr><th>מניה</th><th>סטטוס</th>'
               '<th>רמזור</th><th>מחיר</th><th>עצירה</th><th>יעד</th><th>מקור יעד</th><th>R:R</th><th>אופק</th>'
               '<th>מנוע הכללים</th><th>למה</th></tr></thead><tbody>')
    for i in rows:
        p = i["plan"]
        why = i["judgment"].get("why") or i.get("bucket_reason") or ""
        out.append(f'<tr><td><a href="#idea-{E(i["sym"])}"><b>{E(i["sym"])}</b></a> <small>{E(DIR_HE[i["direction"]])} · {E(i["etf"])}</small></td>'
                   f'<td>{bucket_pill(i["bucket"])}</td><td>{pill(i["timing"]["combined"])}</td>'
                   f'<td class="n">{px(p["entry"])}</td><td class="n">{px(p["stop"])}</td><td class="n">{px(p.get("target"))}</td>'
                   f'<td>{E(TARGET_HE.get(p.get("target_source") or "", "—"))}</td><td class="n">{num(p.get("rr"), 1)}</td>'
                   f'<td class="n">{num(p.get("horizon_months"), 1)}</td><td><code>{E(p["verdict"]["status"])}</code></td>'
                   f'<td class="why">{E(why[:220])}{"…" if len(why) > 220 else ""}</td></tr>')
    out.append("</tbody></table></div>")
    out.append(f'<p class="note">גודל פוזיציה מחושב על תיק רעיוני של {d["meta"]["equity"]:,.0f}$. כניסה ראשונה 6% '
               f'(S11-R5), עצירה = k × ADR60 מהכניסה (S11-R11), ויחס סיכוי-סיכון מינימלי 3 (S10-R3). '
               f'שום פקודה לא נשלחת. כל עסקה דורשת את אישורך.</p></section>')
    return "".join(out)


def _funnel(d: dict) -> str:
    rows = []
    for f in d["funnel"]:
        split = f.get("split") or {}
        sp = " · ".join(f"{E(COLOR_HE.get(k, BUCKET_HE.get(k, k)))} {v}" for k, v in split.items())
        rows.append(f'<li><b>{f["n"]:,}</b> {E(f["label"])}{f" <small>({sp})</small>" if sp else ""}</li>')
    return (f'<section id="funnel"><h2>המשפך</h2><div class="split"><div>{funnel_svg(d["funnel"])}</div>'
            f'<ol class="funnel-list">{"".join(rows)}</ol></div></section>')


def _macro(d: dict) -> str:
    m = d["macro"]
    s1, s2, s3 = m["stage1"], m["stage2"], m["stage3"]
    st = s2["stance"]
    J = d["judgments"].get("A1", {})
    ch = m["charts"]
    cr, cv, rr, com = s1["credit"], s1["curve"], s1["real_rate"], s1.get("commodities") or {}
    tiles = [
        ("אזור פעילות", T(st["zone"]), f'{st["activity"]["total"]}/{st["activity"]["max"]} ({num(st["activity"]["pct"], 0, True)})'),
        ("הטיה", T(st["bias"] or "grey"), "S02"),
        ("מצפן עכשיו", T(st["compass_now"]), f'בעוד חצי שנה: {T(J.get("DC-02", {}).get("in_six_months", "—"))}'),
        ("פד", f'{st["fed"]["points"]}/{st["fed"]["max"]}', f'{T(st["fed"]["period"])} · {T(st["fed"]["state"])}'),
        ("אינפלציה CPI", num(st["inflation"]["cpi_yoy"], 2, True), f'{T(st["inflation"]["direction_3m"])} · {T(st["inflation"]["vs_range"])} לטווח'),
        ("אבטלה", num(st["labor"]["rate"], 1, True), f'ממוצע ארוך {num(st["labor"]["lt_avg"], 1)}'),
        ("מרווח HY", num(cr["value"], 2, True), f'{T(cr["state"])} · MA250 {num(cr["ma250"], 2)}'),
        ("עקום 10Y−3M", num(cv["value"], 2, sign=True), T(cv["shape"])),
        ("ריבית ריאלית", num(rr["value"], 2, True), f'10Y {num(rr["nominal_10y"], 2)}'),
        ("סל סחורות", num(com.get("basket_yoy"), 1, True, True), "WTI " + num((com.get("yoy") or {}).get("wti"), 0, True, True)),
        ("S&P 500", f'{s3["spx_last"]:,.0f}', f'{T(s3["market"]["regime"])} · {num(-s3["market"]["drawdown_pct"], 1, True)} מהשיא'),
        ("רוחב", num(s3["breadth"]["pct_above_ma200"], 1, True), "מעל ממוצע 200"),
    ]
    out = ['<section id="macro"><h2>מקרו: שלבים 1–3</h2><div class="tiles">']
    for lab, val, sub in tiles:
        out.append(f'<div class="tile"><label>{E(lab)}</label><b>{E(str(val))}</b><small>{E(str(sub))}</small></div>')
    out.append("</div>")
    # judgment
    out.append('<div class="judgment"><div class="j-tag">שיקול דעת · A1</div>')
    for card, title in (("DC-01", "אזור"), ("DC-02", "מצפן"), ("DC-03", "השוק מול התחזית"), ("DC-04", "רמת סיכון")):
        c = J.get(card)
        if c:
            out.append(f'<p><b>{title} ({card}): {E(T(c.get("choice")))}</b>. {E(c.get("why", ""))}</p>')
    for n in J.get("notes", []):
        out.append(f'<p class="muted">• {E(n)}</p>')
    out.append("</div>")
    # activity scale
    out.append('<h3>סולם הפעילות (S02): חמישה מדדים, 10 נקודות כל אחד</h3><div class="tbl"><table><thead><tr><th>מדד</th>'
               '<th>עכשיו</th><th>לפני 3ח׳</th><th>לפני 6ח׳</th><th>לפני 12ח׳</th><th>ייחוס</th><th>מעל ייחוס</th>'
               '<th>שנה</th><th>חצי שנה</th><th>רבעון</th><th>ציון</th></tr></thead><tbody>')
    ck = lambda b: '<span class="ok">✓</span>' if b else '<span class="no">✗</span>'
    for s in s2["scores"]:
        out.append(f'<tr><td>{E(INDICATOR_HE.get(s["name"], s["name"]))}</td><td class="n">{num(s["now"])}</td>'
                   f'<td class="n">{num(s["m3"])}</td><td class="n">{num(s["m6"])}</td><td class="n">{num(s["m12"])}</td>'
                   f'<td class="n">{num(s["ref"])}</td><td>{ck(s["level_ok"])}</td><td>{ck(s["yoy_pos"])}</td>'
                   f'<td>{ck(s["half_pos"])}</td><td>{ck(s["quarter_pos"])}</td>'
                   f'<td><div class="score"><span style="width:{s["score"] * 10}%"></span><b>{s["score"]}</b></div></td></tr>')
    out.append("</tbody></table></div>")
    for k, v in s2.get("notes", {}).items():
        out.append(f'<p class="note">{E(INDICATOR_HE.get(k, k))}: הייחוס חושב על פחות חודשים מהנדרש ({E(v)}). לא מהקורס: הסדרה הידנית קצרה.</p>')
    # charts
    grid = [
        ("ISM תעשייה (PMI)", "ism_pmi", [(50, "50", "ref")], lambda v: f"{v:.0f}"),
        ("ISM שירותים (NMI)", "ism_nmi", [(50, "50", "ref")], lambda v: f"{v:.0f}"),
        ("NFIB עסקים קטנים", "small_business", [(98, "98", "ref")], lambda v: f"{v:.0f}"),
        ("אמון צרכנים מישיגן", "michigan", [], lambda v: f"{v:.0f}"),
        ("היתרי בנייה (אלפים)", "permits", [], lambda v: f"{v:,.0f}"),
        ("אינפלציה CPI שנתית %", "cpi_yoy", [(2, "יעד 2%", "ref")], lambda v: f"{v:.1f}"),
        ("ריבית הפד %", "ffr", [], lambda v: f"{v:.2f}"),
        ("אבטלה %", "unemployment", [], lambda v: f"{v:.1f}"),
        ("מרווח HY % (שלוש שנים)", "hy", [(cr["ma250"], "MA250", "ref")], lambda v: f"{v:.1f}"),
        ("עקום 10Y−3M (שלוש שנים)", "curve", [], lambda v: f"{v:.1f}"),
        ("S&P 500 (שנתיים)", "spx", [(s3["market"]["peak"], "שיא", "ref"), (s3["market"]["bear_level"], "דובי −20%", "stop")],
         lambda v: f"{v:,.0f}"),
    ]
    out.append('<h3>הסדרות</h3><div class="charts">')
    for title, key, hl, fmt in grid:
        c = ch.get(key)
        if not c:
            continue
        out.append(f'<figure><figcaption>{E(title)}</figcaption>'
                   f'{line_chart(c["dates"], [(title, c["values"], "main")], hlines=hl, zero=key == "curve", fmt=fmt, label=title, w=380, h=190)}'
                   f'</figure>')
    out.append("</div>")
    # sources
    out.append('<details class="sources"><summary>מקורות המקרו</summary><ul>')
    for k, v in m["sources"].items():
        sid = v.split(":")[1].split("@")[0] if ":" in v else v
        out.append(f'<li>{E(k)}: {src_link(f"https://fred.stlouisfed.org/series/{sid}", v)} (גרסת ALFRED נכון ל-{E(d["meta"]["as_of"])})</li>')
    for k, lst in m["manual_sources"].items():
        for s in lst:
            q = f' · “{E(s["quote"])}”' if s.get("quote") else ""
            nt = f' · {E(s["note"])}' if s.get("note") else ""
            out.append(f'<li>{E(INDICATOR_HE.get(k, k))} {E(str(s.get("months", "")))}: '
                       f'{src_link(s.get("url"), s.get("url") or "הערה")}{nt}{q}</li>')
    out.append("</ul></details></section>")
    return "".join(out)


def _sectors(d: dict) -> str:
    s = d["sectors"]
    J = d["judgments"].get("A2", {})
    card = sorted(s["scorecard"].values(), key=lambda v: -v["total"])
    out = [f'<section id="sectors"><h2>סקטורים: שלבים 4–5</h2><p>שלב המחזור: <b>{E(T(s["phase"]))}</b> (מ-ISM)'
           f'{" · " + flag_chips(s["phase_flags"]) if s["phase_flags"] else ""} · אזור {E(T(s["zone"]))}. '
           f'הציון = דירוג טכני (RS) + דירוג פונדמנטלי (צמיחה צפויה) + נקודות מקרו לפי השלב.</p>']
    out.append('<div class="tbl"><table><thead><tr><th>סקטור</th><th>סוג</th><th>RS שבוע</th><th>חודש</th><th>3ח׳</th>'
               '<th>6ח׳</th><th>שנה</th><th>טכני</th><th>צמיחת רווח</th><th>פונד׳</th><th>מקרו</th><th>סה״כ</th><th>החלטה</th>'
               '</tr></thead><tbody>')
    for v in card:
        rs = v["rs"]
        cells = "".join(f'<td class="n heat" style="--h:{max(-1, min(1, (rs[k] or 0) / 15)):.2f}">{num(rs[k], 1, sign=True)}</td>'
                        for k in ("1w", "1m", "3m", "6m", "1y"))
        b = {"long": "enter", "short": "reject", "neutral": "watch"}[v["bucket"]]
        out.append(f'<tr><td><b>{E(v["etf"])}</b></td><td>{E(v["type"])}</td>{cells}<td class="n">{v["tech_rank"]}</td>'
                   f'<td class="n">{num(v["growth"]["eg1"], 0, True)}</td><td class="n">{v["fund_rank"]}</td>'
                   f'<td class="n">{v["macro"]}</td><td class="n"><b>{v["total"]}</b></td>'
                   f'<td><span class="bucket bucket-{b}">{ {"long": "לונג", "short": "שורט", "neutral": "ניטרלי"}[v["bucket"]] }</span></td></tr>')
    out.append("</tbody></table></div>")
    out.append('<h3>חוזק יחסי מול SPY, שנה אחרונה</h3><div class="charts small">')
    for e in s["long"] + s["short"]:
        c = s["charts"][e]
        base = c["rs"][0] or 1
        vals = [v / base * 100 for v in c["rs"]]
        out.append(f'<figure><figcaption>{E(e)} <small>({"לונג" if e in s["long"] else "שורט"})</small></figcaption>'
                   f'{line_chart(c["dates"], [(e, vals, "main")], hlines=[(100, "100", "ref")], fmt=lambda v: f"{v:.0f}", w=380, h=170, label=e)}</figure>')
    out.append("</div>")
    pr = s["pairs"]
    out.append('<h3>זוגות מחזור (S04-R32)</h3><ul class="pairs">')
    for k, v in pr["trends"].items():
        st = "agree" if k in pr.get("agree", []) else "conflict"
        out.append(f'<li class="{st}"><b>{E(k)}</b> {E(T(v))} · {"מסכים" if st == "agree" else "סותר"} את השלב</li>')
    out.append("</ul>")
    if J:
        out.append('<div class="judgment"><div class="j-tag">שיקול דעת · A2</div>')
        for k, v in (J.get("notes") or {}).items():
            out.append(f'<p><b>{E(k)}</b>: {E(v)}</p>')
        if J.get("pairs"):
            out.append(f'<p><b>זוגות</b>: {E(J["pairs"])}</p>')
        out.append("</div>")
    out.append("</section>")
    return "".join(out)


def _screen(d: dict) -> str:
    out = ['<section id="screen"><h2>סינון ורמזור: שלבים 6 ו-9</h2><div class="tbl"><table><thead><tr><th>סקטור</th>'
           '<th>מניות</th><th>עברו</th><th>נפסלו</th><th>סיבות עיקריות לפסילה</th><th>ירוק</th><th>כתום</th><th>אדום</th>'
           '</tr></thead><tbody>']
    tm = d["timing"]
    for e, s in d["screens"].items():
        passed = len(s["long"]) + len(s["short"])
        reasons = ", ".join(f"{RULE_HE.get(k, k)} {v}" for k, v in list(s["rejected_by_rule"].items())[:3])
        cnt = {c: sum(1 for t in tm if t["etf"] == e and t.get("combined") == c) for c in ("green", "orange", "red")}
        out.append(f'<tr><td><b>{E(e)}</b></td><td class="n">{s["n"]}</td><td class="n">{passed}</td>'
                   f'<td class="n">{s["rejected_count"]}</td><td>{E(reasons)}</td>'
                   + "".join(f'<td class="n"><span class="dotc dot-{c}"></span>{cnt[c]}</td>' for c in cnt) + "</tr>")
    out.append("</tbody></table></div>")
    good = [t for t in tm if t.get("combined") == "green" or (t.get("combined") == "orange" and (t.get("rr_extreme") or 0) >= 3)]
    out.append('<h3>מי שעבר את הרמזור: כל הירוקים, והכתומים עם יחס של 3 ומעלה לשיא</h3><div class="tbl"><table><thead><tr>'
               '<th>מניה</th><th>סקטור</th><th>פרופיל</th><th>מניה</th><th>סקטור</th><th>שוק</th><th>משולב</th><th>מקורות אדומים</th>'
               '<th>R:R לשיא</th><th>ADR</th></tr></thead><tbody>')
    for t in sorted(good, key=lambda t: (t["combined"] != "green", -(t.get("rr_extreme") or 0))):
        reds = [SOURCE_HE.get(k, k) for k, v in t["sources"].items() if v == "red"]
        link = f'<a href="#idea-{E(t["sym"])}">{E(t["sym"])}</a>' if t["sym"] in d["ideas"] else E(t["sym"])
        out.append(f'<tr><td><b>{link}</b></td><td>{E(t["etf"])}</td><td>{E(t["profile"])}</td><td>{pill(t["stock"])}</td>'
                   f'<td>{pill(t["sector"])}</td><td>{pill(t["market"])}</td><td>{pill(t["combined"])}</td>'
                   f'<td>{E(", ".join(reds) or "—")}</td><td class="n">{num(t.get("rr_extreme"), 1)}</td>'
                   f'<td class="n">{num(t["adr"], 1, True)}</td></tr>')
    out.append('</tbody></table></div><p class="note">R:R לשיא מחושב עם k=4 מול השיא של 8 שנים. זה סינון ראשוני בלבד: '
               'היעד הסופי נבחר בניתוח העמוק מבין שלושת מקורות הקורס.</p></section>')
    return "".join(out)


def _idea(i: dict) -> str:
    j, p, f, v, c, tl = i["judgment"], i["plan"], i["fundamentals"], i["valuation"], i["catalysts"], i["timing"]
    full = i["bucket"] in ("enter", "conditional") or j.get("thesis")
    story_chip = f'<span class="chip">{E(STORY_HE.get(j["story_type"], j["story_type"]))}</span>' if j.get("story_type") else ""
    out = [f'<article class="idea idea-{E(i["bucket"])}" id="idea-{E(i["sym"])}">']
    out.append(f'<header><div class="i-title"><h3>{E(i["sym"])} <span>{E(i["profile"]["name"])}</span></h3>'
               f'<p>{E(i["profile"].get("industry") or "")} · {E(i["profile"].get("country") or "")} · שווי {money(i["row"]["market_cap"])}$ · '
               f'{E(DIR_HE[i["direction"]])} · {E(i["etf"])} · פרופיל {E(i["row"]["profile"])}</p></div>'
               f'<div class="i-badges">{bucket_pill(i["bucket"])}{pill(tl["combined"], "רמזור " + COLOR_HE.get(tl["combined"], ""))}'
               f'{story_chip}</div></header>')
    if j.get("why") and not j.get("thesis"):
        out.append(f'<div class="verdict-box"><b>{"למה לא עכשיו" if i["bucket"] != "reject" else "למה נדחה"}:</b> {E(j["why"])}</div>')
    if j.get("story"):
        out.append(f'<div class="judgment"><div class="j-tag">סיפור ותזה · A4</div><p>{E(j["story"])}</p>'
                   + (f'<p><b>התזה.</b> {E(j["thesis"])}</p>' if j.get("thesis") else ""))
        if j.get("growth_engines"):
            out.append("<p><b>מנועי צמיחה</b></p><ul>" + "".join(f"<li>{E(g)}</li>" for g in j["growth_engines"]) + "</ul>")
        if j.get("premium"):
            out.append(f'<p><b>פרמיה (DC-11): {E(j["premium"]["choice"])}</b>. {E(j["premium"]["why"])}</p>')
        out.append("</div>")
    # chart + plan
    out.append(f'<div class="split"><figure class="pchart"><figcaption>מחיר, שנה אחרונה: סגירה וממוצעים 21/63/252'
               f'</figcaption>{price_chart(i["chart"], p, i["levels"], i["direction"])}</figure><div class="plan">')
    out.append(f'<h4>תכנון עסקה (S10–S11)</h4><dl>'
               f'<dt>מחיר אחרון</dt><dd>{px(p["entry"])}</dd>'
               f'<dt>ADR60</dt><dd>{num(p["adr_pct"], 2, True)}</dd>'
               f'<dt>עצירה (k={num(p["k"], 0)})</dt><dd>{px(p["stop"])} <small>({num(-p["stop_pct"], 1, True)})</small></dd>'
               f'<dt>יעד</dt><dd>{px(p.get("target"))} <small>{E(TARGET_HE.get(p.get("target_source") or "", ""))} · {num(p.get("move_pct"), 1, True, True)}</small></dd>'
               f'<dt>R:R</dt><dd><b>{num(p.get("rr"), 2)}</b> <small>(מינימום 3)</small></dd>'
               f'<dt>אופק</dt><dd>{num(p.get("horizon_months"), 1)} חודשים <small>(תזוזה ÷ סטיית תקן חודשית {num(p["monthly_sigma_pct"], 1, True)})</small></dd>'
               f'<dt>גודל ראשון</dt><dd>{num(p["sizing"]["allowed_pct"], 0, True)} = {p["sizing"]["quantity"]:,} יח׳ · סיכון {num(p["sizing"]["risk_pct_of_equity"], 2, True)} מהתיק</dd>'
               f'</dl>{rr_bar(p["entry"], p["stop"], p.get("target"), i["direction"])}')
    if j.get("trigger"):
        t = j["trigger"]
        out.append(f'<div class="trigger"><b>טריגר (DC-17): {E(t.get("choice", ""))}</b> {E(t.get("text", ""))}</div>')
    if j.get("structure"):
        out.append(f'<p class="small"><b>מבנה (DC-18): {E(STRUCT_HE.get(j["structure"]["choice"], j["structure"]["choice"]))}</b>. {E(j["structure"]["why"])}</p>')
    vs = p["verdict"]
    out.append(f'<div class="rules rules-{E(vs["status"])}"><b>מנוע הכללים: {E(vs["status"])}</b><ul>'
               + "".join(f'<li><code>{E(x["rule_id"])}</code> {E(x["severity"])}: {E(x["message"])}</li>' for x in vs["findings"])
               + ("" if vs["findings"] else "<li>אין ממצאים</li>") + "</ul></div></div></div>")
    # targets
    if i["targets"]:
        out.append('<h4>שלושת מקורות היעד (S10-R3)</h4><div class="tbl"><table><thead><tr><th>מקור</th><th>מחיר</th><th>תזוזה</th>'
                   '<th>R:R</th><th>פירוט</th></tr></thead><tbody>')
        for t in i["targets"]:
            chosen = t["source"] == p.get("target_source") and abs(t["price"] - (p.get("target") or 0)) < 1e-6
            out.append(f'<tr class="{"chosen" if chosen else ""}"><td>{E(TARGET_HE.get(t["source"], t["source"]))}{" ✓" if chosen else ""}</td>'
                       f'<td class="n">{px(t["price"])}</td><td class="n">{num(t["move_pct"], 1, True, True)}</td>'
                       f'<td class="n">{num(t["rr"], 1)}</td><td>{E(t["note"])}{" · לא אמין" if t.get("unreliable") else ""}</td></tr>')
        out.append("</tbody></table></div>")
    # traffic light
    st = tl["stock"]
    det = st["details"]
    out.append(f'<h4>רמזור (S09): מניה {pill(st["overall"])} · סקטור {pill(tl["sector"]["overall"])} · שוק '
               f'{pill(tl["market"]["overall"])} → {pill(tl["combined"])}</h4>{light_grid(st["sources"])}'
               f'<p class="small">RSI {num(det.get("rsi"), 1)} · MACD היסטוגרמה {num(det["macd"]["hist"], 2, sign=True)} · '
               f'ממוצעים {", ".join(px(x) for x in det["sma"]["values"])} · ימים מעל ממוצע 21 ללא נגיעה: {det["sma"]["extended_days"]}</p>')
    if j.get("levels_note"):
        out.append(f'<p class="small"><b>רמות (A6): {E(COLOR_HE.get(j.get("levels_color"), ""))}</b>. {E(j["levels_note"])}</p>')
    if st["flags"]:
        out.append(f'<div class="chips">{flag_chips(st["flags"])}</div>')
    # fundamentals
    ann = f.get("annual", {})
    yrs = [x[0][:4] for x in ann.get("TotalRevenue", [])]
    if yrs:
        out.append('<h4>דוחות (S07)</h4><div class="split three">')
        for key, title, scale in (("TotalRevenue", "הכנסות", 1e6), ("OperatingIncome", "רווח תפעולי", 1e6), ("DilutedEPS", "EPS מדולל", 1)):
            s = dict((x[0][:4], x[1]) for x in ann.get(key, []))
            vals = [(s.get(y) / scale) if s.get(y) is not None else None for y in yrs]
            out.append(f'<figure><figcaption>{title}{" (מיליוני $)" if scale > 1 else ""}</figcaption>'
                       f'{bars_chart(yrs, vals, w=300, h=150, fmt=(lambda v: f"{v:,.0f}") if scale > 1 else (lambda v: f"{v:.2f}"), label=title)}</figure>')
        out.append("</div>")
    sp = f.get("surprises") or {}
    rows = [
        ("מגמת ליבה", ", ".join(f"{k}: {v}" for k, v in (f.get("core_trend") or {}).items())),
        ("מינוף תפעולי", (f"{f['operating_leverage']['direction']} · הכנסות {num(f['operating_leverage']['revenue_growth'], 1, True, True)} מול "
                          f"עלות מכר {num(f['operating_leverage']['cogs_growth'], 1, True, True)} ({'TTM' if f.get('ol_basis') == 'ttm' else 'שנתי'})")
         if f.get("operating_leverage") else "—"),
        ("מקור הרווח", f.get("profit_source") or "—"),
        ("צמיחת הכנסות ברבעון (שנתי)", ", ".join(num(x, 1, True, True) for x in f.get("revenue_yoy_last2q") or []) or "—"),
        ("CAGR הכנסות", num(f.get("revenue_cagr"), 1, True, True)),
        ("פער צמיחה מול הסקטור", f.get("gap_vs_sector") or "—"),
        ("בריאות (S07-R7)", f"{f['health']['state']} {f['health']['note']}" if f.get("health") else "—"),
        ("תזרים חופשי TTM", money(f.get("fcf_ttm"))),
        ("שינוי מניות בשנה", num(f.get("shares_change_pct"), 1, True, True)),
        ("חוב נטו/EBITDA (נוסחת הקורס)", " → ".join(num(x, 1) for x in f.get("net_debt_ebitda") or []) + f" ({f.get('net_debt_trend') or '—'})"),
        ("הפתעות ב-4 רבעונים", f"הכאות {num((sp.get('beat_share') or 0) * 100, 0, True)} · ממוצע {num(sp.get('avg_pct'), 1, True, True)} · {sp.get('trend') or '—'}" if sp else "—"),
        ("מכפיל צפוי מול הסקטור", f"{num(i['row'].get('pe1'), 1)} מול {num(i['sector_avg'].get('pe'), 1)} · פרמיה {num((v.get('premium') or {}).get('pct'), 0, True, True)}"),
        ("מכפילים היסטוריים", ", ".join(num(x, 1) for x in v.get("hist_multiples") or []) or "—"),
        ("צמיחת רווח צפויה", f"השנה {num(i['row'].get('eg1'), 0, True, True)} · שנה הבאה {num(i['row'].get('eg2'), 0, True, True)} "
                             f"(F0 {num(i['row'].get('f0'), 2)} → F1 {num(i['row'].get('f1'), 2)} → F2 {num(i['row'].get('f2'), 2)})"),
    ]
    out.append('<div class="kv">' + "".join(f"<div><label>{E(a)}</label><span>{E(str(b))}</span></div>" for a, b in rows) + "</div>")
    allflags = list(f.get("flags", [])) + list(v.get("flags", [])) + list((v.get("target") or {}).get("flags", []))
    if allflags:
        out.append(f'<div class="chips">{flag_chips(dict.fromkeys(allflags))}</div>')
    if v.get("eps_basis_mismatch"):
        out.append(f'<p class="note">EPS בדוחות מול תחזית האנליסטים לאותה שנה: יחס {num(v.get("eps_basis_ratio"), 2)}. '
                   f'פער כזה מעיד על בסיס שונה (ADR או מטבע, GAAP מול מתואם), ולכן יעד המכפיל לא נבחר אוטומטית. (לא מהקורס)</p>')
    # catalysts
    if full:
        out.append('<h4>קטליזטורים ואנליסטים (S08)</h4>')
        if j.get("catalysts"):
            out.append('<ul class="timeline">' + "".join(
                f'<li><time>{E(x["date"])}</time><span class="chip">{E(CAT_HE.get(x["class"], x["class"]))}</span> {E(x["what"])}'
                f'{" · כבר מתומחר" if x.get("priced_in") else ""}</li>' for x in j["catalysts"]) + "</ul>")
        rv = c["revisions"]
        out.append(f'<p class="small">דוח הבא: <b>{E(c.get("next_earnings") or "—")}</b> · רוויזיות 30 יום F1: '
                   f'{rv["F1"]["up30"] or 0}↑ {rv["F1"]["down30"] or 0}↓, F2: {rv["F2"]["up30"] or 0}↑ {rv["F2"]["down30"] or 0}↓ · '
                   f'F1 לפני 90 יום {num(rv["F1"]["eps_90d"], 2)} → עכשיו {num(rv["F1"]["eps_now"], 2)} · '
                   f'יעד ממוצע {px(c.get("mean_target"))} ({c.get("n_analysts") or 0} אנליסטים, {E(c.get("recommendation") or "—")})</p>')
        if c["analyst_actions"]:
            out.append('<div class="tbl"><table class="compact"><thead><tr><th>תאריך</th><th>בית</th><th>פעולה</th><th>המלצה</th>'
                       '<th>יעד</th></tr></thead><tbody>' + "".join(
                           f'<tr><td>{E(a["date"])}</td><td>{E(a["firm"] or "")}</td><td>{E(a["action"] or "")}</td>'
                           f'<td>{E(a["to"] or "")}</td><td class="n">{px(a["prior_target"]) if a.get("prior_target") else "—"} → {px(a["target"])}</td></tr>'
                           for a in c["analyst_actions"][:8]) + "</tbody></table></div>")
        if c["news"]:
            out.append('<p class="small"><b>כותרות אחרונות</b></p><ul class="news">' + "".join(
                f'<li><time>{E(n["time"])}</time> {src_link(n.get("link"), n["title"])} <small>{E(n.get("publisher") or "")}</small></li>'
                for n in c["news"][:6]) + "</ul>")
    if j.get("breakers"):
        out.append('<h4>שוברי תזה (DC-12) ומה לבדוק</h4><ul>' + "".join(
            f'<li><b>{E(b["text"])}</b> <small>({E(b["kind"])})</small>: {E(b["check"])}</li>' for b in j["breakers"]) + "</ul>")
    if j.get("kpis"):
        out.append(f'<p class="small"><b>מדדים למעקב:</b> {E(" · ".join(j["kpis"]))}</p>')
    if i["options"] and full:
        out.append('<h4>מועמדי אופציות (קוד)</h4><div class="tbl"><table class="compact"><thead><tr><th>מבנה</th><th>פקיעה</th>'
                   '<th>סטרייק</th><th>עלות לחוזה</th><th>יחס ליעד</th><th>IV</th></tr></thead><tbody>')
        for o in i["options"]:
            if "error" in o:
                continue
            k = px(o.get("strike")) if o.get("strike") else f'{px(o.get("long_strike"))}/{px(o.get("short_strike"))}'
            out.append(f'<tr><td>{E(o["structure"])}</td><td>{E(o["expiry"])}</td><td>{k}</td>'
                       f'<td class="n">{num(o["cost_per_contract"], 0)}$</td><td class="n">{num(o.get("ratio_to_target"), 1)}</td>'
                       f'<td class="n">{num((o.get("iv") or 0) * 100, 0, True)}</td></tr>')
        out.append("</tbody></table></div>")
    if j.get("sources"):
        out.append('<p class="small"><b>מקורות:</b> ' + " · ".join(src_link(s["url"], s["title"]) for s in j["sources"]) + "</p>")
    if full and i["profile"].get("summary"):
        out.append(f'<details><summary>על החברה (Yahoo)</summary><p class="small" dir="ltr">{E(i["profile"]["summary"])}</p></details>')
    out.append("</article>")
    return "".join(out)


def _ideas(d: dict) -> str:
    order = {"enter": 0, "conditional": 1, "watch": 2, "reject": 3}
    ideas = sorted(d["ideas"].values(), key=lambda i: (order.get(i["bucket"], 9), -(i["plan"].get("rr") or 0)))
    out = ['<section id="ideas"><h2>רעיונות: שלבים 7–11</h2>']
    cur = None
    for i in ideas:
        if i["bucket"] != cur:
            cur = i["bucket"]
            out.append(f'<h3 class="group">{E(BUCKET_HE[cur])}</h3>')
        out.append(_idea(i))
    out.append("</section>")
    return "".join(out)


def _method(d: dict) -> str:
    m = d["meta"]
    return f'''<section id="method"><h2>שיטה, הנחות ומגבלות</h2>
<ul>
<li><b>מי כתב מה.</b> כל מספר בדוח מחושב בקוד (qt.*) לפי הפלייבוק, גרסה {E(m["playbook_version"])}. השדות שמסומנים "שיקול דעת" (אזור, מצפן, סיפור, תזה, קטליזטורים, צבע רמות, טריגר, k ומבנה) נכתבו על ידי {E(m["judged_by"])}.</li>
<li><b>נתונים.</b> מחירים, תחזיות אנליסטים, דוחות, אופציות וחדשות מ-Yahoo Finance. מקרו מ-FRED עם גרסאות ALFRED נכון ל-{E(m["as_of"])}, כך שאין מבט לעתיד. ISM ו-NFIB נאספו ידנית, עם קישור לכל ערך. SEC חסום בסביבה, ולכן הדוחות מ-Yahoo.</li>
<li><b>היסטוריה.</b> {m["history_years"]} שנות מחיר. "שיא" ו"שפל" מתייחסים לחלון הזה, ולא בהכרח לשיא של כל הזמנים.</li>
<li><b>תיק.</b> תיק רעיוני של {m["equity"]:,.0f}$ בלי פוזיציות פתוחות. בתיק האמיתי מנוע הכללים בודק גם חשיפה, סקטורים ותקרות אופציות.</li>
<li><b>לא מהקורס (מסומן בדוח):</b> השמטה של מכפיל היסטורי חריג (מעל פעמיים החציון, או מעל 80 ל-P/E ו-40 ל-P/S) ושימוש בממוצע של השאר; בדיקת התאמה בין EPS בדוחות לתחזית (ADR, מטבע, GAAP); נפילה לחישוב שנתי כשאין 8 רבעונים ל-TTM; ייחוס NFIB על 21 חודשים.</li>
<li><b>Yahoo מספק רק 5 רבעונים,</b> ולכן מינוף תפעולי ומקור הרווח מחושבים על שתי השנים הפיסקליות האחרונות. צמיחת הרבעון היא רבעון אחד מול המקביל.</li>
<li><b>רוחב השוק</b> ({num(m["breadth_pct"], 1, True)} מעל ממוצע 200) מחושב על כל היקום, לא על רכיבי S&P 500.</li>
</ul>
<p class="note">נוצר ב-{E(m["generated_at"])} · as of {E(m["as_of"])}. זה כלי עזר להחלטה ולא המלצת השקעה. שום פקודה לא נשלחת לברוקר.</p>
</section>'''


CSS = """
:root{
  /* Layout: one reading column, 1080px, a sticky section rail on top; ideas as full-width dossiers. */
  --bg:#f6f7f9; --panel:#ffffff; --ink:#15202b; --muted:#5b6876; --line:#dfe3e8; --accent:#0b6e75; --accent-soft:#e3f1f1;
  --g:#1f8a4c; --g-bg:#e2f3e8; --o:#b86e00; --o-bg:#fbefd9; --r:#c23b32; --r-bg:#f9e1df; --b:#3b5bdb; --b-bg:#e5eafc;
  --grid:#e9ecf0; --chart-main:#0b6e75; --ma1:#c08a2b; --ma2:#7b5ea7; --ma3:#9aa5b1;
  --display:"Frank Ruhl Libre", "David Libre", Georgia, serif; --body:"Assistant", "Segoe UI", Arial, sans-serif;
  --mono:"IBM Plex Mono", ui-monospace, Menlo, monospace;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#0f151b; --panel:#162029; --ink:#e4eaf0; --muted:#97a4b2; --line:#26333f; --accent:#4cc2c4; --accent-soft:#16363a;
  --g:#4cc27e; --g-bg:#173726; --o:#f0a63a; --o-bg:#3a2c13; --r:#f07167; --r-bg:#3d1d1b; --b:#8aa2ff; --b-bg:#1f2a4d;
  --grid:#1f2a34; --chart-main:#4cc2c4; --ma1:#d9a648; --ma2:#a88fd6; --ma3:#5f6d7a; color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#0f151b; --panel:#162029; --ink:#e4eaf0; --muted:#97a4b2; --line:#26333f; --accent:#4cc2c4; --accent-soft:#16363a;
  --g:#4cc27e; --g-bg:#173726; --o:#f0a63a; --o-bg:#3a2c13; --r:#f07167; --r-bg:#3d1d1b; --b:#8aa2ff; --b-bg:#1f2a4d;
  --grid:#1f2a34; --chart-main:#4cc2c4; --ma1:#d9a648; --ma2:#a88fd6; --ma3:#5f6d7a; color-scheme:dark}
body{background:var(--bg);color:var(--ink);font-family:var(--body);font-size:15.5px;line-height:1.6;
  padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px))}
body::before{content:"";position:fixed;inset:0 0 auto;height:env(safe-area-inset-top,0px);z-index:10;background:var(--bg)}
.wrap{max-width:1080px;margin:0 auto;padding-block:8px 64px}
a{color:var(--accent)} a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
h1,h2,h3{font-family:var(--display);text-wrap:balance;line-height:1.25}
h1{font-size:2.1rem;margin:0} h2{font-size:1.6rem;margin:2.4rem 0 .8rem;padding-top:.6rem;border-top:2px solid var(--ink)}
h3{font-size:1.2rem;margin:1.6rem 0 .6rem} h4{font-size:1rem;margin:1.4rem 0 .5rem;font-family:var(--body);font-weight:700}
.masthead{display:flex;flex-wrap:wrap;justify-content:space-between;gap:12px;align-items:end;padding-block:20px 12px}
.masthead p{margin:0;color:var(--muted)}
.rail{position:sticky;top:env(safe-area-inset-top,0px);z-index:5;background:var(--bg);border-bottom:1px solid var(--line);
  display:flex;gap:4px;overflow-x:auto;padding-block:8px}
.rail a{white-space:nowrap;text-decoration:none;color:var(--muted);padding:4px 10px;border-radius:4px;font-size:.9rem}
.rail a:hover{background:var(--accent-soft);color:var(--ink)}
.lede .big{font-size:1.25rem;font-family:var(--display)}
.note{color:var(--muted);font-size:.85rem} .muted{color:var(--muted)} .small{font-size:.9rem}
.n{font-family:var(--mono);font-variant-numeric:tabular-nums;direction:ltr;text-align:left;white-space:nowrap}
code{font-family:var(--mono);font-size:.82em}
.tbl{overflow-x:auto;margin:.4rem 0 1rem}
table{border-collapse:collapse;width:100%;font-size:.88rem}
th{font-weight:600;color:var(--muted);text-align:right;border-bottom:1px solid var(--ink);padding:6px 8px;white-space:nowrap}
td{border-bottom:1px solid var(--line);padding:6px 8px;vertical-align:top}
td.why{min-width:240px;font-size:.84rem;color:var(--muted)}
tr.chosen td{background:var(--accent-soft)}
table.compact td,table.compact th{padding:4px 6px}
.pill,.bucket,.chip{display:inline-block;border-radius:3px;padding:1px 8px;font-size:.8rem;font-weight:600;white-space:nowrap;margin-inline-end:4px}
.pill-green{background:var(--g-bg);color:var(--g)} .pill-orange{background:var(--o-bg);color:var(--o)} .pill-red{background:var(--r-bg);color:var(--r)}
.pill-none{background:var(--grid);color:var(--muted)}
.bucket{border:1px solid currentColor;background:transparent}
.bucket-enter{color:var(--g)} .bucket-conditional{color:var(--o)} .bucket-watch{color:var(--b)} .bucket-reject{color:var(--r)}
.chip{background:var(--grid);color:var(--ink);font-weight:500}
.chips{display:flex;flex-wrap:wrap;gap:4px;margin:.4rem 0}
.trade-card{display:block;text-decoration:none;color:inherit;background:var(--panel);border:1px solid var(--line);
  border-inline-start:4px solid var(--g);padding:14px 16px;margin:12px 0;border-radius:4px}
.trade-card:has(.bucket-conditional){border-inline-start-color:var(--o)}
.trade-card:hover{border-color:var(--accent)}
.tc-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.tc-sym{font-family:var(--mono);font-weight:700;font-size:1.2rem} .tc-name{color:var(--muted)}
.tc-nums{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:8px;margin:10px 0}
.tc-nums div{display:flex;flex-direction:column} .tc-nums label{font-size:.75rem;color:var(--muted)}
.tc-nums b{font-family:var(--mono);font-size:1.1rem;direction:ltr;text-align:right} .tc-nums small{color:var(--muted);font-family:var(--mono);direction:ltr;text-align:right}
.tc-why{margin:.4rem 0 0;font-size:.9rem}
.rrbar{display:flex;height:22px;border-radius:3px;overflow:hidden;font-size:.72rem;margin:6px 0}
.rr-risk{background:var(--r-bg);color:var(--r)} .rr-reward{background:var(--g-bg);color:var(--g)}
.rrbar div{display:flex;align-items:center;padding-inline:6px;white-space:nowrap;overflow:hidden}
.split{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(0,1fr);gap:20px;align-items:start}
.split.three{grid-template-columns:repeat(3,minmax(0,1fr))}
.funnel-list{margin:0;padding-inline-start:1.2rem} .funnel-list li{margin:.35rem 0}
.tiles{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:1px;background:var(--line);border:1px solid var(--line)}
.tile{background:var(--panel);padding:10px 12px;display:flex;flex-direction:column;min-width:0}
.tile label{font-size:.75rem;color:var(--muted);letter-spacing:.02em} .tile b{font-family:var(--mono);font-size:1.15rem}
.tile small{color:var(--muted);font-size:.78rem}
.judgment{background:var(--accent-soft);border-inline-start:3px solid var(--accent);padding:10px 14px;margin:14px 0;border-radius:3px}
.judgment p{margin:.35rem 0} .judgment ul{margin:.2rem 0}
.j-tag{font-size:.72rem;font-weight:700;letter-spacing:.06em;color:var(--accent)}
.verdict-box{background:var(--panel);border:1px dashed var(--line);padding:10px 14px;margin:10px 0}
.score{position:relative;height:16px;background:var(--grid);border-radius:2px;min-width:80px}
.score span{position:absolute;inset-block:0;inset-inline-start:0;background:var(--accent);border-radius:2px}
.score b{position:relative;font-family:var(--mono);font-size:.75rem;padding-inline-start:4px;color:var(--ink)}
.ok{color:var(--g)} .no{color:var(--r)}
.charts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
.charts.small{grid-template-columns:repeat(3,minmax(0,1fr))}
figure{margin:0;min-width:0} figcaption{font-size:.82rem;color:var(--muted);margin-bottom:2px}
svg.chart{width:100%;height:auto;display:block;direction:ltr;overflow:visible}
svg .grid{stroke:var(--grid);stroke-width:1} svg .zero{stroke:var(--muted);stroke-width:1}
svg .tick{fill:var(--muted);font-size:10px;font-family:var(--mono)}
svg .val{fill:var(--ink);font-size:10px;font-family:var(--mono)}
svg .ln{fill:none;stroke-width:1.4;vector-effect:non-scaling-stroke}
svg .ln.main{stroke:var(--chart-main);stroke-width:1.8} svg .dot.main{fill:var(--chart-main)}
svg .ln.ma21{stroke:var(--ma1)} svg .ln.ma63{stroke:var(--ma2)} svg .ln.ma252{stroke:var(--ma3);stroke-dasharray:4 3}
svg .hl{stroke-width:1;stroke-dasharray:5 4} svg .hl-label{font-size:10px;font-family:var(--body)}
svg .hl.ref{stroke:var(--muted)} svg .hl-label.ref{fill:var(--muted)}
svg .hl.entry{stroke:var(--ink)} svg .hl-label.entry{fill:var(--ink)}
svg .hl.stop{stroke:var(--r)} svg .hl-label.stop{fill:var(--r)}
svg .hl.target{stroke:var(--g)} svg .hl-label.target{fill:var(--g)}
svg .hl.level{stroke:var(--o);stroke-dasharray:2 3} svg .hl-label.level{fill:var(--o)}
svg .bar.pos{fill:var(--accent)} svg .bar.neg{fill:var(--r)}
svg .fbar{fill:var(--accent);opacity:.85} svg .fbar.f4,svg .fbar.f5{fill:var(--g)} svg .fnum{fill:var(--panel);font:700 13px var(--mono)}
.heat{background:color-mix(in srgb, var(--g) calc(max(var(--h),0) * 45%), color-mix(in srgb, var(--r) calc(max(calc(var(--h) * -1),0) * 45%), transparent))}
.pairs{list-style:none;padding:0;display:flex;flex-wrap:wrap;gap:8px}
.pairs li{border:1px solid var(--line);padding:4px 10px;border-radius:3px;font-size:.88rem}
.pairs li.agree{border-color:var(--g)} .pairs li.conflict{border-color:var(--o)}
.dotc{display:inline-block;width:8px;height:8px;border-radius:50%;margin-inline-end:4px}
.dot-green{background:var(--g)} .dot-orange{background:var(--o)} .dot-red{background:var(--r)}
.idea{background:var(--panel);border:1px solid var(--line);border-radius:4px;padding:16px 18px;margin:16px 0}
.idea header{display:flex;flex-wrap:wrap;justify-content:space-between;gap:10px;border-bottom:1px solid var(--line);padding-bottom:8px}
.idea h3{margin:0;font-family:var(--mono);font-size:1.4rem} .idea h3 span{font-family:var(--display);font-weight:400;font-size:1.1rem;color:var(--muted)}
.idea header p{margin:.2rem 0 0;color:var(--muted);font-size:.85rem}
.i-badges{display:flex;flex-wrap:wrap;gap:4px;align-items:start}
.idea-watch,.idea-reject{background:transparent}
.plan dl{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;margin:0;font-size:.9rem}
.plan dt{color:var(--muted)} .plan dd{margin:0;font-family:var(--mono);direction:ltr;text-align:right}
.plan dd small{font-family:var(--body);color:var(--muted)}
.trigger{background:var(--o-bg);color:var(--ink);padding:8px 10px;border-radius:3px;font-size:.9rem;margin:8px 0}
.rules{font-size:.85rem;border:1px solid var(--line);padding:8px 10px;border-radius:3px;margin-top:8px}
.rules ul{margin:.2rem 0;padding-inline-start:1rem}
.rules-ready_for_approval{border-color:var(--g)} .rules-blocked{border-color:var(--r)} .rules-needs_override{border-color:var(--o)}
.lights{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:6px;margin:.4rem 0}
.light{display:flex;flex-direction:column;align-items:center;gap:3px;font-size:.75rem;color:var(--muted);text-align:center}
.lamp{width:16px;height:16px;border-radius:50%;border:1px solid var(--line)}
.lamp-green{background:var(--g)} .lamp-orange{background:var(--o)} .lamp-red{background:var(--r)}
.kv{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:2px 20px;font-size:.88rem}
.kv div{display:flex;justify-content:space-between;gap:10px;border-bottom:1px dotted var(--line);padding:3px 0;min-width:0}
.kv label{color:var(--muted);flex-shrink:0} .kv span{text-align:left;min-width:0}
.timeline{list-style:none;padding:0;margin:.3rem 0} .timeline li{padding:3px 0}
.timeline time,.news time{font-family:var(--mono);font-size:.8rem;color:var(--muted);margin-inline-end:6px}
.news{padding-inline-start:1rem;font-size:.88rem} .news small{color:var(--muted)}
details{margin:.6rem 0} summary{cursor:pointer;color:var(--accent)}
.sources ul{font-size:.82rem;color:var(--muted);word-break:break-word}
h3.group{border-bottom:1px solid var(--line);padding-bottom:4px}
@media (max-width:760px){
  .split,.split.three,.charts,.charts.small{grid-template-columns:minmax(0,1fr)}
  .tiles{grid-template-columns:repeat(2,minmax(0,1fr))} .tc-nums{grid-template-columns:repeat(3,minmax(0,1fr))}
  .kv{grid-template-columns:minmax(0,1fr)} .lights{grid-template-columns:repeat(4,minmax(0,1fr))}
  h1{font-size:1.6rem}
}
@media (prefers-reduced-motion:reduce){*{scroll-behavior:auto}}
html{scroll-behavior:smooth;scroll-padding-top:56px}
"""


def render(d: dict[str, Any]) -> str:
    m = d["meta"]
    as_of = date.fromisoformat(m["as_of"])
    nav = [("summary", "שורה תחתונה"), ("funnel", "משפך"), ("macro", "מקרו"), ("sectors", "סקטורים"),
           ("screen", "סינון ורמזור"), ("ideas", "רעיונות"), ("method", "שיטה")]
    return f"""<title>מחקר שוק {as_of:%d.%m.%Y}</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Assistant:wght@400;500;600;700&family=Frank+Ruhl+Libre:wght@400;600;700&family=IBM+Plex+Mono:wght@400;600;700&display=swap">
<style>{CSS}</style>
<div class="wrap" dir="rtl" lang="he">
<div class="masthead"><div><h1>מחקר שוק · {as_of:%d.%m.%Y}</h1>
<p>ריצה מלאה של שלבים 0–11 על נתונים חיים · פלייבוק {E(m["playbook_version"])} · סגירה של {E(m["as_of"])}</p></div>
<p class="note">שיקול דעת: {E(m["judged_by"])}</p></div>
<nav class="rail">{"".join(f'<a href="#{k}">{E(v)}</a>' for k, v in nav)}</nav>
{_summary(d)}{_funnel(d)}{_macro(d)}{_sectors(d)}{_screen(d)}{_ideas(d)}{_method(d)}
</div>"""
