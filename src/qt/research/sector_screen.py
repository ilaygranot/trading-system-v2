"""Stages 4-6 for the research run: the sector scorecard on live ETF prices and consensus growth of every
stock in each sector, the industry map, and the stage-6 screen inside the chosen sectors."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta
from typing import Any

from qt import sectors as S
from qt.policy import load_policy
from qt.research.yahoo import Yahoo
from qt.screening import StockRow, run as screen


def _closes(y: Yahoo, sym: str, as_of: date, days: int = 365 * 4) -> tuple[list[str], list[float]]:
    bars = y.bars(sym, as_of - timedelta(days=days), as_of)
    return [b.day.isoformat() for b in bars], [b.close for b in bars]


def _aligned(a: tuple[list[str], list[float]], b: tuple[list[str], list[float]]) -> tuple[list[float], list[float]]:
    bm = dict(zip(*b))
    pairs = [(v, bm[d]) for d, v in zip(*a) if d in bm]
    return [p[0] for p in pairs], [p[1] for p in pairs]


def _monthly(dates: list[str], closes: list[float]) -> list[float]:
    out: dict[str, float] = {}
    for d, c in zip(dates, closes):
        out[d[:7]] = c
    return list(out.values())


def run(y: Yahoo, rows_by_etf: dict[str, list[StockRow]], phase: str, phase_flags: list[str], zone: str,
        as_of: date) -> dict[str, Any]:
    s = load_policy().sector_scorecard
    spy = _closes(y, s.benchmark, as_of)
    etfs = list(s.universe)
    prices = {e: _closes(y, e, as_of) for e in etfs}
    rs, mas, growth, conf, rel = {}, {}, {}, {}, {}
    for e in etfs:
        a, b = _aligned(prices[e], spy)
        rs[e] = S.rs_perf(a, b)
        mas[e] = S.rs_vs_mas(a, b)
        growth[e] = S.group_growth(rows_by_etf.get(e, []))
        rel3y = (a[-1] / b[-1]) / (a[-757] / b[-757]) * 100 - 100 if len(a) > 757 else None
        rel[e] = dict(rel_3y=rel3y, rel_3m=rs[e]["3m"], rel_1m=rs[e]["1m"])
        conf[e] = [f"rs_below_{k.removeprefix('above_')}" for k, v in mas[e].items() if v is False]
    card = S.scorecard(rs, growth, phase, zone, confirmations=conf)          # type: ignore[arg-type]
    # cycle pairs (S04-R32/R33) on monthly closes
    extra = {x: _closes(y, x, as_of) for x in ("SMH", "TLT", "IWM")}
    allp = {**prices, **extra, "SPY": spy}
    trends = {}
    for pair in S.PAIR_GROWTH_DIRECTION:
        a, b = _aligned(allp[pair[0]], allp[pair[1]])
        da = [d for d in allp[pair[0]][0] if d in set(allp[pair[1]][0])]
        trends[pair] = S.ratio_trend(_monthly(da, a), _monthly(da, b))
    pairs = S.cycle_pairs_check(trends, phase)                                 # type: ignore[arg-type]
    industries = {}
    for e in card.long + card.short:
        industries[e] = [asdict(v) for v in S.industry_map(rows_by_etf.get(e, []), growth[e])][:12]
    return dict(
        phase=phase, phase_flags=phase_flags, zone=zone,
        scorecard={e: asdict(v) for e, v in card.sectors.items()}, long=card.long, short=card.short,
        neutral=card.neutral, flags=card.flags, rs_mas=mas, relative=rel,
        pairs=dict(trends={"/".join(k): v for k, v in trends.items()}, **pairs), industries=industries,
        charts={e: dict(dates=prices[e][0][-260:], rs=[a / b for a, b in zip(*_aligned(prices[e], spy))][-260:])
                for e in etfs})


def screens(rows_by_etf: dict[str, list[StockRow]], longs: list[str], shorts: list[str]) -> dict[str, Any]:
    out = {}
    for e in dict.fromkeys(longs + shorts):
        r = screen(rows_by_etf.get(e, []))
        out[e] = dict(
            averages=asdict(r.averages) | {"sg1_threshold": r.averages.sg1_threshold}, n=len(rows_by_etf.get(e, [])),
            long=[_cand(c) for c in r.long] if e in longs else [],
            short=[_cand(c) for c in r.short] if e in shorts else [],
            tracks=r.tracks, rejected_count=len(r.rejected),
            rescue=[_cand(c) | dict(failed=c.failed, patterns=c.patterns) for c in r.rescue],
            rejected_by_rule=_count_rules(r.rejected))
    return out


def backfill_cand(rows: list[StockRow], sym: str, side: str) -> dict:
    """A watchlist stock with no full analysis (Ilay 25.10: every stock on the list has one), as its group's screen
    sees it this week: its own row when it passed, else the same fields with the rules it failed, so the cards
    judge it on this week's facts and the failure is said, not hidden."""
    from qt.screening import Candidate, classify_long, classify_short, compute, universe_failures
    r = screen(rows)
    hit = next((c for c in (r.long if side == "long" else r.short) if c.ticker == sym), None)
    if hit is not None:
        return _cand(hit) | {"backfill_failed": []}
    m = compute(next(x for x in rows if x.ticker == sym))
    prof = None
    if m.track != "missing_data":
        a = r.averages
        prof = classify_long(m.eg1, m.eg2, a.eg1, a.eg2, m.row.f0, m.row.f1, m.row.f2)[0] if side == "long" \
            else classify_short(m.eg1, m.eg2, a.eg1, a.eg2)
    failed = universe_failures(m.row, side) + list(r.rejected.get(sym) or []) + ([m.track] if m.track else [])
    return _cand(Candidate(sym, side, prof, list(m.flags), m)) | {"backfill_failed": list(dict.fromkeys(failed))}


def _cand(c) -> dict:
    m = c.metrics
    return dict(ticker=c.ticker, side=getattr(c, "side", None), profile=getattr(c, "profile", None),
                tags=getattr(c, "tags", []), triple=getattr(c, "triple_filter", False),
                conflicts=getattr(c, "conflicts", []), eg1=m.eg1, eg2=m.eg2, sg1=m.sg1, pe1=m.pe1, pe2=m.pe2, ps1=m.ps1, peg=m.peg,
                flags=m.flags, market_cap=m.row.market_cap, price=m.row.price, industry=m.row.industry,
                f0=m.row.f0, f1=m.row.f1, f2=m.row.f2, short_float=m.row.short_float)


def _count_rules(rej: dict[str, list[str]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for rules in rej.values():
        for r in rules[:1]:
            out[r] = out.get(r, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
