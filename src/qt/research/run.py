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
import os
import pickle
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta, timezone
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from qt.calc import indicators as I
from qt.calc import trade as T
from qt.calc.volatility import adr
from qt.data.http import redact
from qt.policy import load_policy
from qt.research import idea, macro_stage, sector_screen, universe as U, verify
from qt.research.judge import MAX_DEEP, candidates, unexplained_greens
from qt.research.yahoo import Yahoo, raw
from qt.sectors import cycle_phase
from qt.timing import candidate_levels, snapshot

HISTORY_DAYS = 365 * 8
MAX_PEERS = 6                                     # peers per deep-dive stock (S08-J25; not from course: cost)
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


def _stage(work: Path, state, name: str, fn, binary: bool = False):
    """_cached, and with a state (qt.research.state) the stage is kept in the database too: a run in a new
    container takes it from there instead of asking Yahoo again."""
    if state is None:
        return _cached(work / name, fn, binary)
    key = f"stage:{name}"
    got = (state.get_blob if binary else state.get)(key)
    if got is not None:
        return _cached(work / name, lambda: got, binary)
    out = _cached(work / name, fn, binary)
    (state.put_blob if binary else state.put)(key, out)
    return out


def _rows_by_etf(u: dict) -> dict[str, list]:
    out: dict[str, list] = {}
    for sym, q in u["quotes"].items():
        s = u["summaries"].get(sym, {})
        if "error" in s or not q.get("market_cap"):
            continue
        out.setdefault(q["etf"], []).append(U.row(q, s))
    return out


def price_moves(y: Yahoo, sym: str, as_of: date) -> dict:
    """DC-08's input: the price change over 3, 6 and 12 months (about 63, 126 and 252 sessions)."""
    c = [b.close for b in y.bars(sym, as_of - timedelta(days=400), as_of)]   # a failure fails DC-08's card (completeness)
    return {f"{m}m": round((c[-1] / c[-1 - n] - 1) * 100, 1) for m, n in ((3, 63), (6, 126), (12, 252)) if len(c) > n}


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


def peers_for(u: dict, screens: dict, sym: str, etf: str, on_list: set[str] | Callable[[], set[str]] = frozenset(),
              as_of: date | None = None, n: int = MAX_PEERS) -> list[dict]:
    """S08-J25: the stock's peers from the screen of its sector (long and short rows), same industry first, with the
    next report from the summaries already fetched (no extra HTTP) and the screen row's numbers for the article's
    peers table (S07-R3 question 2). on_list: the ideas and the watchlist."""
    scr = screens.get(etf) or {}
    summ = u.get("summaries") or {}
    ind = ((summ.get(sym) or {}).get("assetProfile") or {}).get("industry")
    rows = [c for c in (scr.get("long") or []) + (scr.get("short") or []) if c.get("ticker") and c["ticker"] != sym]
    rows = list({c["ticker"]: c for c in rows}.values())
    same = [c for c in rows if ind and ((summ.get(c["ticker"]) or {}).get("assetProfile") or {}).get("industry") == ind]
    lst = on_list() if callable(on_list) else on_list
    out = []
    for c in (same + [c for c in rows if c not in same])[:n]:
        p = c["ticker"]
        ce = (summ.get(p) or {}).get("calendarEvents") or {}
        ds = sorted(x for x in (raw_date(e) for e in ((ce.get("earnings") or {}).get("earningsDate") or [])) if x)
        ds = [x for x in ds if as_of is None or x > as_of.isoformat()] or ds[-1:]
        out.append({"sym": p, "etf": etf, "same": "industry" if c in same else "etf", "on_list": p in lst,
                    "next_earnings": ds[0] if ds else None, "last_results_8k": None,
                    "name": ((u.get("quotes") or {}).get(p) or {}).get("name"),
                    **{k: c.get(k) for k in PEER_COLS}})
    return out


PEER_COLS = ("industry", "side", "profile", "market_cap", "sg1", "eg1", "eg2", "pe1", "ps1")   # the screen row (S06)


def raw_date(x: Any) -> str | None:
    """A Yahoo date field ({raw: epoch, fmt: 'YYYY-MM-DD'} or an epoch or an ISO string) as YYYY-MM-DD."""
    if isinstance(x, dict):
        if x.get("fmt"):
            return str(x["fmt"])[:10]
        x = x.get("raw")
    if isinstance(x, (int, float)):
        return datetime.fromtimestamp(x, tz=UTC).date().isoformat()
    if isinstance(x, str) and len(x) >= 10:
        return x[:10]
    return None


def _rows_with_ids(rows: list | None) -> list[dict] | None:
    """Catalyst rows for the horizon code; a hand-written row has no id or timing."""
    if not rows:
        return None
    return [{"id": f"J{n:02d}", "timing": "expected", **r} for n, r in enumerate(rows, 1) if isinstance(r, dict)]


def deep_dive(y: Yahoo, u: dict, screens: dict, sym: str, etf: str, direction: str, j: dict, macro_bias: str,
              buckets: dict, as_of: date, docs: Callable[[str], dict] | None = None,
              peers: Callable[[str, str], list] | None = None) -> dict[str, Any]:
    """docs: the SEC extracts for the stock (qt.data.filings, memoised per run); peers: its peers (S08-J25)."""
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
                           j.get("catalyst_expected"), unhealthy, j.get("catalyst_chain", ""),
                           catalysts=_rows_with_ids(j.get("catalysts")), as_of=as_of, next_earnings=cat.get("next_earnings"),
                           heat=j.get("heat") or None, news_adverse=bool(j.get("news_adverse")))
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
        plan=plan, options=opts, chart=idea.chart(bars), tech=idea.tech(bars, eb, spy), sector_avg=avg, judgment=j,
        documents=docs(sym) if docs else None, peers=peers(sym, etf) if peers else [])


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


def documents_source(sec, as_of: date, memo: dict[str, dict], calls=None) -> Callable[[str], dict]:
    """The SEC extracts per deep-dive stock, fetched once per symbol per run (both deep-dive passes share the memo).
    Without SEC_CONTACT the stock gets {"errors": ["SEC_CONTACT not set"]} and the run goes on with Yahoo events.
    calls: the transcript source (qt.data.transcripts.RoicTranscripts); its latest call before as_of goes under
    "transcript", with every turn for DC-26 (strip_turns drops them once the agents are done)."""
    def get(sym: str) -> dict:
        if sym not in memo:
            try:
                memo[sym] = sec.documents(sym, as_of)
            except Exception as e:                                       # noqa: BLE001 - documents() should never raise
                memo[sym] = {"symbol": sym, "transcript": None, "errors": [f"{type(e).__name__}: {e}"[:300]]}
            if calls is not None and calls.ok():
                try:
                    memo[sym]["transcript"] = calls.latest(sym, as_of)
                except Exception as e:                                   # noqa: BLE001 - the SEC documents stand alone
                    memo[sym].setdefault("errors", []).append(f"transcript: {redact(str(e))[:300]}")
        return memo[sym]
    return get


def strip_turns(memo: dict[str, dict]) -> None:
    """The call's turns are for DC-26 only; the dossier keeps the call's date, source and link (and DC-26's answers)."""
    for docs in memo.values():
        tr = docs.get("transcript")
        if tr and tr.get("turns"):
            tr["turns_n"] = len(tr.pop("turns"))


def _watchlist_symbols(watchlist: dict | None) -> set[str]:
    if watchlist is None and os.environ.get("QT_WATCHLIST") and Path(os.environ["QT_WATCHLIST"]).exists():
        watchlist = yaml.safe_load(Path(os.environ["QT_WATCHLIST"]).read_text(encoding="utf-8"))
    return {e.get("symbol") for e in (watchlist or {}).get("entries") or [] if isinstance(e, dict)}


def run(as_of: date, work: Path, manual_path: Path, judgments_path: Path | None, cache: Path, fred,
        judge=None, manual_text: str | None = None, sec=None, watchlist: dict | None = None, calls=None,
        state=None) -> dict[str, Any]:
    """judgments_path: a hand-written judgments file. judge: the agents instead (qt.research.judge.Judge); their
    answers are saved under work/ like every other stage, so a resumed run does not ask again. sec: the SEC filings
    source (default qt.data.filings.SECFilings on SEC_CONTACT); calls: the earnings-call transcripts (default
    qt.data.transcripts.RoicTranscripts on ROIC_API_KEY); watchlist: the current watchlist file (else
    QT_WATCHLIST) for the peers' on_list. state: qt.research.state.State, the stages kept in the database as well,
    so a run in a new container continues from them."""
    y = Yahoo(cache)

    def cached(name: str, fn, binary: bool = False):
        return _stage(work, state, name, fn, binary)
    run_events: list[dict] = []
    if sec is None:
        from qt.data.filings import SECFilings
        from qt.data.store import Store
        sec = SECFilings(Store(str(work / "sec.sqlite")), contact=os.environ.get("SEC_CONTACT"))
    if hasattr(sec, "ok") and not sec.ok():
        run_events.append({"level": "warn", "where": "documents", "text": "SEC_CONTACT not set: no SEC 8-K, results "
                           "release or risk factors; catalysts from Yahoo only"})
    if calls is None:
        from qt.data.store import Store
        from qt.data.transcripts import RoicTranscripts
        calls = RoicTranscripts(Store(str(work / "transcripts.sqlite")))
    if not calls.ok():
        run_events.append({"level": "warn", "where": "documents", "text": "ROIC_API_KEY not set: no earnings-call "
                           "transcript; management's words from the SEC results release only"})
    docs_memo: dict[str, dict] = {}
    docs = documents_source(sec, as_of, docs_memo, calls)
    on_wl = _watchlist_symbols(watchlist)
    u = cached("universe.pkl", lambda: U.fetch(y), binary=True)   # a resume does not ask Yahoo for it again
    breadth = U.breadth_pct(u["quotes"])
    spx = y.bars("^GSPC", as_of - timedelta(days=3650), as_of)
    macro = cached("macro.json", lambda: macro_stage.run(
        fred, manual_text if manual_text is not None else manual_path.read_text(), spx, as_of, breadth))
    st = macro["stage2"]["stance"]
    sc = {x["name"]: x for x in macro["stage2"]["scores"]}
    phase, phase_flags = cycle_phase(sc["ism_pmi"]["cycle_quadrant"], (sc.get("permits") or {}).get("cycle_quadrant"))
    rows = _rows_by_etf(u)
    sectors = cached("sectors.json", lambda: sector_screen.run(y, rows, phase, phase_flags, st["zone"], as_of))
    screens = cached("screens.json", lambda: sector_screen.screens(rows, sectors["long"], sectors["short"]))
    if judge is not None and hasattr(judge, "rescue"):            # DC-08 before the traffic light: rescued ideas get one
        screens = cached("screens_dc08.json", lambda: judge.rescue(screens, macro, sectors, as_of,
                                                                           lambda s: price_moves(y, s, as_of)))
    timing = cached("timing.json", lambda: stage_timing(y, screens, as_of))

    bias = st.get("bias") or "neutral"
    buckets = {e: ("long" if e in sectors["long"] else "short" if e in sectors["short"] else "neutral")
               for e in sectors["scorecard"]}
    hand: dict = {}
    if judge is None:
        hand = yaml.safe_load(judgments_path.read_text()) if judgments_path and judgments_path.exists() else {}

    def on_list() -> set[str]:                       # the ideas so far (the judge's picks, or the file) + the watchlist
        picked = (getattr(judge, "selection", None) or {}).get("picked") or []
        return set(picked) | set(hand.get("ideas") or {}) | on_wl

    def peers(sym: str, etf: str) -> list[dict]:
        return peers_for(u, screens, sym, etf, on_list, as_of)
    news = None
    if judge is not None:
        from qt.research import macro_story as MS
        news = state.get("stage:macro_news.json") if state is not None else None
        if not news:                                   # kept only when whole: an empty or partial fetch is retried
            news = MS.fetch_news(as_of)
            if state is not None and news["items"] and not news["errors"]:
                state.put("stage:macro_news.json", news)
        stages = dict(as_of=as_of, macro=macro, sectors=sectors, screens=screens, timing=timing, macro_news=news,
                      watchlist=sorted(on_wl))
        J = _cached(work / "judgments.json", lambda: judge(stages, lambda sym, etf, dr, j: deep_dive(
            y, u, screens, sym, etf, dr, j, bias, buckets, as_of, docs=docs, peers=peers)))
        if J.get("incomplete"):                         # cut by the budget: the next run asks for what is missing
            (work / "judgments.json").unlink(missing_ok=True)
    else:
        J = hand
    strip_turns(docs_memo)
    missing = unexplained_greens(timing, set(J.get("ideas") or {}), (J.get("A3") or {}).get("skipped"))
    if missing:                                         # Ilay 22.10: a green stock is analysed or skipped with a reason
        raise ValueError(f"green stocks with no deep dive and no reason under A3.skipped: {', '.join(missing)}")
    ideas = {}
    for sym, j in (J.get("ideas") or {}).items():
        etf = j["etf"]
        ideas[sym] = deep_dive(y, u, screens, sym, etf, j["direction"], j, bias, buckets, as_of, docs=docs, peers=peers)
        ideas[sym]["bucket"], ideas[sym]["bucket_reason"] = classify(ideas[sym])
        ideas[sym]["exit"] = exit_plan(ideas[sym]["plan"], j["direction"], ideas[sym]["bucket"], as_of,
                                       ideas[sym]["catalysts"].get("next_earnings"), j)
    assert len(docs_memo) <= MAX_DEEP + len(J.get("ideas") or {}), "SEC documents fetched beyond the deep-dive cap"
    fit = portfolio_fit(y, ideas, as_of)

    surv = sum(len(s["long"]) + len(s["short"]) for s in screens.values())
    in_sectors = sum(s["n"] for s in screens.values())
    n_ok = len(candidates(timing))
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
                  judged_by=J.get("judged_by", "missing"), breadth_pct=breadth, events=run_events,
                  universe={"n": len(u["quotes"]), "errors": {s: x["error"] for s, x in u["summaries"].items()
                                                              if isinstance(x, dict) and "error" in x}},
                  news=None if news is None else {"n": len(news["items"]), "errors": news["errors"],
                                                  "empty": news.get("empty") or []},
                  documents={"fetched": len(docs_memo),
                             "with_errors": sorted(s for s, x in docs_memo.items() if x.get("errors"))}),
        macro=macro, sectors=sectors, screens=screens, timing=timing, ideas=ideas, funnel=funnel, portfolio=fit,
        judgments={k: v for k, v in J.items() if k != "ideas"})
    from qt.data.store import Store
    check = lambda: verify.run(d, **verify.default_sources(Store(str(work / "verify.sqlite"))))  # noqa: E731
    d["verification"] = check() if J.get("incomplete") else cached("verify.json", check)   # cut: the ideas will change
    return d
