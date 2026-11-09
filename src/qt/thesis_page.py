"""The thesis store on the dashboard (qt.theses, phase A): read only. A list of every standing thesis, and one
thesis with its chain (macro -> sector -> company -> idea), its pillars and its versions."""
from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import quote

from qt import webui as ui
from qt.theses import KIND_HE, KINDS, STATUS_HE, Store

PILL = {"holding": "ok", "weakened": "warn", "broken": "bad"}
SIDE_HE = {"long": "לונג", "short": "שורט", "neutral": "ניטרלי"}
BUCKET_HE = {"enter": "כניסה", "conditional": "מותנה", "watch": "מעקב", "reject": "נדחה"}


def href(kind: str, subject: str) -> str:
    return f"/thesis/{kind}/{quote(subject, safe='')}"


def headline(t: dict) -> str:
    """The thesis in a few words, by kind."""
    b = t.get("body") or {}
    if t["kind"] == "macro":
        return " · ".join(str(x) for x in (b.get("zone"), b.get("compass_now"), b.get("bias"), b.get("market")) if x)
    if t["kind"] == "sector":
        return f"{SIDE_HE.get(b.get('side'), b.get('side') or '')} · ציון {b.get('total')}"
    if t["kind"] == "company":
        return " · ".join(str(x) for x in (b.get("name"), b.get("story_type")) if x)
    return " · ".join(str(x) for x in (BUCKET_HE.get(b.get("bucket"), b.get("bucket")), b.get("line")) if x)


def _stale(t: dict, today: date) -> str:
    return ui.pill("צריך מבט חדש", "warn") if t.get("stale_after") and t["stale_after"] < today.isoformat() else ""


def list_html(store: Store, today: date | None = None) -> str:
    today = today or date.today()
    out = []
    for kind in KINDS:
        rows = []
        for t in store.all(kind):
            ps = store.pillars(t["id"])
            bad = sum(1 for p in ps if p["status"] != "holding")
            behind = [r for r in t.get("rests_on") or [] if (store.get(r["kind"], r["subject"]) or {}).get("version", r["version"]) != r["version"]]
            flags = _stale(t, today) + (ui.pill(f"{bad} עמודים לא מחזיקים", "bad") if bad else "") + \
                (ui.pill("התזה שמעליה השתנתה", "warn") if behind else "")
            rows.append([f"<a href='{href(kind, t['subject'])}'><b>{ui.e(t['subject'])}</b></a>",
                         ui.e(headline(t)[:140]), f"<td class=t>v{t['version']} · {ui.e(t['as_of'])}</td>", flags])
        out.append(ui.section(f"{KIND_HE[kind]} ({len(rows)})", ui.table(("נושא", "התזה", "גרסה", ""), rows)
                              if rows else ui.empty("עוד אין תזות מהסוג הזה"), id=kind))
    if not any(store.all(k) for k in KINDS):
        return ui.empty("מאגר התזות ריק: הוא מתמלא מהמחקר השבועי. אפשר למלא אותו מהדוחות הקיימים עם qt theses backfill")
    return "".join(out)


def _value(v: Any) -> str:
    if isinstance(v, list):
        return "<ul>" + "".join(f"<li>{_value(x)}</li>" for x in v) + "</ul>" if v else "—"
    if isinstance(v, dict):
        return " · ".join(f"{ui.e(k)}: {_value(x)}" for k, x in v.items() if x not in (None, "", [], {})) or "—"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return ui.e("—" if v in (None, "") else v)


EFFECT = {"supports": ("תומכת", "ok"), "weakens": ("מחלישה", "warn"), "breaks": ("שוברת", "bad"), "neutral": ("ניטרלית", "mute")}


def _evidence(store: Store, pillar_id: int) -> str:
    """The latest evidence A5 linked to the pillar (phase B), newest first."""
    ev = store.evidence_for(pillar_id, 3)
    return "<br>".join(f"{ui.pill(*EFFECT.get(x['effect'], (x['effect'], '')))} <a href='{ui.e(x['url'])}' rel=noopener "
                       f"target=_blank>{ui.e((x.get('title') or x['url'])[:90])}</a> <span class=note>{ui.e((x.get('published') or '')[:10])}</span>"
                       for x in ev) or "<span class=note>—</span>"


def thesis_html(store: Store, kind: str, subject: str, today: date | None = None) -> str:
    today = today or date.today()
    t = store.get(kind, subject)
    if t is None:
        return ui.empty("אין תזה כזאת")
    parts = []
    chain = store.chain(subject) if kind == "idea" else \
        [{**r, "current": r["version"] == x["version"], "rested_on": x["version"]}
         for x in t.get("rests_on") or [] if (r := store.get(x["kind"], x["subject"]))] + [{**t, "current": True, "rested_on": t["version"]}]
    if len(chain) > 1:
        steps = "".join(
            f"<a class=row href='{href(c['kind'], c['subject'])}'><span class=l><b>{KIND_HE[c['kind']]}: {ui.e(c['subject'])}</b>"
            f"<span class=note> {ui.e(headline(c)[:120])}</span></span><span class=r>"
            + ("" if c["current"] else ui.pill(f"השתנתה: v{c['rested_on']} → v{c['version']}", "warn")) + "</span></a>"
            for c in chain)
        parts.append(ui.section("השרשרת", f"<div class=panel><div class=rows>{steps}</div></div>", id="chain"))
    ps = t.get("pillars") or []
    prow = [[ui.pill(STATUS_HE.get(p["status"], p["status"]), PILL.get(p["status"], "")), ui.e(p["claim"]),
             f"<span class=note>{ui.e(p.get('check_text') or '')}</span>", _evidence(store, p["id"]),
             f"<td class=t>מ-v{p['since_version']}</td>"] for p in ps]
    parts.append(ui.section("העמודים", ui.table(("מצב", "טענה", "מה יפריך", "ראיות", ""), prow) if prow else
                            ui.empty("לתזה הזו אין עמודים רשומים"), id="pillars"))
    body = t.get("body") or {}
    rows = [[f"<b>{ui.e(k)}</b>", _value(v)] for k, v in body.items() if v not in (None, "", [], {})]
    parts.append(ui.section("התוכן", ui.table(("שדה", "ערך"), rows), id="body"))
    hist = store.history(kind, subject)
    hrow = [[f"<td class=t>v{h['version']}</td>", f"<td class=t>{ui.e(h['as_of'])}</td>", ui.e(h["author"]),
             ui.e(h.get("change") or ""), f"<span class=note>{ui.e(h.get('source') or '')}</span>"] for h in hist]
    parts.append(ui.section(f"גרסאות ({len(hist)})", ui.table(("גרסה", "as of", "מי", "מה השתנה", "מקור"), hrow), id="history"))
    deps = store.dependents(kind, subject) if kind != "idea" else []
    if deps:
        drow = [f"<a class=row href='{href(x['kind'], x['subject'])}'><span class=l><b>{KIND_HE[x['kind']]}: {ui.e(x['subject'])}</b>"
                f"</span><span class=r>{ui.e(headline(x)[:80])}</span></a>" for x in deps]
        parts.append(ui.section(f"נשענות עליה ({len(deps)})", f"<div class=panel><div class=rows>{''.join(drow)}</div></div>", id="deps"))
    stale = _stale(t, today)
    ver = f"v{t['version']}"
    head = (f"<div class=strip>{ui.cell(KIND_HE[kind], ui.e(subject))}{ui.cell(ver, 'as of ' + ui.e(t['as_of']))}"
            f"{ui.cell('רעננה עד', ui.e(t.get('stale_after') or '—'), 'warn' if stale else 'ok')}</div>")
    return head + "".join(parts)
