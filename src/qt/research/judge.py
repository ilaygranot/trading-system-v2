"""The judgment layer of a research run done by the agents (architecture 5), instead of a person in a session.

The research run (qt.research.run) computes every number; this module asks the cards and returns the same
judgments dict a person writes by hand (judgments.yaml), so the dossier, the report and the watchlist do not
change. Order:
  A1  DC-01..DC-04 on the macro stance (grey band, compass, macro vs market, confidence).
  A3  DC-09 ranks the green and orange stocks when there are more than the deep-dive cap (12); otherwise all pass.
      Red never passes; green always does unless skipped with a reason. J["A3"] keeps who was skipped and why.
  A4  per idea: DC-10 story, DC-11 premium and target method, DC-12 thesis breakers, DC-13 watchlist and heat.
  A5  per idea, before DC-13: DC-14 on each candidate event code could not decide (qt.research.catalyst_chain: the
      next report, analyst actions in the judgment band, SEC 8-K/6-K and the results release; at most 5). Code then
      derives catalyst_expected, the chain and the horizon; "hot" with no counted catalyst becomes cold (S08-R33).
  A6  only for a "hot" idea (DC-17's trigger): DC-17 trigger, DC-19 k, DC-20 tranches and first size.
Every card gets a fact sheet built from the code's numbers for that idea (principle 4: no fact without an id), and
the earlier cards' answers as the world state. The Critic checks each decision (qt.agents.runner).
Not in v1: A2 (the sector scorecard keeps the code default), DC-18 (the instrument: stock only in the paper book).
DC-14 in the daily news pass is qt.news; here it runs only on deep-dive events.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

from qt.facts import FactSheet
from qt.policy import load_policy
from qt.research import catalyst_chain as CC

MIN_DEEP, MAX_DEEP = 8, 12     # DC-09: up to 12 green and orange stocks all pass; above, the agent ranks and 8 to 12
                                # go on (Ilay 22.10, to start; the card had 8-15 per direction; not from course)
K_OPTIONS = {"k_min": 3.0, "k_mid": 4.0, "k_max": 5.0}       # DC-19: 3 (20.10), default 4, 5 inferred from 102
TARGET_SOURCE = {"hist_3y": "own_multiple_target", "sector": "own_multiple_target", "other": None}
SRC = "code:research"
LANGUAGE = "Hebrew for every text field (reasoning, breakers, tldr); codes, ids and tickers as they are"
JUDGED_BY = "Agents A1, A3, A4, A5, A6 with the Critic (cards DC-01..04, 09..14, 17, 19, 20)"

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
    for i, a in enumerate((cat.get("analyst_actions") or [])[:5]):
        fs.add(f"{sym}.catalysts.analyst_actions[{i}]", f"{a.get('date')} {a.get('firm')} {a.get('action')} "
               f"{a.get('from_')}->{a.get('to')} target {a.get('target')} ({a.get('prior_target')})",
               "yahoo:upgradeDowngradeHistory", as_of)
    document_facts(fs, sym, d.get("documents"), as_of)
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


def document_facts(fs: FactSheet, sym: str, docs: dict | None, as_of: date) -> None:
    """SEC extracts as facts (S07-R30 risk factors for DC-12 risk_10k, S07-R32 announcements and reports)."""
    if not docs:
        return
    for i, f in enumerate((docs.get("current") or [])[:8]):
        ex = (f.get("exhibits") or [{}])[0]
        fs.add(f"{sym}.filing[{i}]", f"{f.get('date')} {f.get('form')} {' '.join(f.get('items') or [])} "
               f"{' / '.join(f.get('item_names') or [])} | {ex.get('title') or ''}".strip(),
               f"sec:{f.get('form')} {f.get('index_url') or f.get('url') or ''}".strip(), as_of)
    res = docs.get("results")
    if res:
        src = f"sec:{res.get('form')} {res.get('url') or ''}".strip()
        rel = f"[{res.get('form')} {res.get('date')}] "               # the release date travels with every value
        if res.get("date"):
            fs.add(f"{sym}.results.date", res["date"], src, as_of)
        if res.get("lead"):
            fs.add(f"{sym}.results.lead", rel + res["lead"], src, as_of)
        for i, o in enumerate((res.get("outlook") or [])[:6]):
            fs.add(f"{sym}.results.outlook[{i}]", rel + o, src, as_of)
        for i, g in enumerate((res.get("guidance") or [])[:4]):
            fs.add(f"{sym}.results.guidance[{i}]", f"{rel}{g.get('metric')} {g.get('period')} {g.get('low')}-"
                   f"{g.get('high')} {g.get('unit')}: {g.get('sentence', '')}", src, as_of)
    rf = docs.get("risk_factors")
    if rf:
        src = f"sec:{rf.get('form')} Item 1A {rf.get('url') or ''}".strip()
        if rf.get("headlines"):
            fs.add(f"{sym}.risk_factors.headlines", " | ".join(rf["headlines"])[:3000], src, as_of)
        if rf.get("excerpt"):
            fs.add(f"{sym}.risk_factors.excerpt", rf["excerpt"], src, as_of)
        dl = rf.get("delta")
        if dl:
            fs.add(f"{sym}.risk_factors.delta", ("no material change; " if dl.get("no_material_change") else "")
                   + (dl.get("excerpt") or ""), f"sec:{dl.get('form')} Item 1A {dl.get('url') or ''}".strip(), as_of)
    if docs.get("errors"):
        fs.add(f"{sym}.documents.errors", list(docs["errors"])[:8], "code:filings", as_of)


def _peers_on_list(d: dict) -> list[str]:
    return [p["sym"] for p in d.get("peers") or [] if p.get("on_list")]


def event_facts(d: dict, ev: dict, as_of: date, j: dict | None = None, macro: dict | None = None) -> FactSheet:
    """A small sheet per DC-14 call: the event and its exhibit, its price facts, the profile, story and breakers,
    the regime and the peers on the list. Not the whole idea sheet (LLM budget)."""
    fs = FactSheet()
    sym = d["sym"]
    add_facts(fs, f"{sym}.event", {k: v for k, v in CC.public(ev).items() if k not in ("facts", "flags")}, as_of,
              ev.get("source") or SRC, depth=1)
    add_facts(fs, f"{sym}.event.facts", ev.get("facts") or {}, as_of, depth=1)
    if ev.get("flags"):
        fs.add(f"{sym}.event.flags", list(ev["flags"]), SRC, as_of)
    docs = d.get("documents") or {}
    f = next((x for x in docs.get("current") or [] if ev.get("accession") and x.get("accession") == ev["accession"]),
             None)
    for i, ex in enumerate((f or {}).get("exhibits") or []):
        fs.add(f"{sym}.event.exhibit[{i}]", f"{ex.get('type')} {ex.get('title', '')}: {ex.get('lead', '')}",
               f"sec:{ex.get('type')} {ex.get('url', '')}".strip(), as_of)
    if ev.get("kind") in ("results", "guidance", "own_report"):
        sub = FactSheet()
        document_facts(sub, sym, {k: docs.get(k) for k in ("results",)}, as_of)
        for x in sub.to_list():
            fs.add(x["key"], x["value"], x["source"], as_of)
    if ev.get("kind") == "guidance" and docs.get("results_prev"):
        for i, g in enumerate((docs["results_prev"].get("guidance") or [])[:4]):
            fs.add(f"{sym}.results_prev.guidance[{i}]", f"{g.get('metric')} {g.get('period')} {g.get('low')}-"
                   f"{g.get('high')}", f"sec:{docs['results_prev'].get('form')} {docs['results_prev'].get('url', '')}",
                   as_of)
    p = d.get("profile") or {}
    add_facts(fs, f"{sym}.profile", {k: p.get(k) for k in ("name", "industry")}, as_of, depth=1)
    j = j or {}
    if j.get("story"):
        fs.add(f"{sym}.story", f"{j.get('story_type', '')}: {j['story']}"[:800], "agent:DC-10", as_of)
    for i, b in enumerate((j.get("breakers") or [])[:5]):
        fs.add(f"{sym}.thesis_breaker[{i}]", f"{b.get('kind')}: {b.get('text')}", "agent:DC-12", as_of)
    if macro:
        st = macro["stage2"]["stance"]
        add_facts(fs, "macro.stance", {k: st.get(k) for k in ("zone", "bias")}, as_of, depth=1)
    if _peers_on_list(d):
        fs.add(f"{sym}.peers_on_list", _peers_on_list(d), "code:peers", as_of)
    return fs


def candidates(timing: list[dict]) -> list[dict]:
    """Who may go to the deep dive (DC-09): green or orange on the combined light, with data. Red never does."""
    return [t for t in timing if not t.get("error") and t.get("combined") in ("green", "orange")]


def unexplained_greens(timing: list[dict], analysed, skipped: dict | None) -> list[str]:
    """Green stocks that were neither analysed nor skipped with a reason (Ilay 22.10: must be empty)."""
    skipped = skipped if isinstance(skipped, dict) else {}
    return [t["sym"] for t in candidates(timing) if t["combined"] == "green" and t["sym"] not in analysed
            and not str(skipped.get(t["sym"]) or "").strip()]


class Judge:
    """Calls the cards for one research run and assembles the judgments dict."""

    def __init__(self, runner, run_id: str | None = None, max_deep: int = MAX_DEEP, step: str = "research"):
        self.runner, self.run_id, self.max_deep, self.step = runner, run_id, max_deep, step
        self.log: list[dict] = []
        self.selection: dict[str, Any] = {}

    def _decide(self, agent: str, card: str, fs: FactSheet, world: dict, subject: str) -> dict:
        r = self.runner.decide(agent, card, fs, world, run_id=self.run_id, step=self.step, subject=subject)
        self.log.append(dict(card=card, subject=subject, option=r.option, status=r.status, cost=round(r.cost_usd, 4)))
        out = dict(r.output)
        out["status"] = r.status
        return out

    # ---------------------------------------------------------------- A1
    def macro(self, macro: dict | None, as_of: date, fs: FactSheet | None = None) -> dict:
        """fs: a fact sheet built elsewhere (the monthly run's stance) instead of the research run's macro."""
        fs = fs or macro_facts(macro, as_of)
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
    def code_order(self, ok: list[dict]) -> list[dict]:
        """DC-09 when undecided: the profile order (decision 12, D-S06-11), green before orange, then R:R to the
        extreme. The card's territory step is not here: the timing rows do not carry it."""
        sc = load_policy().screening
        rank = {"long": list(sc.long_rank), "short": list(sc.short_rank)}

        def key(t: dict) -> tuple:
            r = rank.get(t.get("dir"), [])
            return (r.index(t.get("profile")) if t.get("profile") in r else len(r), t.get("combined") != "green",
                    -(t.get("rr_extreme") or 0))
        return sorted(ok, key=key)

    def pick(self, timing: list[dict], macro: dict, as_of: date) -> list[dict]:
        """Every green and orange stock when they fit under the cap; above it DC-09 ranks and 8 to 12 go on. A green
        stock is never left out silently: it goes on, or it is skipped with the agent's reason, or (only when the
        cap is full of green stocks) with the code's. Every skipped stock has a reason; self.selection keeps them."""
        ok = candidates(timing)
        sel: dict[str, Any] = {"candidates": len(ok), "cap": self.max_deep}
        self.selection = sel
        if len(ok) <= self.max_deep:
            sel.update(mode="all", picked=[t["sym"] for t in ok], skipped={})
            return ok
        fs = FactSheet()
        for t in ok:
            add_facts(fs, t["sym"], {k: t.get(k) for k in ("etf", "dir", "profile", "combined", "stock", "sector",
                                                          "market", "rr_extreme", "adr", "price")}, as_of, depth=1)
        add_facts(fs, "macro.stance", {k: macro["stage2"]["stance"].get(k) for k in ("zone", "bias")}, as_of, depth=1)
        world = {"candidates": [t["sym"] for t in ok], "green": [t["sym"] for t in ok if t["combined"] == "green"],
                 "min": MIN_DEEP, "cap": self.max_deep,
                 "rule": "every green stock goes on unless skipped with a reason of its own in skipped"}
        o = self._decide("A3", "DC-09", fs, world, "screen")
        by = {t["sym"]: t for t in ok}
        why = {x.get("sym"): (x.get("reason") or "").strip() for x in o.get("skipped") or [] if isinstance(x, dict)}
        ranked = [by[s] for s in dict.fromkeys(o.get("ranked") or []) if s in by]
        sel.update(mode="ranked", why=o.get("reasoning", ""))
        if not ranked:                                    # undecided: the card's code order
            ranked, why = self.code_order(ok), {}
            sel.update(mode="code_order", why="")
        order = ranked + [t for t in self.code_order(ok) if t not in ranked]
        picked = ranked[: self.max_deep]
        if len(picked) < min(MIN_DEEP, self.max_deep, len(ok)):          # the agent passed too few: fill by the code order
            picked += [t for t in order if t not in picked][: min(MIN_DEEP, self.max_deep) - len(picked)]
        for t in [t for t in order if t["combined"] == "green" and t not in picked and not why.get(t["sym"])]:
            oranges = [x for x in picked if x["combined"] != "green"]
            if len(picked) < self.max_deep:
                picked.append(t)                          # an unexplained green goes on while there is room
            elif oranges:
                picked.remove(oranges[-1])                # or takes the place of the last orange
                picked.append(t)
        picked = [t for t in picked if t["combined"] == "green"] + [t for t in picked if t["combined"] != "green"]
        chosen = {t["sym"] for t in picked}
        rank = {t["sym"]: n + 1 for n, t in enumerate(order)}
        skipped = {}
        for t in order:
            if t["sym"] in chosen:
                continue
            skipped[t["sym"]] = why.get(t["sym"]) or (
                f"תקרה של {self.max_deep}: כל המקומות תפוסים במניות ירוקות שדורגו לפניה" if t["combined"] == "green"
                else f"כתומה, מקום {rank[t['sym']]} בדירוג, מחוץ ל-{len(picked)} שנבחרו")
        sel.update(picked=[t["sym"] for t in picked], skipped=skipped)
        assert not unexplained_greens(timing, chosen, skipped)
        return picked

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
        j.update(story_type=story["option"], story=story.get("reasoning", ""), thesis=prem.get("reasoning", ""),
                 premium={"choice": prem["option"], "why": prem.get("reasoning", "")},
                 breakers=[dict(b) for b in brk.get("thesis_breakers") or []])

        # A5 DC-14 on the events code could not decide, then the code's catalyst gate (S07-R13, S08-R33, S10-R9)
        cat = d.get("catalysts") or {}
        ne = cat.get("next_earnings")
        evs = CC.events(d, as_of)
        st = macro["stage2"]["stance"]
        regime = {k: st.get(k) for k in ("zone", "bias")}
        prof = {k: (d.get("profile") or {}).get(k) for k in ("name", "industry")}
        for ev in CC.select_for_agent(evs):
            try:
                o = self._decide("A5", "DC-14", event_facts(d, ev, as_of, j, macro),
                                 world | {"event": CC.public(ev), "peers_on_list": _peers_on_list(d), "regime": regime,
                                          "profile": prof}, sym)
                CC.apply_answer(ev, o)
            except Exception as e:                       # one event's failure is the lower class (S13-J7)
                if type(e).__name__ == "BudgetExceeded":     # the run's budget is gone: stop, as __call__ expects
                    raise
                CC.agent_error(ev, e)
        rows = CC.judgment_rows(evs, ne)
        ch = CC.catalyst_horizon((d.get("plan") or {}).get("horizon_months"), evs, as_of, ne)
        world["DC-14"] = rows
        world["catalyst_code"] = {k: ch[k] for k in ("expected", "chain", "wait", "months", "basis")}

        heat = self._decide("A4", "DC-13", fs, world, sym)
        world["DC-13"] = {"option": heat["option"]}
        j.update(tldr=dict(heat.get("tldr") or {}), heat=heat["option"], catalysts=rows,
                 catalyst_expected=bool(ch["expected"]), catalyst_chain=ch["chain"], catalyst_wait=ch["wait"],
                 news_adverse=CC.adverse(evs, as_of, ne))
        src = CC.sources(d)
        if src:
            j["sources"] = src
        ts = TARGET_SOURCE.get(prem.get("target_method") or "")
        if ts:
            j["target_source"] = ts
        if heat["option"] == "no":
            j.update(decision="no", why=heat.get("reasoning", ""))
            return j
        if heat["option"] == "hot" and not ch["expected"]:          # S08-R33 / S07-R13: hot needs a counted catalyst
            why = ("S08-R33/S07-R13: אין קטליסט צפוי שאושר בקוד (A או B בעד הרעיון, לא מתומחר, בתוקף בתוך "
                   f"{ch['months'] or 3:.1f} חודשים)")
            j.update(heat="cold", decision="watch_only", why=why)
            j.setdefault("code_overrides", []).append({"card": "DC-13", "from": "hot", "to": "cold",
                                                       "rule_id": "S08-R33", "why": why})
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
        picked = self.pick(stages["timing"], macro, as_of)
        for n, t in enumerate(picked):
            try:
                J["ideas"][t["sym"]] = self.idea(t, deep_dive, macro, as_of)
            except Exception as e:                       # noqa: BLE001 - one idea's failure (budget, data) is reported
                J.setdefault("errors", {})[t["sym"]] = f"{type(e).__name__}: {e}"
                self.selection.setdefault("skipped", {})[t["sym"]] = f"הניתוח נכשל: {type(e).__name__}: {e}"
                if type(e).__name__ == "BudgetExceeded":
                    self.selection["cut_by_budget"] = [x["sym"] for x in picked[n + 1:]]
                    for x in picked[n + 1:]:
                        self.selection["skipped"][x["sym"]] = "נעצר: תקציב ה-Agents לריצה נגמר (ירוקות נותחו ראשונות)"
                    break
        J["A3"] = self.selection
        J["agent_log"] = self.log
        hot_max = load_policy().watchlist.hot_max                   # S07-R35: 3-4 hot per cycle, a flag
        if sum(j.get("heat") == "hot" for j in J["ideas"].values()) > hot_max:
            J.setdefault("warnings", []).append(f"hot_above_{hot_max}")
        return J
