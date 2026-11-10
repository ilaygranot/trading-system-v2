"""The nightly scan (Ilay 08.11, stage 1 of the cadence proposal: breadth). Not from course: the course runs the funnel
by hand once a week or two (060); the AI can run its code part every night, over the whole market.

universe -> macro -> screen -> compare -> cascade -> notify. Code only, except the cascade; nothing enters the watchlist:

universe: the whole universe (Yahoo screener, every sector, qt.research.universe), as in the weekly research.
macro:    stages 1-3 every trading day (qt.research.macro_stage) after the ISM / NFIB / NAAIM refresh
          (qt.data.macro_auto), so a month's report counts the day it is out and the daily series (HY spread, curve,
          S&P 500) move the picture every night instead of once a month. Code's numbers only: the grey band stays
          unresolved here (A1's DC-01 is the monthly run's).
screen:   the sector scorecard (S04) on tonight's stance, then the S06 screen and the S09 traffic light on every
          sector, not only the chosen ones (funnel audit 3.11: the healthy longs sat in sectors nobody scanned).
          A stock in a long sector going long, or in a short sector going short, is "aligned"; a stock in a neutral
          sector is "watch only" (information, never an entry); against its sector's side it is not listed (S04-R13).
compare:  with the last scan: new green / orange stocks, oranges that turned green, and what moved in the macro
          picture and the sector sides.
cascade:  a change in the stance (zone, bias, compass, cycle phase) writes a new version of the macro thesis
          (qt.theses) and marks the theses built on the old one due; it sets off A1 on tonight's stance and A7 (DC-21) on
          every open position (architecture 8, stage 2 of the proposal). Proposals only, to the approval queue.
notify:   one Telegram message; a change in the macro picture gets its own line on top.

The weekly research (deep dives, agents) and the monthly sector review are unchanged: the course refreshes sector
growth monthly on purpose (070) and the research ranks its candidates together (S07-R35).
"""
from __future__ import annotations

import os
import pickle
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from qt.approvals import Queue
from qt.runs import RunContext, Step

LIGHTS = ("green", "orange")
SCAN_KEEP_DAYS = 4                     # scan universes kept in the database (Friday's lasts to Saturday's research)
NEW_SHOWN = 12                         # new stocks named in the message (not from course: length)
SECTORS_HE = {"long": "לונג", "short": "שורט", "neutral": "נייטרלי"}
FIT_HE = {"aligned": "בכיוון הסקטור", "watch_only": "מעקב בלבד (סקטור נייטרלי)"}


class ScanDataMissing(RuntimeError):
    pass


def _work(as_of: date) -> Path:
    p = Path(os.environ.get("QT_WORK_DIR", "/tmp/qt-research")) / "scan" / as_of.isoformat()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _yahoo(ctx: RunContext):
    y = ctx.services.get("research_yahoo")                          # tests stand in for Yahoo here
    if y is not None:
        return y
    from qt.research.yahoo import Yahoo
    return Yahoo(_work(ctx.as_of) / "yahoo")


def _universe(ctx: RunContext) -> dict:
    """The universe of this run, from the work folder (a resume in the same container does not fetch it again)."""
    fetched = ctx.services.get("scan_universe")
    if fetched is not None:
        return fetched
    from qt.research import universe as U
    from qt.research.state import SCAN_TAG, UNIVERSE, State, universe_ok
    f = _work(ctx.as_of) / "universe.pkl"
    if f.exists():
        return pickle.loads(f.read_bytes())
    state = State(ctx.db, ctx.as_of, tag=SCAN_TAG)
    u = state.get_blob(UNIVERSE)                    # a resume in a new container
    if u is None:
        u = U.fetch(_yahoo(ctx))
        if universe_ok(u):                          # the evening's research runs take it from here (state.universe_from_scan)
            ctx.db.execute("DELETE FROM research_state WHERE as_of LIKE ? AND as_of < ?",   # tens of MB a night:
                           (f"%:{SCAN_TAG}", (ctx.as_of - timedelta(days=SCAN_KEEP_DAYS)).isoformat()))  # keep a few
            state.put_blob(UNIVERSE, u)
    f.write_bytes(pickle.dumps(u))
    return u


# ---------------------------------------------------------------- steps

def universe(ctx: RunContext, _: str) -> dict:
    from qt.research import universe as U
    u = _universe(ctx)
    errors = sum(1 for x in u["summaries"].values() if isinstance(x, dict) and "error" in x)
    return {"n": len(u["quotes"]), "errors": errors, "breadth_pct": U.breadth_pct(u["quotes"])}


def macro(ctx: RunContext, _: str) -> dict:
    """Stages 1-3 tonight. Needs FRED and the macro file (refreshed first when the auto source is set)."""
    from qt.approvals import Queue
    from qt.research import macro_stage
    from qt.sectors import cycle_phase
    auto = None
    if ctx.services.get("macro_auto") is not None:
        from qt.pipelines import refresh_macro
        auto = refresh_macro(ctx, ctx.services["macro_auto"])
    fred, manual = ctx.services.get("fred"), Queue(ctx.db).latest_upload("manual_macro")
    missing = [n for n, v in (("FRED_API_KEY", fred), ("קובץ המקרו (ISM / NFIB)", manual)) if v is None]
    if missing:
        raise ScanDataMissing("הסריקה הלילית לא רצה: חסר " + "; ".join(missing))
    spx = _yahoo(ctx).bars("^GSPC", ctx.as_of - timedelta(days=3650), ctx.as_of)
    m = macro_stage.run(fred, manual["content"], spx, ctx.as_of, ctx.out("universe")["breadth_pct"])
    st = m["stage2"]["stance"]
    sc = {x["name"]: x for x in m["stage2"]["scores"]}
    phase, phase_flags = cycle_phase(sc["ism_pmi"]["cycle_quadrant"], (sc.get("permits") or {}).get("cycle_quadrant"))
    s1, s3 = m["stage1"], m["stage3"]
    return {"zone": st["zone"], "bias": st.get("bias"), "compass": st.get("compass_now"),
            "activity_pct": round(st["activity"]["pct"], 1), "phase": phase, "phase_flags": phase_flags,
            "credit": s1["credit"]["state"], "curve": s1["curve"]["shape"], "market": s3["market"]["regime"],
            "drawdown_pct": round(s3["market"]["drawdown_pct"], 1), "flags": st.get("flags") or [],
            "auto": None if auto is None else {k: auto.get(k) for k in ("added", "stale", "rejected", "error")},
            "stance": st}


def classify(timing: list[dict], longs: list[str], shorts: list[str], on_list: set[str]) -> list[dict]:
    """The radar: green and orange stocks with their fit to the sector's side, best first."""
    out = []
    for t in timing:
        if t.get("error") or t.get("combined") not in LIGHTS:
            continue
        side = "long" if t["etf"] in longs else "short" if t["etf"] in shorts else "neutral"
        if side != "neutral" and side != t["dir"]:
            continue                                                 # S04-R13: never against the sector's side
        out.append(dict(sym=t["sym"], etf=t["etf"], dir=t["dir"], profile=t.get("profile"), light=t["combined"],
                        stock=t.get("stock"), price=t.get("price"), adr=t.get("adr"), rr=t.get("rr_extreme"),
                        fit="aligned" if side != "neutral" else "watch_only", on_list=t["sym"] in on_list))
    out.sort(key=lambda r: (r["light"] != "green", r["fit"] != "aligned", -(r["rr"] or 0), r["sym"]))
    return out


def screen(ctx: RunContext, _: str) -> dict:
    from qt.research import sector_screen
    from qt.research.run import _rows_by_etf, stage_timing
    u, m, y = _universe(ctx), ctx.out("macro"), _yahoo(ctx)
    rows = _rows_by_etf(u)
    sectors = sector_screen.run(y, rows, m["phase"], m["phase_flags"], m["zone"], ctx.as_of)
    neutral = [e for e in sectors["scorecard"] if e not in sectors["long"] + sectors["short"]]
    screens = sector_screen.screens(rows, sectors["long"] + neutral, sectors["short"] + neutral)
    timing = (ctx.services.get("scan_timing") or stage_timing)(y, screens, ctx.as_of)
    on_list = {e["symbol"] for e in (ctx.services.get("watchlist") or {}).get("entries") or []}
    radar = classify(timing, sectors["long"], sectors["short"], on_list)
    lights = Counter("error" if t.get("error") else t.get("combined") for t in timing)
    return {"sectors": {"long": sectors["long"], "short": sectors["short"], "neutral": neutral},
            "screened": sum(len(s["long"]) + len(s["short"]) for s in screens.values()),
            "lights": dict(lights), "radar": radar}


def _previous(ctx: RunContext) -> dict | None:
    row = ctx.db.one("SELECT id, as_of FROM runs WHERE kind = 'scan' AND status = 'done' AND as_of < ? "
                     "ORDER BY as_of DESC, started_at DESC LIMIT 1", (ctx.as_of.isoformat(),))
    if row is None:
        return None
    steps = {r["step"]: r["output"] for r in ctx.db.all(
        "SELECT step, output FROM run_steps WHERE run_id = ? AND step IN ('macro', 'screen') AND status = 'done'",
        (row["id"],), ["output"])}
    return {"as_of": row["as_of"], **steps} if "screen" in steps else None


MACRO_FIELDS = (("zone", "אזור"), ("bias", "הטיה"), ("compass", "מצפן"), ("phase", "שלב מחזור"),
                ("credit", "מרווחי אשראי"), ("curve", "עקום"), ("market", "מצב השוק"))


STANCE_FIELDS = ("zone", "bias", "compass", "phase")   # a change here sets off A1 and A7 (architecture 8)


def diff(today_macro: dict, today: dict, prev: dict | None) -> dict:
    if prev is None:
        return {"first": True, "macro": [], "stance_changed": False, "sectors": [], "new": [], "upgraded": [],
                "dropped": 0}
    pm = prev.get("macro") or {}
    mac = [f"{he}: {pm.get(k) or '-'} → {today_macro.get(k) or '-'}" for k, he in MACRO_FIELDS
           if pm and pm.get(k) != today_macro.get(k)]
    stance_changed = bool(pm) and any(pm.get(k) != today_macro.get(k) for k in STANCE_FIELDS)
    ps, ts = (prev["screen"].get("sectors") or {}), today["sectors"]
    sec = [f"{e}: {SECTORS_HE[a]} → {SECTORS_HE[b]}" for e in sorted(set().union(*ts.values()))
           for a in [next((k for k, v in ps.items() if e in v), None)] for b in [next(k for k, v in ts.items() if e in v)]
           if a is not None and a != b]
    before = {(r["sym"], r["dir"]): r for r in prev["screen"].get("radar") or []}
    now = {(r["sym"], r["dir"]): r for r in today["radar"]}
    new = [r for k, r in now.items() if k not in before]
    up = [r for k, r in now.items() if k in before and before[k]["light"] == "orange" and r["light"] == "green"]
    return {"first": False, "prev_as_of": prev["as_of"], "macro": mac, "stance_changed": stance_changed,
            "sectors": sec, "new": new, "upgraded": up,
            "dropped": sum(1 for k in before if k not in now)}


def compare(ctx: RunContext, _: str) -> dict:
    return diff(ctx.out("macro"), ctx.out("screen"), _previous(ctx))


def cascade(ctx: RunContext, _: str) -> dict:
    """Architecture 8: a change in the stance (zone, bias, compass, cycle phase) sets off A1 on tonight's stance, then
    A7 (DC-21) on every open position, the change as its event. Proposals only: a money-moving answer goes to the
    approval queue for Ilay, as in the daily run. Without the agents, the change is the message's top line only."""
    c, runner = ctx.out("compare"), ctx.services.get("agents")
    if not c.get("stance_changed"):
        return {"ran": False}
    event = "שינוי בתמונת המקרו (סריקה לילית): " + " · ".join(c["macro"])
    if runner is None:
        return {"ran": False, "theses": macro_thesis(ctx, {}, event)}
    from qt import monthly as MO
    from qt.daily import AGENT_KIND, event_card
    from qt.intraday import _decided_today, _last_daily
    ctx.outputs["stance"] = {"": {k: v for k, v in ctx.out("macro")["stance"].items()}}
    a1 = MO.a1(ctx, "")
    out: dict[str, Any] = {"ran": True, "a1": {k: (a1.get(k) or {}).get("choice") for k in ("DC-01", "DC-02", "DC-03",
                                                                                         "DC-04") if a1.get(k)},
                           "a1_error": a1.get("error"), "decisions": {}, "added": [],
                           "theses": macro_thesis(ctx, {} if a1.get("error") else a1, event)}
    base, pf = _last_daily(ctx), ctx.services.get("portfolio") or {}
    if "calc" not in base or "rules" not in base or not pf.get("positions"):
        return out
    ctx.outputs.update(calc={"": base["calc"]}, rules={"": base["rules"]},
                       fetch={"": {"sources": {}, "portfolio": pf, "stale": []}})
    th = out["theses"]
    on_old = set(th.get("due_symbols") or [])
    known = set(th.get("known_symbols") or []) if th.get("version") else set()    # no new version: ask every one
    q = Queue(ctx.db)
    for p in pf["positions"]:
        sym = p["symbol"]
        # the chain: a position whose thesis rests on the old stance is asked; one whose thesis was already written
        # on tonight's stance is not; one with no thesis in the store is asked, as before the store
        if _decided_today(ctx, sym) or (sym in known and sym not in on_old):
            continue
        try:
            d = event_card(ctx, runner, p, event, "code:scan", step="cascade")
        except Exception as ex:                                      # noqa: BLE001 - the change is still in the message
            ctx.event(f"{sym}: DC-21 {type(ex).__name__}: {ex}", "warn", "cascade")
            continue
        out["decisions"][sym] = d
        if d["for_ilay"]:
            iid = q.add(ctx.run_id, ctx.as_of, dict(kind=AGENT_KIND.get(d["option"], "flag"), rule_id="DC-21", symbol=sym,
                                                   qty=None, price=None, preapproved=False, agent=d,
                                                   detail=f"DC-21 (שינוי מקרו): {d['option']}. {d['reasoning']}"[:600]),
                        (base["calc"].get("prices") or {}).get(sym), d["decision_id"])
            if iid:
                out["added"].append(iid)
    return out


def macro_thesis(ctx: RunContext, a1: dict, event: str) -> dict:
    """Tonight's stance as a new version of the macro thesis (qt.theses, phase C), with A1's cards when they ran;
    the theses still resting on the old version are marked due. Returns the new version, what is due, and which
    symbols' company or idea theses rest on the old stance (directly or through their sector). Never fails the
    cascade."""
    from qt.theses import MACRO, Store, macro_thesis as build
    try:
        s = Store(ctx.db)
        cur = s.get("macro", MACRO)
        st = ctx.out("macro")["stance"]
        cards = {k: a1[k] for k in ("DC-01", "DC-02", "DC-03", "DC-04") if a1.get(k)}
        body, pillars = build({"macro": {"stage2": {"stance": st}}, "judgments": {"A1": cards},
                               "sectors": {"phase": ctx.out("macro").get("phase")}})
        v = s.put("macro", MACRO, body, pillars, as_of=ctx.as_of, author="agents" if cards else "code",
                  source=f"scan:{ctx.as_of.isoformat()}", change=event)
        due: list[dict] = []
        if v and cur is not None:
            due = s.mark_due("macro", MACRO, cur["version"], ctx.as_of)
            for t in [t for t in due if t["kind"] == "sector"]:        # a sector on the old stance: its companies
                due += [x for x in s.dependents("sector", t["subject"]) if x not in due]
        def syms(ts):
            return sorted({t["subject"].split(":")[0] for t in ts if t["kind"] in ("company", "idea")})
        return {"version": v, "due": [f"{t['kind']}:{t['subject']}" for t in due], "due_symbols": syms(due),
                "known_symbols": syms(s.all("company") + s.all("idea"))}
    except Exception as ex:                                          # noqa: BLE001 - the store is a reader's aid
        ctx.event(f"macro thesis not updated: {type(ex).__name__}: {ex}", "warn", "cascade")
        return {}


def _name(r: dict) -> str:
    rr = f", יחס לשיא {r['rr']:.1f}" if r.get("rr") else ""
    return (f"{r['sym']} ({r['etf']} {'לונג' if r['dir'] == 'long' else 'שורט'}, "
            f"{'ירוק' if r['light'] == 'green' else 'כתום'}{rr}"
            f"{', מעקב בלבד' if r['fit'] == 'watch_only' else ''}{', כבר ברשימה' if r['on_list'] else ''})")


def message(as_of: date, u: dict, m: dict, s: dict, c: dict, k: dict | None = None) -> str:
    radar = s["radar"]
    by = Counter((r["light"], r["fit"]) for r in radar)
    lines = []
    if c["macro"]:
        lines.append("⚠️ שינוי בתמונת המקרו: " + " · ".join(c["macro"]))
    if k and k.get("ran"):
        a1 = ", ".join(f"{card} {v}" for card, v in (k.get("a1") or {}).items()) or (k.get("a1_error") or "בלי תשובה")
        lines.append(f"A1 על המקרו של הלילה: {a1}")
        for sym, d in (k.get("decisions") or {}).items():
            lines.append(f"A7 על {sym}: {d['option']}. {d['reasoning'][:200]}")
    th = (k or {}).get("theses") or {}
    if th.get("version"):
        lines.append(f"🧱 תזת המקרו עודכנה לגרסה {th['version']}"
                     + (f" · {len(th['due'])} תזות שנשענו על הקודמת מסומנות למבט חדש" if th.get("due") else ""))
    if c["sectors"]:
        lines.append("⚠️ שינוי בצד של סקטור: " + " · ".join(c["sectors"]))
    lines.append(f"סריקה לילית {as_of.isoformat()} · {u['n']:,} מניות · {s['screened']} עברו סינון S06 בכל 11 הסקטורים")
    lines.append(f"מקרו (קוד בלבד): אזור {m['zone']} ({m['activity_pct']:.0f}%) · הטיה {m['bias'] or 'אזור אפור'}"
                 f" · מצפן {m['compass'] or '-'} · שלב {m['phase']} · אשראי {m['credit']} · עקום {m['curve']}"
                 f" · שוק {m['market']} ({m['drawdown_pct']:.1f}% מהשיא)")
    sec = s["sectors"]
    lines.append(f"סקטורים: לונג {', '.join(sec['long']) or '-'} · שורט {', '.join(sec['short']) or '-'}")
    lines.append(f"רדאר: ירוקות {by[('green', 'aligned')]} בכיוון הסקטור + {by[('green', 'watch_only')]} מעקב בלבד · "
                 f"כתומות {by[('orange', 'aligned')]} + {by[('orange', 'watch_only')]}")
    if c["first"]:
        lines.append("סריקה ראשונה: מחר תופיע ההשוואה.")
    else:
        if c["upgraded"]:
            lines.append("עלו לירוק: " + ", ".join(_name(r) for r in c["upgraded"][:NEW_SHOWN]))
        fresh = c["new"]
        if fresh:
            more = f" ועוד {len(fresh) - NEW_SHOWN}" if len(fresh) > NEW_SHOWN else ""
            lines.append(f"חדשות ברדאר מאז {c['prev_as_of']}: " + ", ".join(_name(r) for r in fresh[:NEW_SHOWN]) + more)
        if not c["upgraded"] and not fresh:
            lines.append(f"אין מניות חדשות ברדאר מאז {c['prev_as_of']}" + (f" ({c['dropped']} יצאו)" if c["dropped"] else ""))
    lines.append("מידע בלבד: שום מניה לא נכנסת לרשימה או לתור בלי המחקר השבועי.")
    return "\n".join(lines)


def notify(ctx: RunContext, _: str) -> dict:
    text = message(ctx.as_of, ctx.out("universe"), ctx.out("macro"), ctx.out("screen"), ctx.out("compare"),
                   ctx.out("cascade"))
    bot = ctx.services.get("telegram")
    if bot is not None:
        bot.send(text)
        from qt.telegram import send_item
        q = Queue(ctx.db)
        for iid in (ctx.out("cascade") or {}).get("added") or []:
            send_item(bot, q, q.get(iid))
    if ctx.services.get("notifier") is not None:
        ctx.services["notifier"](text)
    return {"text": text}


SCAN = (Step("universe", universe), Step("macro", macro), Step("screen", screen), Step("compare", compare),
        Step("cascade", cascade), Step("notify", notify))


def latest(db) -> dict[str, Any] | None:
    """The last finished scan's macro, sectors and radar (for the dashboard and the digest)."""
    row = db.one("SELECT id, as_of FROM runs WHERE kind = 'scan' AND status = 'done' ORDER BY as_of DESC, "
                 "started_at DESC LIMIT 1")
    if row is None:
        return None
    steps = {r["step"]: r["output"] for r in db.all("SELECT step, output FROM run_steps WHERE run_id = ?",
                                                    (row["id"],), ["output"])}
    return {"as_of": row["as_of"], "run_id": row["id"], **steps}
