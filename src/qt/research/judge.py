"""The judgment layer of a research run done by the agents (architecture 5), instead of a person in a session.

The research run (qt.research.run) computes every number; this module asks the cards and returns the same
judgments dict a person writes by hand (judgments.yaml), so the dossier, the report and the watchlist do not
change. Order:
  A1  DC-01..DC-04 on the macro stance (grey band, compass, macro vs market, confidence).
  A3  DC-09 ranks the timing survivors when there are more than the deep-dive cap; otherwise all pass (card).
  A4  per idea: DC-10 story, DC-11 premium and target method, DC-12 thesis breakers, DC-13 watchlist and heat.
  A6  only for a "hot" idea (DC-17's trigger): DC-17 trigger, DC-19 k, DC-20 tranches and first size.
Every card gets a fact sheet built from the code's numbers for that idea (principle 4: no fact without an id), and
the earlier cards' answers as the world state. The Critic checks each decision (qt.agents.runner).
Not in v1: A2 (the sector scorecard keeps the code default), DC-14 catalysts (no news source yet), DC-18 (the
instrument: stock only in the paper book).
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

from qt.facts import FactSheet
from qt.policy import load_policy

MAX_DEEP = 8                    # DC-09: below 8 candidates all pass; above, the agent ranks and the top 8 go on
                                # (not from course: the cap at the card's lower bound, for cost)
K_OPTIONS = {"k_min": 3.0, "k_mid": 4.0, "k_max": 5.0}       # DC-19: 3 (20.10), default 4, 5 inferred from 102
TARGET_SOURCE = {"hist_3y": "own_multiple_target", "sector": "own_multiple_target", "other": None}
SRC = "code:research"
LANGUAGE = "Hebrew for every text field (reasoning, breakers, tldr); codes, ids and tickers as they are"
JUDGED_BY = "Agents A1, A3, A4, A6 with the Critic (cards DC-01..04, 09..13, 17, 19, 20)"

DeepDive = Callable[[str, str, str, dict], dict]               # (sym, etf, direction, judgment) -> idea


def _scalar(v: Any) -> bool:
    return v is None or isinstance(v, (int, float, str, bool))


def add_facts(fs: FactSheet, prefix: str, obj: Any, as_of: date, source: str = SRC, depth: int = 3) -> None:
    """Every scalar under obj as a fact; short lists of scalars as one fact; longer structures are skipped."""
    if _scalar(obj):
        if obj is not None and obj != "":
            fs.add(prefix, round(obj, 4) if isinstance(obj, float) else obj, source, as_of)
    elif isinstance(obj, list):
        if len(obj) <= 8 and all(_scalar(x) for x in obj):
            fs.add(prefix, [round(x, 4) if isinstance(x, float) else x for x in obj], source, as_of)
        elif depth > 0 and len(obj) <= 5:
            for i, x in enumerate(obj):
                add_facts(fs, f"{prefix}[{i}]", x, as_of, source, depth - 1)
    elif isinstance(obj, dict) and depth > 0:
        for k, v in obj.items():
            add_facts(fs, f"{prefix}.{k}", v, as_of, source, depth - 1)


def macro_facts(macro: dict, as_of: date) -> FactSheet:
    fs = FactSheet()
    st = macro["stage2"]
    add_facts(fs, "macro.stance", st["stance"], as_of, depth=2)
    for s in st.get("scores", []):
        add_facts(fs, f"macro.score.{s['name']}", {k: v for k, v in s.items() if k != "name"}, as_of, depth=1)
    add_facts(fs, "macro.stage1", macro.get("stage1", {}), as_of, depth=2)
    add_facts(fs, "macro.stage3", macro.get("stage3", {}), as_of, depth=2)
    return fs


IDEA_KEYS = ("row", "fin_data", "valuation", "levels", "plan", "sector_avg")


def idea_facts(d: dict, macro: dict, as_of: date) -> FactSheet:
    fs = FactSheet()
    sym = d["sym"]
    for k in IDEA_KEYS:
        add_facts(fs, f"{sym}.{k}", d.get(k) or {}, as_of, depth=2)
    fv = d.get("fundamentals") or {}
    for k in ("flags", "core_trend", "profit_source", "revenue_yoy_last2q", "revenue_cagr", "gap_vs_sector", "health",
              "shares_change_pct", "net_debt_trend", "surprises"):
        add_facts(fs, f"{sym}.fundamentals.{k}", fv.get(k), as_of, depth=1)
    tl = d.get("timing") or {}
    for part in ("stock", "sector", "market"):
        add_facts(fs, f"{sym}.timing.{part}", {k: v for k, v in (tl.get(part) or {}).items()
                                              if k in ("overall", "sources", "flags")}, as_of, depth=2)
    add_facts(fs, f"{sym}.timing.combined", tl.get("combined"), as_of)
    cat = d.get("catalysts") or {}
    for k in ("next_earnings", "revisions", "mean_target", "n_analysts", "recommendation"):
        add_facts(fs, f"{sym}.catalysts.{k}", cat.get(k), as_of, depth=2)
    for i, n in enumerate((cat.get("news") or [])[:5]):
        fs.add(f"{sym}.news[{i}]", f"{n.get('date', '')} {n.get('publisher', '')}: {n.get('title', '')}",
               "yahoo:news", as_of)
    for i, t in enumerate(d.get("targets") or []):
        add_facts(fs, f"{sym}.target_option[{i}]", {k: t.get(k) for k in ("source", "price", "rr", "move_pct",
                                                                         "unreliable", "multiple_source")}, as_of, depth=1)
    p = d.get("profile") or {}
    add_facts(fs, f"{sym}.profile", {k: p.get(k) for k in ("name", "industry", "country")}, as_of, depth=1)
    if p.get("summary"):
        fs.add(f"{sym}.profile.summary", p["summary"][:600], "yahoo:assetProfile", as_of)
    st = macro["stage2"]["stance"]
    add_facts(fs, "macro.stance", {k: st.get(k) for k in ("zone", "bias")}, as_of, depth=1)
    return fs


class Judge:
    """Calls the cards for one research run and assembles the judgments dict."""

    def __init__(self, runner, run_id: str | None = None, max_deep: int = MAX_DEEP):
        self.runner, self.run_id, self.max_deep = runner, run_id, max_deep
        self.log: list[dict] = []

    def _decide(self, agent: str, card: str, fs: FactSheet, world: dict, subject: str) -> dict:
        r = self.runner.decide(agent, card, fs, world, run_id=self.run_id, step="research", subject=subject)
        self.log.append(dict(card=card, subject=subject, option=r.option, status=r.status, cost=round(r.cost_usd, 4)))
        out = dict(r.output)
        out["status"] = r.status
        return out

    # ---------------------------------------------------------------- A1
    def macro(self, macro: dict, as_of: date) -> dict:
        fs = macro_facts(macro, as_of)
        out, world = {}, {"write_in": LANGUAGE}
        for card in ("DC-01", "DC-02", "DC-03", "DC-04"):
            o = self._decide("A1", card, fs, world, "macro")
            out[card] = {"choice": o["option"], "why": o.get("reasoning", "")}
            if card == "DC-02" and o.get("in_six_months"):
                out[card]["in_six_months"] = o["in_six_months"]
            world[card] = o["option"]
        out["notes"] = "שיקול הדעת של A1 (כרטיסים DC-01 עד DC-04), נבדק על ידי ה-Critic"
        return out

    # ---------------------------------------------------------------- A3
    def pick(self, timing: list[dict], macro: dict, as_of: date) -> list[dict]:
        ok = [t for t in timing if not t.get("error") and t.get("combined") in ("green", "orange")]
        if len(ok) <= self.max_deep:
            return ok
        fs = FactSheet()
        for t in ok:
            add_facts(fs, t["sym"], {k: t.get(k) for k in ("etf", "dir", "profile", "combined", "stock", "sector",
                                                          "market", "rr_extreme", "adr", "price")}, as_of, depth=1)
        add_facts(fs, "macro.stance", {k: macro["stage2"]["stance"].get(k) for k in ("zone", "bias")}, as_of, depth=1)
        o = self._decide("A3", "DC-09", fs, {"candidates": [t["sym"] for t in ok], "cap": self.max_deep}, "screen")
        by = {t["sym"]: t for t in ok}
        ranked = [by[s] for s in o.get("ranked") or [] if s in by]
        if not ranked:                                    # undecided: the code's order (rr to the extreme)
            ranked = sorted(ok, key=lambda t: -(t.get("rr_extreme") or 0))
        return ranked[: self.max_deep]

    # ---------------------------------------------------------------- A4 + A6
    def idea(self, t: dict, deep_dive: DeepDive, macro: dict, as_of: date) -> dict:
        sym, etf, dr = t["sym"], t["etf"], t["dir"]
        j: dict[str, Any] = {"etf": etf, "direction": dr}
        d = deep_dive(sym, etf, dr, dict(j, options=False))      # the facts; the option chains come at the end
        fs = idea_facts(d, macro, as_of)
        world: dict[str, Any] = {"symbol": sym, "direction": dr, "etf": etf, "traffic_light": t.get("combined"),
                                 "write_in": LANGUAGE}

        story = self._decide("A4", "DC-10", fs, world, sym)
        world["DC-10"] = {"option": story["option"], "reasoning": story.get("reasoning")}
        prem = self._decide("A4", "DC-11", fs, world, sym)
        world["DC-11"] = {"option": prem["option"], "target_method": prem.get("target_method")}
        brk = self._decide("A4", "DC-12", fs, world, sym)
        world["DC-12"] = {"thesis_breakers": brk.get("thesis_breakers", [])}
        heat = self._decide("A4", "DC-13", fs, world, sym)
        world["DC-13"] = {"option": heat["option"]}

        j.update(story_type=story["option"], story=story.get("reasoning", ""), thesis=prem.get("reasoning", ""),
                 premium={"choice": prem["option"], "why": prem.get("reasoning", "")},
                 breakers=[dict(b) for b in brk.get("thesis_breakers") or []],
                 tldr=dict(heat.get("tldr") or {}), heat=heat["option"])
        ts = TARGET_SOURCE.get(prem.get("target_method") or "")
        if ts:
            j["target_source"] = ts
        if heat["option"] == "no":
            j.update(decision="no", why=heat.get("reasoning", ""))
            return j
        if heat["option"] == "cold":
            j.update(decision="watch_only", why=heat.get("reasoning", ""))
            return j

        trig = self._decide("A6", "DC-17", fs, world, sym)
        world["DC-17"] = {"option": trig["option"], "level": trig.get("level")}
        if trig["option"] == "wait":
            j.update(decision="watch_only", why=trig.get("return_condition") or trig.get("reasoning", ""))
            return j
        j["trigger"] = {"choice": trig["option"], "level": trig.get("level"),
                        "text": trig.get("return_condition") or trig.get("reasoning", "")}
        k = self._decide("A6", "DC-19", fs, world, sym)
        j.update(k=K_OPTIONS.get(k["option"], load_policy().stop.k_default), k_reason=k.get("reasoning", ""))
        world["DC-19"] = {"option": k["option"]}
        sz = self._decide("A6", "DC-20", fs, world, sym)
        j.update(decision=dr, structure={"choice": "stock", "why": "גרסה 1: מניות בלבד (לא מהקורס)"},
                 sizing={"choice": sz["option"], "first_size_pct": sz.get("first_size_pct"),
                         "add_conditions": sz.get("add_conditions") or []})
        return j

    # ---------------------------------------------------------------- the run
    def __call__(self, stages: dict, deep_dive: DeepDive) -> dict:
        as_of, macro = stages["as_of"], stages["macro"]
        J: dict[str, Any] = {"judged_by": JUDGED_BY, "A1": self.macro(macro, as_of), "ideas": {}}
        for t in self.pick(stages["timing"], macro, as_of):
            try:
                J["ideas"][t["sym"]] = self.idea(t, deep_dive, macro, as_of)
            except Exception as e:                       # noqa: BLE001 - one idea's failure (budget, data) is reported
                J.setdefault("errors", {})[t["sym"]] = f"{type(e).__name__}: {e}"
                if type(e).__name__ == "BudgetExceeded":
                    break
        J["agent_log"] = self.log
        return J
