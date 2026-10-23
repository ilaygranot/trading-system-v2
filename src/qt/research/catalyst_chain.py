"""Stage 7-8 catalysts for one deep-dive stock, in code (no I/O, no LLM): the candidate events, what code decides
on its own, which events go to DC-14, and from the classified rows the three things the gate needs:
catalyst_expected (S07-R13, S08-R33), the catalyst chain and a catalyst-aware horizon (S10-R9, S10-R10, D-S10-5).

Sources: d["catalysts"] (next report, analyst actions, Yahoo news), d["documents"] (SEC 8-K/6-K in the last 90 days,
the results release, risk factors) and d["peers"]. Code decides 8-K item codes (governance = D), analyst-target bands
(S08-R17), the annual guidance change vs the previous midpoint (S08-R19, decision 13), persistence expiry (S08-R20)
and peer reports as C (S08-R18, S08-J25). DC-14 judges the rest. Undecided always means the lower class (S13-J7).
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from qt import watchlist as W
from qt.policy import load_policy
from qt.screening import guidance_change_pct

MAX_AGENT_EVENTS = 5            # DC-14 calls per stock in the deep dive (not from course: cost)
ANALYST_DAYS = 90               # analyst actions looked at (the window catalysts() already fetches; not from course)
PEER_MONTHS = 6                 # peer reports recorded up to the extended horizon (S08-J25; window not from course)
MONTH_DAYS = 30.4375            # days per month for the horizon arithmetic
PERSIST_DAYS = {"days_1_3": 3}  # S08-R20: an analyst upgrade works up to 3 days
UNTIL_REPORT_DAYS = 91          # until_next_report when the next report date is unknown (one quarter; not from course)
ACCUMULATING_DAYS = 183         # accumulating_2_6m ends after 6 months (S08-R20 / S09-R38)
WAIT_NEAR_DAYS, WAIT_FAR_DAYS = 30, 90                     # S10-R10: "next month", and how far a first catalyst may sit
SHORT_LIVED = ("days_1_3", "none")   # S08-R20 / S09-R38: rides 1-3 days at most; an occurred one never serves a swing
MIN_SERVE_DAYS = 14             # an occurred A/B must stay active this long past as_of to serve the horizon (not from course)

ITEM_KIND = {"2.02": "results", "1.01": "agreement", "2.01": "acquisition", "2.05": "restructuring",
             "2.06": "impairment", "5.02": "officer", "7.01": "reg_fd", "8.01": "other_event"}
GOVERNANCE_ITEMS = {"5.03", "5.05", "5.07", "5.08", "9.01"}   # bylaws, code of ethics, votes, nominations, exhibits
CLASSES = ("macro_lead", "macro_noise", "A", "B", "C", "D")
PERSISTENCE = ("days_1_3", "until_next_report", "accumulating_2_6m", "none")
UNCLEAR_STATUS = ("critic_rejected", "agent_undecided", "refused")   # runner statuses that never heat (S13-J7)
COUNTED = ("A", "B")                                       # S08-R18: C, D and macro never count
PRIORITY = {"own_report": 0, "results": 1, "guidance": 1, "agreement": 2, "acquisition": 2, "restructuring": 3,
            "impairment": 3, "analyst_target": 4}
ROW_KEYS = ("id", "date", "timing", "kind", "what", "source", "url", "class", "direction", "priced_in", "persistence",
            "follow_on_expected", "expected_date", "decided_by", "code_reason", "facts", "level", "strength",
            "flow_technical", "peers_affected", "flags", "why")
_INTERNAL = ("needs_agent", "final", "max_class")


def _d(s: Any) -> date | None:
    if isinstance(s, date):
        return s
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _months(a: date, b: date) -> float:
    return (b - a).days / MONTH_DAYS


def _plus_months(a: date, m: float) -> date:
    return a + timedelta(days=round(m * MONTH_DAYS))


def _prospect(direction: str | None) -> str | None:
    return {"long": "positive", "short": "negative"}.get(direction or "")


def _row(**kw) -> dict:
    ev = dict(id="", date=None, timing="occurred", kind="unknown", what="", source="", url="", **{"class": None},
              direction=None, priced_in=None, persistence=None, follow_on_expected=False, expected_date=None,
              decided_by="code", code_reason="", facts={}, flags=[], needs_agent=False, final=[])
    ev.update(kw)
    return ev


def _code(ev: dict, cls: str, reason: str, **fixed) -> dict:
    """A class decided by code: final, no agent call for it."""
    ev.update({"class": cls, "code_reason": reason, "needs_agent": False, "decided_by": "code", **fixed})
    ev["final"] = sorted(set(ev["final"]) | {"class"} | set(fixed))
    return ev


# ---------------------------------------------------------------- price and news facts (S08-R16, S08-R22)

def _price_facts(d: dict, day: date, as_of: date) -> dict:
    ch = d.get("chart") or {}
    dates, close = ch.get("dates") or [], ch.get("close") or []
    out: dict[str, Any] = {"pre_move_20d_pct": None, "since_event_pct": None, "prior_items_30d": 0}
    ref = min(day, as_of)
    idx = max((i for i, s in enumerate(dates) if s <= ref.isoformat()), default=None)
    if idx is not None and idx < len(close):
        if idx >= 20 and close[idx - 20]:
            out["pre_move_20d_pct"] = round((close[idx] / close[idx - 20] - 1) * 100, 2)
        if day <= as_of and close[idx]:
            out["since_event_pct"] = round((close[-1] / close[idx] - 1) * 100, 2)
    hi = day if day <= as_of else as_of + timedelta(days=1)          # an expected event: what is known up to today
    lo = hi - timedelta(days=30)
    news = [_d(n.get("date") or n.get("time")) for n in (d.get("catalysts") or {}).get("news") or []]
    k8 = [_d(f.get("date")) for f in ((d.get("documents") or {}).get("current") or [])]
    out["prior_items_30d"] = sum(1 for x in news + k8 if x and lo <= x < hi)
    return out


# ---------------------------------------------------------------- the candidates

def _analyst(a: dict, price: float | None, dr: str | None) -> dict:
    tgt, act = a.get("target"), (a.get("action") or "").lower()
    ch = (tgt / price - 1) * 100 if tgt and price else None
    what = f"{a.get('firm')}: {act} {a.get('from_') or ''}->{a.get('to') or ''}, יעד {tgt} (קודם {a.get('prior_target')})"
    if ch is not None:
        what += f", {ch:+.1f}% מהמחיר"
    ev = _row(date=a.get("date"), kind="analyst_target", what=what, source="yahoo:upgradeDowngradeHistory")
    ev["facts"] = {"change_vs_price_pct": round(ch, 2) if ch is not None else None}
    if ch is None:
        if act in ("up", "down"):
            ev.update(needs_agent=True, code_reason="rating change without a target: judgment (S08-R17)")
            return ev
        return _code(ev, "D", "no target and no rating change (S08-R17)")
    band = W.analyst_target(ch)
    with_idea = (ch > 0) == (dr != "short")
    if band == "material":                                 # S08-R17: +50% and above
        c = W.classify("analyst_target", change_pct=ch, prospect=_prospect(dr))
        ev["flags"] = list(c.flags)
        return _code(ev, "B", f"analyst target {ch:+.1f}% vs price: material (S08-R17)", persistence="days_1_3",
                     direction="for_idea" if with_idea and not c.flags else "against_idea", priced_in=False)
    if band == "not_renewed":                              # S08-R17: +12% and below
        if with_idea and (act == "init" or act == ("down" if dr == "short" else "up")):   # mirrored for a short
            return _code(ev, "C", f"analyst target {ch:+.1f}%: did not renew (S08-R17, S08-R18)", persistence="none",
                         direction="for_idea", priced_in=False)
        return _code(ev, "D", f"analyst target {ch:+.1f}%, action {act or '-'}: noise (S08-R17)")
    ev.update(needs_agent=True, code_reason=f"analyst target {ch:+.1f}%: between 12% and 50%, judgment (S08-R17)")
    return ev


def _fy_revenue(res: dict | None) -> dict | None:
    for g in (res or {}).get("guidance") or []:
        if g.get("metric") == "revenue" and g.get("period") == "FY" and g.get("low") is not None:
            return g
    return None


def _filing(f: dict) -> dict:
    items = [str(i) for i in f.get("items") or []]
    kind = f.get("kind") or "unknown"
    if "2.02" in items:
        kind = "results"
    elif items:
        kind = next((ITEM_KIND.get(i, "unknown") for i in items if i != "9.01"), kind)
    ex = (f.get("exhibits") or [{}])[0]
    what = f"{f.get('form')} {' '.join(items)} {' / '.join(f.get('item_names') or [])}".strip()
    if ex.get("title"):
        what += f" | {ex['title']}"
    ev = _row(date=f.get("date"), kind=kind, what=what[:300], source=f"sec:{f.get('form')}",
              url=f.get("index_url") or f.get("url") or "", accession=f.get("accession"))
    if items and set(items) <= GOVERNANCE_ITEMS:
        return _code(ev, "D", f"8-K items {', '.join(items)}: governance only (S08-R18)", kind="governance")
    ev["needs_agent"] = True
    return ev


def _guidance(docs: dict, dr: str | None) -> tuple[dict | None, str]:
    """S08-R19 / decision 13: an annual guidance change vs the previous midpoint, in code. Returns the event (or None)
    and a reason that caps the results event below A when only quarterly or no previous guidance exists."""
    res, prev = docs.get("results"), docs.get("results_prev")
    if not res or not res.get("guidance"):
        return None, ""
    new, old = _fy_revenue(res), _fy_revenue(prev)
    if new is None:
        return None, "quarterly guidance is not an A (S08-R19)"
    if old is None:
        return None, "previous annual guidance missing: never an automatic A (decision 13)"
    mid = (new["low"] + (new.get("high") or new["low"])) / 2
    ch = guidance_change_pct(mid, old["low"], old.get("high"))
    c = W.classify("guidance_change", change_pct=ch, prospect=_prospect(dr))
    ev = _row(date=res.get("date"), kind="guidance", source=res.get("source") or "sec", url=res.get("url") or "",
              what=f"תחזית הכנסות שנתית {new['low']:g}-{new.get('high') or new['low']:g} מול "
                   f"{old['low']:g}-{old.get('high') or old['low']:g}: {ch:+.1f}% מאמצע התחזית הקודמת")
    ev["facts"] = {"guidance_change_pct": round(ch, 2)}
    if c.cls != "A":
        why = f"annual guidance {ch:+.1f}% vs the previous midpoint, below the threshold (S08-R19)"
        return _code(ev, "D", why), why                                      # the release is then no A either
    ev["flags"] = list(c.flags)
    fixed: dict[str, Any] = {"persistence": "until_next_report"}             # S08-R20: guidance works until the report
    if "catalyst_against_prospect" in c.flags:
        fixed["direction"] = "against_idea"                                  # S08-R23
    _code(ev, "A", f"annual guidance {ch:+.1f}% vs the previous midpoint (S08-R19, decision 13)", **fixed)
    ev["needs_agent"] = True                                                 # priced in and direction: DC-14 (S08-R16)
    return ev, ""


def events(d: dict, as_of: date) -> list[dict]:
    """Every candidate event for the stock, newest information first by kind. Code-decided rows carry their class."""
    cat, docs, dr = d.get("catalysts") or {}, d.get("documents") or {}, d.get("direction")
    price = (d.get("plan") or {}).get("entry") or ((d.get("chart") or {}).get("close") or [None])[-1]
    out: list[dict] = []
    ne = _d(cat.get("next_earnings"))
    if ne and ne > as_of:
        out.append(_row(date=ne.isoformat(), timing="expected", kind="own_report", what="דוח רבעוני של החברה",
                        source="yahoo:calendarEvents", expected_date=ne.isoformat(), needs_agent=True))
    for a in cat.get("analyst_actions") or []:
        ad = _d(a.get("date"))
        if ad and as_of - timedelta(days=ANALYST_DAYS) <= ad <= as_of:
            out.append(_analyst(a, price, dr))
    for f in docs.get("current") or []:
        fd = _d(f.get("date"))
        if fd and fd <= as_of:
            out.append(_filing(f))
    g, cap_reason = _guidance(docs, dr)
    if g:
        out.append(g)
    if cap_reason:
        for ev in out:
            if ev["kind"] == "results":
                ev.update(max_class="B", code_reason=cap_reason)
    for p in d.get("peers") or []:
        pd = _d(p.get("next_earnings"))
        if pd and as_of < pd <= _plus_months(as_of, PEER_MONTHS):
            ev = _row(date=pd.isoformat(), timing="expected", kind="competitor_results", source="code:peers",
                      what=f"דוח של מתחרה: {p.get('sym')}", expected_date=pd.isoformat())
            c = W.classify("competitor_results", on_list=True)
            out.append(_code(ev, c.cls, "peer report: C, recorded because the stock is on the list "
                                        "(S08-R18, S08-J25, D-S08-2)", persistence="none", priced_in=False))
    for n, ev in enumerate(out, 1):
        ev["id"] = f"EV{n:02d}"
        ev["facts"] = {**ev["facts"], **_price_facts(d, _d(ev["date"]) or as_of, as_of)}
    return out


def select_for_agent(evs: list[dict]) -> list[dict]:
    """The events DC-14 judges, by priority, at most MAX_AGENT_EVENTS. The rest get the lower class (S13-J7)."""
    want = sorted((e for e in evs if e.get("needs_agent")), key=lambda e: (PRIORITY.get(e["kind"], 5), e["id"]))
    for e in want[MAX_AGENT_EVENTS:]:
        e.update(needs_agent=False, code_reason="cap", decided_by="code")
        if "class" not in e["final"]:
            e["class"] = "D"
        if e.get("priced_in") is None:
            e["priced_in"] = True
    return want[:MAX_AGENT_EVENTS]


def apply_answer(ev: dict, answer: dict) -> dict:
    """Merges the DC-14 output. Code-final fields are never overwritten; undecided means lower class and priced in.
    An answer the critic rejected or the agent left undecided is unclear: it never heats (S13-J7), so it is D and
    priced in unless code already fixed the class."""
    fin = set(ev.get("final") or [])
    opt = answer.get("option")
    if answer.get("status") in UNCLEAR_STATUS:
        answer = {**answer, "option": "D", "priced_in": True}
        ev["code_reason"] = (ev.get("code_reason") + "; " if ev.get("code_reason") else "") + \
            f"DC-14 {answer['status']}: lower class (S13-J7)"
        opt = "D"
    if "class" not in fin:
        ev["class"] = opt if opt in CLASSES else "D"
        ev["decided_by"] = "DC-14"
    if ev.get("max_class") == "B" and ev["class"] == "A":
        ev["class"] = "B"                                                   # S08-R19: only an annual change is an A
    if "priced_in" not in fin:
        pi = answer.get("priced_in")
        ev["priced_in"] = pi if isinstance(pi, bool) else True              # S13-J7
    if "direction" not in fin:
        dr = answer.get("direction")
        ev["direction"] = dr if dr in ("for_idea", "against_idea") else None
    if "persistence" not in fin:
        p = answer.get("persistence")
        ev["persistence"] = p if p in PERSISTENCE else "none"
    if ev["timing"] == "expected" and not ev.get("expected_date"):
        ev["expected_date"] = answer.get("expected_date") or None
    if (answer.get("what") or "").strip():
        ev["what"] = answer["what"].strip()
    for k in ("level", "strength", "flow_technical", "peers_affected", "follow_on_expected"):
        if k in answer:
            ev[k] = answer[k]
    ev["why"] = answer.get("reasoning", "")
    ev["needs_agent"] = False
    return ev


def agent_error(ev: dict, e: Exception) -> dict:
    """DC-14 failed (budget, API): the lower class, never A (S13-J7)."""
    ev.update(needs_agent=False, decided_by="code", code_reason=f"agent_error: {type(e).__name__}")
    if "class" not in ev["final"] or ev["class"] == "A":
        ev["class"] = "D"
    if ev.get("priced_in") is None or ev["class"] == "D":
        ev["priced_in"] = True
    return ev


# ---------------------------------------------------------------- what counts (S08-R16, R18, R20, R23)

def _start(ev: dict) -> date | None:
    return _d(ev.get("date")) or _d(ev.get("expected_date"))


def active_until(ev: dict, next_earnings: Any = None) -> date | None:
    """S08-R20 / S09-R38: analyst 1-3 days, guidance until the next report, accumulation 2-6 months, none = the day."""
    s = _start(ev)
    if s is None:
        return None
    p = ev.get("persistence") or "none"
    if p in PERSIST_DAYS:
        return s + timedelta(days=PERSIST_DAYS[p])
    if p == "until_next_report":
        ne = _d(next_earnings)
        return ne if ne and ne > s else s + timedelta(days=UNTIL_REPORT_DAYS)
    if p == "accumulating_2_6m":
        return s + timedelta(days=ACCUMULATING_DAYS)
    return s


def _active(ev: dict, lo: date, hi: date, next_earnings: Any) -> bool:
    s, e = _start(ev), active_until(ev, next_earnings)
    return s is not None and e is not None and e >= lo and s <= hi


def counts(ev: dict, as_of: date | None = None, until: date | None = None, next_earnings: Any = None) -> bool:
    """A or B, for the idea, not priced in, not against the prospect, and (with as_of) active in (as_of, until]."""
    ok = (ev.get("class") in COUNTED and ev.get("direction") == "for_idea" and ev.get("priced_in") is False
          and "catalyst_against_prospect" not in (ev.get("flags") or []))
    if ok and as_of is not None:
        ok = _active(ev, as_of, until or _plus_months(as_of, load_policy().horizon.default_max_months), next_earnings)
    return ok


def serves_horizon(ev: dict, as_of: date, until: date | None = None, next_earnings: Any = None) -> bool:
    """A counted event that can carry a 1-3(+) month swing (S08-R21, S08-R33, S07-R13): a future dated one, or an
    occurred one whose effect lasts well into the horizon (e.g. a guidance raise until the next report). An occurred
    short-lived event (an analyst upgrade, days_1_3, or persistence none) belongs to the short-term track (S08-R20,
    S09-R38, S09-R44..R49, out of v1) and never sets catalyst_expected or the wait text."""
    if not counts(ev, as_of, until, next_earnings):
        return False
    s = _start(ev)
    if s is not None and s > as_of:
        return True
    if (ev.get("persistence") or "none") in SHORT_LIVED:
        return False
    au = active_until(ev, next_earnings)
    return au is not None and au >= as_of + timedelta(days=MIN_SERVE_DAYS)


def adverse(evs: Iterable[dict], as_of: date, next_earnings: Any = None) -> bool:
    """S08-R28 news_adverse: an A/B against the idea, not priced in, that occurred and is still active."""
    for ev in evs:
        s = _start(ev)
        if (ev.get("class") in COUNTED and ev.get("direction") == "against_idea" and ev.get("priced_in") is False
                and s and s <= as_of and _active(ev, as_of, as_of, next_earnings)):
            return True
    return False


# ---------------------------------------------------------------- the horizon (S10-R9, S10-R10, D-S10-5)

def catalyst_horizon(sigma_months: float | None, evs: list[dict], as_of: date, next_earnings: Any = None,
                     policy_h=None) -> dict:
    """The horizon from the sigma estimate and the catalysts. A chain is >= 2 dated future catalysts that count (A/B
    for the idea, not priced in; S08-R20, S10-R9): C, D and macro rows are never links (S08-R18, D-S08-2), a C may
    appear in the text only as context. A chain extends the horizon up to 6 months (D-S10-5). Never below sigma
    (S10-R35). expected: a counted event that serves the horizon (at least 3 months; S07-R13, S08-R21): future
    dated, or occurred and still active well into it; an occurred 1-3 day event never does (S08-R20)."""
    h = policy_h or load_policy().horizon
    dmax, xmax = float(h.default_max_months), float(h.extended_max_months)
    hi = _plus_months(as_of, xmax)
    future = sorted((e for e in evs if (t := _start(e)) and as_of < t <= hi), key=lambda e: (_start(e), e["id"]))
    chain = [e for e in future if counts(e)]                     # links: counted catalysts only
    context = [e for e in future if e.get("class") == "C" and e.get("direction") != "against_idea"]
    qualifies = len(chain) >= 2
    s = sigma_months
    if qualifies:
        cat = _months(as_of, _start(chain[-1])) + 3 / MONTH_DAYS
        months = min(max(s or 0.0, cat), max(xmax, s or 0.0)) if s is not None else min(cat, xmax)
        basis = "catalyst"
    else:
        single = _months(as_of, _start(chain[0])) + 3 / MONTH_DAYS if chain else 0.0
        months = max(s, min(single, dmax)) if s is not None else None
        basis = "sigma"                                   # one event is no chain (S10-R9)
    end = _plus_months(as_of, max(months or 0.0, dmax))
    live = [e for e in evs if serves_horizon(e, as_of, end, next_earnings)]
    text = ""
    if qualifies and months is not None and months > dmax:
        last = _start(chain[-1])
        text = "; ".join(f"{_start(e).isoformat()} {e.get('class')} {e.get('what')}" for e in chain)
        ctx = [e for e in context if _start(e) <= last]
        if ctx:                                                  # recorded only, never a link (D-S08-2)
            text += " | context (C, not a link): " + "; ".join(f"{_start(e).isoformat()} {e.get('what')}" for e in ctx)
    wait = None
    near = as_of + timedelta(days=WAIT_NEAR_DAYS)
    if live and not any(_active(e, as_of, near, next_earnings) for e in live):
        first = min(_start(e) for e in live)
        if first <= as_of + timedelta(days=WAIT_FAR_DAYS):
            wait = f"no catalyst in the next month; first on {first.isoformat()}: extend the horizon or wait"
    if wait is None and live and all(e.get("kind") == "own_report" for e in live):
        wait = "no catalyst until the report: waiting is allowed (S09-R53)"
    if months is not None:
        months = round(months, 2)
        if s is not None and months < s:
            months = s                                           # rounding never goes below sigma (S10-R35)
    return {"months": months, "basis": basis, "expected": bool(live),
            "chain": text, "chain_events": [e["id"] for e in chain] if text else [], "wait": wait}


# ---------------------------------------------------------------- output

def public(ev: dict) -> dict:
    return {k: v for k, v in ev.items() if k not in _INTERNAL}


def judgment_rows(evs: list[dict], next_earnings: Any = None) -> list[dict]:
    """j["catalysts"]: a superset of the report's {date, what, class, direction, priced_in}."""
    out = []
    for ev in evs:
        r = public(ev)
        if not r.get("url"):
            r.pop("url", None)                                  # no empty link for verify.collect_sources
        au = active_until(ev, next_earnings)
        r["active_until"] = au.isoformat() if au else None
        out.append(r)
    return out


def sources(d: dict) -> list[dict]:
    """The SEC documents behind the events and facts, for j["sources"] (verify.collect_sources checks them)."""
    docs = d.get("documents") or {}
    out = []
    for f in docs.get("current") or []:
        if f.get("index_url") or f.get("url"):
            out.append({"title": f"SEC {f.get('form')} {f.get('date')} ({', '.join(f.get('items') or [])})",
                        "url": f.get("index_url") or f.get("url")})
    for k in ("results", "results_prev"):
        r = docs.get(k)
        if r and r.get("url"):
            out.append({"title": f"SEC {r.get('form')} {r.get('date')}: {r.get('title') or 'results release'}"[:200],
                        "url": r["url"]})
    rf = docs.get("risk_factors")
    if rf and rf.get("url"):
        out.append({"title": f"SEC {rf.get('form')} Item 1A risk factors ({rf.get('filed')})", "url": rf["url"]})
        dl = rf.get("delta")
        if dl and dl.get("url"):
            out.append({"title": f"SEC {dl.get('form')} Item 1A ({dl.get('filed')})", "url": dl["url"]})
    seen: set[str] = set()
    return [s for s in out if not (s["url"] in seen or seen.add(s["url"]))]
