"""Stages 1-3 for the research run: every number from qt.macro, every input with its source, plus the series
the report charts."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta
from typing import Any

import yaml

from qt import macro as M
from qt.data.macro_feed import fetch_fred, monthly_last, parse_manual, yoy_pct
from qt.policy import load_policy

COMMODITIES = {"wti": "DCOILWTICO", "copper": "PCOPPUSDM", "natgas": "DHHNGSP"}
LABELS = {"ism_pmi": "ISM תעשייה (PMI)", "ism_nmi": "ISM שירותים (NMI)", "michigan": "אמון צרכנים מישיגן",
          "permits": "היתרי בנייה", "small_business": "NFIB עסקים קטנים"}


def _months(obs) -> list[tuple[str, float]]:
    return monthly_last(obs)


def run(fred, manual_text: str, spx: list, as_of: date, breadth_pct: float | None) -> dict[str, Any]:
    manual_raw = yaml.safe_load(manual_text)
    man = parse_manual(yaml.safe_dump({k: v for k, v in manual_raw.items() if k != "sources"}), as_of)
    manual_months = {k: sorted(str(m) for m in v) for k, v in manual_raw.items() if k != "sources"}
    monthly, daily, src = fetch_fred(fred, as_of)
    start = as_of - timedelta(days=365 * 12)
    raw_obs = {}
    for name, sid in {"hy_oas": "BAMLH0A0HYM2", "ust10y": "DGS10", "ust3m": "DGS3MO", "cpi": "CPIAUCSL",
                      "ffr": "FEDFUNDS", "unemployment": "UNRATE", "michigan": "UMCSENT", "permits": "PERMIT",
                      **COMMODITIES}.items():
        obs, s = fred.observations(sid, start, as_of, known_on=as_of)
        raw_obs[name] = obs
        src.setdefault(name, s)
    out: dict[str, Any] = {"as_of": as_of.isoformat(), "sources": src, "manual_sources": manual_raw.get("sources", {}),
                           "flags": []}

    # ---------------- stage 1: global macro
    hy = daily["hy_oas"]
    stress = M.credit_stress(hy)
    last21 = 21
    curve = M.yield_curve(daily["ust10y"][-last21:], daily["ust3m"][-last21:])
    cpi_yoy = yoy_pct(monthly["cpi"])
    real, neg = M.real_rate(daily["ust10y"][-1], cpi_yoy[-1])
    com_now, com_3m = {}, {}
    for k in COMMODITIES:
        mv = [v for _, v in _months(raw_obs[k])]
        y = yoy_pct(mv)
        if len(y) >= 4:
            com_now[k], com_3m[k] = y[-1], y[-4]
    com = M.commodities(com_now, com_3m) if com_now else None
    out["stage1"] = dict(credit=asdict(stress), curve=asdict(curve), real_rate=dict(value=real, negative=neg,
                         nominal_10y=daily["ust10y"][-1], cpi_yoy=cpi_yoy[-1]),
                         commodities=asdict(com) | {"yoy": com_now} if com else None)

    # ---------------- stage 2: activity scale, inflation, labor, Fed
    series = {**monthly, **man}
    scores, skipped, notes = [], [], {}
    for name in load_policy().macro.activity_indicators:
        v = series.get(name, [])
        if len(v) < 13:
            skipped.append(name)
            continue
        sc = M.score_series(name, v)
        scores.append(sc)
        spec = load_policy().macro.level_ref[name]
        if spec.get("kind") == "moving_average" and len(v) < spec["months"]:
            notes[name] = f"reference MA{spec['months']} computed over {len(v)} months only"
            out["flags"].append(f"{name}_reference_short_history")
    fed = M.fed_scale(monthly["ffr"], yoy_pct(monthly["m2"]), monthly["fed_balance_sheet"])
    infl = M.inflation(cpi_yoy)
    lab = M.labor(monthly["unemployment"])
    ism = M.ism_state(man["ism_pmi"])
    st = M.stance(scores, fed=fed, infl=infl, lab=lab, ism=ism, stress=stress, curve=curve)
    out["stage2"] = dict(stance=asdict(st), scores=[asdict(s) for s in scores], skipped=skipped, notes=notes,
                         labels=LABELS)
    if st.compass_now:
        out["stage2"]["assets"] = M.preferred_assets(st.compass_now)

    # ---------------- stage 3: market state
    closes = [b.close for b in spx]
    ms = M.market_regime(closes)
    br = M.breadth(pct_above_ma200=breadth_pct)
    out["stage3"] = dict(market=asdict(ms) | {"history": None}, breadth=asdict(br), spx_last=closes[-1])

    # ---------------- chart series (monthly, last 5 years unless noted)
    def tail(xs, n=60):
        return xs[-n:]
    mlabels = lambda obs: [m for m, _ in _months(obs)]
    out["charts"] = {
        "hy": {"dates": [d.isoformat() for d, _ in raw_obs["hy_oas"]][-750:], "values": hy[-750:]},
        "curve": {"dates": [d.isoformat() for d, _ in raw_obs["ust10y"]][-750:],
                  "values": [a - b for a, b in zip(daily["ust10y"][-750:], daily["ust3m"][-750:])]},
        "cpi_yoy": {"dates": tail(mlabels(raw_obs["cpi"])[12:]), "values": tail(cpi_yoy)},
        "ffr": {"dates": tail(mlabels(raw_obs["ffr"])), "values": tail(monthly["ffr"])},
        "unemployment": {"dates": tail(mlabels(raw_obs["unemployment"])), "values": tail(monthly["unemployment"])},
        "ism_pmi": {"dates": manual_months["ism_pmi"], "values": [manual_raw["ism_pmi"][m] for m in manual_months["ism_pmi"]]},
        "ism_nmi": {"dates": manual_months["ism_nmi"], "values": [manual_raw["ism_nmi"][m] for m in manual_months["ism_nmi"]]},
        "small_business": {"dates": manual_months["small_business"],
                           "values": [manual_raw["small_business"][m] for m in manual_months["small_business"]]},
        "michigan": {"dates": tail(mlabels(raw_obs["michigan"]), 36), "values": tail(monthly["michigan"], 36)},
        "permits": {"dates": tail(mlabels(raw_obs["permits"]), 36), "values": tail(monthly["permits"], 36)},
        "spx": {"dates": [b.day.isoformat() for b in spx][-504:], "values": closes[-504:]},
    }
    return out
