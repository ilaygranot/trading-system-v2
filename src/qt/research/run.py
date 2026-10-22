"""One full research run, stages 0-10, into a dossier the report renders (qt research).

universe -> macro (S01-S03) -> sectors (S04-S05) -> screen (S06) -> traffic light on every survivor (S09)
-> deep dive for the shortlisted ideas (S07-S08) -> trade plan, options and the rules engine (S10-S11)
-> data verification (qt.research.verify: SEC, a second close, sanity, freshness, sources).

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

from qt.calc import indicators as I
from qt.calc import trade as T
from qt.calc.volatility import adr
from qt.policy import load_policy
from qt.research import idea, macro_stage, sector_screen, universe as U, verify
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


TIME_STOP_WEEKS = 4                                               # not from course: the default expiry of a trigger
FACTOR_REFS = ("SPY", "USO", "GLD")                               # market, oil, gold (not from course: the proxies)


def exit_plan(p: dict, direction: str, bucket: str, as_of: date, next_earnings: str | None, j: dict) -> dict[str, Any]:
    """What happens after entry and before it, from code: the 60/40 trail level (S12-R11 at 3R, D-S12-4), the
    target as a partial-take zone (S12-R13), the trigger's expiry (not from course) and whether earnings land
    before it. A conditional trade is figured from its trigger level with the same stop width; the fill recomputes it."""
    sg = 1 if direction == "long" else -1
    r = abs(p["entry"] - p["stop"])
    trig = (j.get("trigger") or {}).get("level")
    e = float(trig) if bucket == "conditional" and trig else p["entry"]
    t = p.get("target")
    out: dict[str, Any] = dict(r=r, entry=e, stop=e - sg * r, trail_from=e + sg * 3 * r, trail_stop=e + sg * 0.6 * 3 * r, target=t,
                               target_before_trail=bool(t and sg * (t - (e + sg * 3 * r)) < 0))
    weeks = int(j.get("time_stop_weeks") or TIME_STOP_WEEKS)
    expiry = as_of + timedelta(weeks=weeks) if bucket == "conditional" else None
    out.update(time_stop_weeks=weeks if expiry else None, time_stop_date=expiry.isoformat() if expiry else None)
    if next_earnings:
        ne = date.fromisoformat(next_earnings)
        horizon_end = as_of + timedelta(days=round((p.get("horizon_months") or 3) * 30.4))
        out.update(next_earnings=next_earnings, days_to_earnings=(ne - as_of).days,
                   earnings_before_expiry=bool(expiry and ne <= expiry), earnings_in_horizon=ne <= horizon_end)
    return out


def _monthly_returns(y: Yahoo, sym: str, as_of: date) -> dict[str, float]:
    m = I.resample(y.bars(sym, as_of - timedelta(days=365 * 3 + 62), as_of), "month")
    return {m[i].day.strftime("%Y-%m"): m[i].close / m[i - 1].close - 1 for i in range(1, len(m))}


def _corr(a: dict[str, float], b: dict[str, float], months: int) -> float | None:
    keys = sorted(set(a) & set(b))[-months:]
    if len(keys) < min(months, 12) - 1:
        return None
    xs, ys = [a[k] for k in keys], [b[k] for k in keys]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxy = sum((x - mx) * (y_ - my) for x, y_ in zip(xs, ys))
    sxx, syy = sum((x - mx) ** 2 for x in xs), sum((y_ - my) ** 2 for y_ in ys)
    return sxy / (sxx * syy) ** 0.5 if sxx and syy else None


def portfolio_fit(y: Yahoo, ideas: dict, as_of: date) -> dict[str, Any]:
    """S11-R17: correlation of monthly returns, 12 months and 3 years, between the trades on the list and
    against market, oil and gold; S11-R2 sector count; the combined size and risk if every trade fills."""
    syms = [s for s, i in ideas.items() if i["bucket"] in ("enter", "conditional")]
    if not syms:
        return {}
    refs = list(dict.fromkeys([ideas[s]["etf"] for s in syms] + list(FACTOR_REFS)))
    rets = {s: _monthly_returns(y, s, as_of) for s in syms + refs}
    pairs = [dict(a=a, b=b, c12=_corr(rets[a], rets[b], 12), c36=_corr(rets[a], rets[b], 36))
             for k, a in enumerate(syms) for b in syms[k + 1:]]
    vs_refs = {s: {r: dict(c12=_corr(rets[s], rets[r], 12), c36=_corr(rets[s], rets[r], 36)) for r in refs} for s in syms}
    sz = [ideas[s]["plan"]["sizing"] for s in syms]
    return dict(syms=syms, refs=refs, pairs=pairs, vs_refs=vs_refs, strong=0.5,
                sectors=dict(Counter(ideas[s]["etf"] for s in syms)),
                total_size_pct=sum(x["allowed_pct"] for x in sz), total_risk_pct=sum(x["risk_pct_of_equity"] for x in sz))


def run(as_of: date, work: Path, manual_path: Path, judgments_path: Path | None, cache: Path, fred,
        judge=None, manual_text: str | None = None) -> dict[str, Any]:
    """judgments_path: a hand-written judgments file. judge: the agents instead (qt.research.judge.Judge); their
    answers are saved under work/ like every other stage, so a resumed run does not ask again."""
    y = Yahoo(cache)
    u = _cached(work / "universe.pkl", lambda: U.fetch(y), binary=True)
    breadth = U.breadth_pct(u["quotes"])
    spx = y.bars("^GSPC", as_of - timedelta(days=3650), as_of)
    macro = _cached(work / "macro.json", lambda: macro_stage.run(
        fred, manual_text if manual_text is not None else manual_path.read_text(), spx, as_of, breadth))
    st = macro["stage2"]["stance"]
    sc = {x["name"]: x for x in macro["stage2"]["scores"]}
    phase, phase_flags = cycle_phase(sc["ism_pmi"]["cycle_quadrant"], (sc.get("permits") or {}).get("cycle_quadrant"))
    rows = _rows_by_etf(u)
    sectors = _cached(work / "sectors.json", lambda: sector_screen.run(y, rows, phase, phase_flags, st["zone"], as_of))
    screens = _cached(work / "screens.json", lambda: sector_screen.screens(rows, sectors["long"], sectors["short"]))
    timing = _cached(work / "timing.json", lambda: stage_timing(y, screens, as_of))

    bias = st.get("bias") or "neutral"
    buckets = {e: ("long" if e in sectors["long"] else "short" if e in sectors["short"] else "neutral")
               for e in sectors["scorecard"]}
    if judge is not None:
        stages = dict(as_of=as_of, macro=macro, sectors=sectors, screens=screens, timing=timing)
        J = _cached(work / "judgments.json", lambda: judge(stages, lambda sym, etf, dr, j: deep_dive(
            y, u, screens, sym, etf, dr, j, bias, buckets, as_of)))
    else:
        J = yaml.safe_load(judgments_path.read_text()) if judgments_path and judgments_path.exists() else {}
    ideas = {}
    for sym, j in (J.get("ideas") or {}).items():
        etf = j["etf"]
        ideas[sym] = deep_dive(y, u, screens, sym, etf, j["direction"], j, bias, buckets, as_of)
        ideas[sym]["bucket"], ideas[sym]["bucket_reason"] = classify(ideas[sym])
        ideas[sym]["exit"] = exit_plan(ideas[sym]["plan"], j["direction"], ideas[sym]["bucket"], as_of,
                                       ideas[sym]["catalysts"].get("next_earnings"), j)
    fit = portfolio_fit(y, ideas, as_of)

    surv = sum(len(s["long"]) + len(s["short"]) for s in screens.values())
    in_sectors = sum(s["n"] for s in screens.values())
    n_ok = sum(1 for t in timing if not t.get("error") and t.get("combined") in ("green", "orange"))
    funnel = [
        dict(stage="universe", label="יקום: מניות ארה\"ב בבורסות NYSE/NASDAQ מעל רצפת השווי", n=len(u["quotes"])),
        dict(stage="sectors", label=f"בסקטורים שנבחרו ({', '.join(sectors['long'] + sectors['short'])})", n=in_sectors),
        dict(stage="screen", label="עברו את סינון S06 (פרופיל צמיחה/תמחור)", n=surv),
        dict(stage="timing", label="רמזור S09 (ירוק / כתום / אדום)",
             n=surv, split=dict(Counter(t.get("combined", "error") for t in timing))),
        dict(stage="deep_dive", label=f"נבחרו ל-deep dive (מתוך {n_ok} ירוקים וכתומים)", n=len(ideas)),
        dict(stage="trades", label="מוכנות לכניסה / מותנות טריגר",
             n=sum(i["bucket"] in ("enter", "conditional") for i in ideas.values()),
             split=dict(Counter(i["bucket"] for i in ideas.values()))),
    ]
    d = dict(
        meta=dict(as_of=as_of.isoformat(), generated_at=datetime.now(timezone.utc).isoformat(timespec="minutes"),
                  equity=idea.EQUITY, playbook_version=load_policy().playbook_version, history_years=HISTORY_DAYS // 365,
                  judged_by=J.get("judged_by", "missing"), breadth_pct=breadth),
        macro=macro, sectors=sectors, screens=screens, timing=timing, ideas=ideas, funnel=funnel, portfolio=fit,
        judgments={k: v for k, v in J.items() if k != "ideas"})
    from qt.data.store import Store
    d["verification"] = _cached(work / "verify.json",
                                lambda: verify.run(d, **verify.default_sources(Store(str(work / "verify.sqlite")))))
    return d
