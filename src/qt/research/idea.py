"""Stages 7-10 for one idea in the research run: the deep-dive numbers from the statements, the catalyst
calendar and analyst actions, the traffic light with sector and market, the trade plan (stop, target, R:R,
size, horizon, option candidates) and the rules engine's verdict. Code computes; the judgment fields
(story, thesis, growth engines, levels color, trigger) come from the agent cards and are merged later."""
from __future__ import annotations

import math
import statistics
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from qt import deepdive as D
from qt.calc import indicators as I
from qt.calc import trade as T
from qt.calc.portfolio import Portfolio
from qt.calc.traffic_light import combine
from qt.calc.volatility import adr, daily_range_pct
from qt.policy import load_policy
from qt.research import catalyst_chain as CC
from qt.research.universe import trend
from qt.research.yahoo import Yahoo, raw
from qt.rules.checks import Context, TradeProposal, evaluate
from qt.timing import snapshot

EQUITY = 100_000.0          # notional account for sizing in the report (Ilay's real equity replaces it)


def _q(f: dict, name: str, n: int | None = None) -> list[tuple[str, float]]:
    v = f.get(name, [])
    return v[-n:] if n else v


def _vals(xs: list[tuple[str, float]]) -> list[float]:
    return [v for _, v in xs]


def _ttm(f: dict, name: str, back: int = 0) -> float | None:
    q = _q(f, f"quarterly{name}")
    if len(q) < 4 + back:
        return None
    w = q[len(q) - 4 - back: len(q) - back]
    return sum(v for _, v in w)


def fundamentals_view(f: dict, s: dict, row: dict, sector_avg: dict, direction: str) -> dict[str, Any]:
    out: dict[str, Any] = {"flags": [], "notes": []}
    ann = {k: _q(f, f"annual{k}", 5) for k in ("TotalRevenue", "GrossProfit", "OperatingIncome", "DilutedEPS",
                                               "OperatingCashFlow", "NetIncome", "FreeCashFlow")}
    out["annual"] = {k: v for k, v in ann.items() if v}
    lines = {k: _vals(v) for k, v in ann.items() if len(v) >= 2 and k in ("TotalRevenue", "GrossProfit", "OperatingIncome",
                                                                           "DilutedEPS", "OperatingCashFlow")}
    out["core_trend"] = D.core_trend(lines) if lines else {}
    qrev = _q(f, "quarterlyTotalRevenue")
    out["quarterly"] = {k: _q(f, f"quarterly{k}", 8) for k in ("TotalRevenue", "GrossProfit", "OperatingIncome",
                                                               "DilutedEPS", "FreeCashFlow")}
    # operating leverage and profit source on TTM vs the TTM before (S07-R9, R10)
    r1, r0 = _ttm(f, "TotalRevenue"), _ttm(f, "TotalRevenue", 4)
    c1, c0 = _ttm(f, "CostOfRevenue"), _ttm(f, "CostOfRevenue", 4)
    o1, o0 = _ttm(f, "OperatingIncome"), _ttm(f, "OperatingIncome", 4)
    out["ol_basis"] = "ttm"
    if r0 is None or c0 is None:                    # the provider keeps 5 quarters: the last two fiscal years
        ar, ac, ao = (_vals(_q(f, f"annual{k}", 2)) for k in ("TotalRevenue", "CostOfRevenue", "OperatingIncome"))
        if len(ar) == 2 and len(ac) == 2:
            (r0, r1), (c0, c1) = ar, ac
            o0, o1 = (ao[0], ao[1]) if len(ao) == 2 else (None, None)
            out["ol_basis"] = "annual"
    if r1 and r0 and c1 and c0:
        ol = D.operating_leverage(r0, r1, c0, c1)
        out["operating_leverage"] = asdict(ol)
        if ol.direction == "negative":
            out["flags"].append("negative_operating_leverage")
    if r1 and r0 and o1 is not None and o0 is not None:
        out["profit_source"] = D.profit_source(r0, r1, r0 - o0, r1 - o1)
    # acceleration: last two quarters' YoY revenue growth vs the multi-year average (S07-R11)
    if len(qrev) >= 5:
        yoy = [(qrev[i][1] / qrev[i - 4][1] - 1) * 100 for i in range(max(4, len(qrev) - 2), len(qrev))
               if qrev[i - 4][1]]
        av = _vals(ann["TotalRevenue"])
        avg = ((av[-1] / av[0]) ** (1 / (len(av) - 1)) - 1) * 100 if len(av) >= 3 and av[0] > 0 else None
        out["revenue_yoy_last2q"] = yoy
        out["revenue_cagr"] = avg
        if avg is not None and yoy:
            out["flags"] += D.acceleration(yoy, avg)
    # growth gap vs the sector over two years (S07-R12)
    if row.get("eg1") is not None and sector_avg.get("eg1") is not None:
        state, fl = D.gap_vs_sector((row["eg1"], row["eg2"]), (sector_avg["eg1"], sector_avg["eg2"]))
        out["gap_vs_sector"] = state
        out["flags"] += fl
    # health (S07-R7): gross profit YoY in the last two quarters, debt and interest a year apart
    gp = _q(f, "quarterlyGrossProfit")
    debt = _q(f, "quarterlyTotalDebt")
    if len(gp) >= 5 and len(debt) >= 5:
        gyoy = [(gp[i][1] / gp[i - 4][1] - 1) * 100 for i in range(max(4, len(gp) - 2), len(gp)) if gp[i - 4][1]]
        out["gross_profit_yoy"] = gyoy
        intr = _q(f, "quarterlyInterestExpense")
        h = D.health(gyoy, (debt[-5][1], debt[-1][1]), (intr[-5][1], intr[-1][1]) if len(intr) >= 5 else None,
                     direction)
        out["health"] = asdict(h)
    # FCF from operations (S07-R8)
    ocf, capex = _ttm(f, "OperatingCashFlow"), _ttm(f, "CapitalExpenditure")
    if ocf is not None and capex is not None:
        fcf, fl = D.fcf_from_operations(ocf, abs(capex))
        out["fcf_ttm"] = fcf
        out["flags"] += fl
    sh = _q(f, "quarterlyDilutedAverageShares")
    if len(sh) >= 5:
        out["shares"] = (sh[-5][1], sh[-1][1])
        out["shares_change_pct"] = (sh[-1][1] / sh[-5][1] - 1) * 100
        out["flags"] += D.capital_structure((sh[-5][1], sh[-1][1]))
    # net debt / EBITDA by the course formula (S07-R24) and its trend
    liab, cash, oi = (_q(f, "annualTotalLiabilitiesNetMinorityInterest"),
                      _q(f, "annualCashCashEquivalentsAndShortTermInvestments") or _q(f, "annualCashAndCashEquivalents"),
                      _q(f, "annualOperatingIncome"))
    nd = []
    cm = dict(cash)
    for (d_, l), (d2, o) in zip(liab[-4:], oi[-4:]):
        if d_ == d2 and o and d_ in cm:
            nd.append(D.net_debt_to_ebitda(l, cm[d_], o))
    if nd:
        out["net_debt_ebitda"] = nd
        st, fl = D.net_debt_trend(nd)
        out["net_debt_trend"] = st
        out["flags"] += fl
    ca, cl = _q(f, "quarterlyCurrentAssets"), _q(f, "quarterlyCurrentLiabilities")
    qc = _q(f, "quarterlyCashCashEquivalentsAndShortTermInvestments") or _q(f, "quarterlyCashAndCashEquivalents")
    if ca and cl and qc:
        out["liquidity"] = dict(cash=qc[-1][1], current_assets=ca[-1][1], current_liabilities=cl[-1][1])
        out["flags"] += D.liquidity(qc[-1][1], cl[-1][1], ca[-1][1], direction)
    for g in (row.get("eg1"), row.get("sg1")):
        if g is not None:
            out["flags"] += D.extreme_growth(g)
    # surprises (S07-R31)
    hist = s.get("earningsHistory", {}).get("history", [])
    sp = [raw(h, "surprisePercent") for h in hist if raw(h, "surprisePercent") is not None]
    if sp:
        out["surprises"] = asdict(D.surprises([x * 100 for x in sp])) | {
            "quarters": [dict(q=(h.get("quarter") or {}).get("fmt"), actual=raw(h, "epsActual"),
                              estimate=raw(h, "epsEstimate"), pct=(raw(h, "surprisePercent") or 0) * 100) for h in hist]}
    out["flags"] = sorted(set(out["flags"]))
    return out


def valuation(bars: list, f: dict, row: dict, sector_avg: dict) -> dict[str, Any]:
    """Premium vs the sector (S07-R19) and the target from the stock's own 3-year multiple (D-S07-4)."""
    out: dict[str, Any] = {}
    price = bars[-1].close
    if row.get("pe1") and sector_avg.get("pe1"):
        out["premium"] = asdict(D.premium(row["pe1"], sector_avg["pe1"]))
    eps = _q(f, "annualDilutedEPS")
    rev, shares = _q(f, "annualTotalRevenue"), _q(f, "annualDilutedAverageShares")
    closes = {b.day.isoformat(): b.close for b in bars}

    def px(d: str) -> float | None:
        ks = [k for k in closes if k <= d]
        return closes[ks[-1]] if ks else None
    profitable = (row.get("f1") or 0) > 0 and (row.get("f2") or 0) > 0
    hist, years = [], []
    if profitable:
        for d, e in eps[-3:]:
            p = px(d)
            if p and e > 0:
                hist.append(p / e)
                years.append(d[:4])
        fwd = row.get("f2")
    else:
        sm = dict(shares)
        for d, r in rev[-3:]:
            p = px(d)
            if p and r > 0 and d in sm:
                hist.append(p * sm[d] / r)
                years.append(d[:4])
        fwd = row.get("sales_f2") or row.get("sales_f1")
    out["hist_multiples"] = hist
    out["hist_years"], out["method"] = years, "pe" if profitable else "ps"
    # not from the course: the statements' EPS and the consensus must be on one basis (an ADR holds several
    # shares in another currency; GAAP vs adjusted). Last fiscal year's EPS against the consensus F0 for it:
    # off by more than half, and the own-multiple target is marked unreliable and is not chosen automatically.
    if profitable and eps and row.get("f0"):
        r = eps[-1][1] / row["f0"]
        out["eps_basis_ratio"] = r
        if not 0.67 <= r <= 1.5:
            out["eps_basis_mismatch"] = True
    # not from the course: a year with EPS near zero gives a P/E in the hundreds and a meaningless target. A
    # multiple above twice the median of the three (or above the cap) is dropped and flagged; the target then
    # uses the mean of the remaining years instead of falling back to the sector.
    cap = 80.0 if profitable else 40.0
    med = statistics.median(hist) if hist else None
    kept = [m for m in hist if m <= cap and (med is None or m <= 2 * med)]
    out["flags"] = []
    if len(kept) < len(hist):
        out["dropped_multiples"] = [m for m in hist if m not in kept]
        out["flags"].append("hist_multiple_outlier_dropped")
    try:
        if fwd and out["flags"] and kept:
            mult = statistics.mean(kept)
            tp = mult * fwd if profitable else mult * fwd / shares[-1][1]
            out["target"] = dict(price=round(tp, 2), method="pe" if profitable else "ps", multiple=mult,
                                 multiple_source="hist_trimmed", flags=["not_from_course_trimmed_mean"],
                                 peer_multiple=None)
            out["target_upside_pct"] = (tp / price - 1) * 100
        elif fwd:
            t = D.target_price(profitable, fwd, kept, sector_avg.get("pe1") if profitable else sector_avg.get("ps1"),
                               shares=shares[-1][1] if shares and not profitable else None)
            out["target"] = asdict(t)
            out["target_upside_pct"] = (t.price / price - 1) * 100
        if out.get("eps_basis_mismatch") and "target" in out:
            out["target"]["flags"] = list(out["target"]["flags"]) + ["unreliable_eps_basis"]
    except Exception as e:                                   # noqa: BLE001 - reported, the agent picks a level
        out["target_error"] = str(e)
    return out


def catalysts(s: dict, news: list[dict], as_of: date) -> dict[str, Any]:
    cal = s.get("calendarEvents", {}).get("earnings", {})
    dates = [d.get("fmt") for d in cal.get("earningsDate", [])]
    acts = []
    cutoff = datetime.combine(as_of - timedelta(days=90), datetime.min.time(), timezone.utc).timestamp()
    for a in s.get("upgradeDowngradeHistory", {}).get("history", [])[:40]:
        if a.get("epochGradeDate", 0) >= cutoff:
            acts.append(dict(date=datetime.fromtimestamp(a["epochGradeDate"], timezone.utc).date().isoformat(),
                             firm=a.get("firm"), action=a.get("action"), from_=a.get("fromGrade"), to=a.get("toGrade"),
                             target=a.get("currentPriceTarget"), prior_target=a.get("priorPriceTarget")))
    t0, t1 = trend(s, "0y"), trend(s, "+1y")
    rev = {k: dict(up30=raw(t.get("epsRevisions"), "upLast30days"), down30=raw(t.get("epsRevisions"), "downLast30days"),
                   eps_now=raw(t.get("epsTrend"), "current"), eps_90d=raw(t.get("epsTrend"), "90daysAgo"))
           for k, t in (("F1", t0), ("F2", t1))}
    fd = s.get("financialData", {})
    return dict(next_earnings=dates[0] if dates else None, analyst_actions=acts, revisions=rev,
                mean_target=raw(fd, "targetMeanPrice"), n_analysts=raw(fd, "numberOfAnalystOpinions"),
                recommendation=fd.get("recommendationKey"), news=news)


def monthly_sigma(bars: list) -> float:
    m = I.resample(bars, "month")[-37:]
    rets = [(b.close / a.close - 1) * 100 for a, b in zip(m[:-1], m[1:])]
    return statistics.stdev(rets)


def timing(bars: list, sector_bars: list, spy_bars: list, direction: str) -> dict[str, Any]:
    st = snapshot(bars, direction)                                           # type: ignore[arg-type]
    sec = snapshot(sector_bars, direction)                                   # type: ignore[arg-type]
    mkt = snapshot(spy_bars, direction)                                      # type: ignore[arg-type]
    pb = st.details["price_behavior"]
    qm = tuple("green" if pb[k] else "red" for k in ("quarter", "month"))
    return dict(stock=asdict(st), sector=dict(overall=sec.overall, sources=sec.sources),
                market=dict(overall=mkt.overall, sources=mkt.sources),
                combined=combine(st.overall, sec.overall, mkt.overall), price_behaviour_qm=qm)


def option_candidates(y: Yahoo, sym: str, direction: str, price: float, target: float, months: float,
                      as_of: date) -> list[dict]:
    """Code builds the candidates (DC-18): the first expiry at least the horizon away (min 60 days), a
    debit spread from near the money to the target and a single option, with the ratio from the premium."""
    try:
        base = y.options(sym)
    except Exception as e:                                                    # noqa: BLE001
        return [dict(error=str(e))]
    want = as_of + timedelta(days=max(60, int(months * 30.5)))
    exps = [e for e in base.get("expirationDates", []) if datetime.fromtimestamp(e, timezone.utc).date() >= want]
    if not exps:
        return []
    ch = y.options(sym, exps[0])
    exp_date = datetime.fromtimestamp(exps[0], timezone.utc).date()
    kind = "call" if direction == "long" else "put"
    legs = ch["options"][0]["calls" if kind == "call" else "puts"]

    def mid(o):
        b, a = o.get("bid") or 0, o.get("ask") or 0
        return (b + a) / 2 if b and a else o.get("lastPrice")
    legs = [o for o in legs if mid(o)]
    if not legs:
        return []
    atm = min(legs, key=lambda o: abs(o["strike"] - price))
    out = []
    single = T.single_option_ratio(kind, atm["strike"], mid(atm), target)
    out.append(dict(structure=f"single_{kind}", expiry=exp_date.isoformat(), strike=atm["strike"], premium=mid(atm),
                    iv=atm.get("impliedVolatility"), oi=atm.get("openInterest"), ratio_to_target=single,
                    cost_per_contract=mid(atm) * 100))
    far = [o for o in legs if (o["strike"] > atm["strike"] if kind == "call" else o["strike"] < atm["strike"])]
    if far:
        short = min(far, key=lambda o: abs(o["strike"] - target))
        vs = T.VerticalSpread(kind, atm["strike"], mid(atm), short["strike"], mid(short))
        if vs.cost > 0:
            out.append(dict(structure=f"{kind}_spread", expiry=exp_date.isoformat(), long_strike=atm["strike"],
                            short_strike=short["strike"], cost=vs.cost, max_ratio=vs.ratio,
                            ratio_to_target=vs.ratio_to_target(target), cost_per_contract=vs.cost * 100,
                            iv=atm.get("impliedVolatility"), oi=(atm.get("openInterest"), short.get("openInterest"))))
    return out


def trade_plan(sym: str, direction: str, bars: list, target: float | None, target_source: str | None, k: float,
               etf: str, buckets: dict, macro_bias: str, tl: dict, row: dict, catalyst_expected: bool | None,
               unhealthy: bool, catalyst_chain: str = "", catalysts: list | None = None, as_of: date | None = None,
               next_earnings: str | None = None, heat: str | None = "hot", news_adverse: bool = False) -> dict[str, Any]:
    """catalysts and as_of: the judgment's catalyst rows set the horizon and chain (S10-R9, S10-R10, D-S10-5) and,
    when the judgment has none, catalyst_expected (S07-R13). An explicit hand-written value wins. heat None (a
    hand-written judgment without one): cold when catalyst_expected comes out False (S08-R33), else hot."""
    price = bars[-1].close
    a = adr(bars)
    stop = T.stop_price(price, a, k, direction)                              # type: ignore[arg-type]
    sd = T.stop_distance_pct(price, stop)
    sig = monthly_sigma(bars)
    out: dict[str, Any] = dict(entry=price, adr_pct=a, k=k, stop=stop, stop_pct=sd, monthly_sigma_pct=sig,
                               options_route=a > load_policy().stop.adr_route_to_options_pct)
    if target:
        out.update(target=target, target_source=target_source, move_pct=T.pct_move(price, target))
        try:
            out["rr"] = T.reward_risk(price, stop, target, direction)       # type: ignore[arg-type]
        except ValueError as e:
            out["rr"], out["rr_error"] = None, str(e)
        out["horizon_months"] = T.horizon_months(out["move_pct"], sig)
    if catalysts is not None and as_of is not None:
        ch = CC.catalyst_horizon(out.get("horizon_months"), catalysts, as_of, next_earnings)
        if out.get("horizon_months") is not None:
            out.update(horizon_months=ch["months"], horizon_basis=ch["basis"],
                       horizon_sigma_months=out["horizon_months"])
        catalyst_chain = catalyst_chain or ch["chain"]
        out.update(catalyst_chain=catalyst_chain, catalyst_wait=ch["wait"])
        if catalyst_expected is None:
            catalyst_expected = ch["expected"]
    if heat is None:
        heat = "cold" if catalyst_expected is False else "hot"
    sz = T.size_position(EQUITY, price, sd, "initial")
    out["sizing"] = asdict(sz) | {"equity": EQUITY}
    p = TradeProposal(symbol=sym, direction=direction, stage="initial", sector=etf, entry=price, stop=stop,  # type: ignore[arg-type]
                      target=target, target_source=target_source, horizon_months=out.get("horizon_months"),
                      catalyst_chain=catalyst_chain, adr_pct=a, monthly_sigma_pct=sig, requested_pct=sz.allowed_pct,
                      traffic_light=tl["combined"], market_cap=row.get("market_cap"), price=price,
                      revenue=row.get("sales"), price_behaviour_qm=tl["price_behaviour_qm"],
                      unhealthy_finances=unhealthy, catalyst_expected=catalyst_expected, heat=heat,  # type: ignore[arg-type]
                      news_adverse=news_adverse,
                      reasons={"DC-19": f"k={k}"} if k < 4 else {})
    v = evaluate(p, Context(Portfolio(EQUITY, []), macro_bias=macro_bias, sector_buckets=buckets))
    out["verdict"] = dict(status=v.status.value,
                          findings=[dict(rule_id=x.rule_id, severity=x.severity.value, message=x.message)
                                    for x in v.findings])
    return out


def chart(bars: list, n: int = 260) -> dict:
    closes = [b.close for b in bars]
    ma = {p: I.sma(closes, p) for p in (21, 63, 252)}
    return dict(dates=[b.day.isoformat() for b in bars][-n:], close=closes[-n:],
                high=[b.high for b in bars][-n:], low=[b.low for b in bars][-n:], open=[b.open for b in bars][-n:],
                ma={str(p): v[-n:] for p, v in ma.items()})


def tech(bars: list, sector_bars: list, spy_bars: list, n: int = 260, weeks: int = 156) -> dict:
    """The series behind the article's technical charts (S09), at the course's periods: three years of weekly closes,
    daily RSI and MACD, ADR20/ADR60 (D-S09-10) and the relative strength against the sector ETF and SPY. Code only."""
    t = load_policy().timing
    closes = [b.close for b in bars]
    wk = I.resample(bars, "week")[-weeks:]
    r = I.rsi(closes, t.rsi_period)
    line, sig, hist = I.macd(closes, *t.macd)
    rng = [daily_range_pct(b) for b in bars]
    ratio: list[float | None] = [None] * len(bars)
    for i in range(59, len(bars)):
        ratio[i] = (sum(rng[i - 19:i + 1]) / 20) / (sum(rng[i - 59:i + 1]) / 60)
    days = [b.day.isoformat() for b in bars][-n:]

    def rel(other: list) -> list[float | None]:
        oc = {b.day.isoformat(): b.close for b in other}
        base = next(((c, oc[d]) for d, c in zip(days, closes[-n:]) if d in oc), None)
        if not base:
            return [None] * len(days)
        return [round(c / base[0] / (oc[d] / base[1]) * 100, 2) if d in oc else None for d, c in zip(days, closes[-n:])]
    rnd = lambda xs: [None if x is None else round(x, 4) for x in xs[-n:]]
    return dict(dates=days, rsi=rnd(r), macd=rnd(line), signal=rnd(sig), hist=rnd(hist), adr_ratio=rnd(ratio),
                rs_sector=rel(sector_bars), rs_spy=rel(spy_bars),
                weekly=dict(dates=[b.day.isoformat() for b in wk], close=[b.close for b in wk]),
                periods=dict(rsi=t.rsi_period, macd=list(t.macd), adr_wait=t.atr_ratio_wait))
