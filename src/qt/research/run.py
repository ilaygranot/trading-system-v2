"""One full research run, stages 0-10, into a dossier the report renders (qt research).

universe -> macro (S01-S03) -> sectors (S04-S05) -> screen (S06) -> traffic light on every survivor (S09)
-> deep dive for the shortlisted ideas (S07-S08) -> trade plan, options and the rules engine (S10-S11).

Code computes every number. The judgment fields (stance commentary, sector notes, picks, thesis, catalyst
classes, levels color, trigger, k, structure) come from a judgments file: the agents' cards in production,
or a person / Claude in a session, and the dossier says which. Every stage is saved under work/ so a rerun
reuses what was already fetched (delete a file there to recompute that stage).
"""
from __future__ import annotations

import json
import pickle
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from qt.calc import trade as T
from qt.calc.volatility import adr
from qt.policy import load_policy
from qt.research import idea, macro_stage, sector_screen, universe as U
from qt.research.yahoo import Yahoo, raw
from qt.sectors import cycle_phase
from qt.timing import candidate_levels, snapshot

HISTORY_DAYS = 365 * 8
SRC_HE = {"hist_3y": "ממוצע 3 שנים", "sector": "מכפיל הסקטור", "hist_trimmed": "ממוצע ללא חריג, לא מהקורס"}


def _cached(path: Path, fn, binary: bool = False):
    if path.exists():
        return pickle.loads(path.read_bytes()) if binary else json.loads(path.read_text())
    out = fn()
    path.parent.mkdir(parents=True, exist_ok=True)
    if binary:
        path.write_bytes(pickle.dumps(out))
    else:
        path.write_text(json.dumps(out, default=str))
        out = json.loads(path.read_text())
    return out


def _rows_by_etf(u: dict) -> dict[str, list]:
    out: dict[str, list] = {}
    for sym, q in u["quotes"].items():
        s = u["summaries"].get(sym, {})
        if "error" in s or not q.get("market_cap"):
            continue
        out.setdefault(q["etf"], []).append(U.row(q, s))
    return out


def stage_timing(y: Yahoo, screens: dict, as_of: date) -> list[dict]:
    """S09 on every screen survivor, with R:R at the default k to each course target that code can compute
    without the agent: the extreme (all-time high / low in the history held) and the own-multiple target."""
    start = as_of - timedelta(days=HISTORY_DAYS)
    spy = y.bars("SPY", start, as_of)
    etfb = {e: y.bars(e, start, as_of) for e in screens}
    k = load_policy().stop.k_default
    jobs = [(e, c) for e, s in screens.items() for c in s["long"] + s["short"]]

    def one(j):
        e, c = j
        s, dr = c["ticker"], c["side"]
        try:
            b = y.bars(s, start, as_of)
            if len(b) < 260:
                return dict(etf=e, sym=s, dir=dr, profile=c["profile"], error=f"only {len(b)} daily bars")
            tl = idea.timing(b, etfb[e], spy, dr)
            px, a = b[-1].close, adr(b)
            ext = max(x.high for x in b) if dr == "long" else min(x.low for x in b)
            st = T.stop_price(px, a, k, dr)                                 # type: ignore[arg-type]
            rr = T.reward_risk(px, st, ext, dr) if (ext > px) == (dr == "long") else None  # type: ignore[arg-type]
            return dict(etf=e, sym=s, dir=dr, profile=c["profile"], stock=tl["stock"]["overall"],
                        sector=tl["sector"]["overall"], market=tl["market"]["overall"], combined=tl["combined"],
                        sources=tl["stock"]["sources"], flags=tl["stock"]["flags"], adr=a, price=px, extreme=ext,
                        rr_extreme=rr)
        except Exception as ex:                                              # noqa: BLE001 - reported per stock
            return dict(etf=e, sym=s, dir=dr, profile=c["profile"], error=f"{type(ex).__name__}: {ex}")
    with ThreadPoolExecutor(8) as ex:
        return list(ex.map(one, jobs))


def _target_options(price: float, direction: str, ext: float, val: dict, levels: dict) -> list[dict]:
    """The three course target sources (reward_risk.target_sources) with their price, where code has one."""
    up = direction == "long"
    out = []
    res = levels.get("nearest_resistance" if up else "nearest_support")
    if res:
        out.append(dict(source="technical_level", price=res, note="הרמה הקרובה מנרות שנתיים, רבעוניים וחודשיים"))
    t = val.get("target") or {}
    if t.get("price"):
        out.append(dict(source="own_multiple_target", price=t["price"], unreliable="unreliable_eps_basis" in t["flags"],
                        multiple_source=t["multiple_source"],
                        note=f"{'P/E' if t['method'] == 'pe' else 'P/S'} ×{t['multiple']:.1f} ({SRC_HE.get(t['multiple_source'], t['multiple_source'])})"))
    out.append(dict(source="all_time_high_target", price=ext,
                    note=f"{'השיא' if up else 'השפל'} של {HISTORY_DAYS // 365} השנים האחרונות"))
    return [o for o in out if (o["price"] > price) == up]


def deep_dive(y: Yahoo, u: dict, screens: dict, sym: str, etf: str, direction: str, j: dict, macro_bias: str,
              buckets: dict, as_of: date) -> dict[str, Any]:
    start = as_of - timedelta(days=HISTORY_DAYS)
    scr = screens[etf]
    row = dict(next(c for c in scr["long"] + scr["short"] if c["ticker"] == sym))
    s, q = u["summaries"][sym], u["quotes"][sym]
    t0, t1 = U.trend(s, "0y"), U.trend(s, "+1y")
    row.update(sales=raw(t0.get("revenueEstimate"), "yearAgoRevenue"), sales_f1=raw(t0.get("revenueEstimate"), "avg"),
               sales_f2=raw(t1.get("revenueEstimate"), "avg"))
    f = y.fundamentals(sym)
    bars, eb, spy = y.bars(sym, start, as_of), y.bars(etf, start, as_of), y.bars("SPY", start, as_of)
    avg = scr["averages"]
    fv = idea.fundamentals_view(f, s, row, avg, direction)
    val = idea.valuation(bars, f, row, {"pe1": avg.get("pe"), "ps1": avg.get("ps")})
    tl = idea.timing(bars, eb, spy, direction)
    if j.get("levels_color"):                                     # A6's levels color feeds the stock light
        st = snapshot(bars, direction, j["levels_color"])         # type: ignore[arg-type]
        from qt.calc.traffic_light import combine
        tl["stock"] = asdict(st)
        tl["combined"] = combine(st.overall, tl["sector"]["overall"], tl["market"]["overall"])
    cat = idea.catalysts(s, y.news(sym), as_of)
    price = bars[-1].close
    ext = max(b.high for b in bars) if direction == "long" else min(b.low for b in bars)
    levels = candidate_levels(bars, price, ("month", "quarter", "year"))
    k = float(j.get("k", load_policy().stop.k_default))
    stop = T.stop_price(price, adr(bars), k, direction)                       # type: ignore[arg-type]
    targets = _target_options(price, direction, ext, val, levels)
    for t in targets:
        t["rr"] = T.reward_risk(price, stop, t["price"], direction)          # type: ignore[arg-type]
        t["move_pct"] = T.pct_move(price, t["price"])
    chosen = next((t for t in targets if t["source"] == j.get("target_source")), None)
    if chosen is None and targets:
        usable = [t for t in targets if not t.get("unreliable")
                  and t.get("multiple_source") != "sector"] or targets
        chosen = max(usable, key=lambda t: t["rr"])
    unhealthy = (fv.get("health") or {}).get("state") == "unhealthy"
    plan = idea.trade_plan(sym, direction, bars, chosen["price"] if chosen else None,
                           chosen["source"] if chosen else None, k, etf, buckets, macro_bias, tl, row,
                           j.get("catalyst_expected"), unhealthy, j.get("catalyst_chain", ""))
    opts = []
    if chosen and j.get("options", True):
        opts = idea.option_candidates(y, sym, direction, price, chosen["price"], plan.get("horizon_months") or 3, as_of)
    ap = s.get("assetProfile", {})
    return dict(
        sym=sym, etf=etf, direction=direction, row=row, quote=q,
        profile=dict(name=q["name"], industry=ap.get("industry"), sector=ap.get("sector"), country=ap.get("country"),
                     employees=ap.get("fullTimeEmployees"), website=ap.get("website"),
                     summary=ap.get("longBusinessSummary") or ""),
        fin_data={k_: raw(s.get("financialData", {}), k_) for k_ in (
            "grossMargins", "operatingMargins", "profitMargins", "revenueGrowth", "earningsGrowth", "returnOnEquity",
            "debtToEquity", "totalCash", "totalDebt", "currentRatio", "freeCashflow")},
        fundamentals=fv, valuation=val, timing=tl, catalysts=cat, extreme=ext, levels=levels, targets=targets,
        plan=plan, options=opts, chart=idea.chart(bars), sector_avg=avg, judgment=j)


def classify(d: dict) -> tuple[str, str]:
    """The trade list bucket from code facts only: enter (green, R:R ok, rules engine not blocking),
    conditional (orange with R:R ok: waits for the trigger), watch, reject (rules engine blocks)."""
    v = d["plan"]["verdict"]["status"]
    tl = d["timing"]["combined"]
    rr = d["plan"].get("rr") or 0
    ok_rr = rr >= load_policy().reward_risk.min_ratio
    if d["judgment"].get("decision") in ("no", "watch_only"):
        return ("watch" if d["judgment"]["decision"] == "watch_only" else "reject"), d["judgment"].get("why", "")
    if v == "blocked":
        return "reject", "מנוע הכללים חוסם: " + "; ".join(f["rule_id"] for f in d["plan"]["verdict"]["findings"]
                                                             if f["severity"] == "block")
    if tl == "green" and ok_rr:
        return "enter", ""
    if tl == "orange" and ok_rr:
        return "conditional", "כתום: כניסה רק בטריגר"
    if tl == "red":
        return "watch", "רמזור אדום"
    return "watch", f"יחס סיכוי-סיכון {rr:.1f}, מתחת ל-{load_policy().reward_risk.min_ratio:.0f}"


def run(as_of: date, work: Path, manual_path: Path, judgments_path: Path, cache: Path, fred) -> dict[str, Any]:
    y = Yahoo(cache)
    u = _cached(work / "universe.pkl", lambda: U.fetch(y), binary=True)
    breadth = U.breadth_pct(u["quotes"])
    spx = y.bars("^GSPC", as_of - timedelta(days=3650), as_of)
    macro = _cached(work / "macro.json", lambda: macro_stage.run(fred, manual_path.read_text(), spx, as_of, breadth))
    st = macro["stage2"]["stance"]
    sc = {x["name"]: x for x in macro["stage2"]["scores"]}
    phase, phase_flags = cycle_phase(sc["ism_pmi"]["cycle_quadrant"], (sc.get("permits") or {}).get("cycle_quadrant"))
    rows = _rows_by_etf(u)
    sectors = _cached(work / "sectors.json", lambda: sector_screen.run(y, rows, phase, phase_flags, st["zone"], as_of))
    screens = _cached(work / "screens.json", lambda: sector_screen.screens(rows, sectors["long"], sectors["short"]))
    timing = _cached(work / "timing.json", lambda: stage_timing(y, screens, as_of))

    J = yaml.safe_load(judgments_path.read_text()) if judgments_path.exists() else {}
    bias = st.get("bias") or "neutral"
    buckets = {e: ("long" if e in sectors["long"] else "short" if e in sectors["short"] else "neutral")
               for e in sectors["scorecard"]}
    ideas = {}
    for sym, j in (J.get("ideas") or {}).items():
        etf = j["etf"]
        ideas[sym] = deep_dive(y, u, screens, sym, etf, j["direction"], j, bias, buckets, as_of)
        ideas[sym]["bucket"], ideas[sym]["bucket_reason"] = classify(ideas[sym])

    surv = sum(len(s["long"]) + len(s["short"]) for s in screens.values())
    in_sectors = sum(s["n"] for s in screens.values())
    funnel = [
        dict(stage="universe", label="יקום: מניות ארה\"ב בבורסות NYSE/NASDAQ מעל רצפת השווי", n=len(u["quotes"])),
        dict(stage="sectors", label=f"בסקטורים שנבחרו ({', '.join(sectors['long'] + sectors['short'])})", n=in_sectors),
        dict(stage="screen", label="עברו את סינון S06 (פרופיל צמיחה/תמחור)", n=surv),
        dict(stage="timing", label="רמזור S09 (ירוק / כתום / אדום)",
             n=surv, split=dict(Counter(t.get("combined", "error") for t in timing))),
        dict(stage="deep_dive", label="נבחרו ל-deep dive", n=len(ideas)),
        dict(stage="trades", label="מוכנות לכניסה / מותנות טריגר",
             n=sum(i["bucket"] in ("enter", "conditional") for i in ideas.values()),
             split=dict(Counter(i["bucket"] for i in ideas.values()))),
    ]
    return dict(
        meta=dict(as_of=as_of.isoformat(), generated_at=datetime.now(timezone.utc).isoformat(timespec="minutes"),
                  equity=idea.EQUITY, playbook_version=load_policy().playbook_version, history_years=HISTORY_DAYS // 365,
                  judged_by=J.get("judged_by", "missing"), breadth_pct=breadth),
        macro=macro, sectors=sectors, screens=screens, timing=timing, ideas=ideas, funnel=funnel,
        judgments={k: v for k, v in J.items() if k != "ideas"})
