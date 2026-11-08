"""The nightly scan (Ilay 08.11, stage 1 of the cadence proposal: breadth). Not from course: the course runs the funnel
by hand once a week or two (060); the AI can run its code part every night, over the whole market.

universe -> macro -> screen -> compare -> notify. Code only, no agent, nothing enters the watchlist or the queue:

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

from qt.runs import RunContext, Step

LIGHTS = ("green", "orange")
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
    f = _work(ctx.as_of) / "universe.pkl"
    if f.exists():
        return pickle.loads(f.read_bytes())
    u = U.fetch(_yahoo(ctx))
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
            "auto": None if auto is None else {k: auto.get(k) for k in ("added", "stale", "rejected", "error")}}


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


def diff(today_macro: dict, today: dict, prev: dict | None) -> dict:
    if prev is None:
        return {"first": True, "macro": [], "sectors": [], "new": [], "upgraded": [], "dropped": 0}
    pm = prev.get("macro") or {}
    mac = [f"{he}: {pm.get(k) or '-'} → {today_macro.get(k) or '-'}" for k, he in MACRO_FIELDS
           if pm and pm.get(k) != today_macro.get(k)]
    ps, ts = (prev["screen"].get("sectors") or {}), today["sectors"]
    sec = [f"{e}: {SECTORS_HE[a]} → {SECTORS_HE[b]}" for e in sorted(set().union(*ts.values()))
           for a in [next((k for k, v in ps.items() if e in v), None)] for b in [next(k for k, v in ts.items() if e in v)]
           if a is not None and a != b]
    before = {(r["sym"], r["dir"]): r for r in prev["screen"].get("radar") or []}
    now = {(r["sym"], r["dir"]): r for r in today["radar"]}
    new = [r for k, r in now.items() if k not in before]
    up = [r for k, r in now.items() if k in before and before[k]["light"] == "orange" and r["light"] == "green"]
    return {"first": False, "prev_as_of": prev["as_of"], "macro": mac, "sectors": sec, "new": new, "upgraded": up,
            "dropped": sum(1 for k in before if k not in now)}


def compare(ctx: RunContext, _: str) -> dict:
    return diff(ctx.out("macro"), ctx.out("screen"), _previous(ctx))


def _name(r: dict) -> str:
    rr = f", יחס לשיא {r['rr']:.1f}" if r.get("rr") else ""
    return (f"{r['sym']} ({r['etf']} {'לונג' if r['dir'] == 'long' else 'שורט'}, "
            f"{'ירוק' if r['light'] == 'green' else 'כתום'}{rr}"
            f"{', מעקב בלבד' if r['fit'] == 'watch_only' else ''}{', כבר ברשימה' if r['on_list'] else ''})")


def message(as_of: date, u: dict, m: dict, s: dict, c: dict) -> str:
    radar = s["radar"]
    by = Counter((r["light"], r["fit"]) for r in radar)
    lines = []
    if c["macro"]:
        lines.append("⚠️ שינוי בתמונת המקרו: " + " · ".join(c["macro"]))
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
    text = message(ctx.as_of, ctx.out("universe"), ctx.out("macro"), ctx.out("screen"), ctx.out("compare"))
    bot = ctx.services.get("telegram")
    if bot is not None:
        bot.send(text)
    if ctx.services.get("notifier") is not None:
        ctx.services["notifier"](text)
    return {"text": text}


SCAN = (Step("universe", universe), Step("macro", macro), Step("screen", screen), Step("compare", compare),
        Step("notify", notify))


def latest(db) -> dict[str, Any] | None:
    """The last finished scan's macro, sectors and radar (for the dashboard and the digest)."""
    row = db.one("SELECT id, as_of FROM runs WHERE kind = 'scan' AND status = 'done' ORDER BY as_of DESC, "
                 "started_at DESC LIMIT 1")
    if row is None:
        return None
    steps = {r["step"]: r["output"] for r in db.all("SELECT step, output FROM run_steps WHERE run_id = ?",
                                                    (row["id"],), ["output"])}
    return {"as_of": row["as_of"], "run_id": row["id"], **steps}
