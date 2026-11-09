"""The thesis store (thesis-store spec, phase A; Ilay 08.11): the course's thread kept as data.

The course rests every trade on a chain: the macro stance, a sector on the same side, the company's story, the
idea's plan (S08-R28, S13-R10). The runs compute each link and save it as a report; nothing kept the chain itself.
Here each link is a standing thesis that is updated, not recomputed:

  macro    one thesis ("stance"): zone, compass now and in six months, bias, market, risk (S01-S03, A1)
  sector   one per SPDR ETF: side, score, note (S04-S05, A2)                           rests on the macro
  company  one per ticker: story, target, growth engines, KPIs, catalysts (S06-S07, A4) rests on its sector
  idea     one per ticker and direction: bucket, plan, trigger, heat, summary (S08-S10) rests on all three

A thesis has pillars (what it stands on, each with a check and a state: holding / weakened / broken) and versions.
A version records what it rested on (kind, subject, version), so a change upstream finds exactly the ideas built
on the old one. Evidence (filings, transcripts, headlines) and its links to pillars come in phase B; the tables
are created here so the schema is whole.

Phase A changes no behaviour: the store is filled from what the system already produces (the weekly research
dossier and the watchlist) and read by the dashboard. No rule of the course changes, nothing trades.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from qt.db import DB, dumps

SCHEMA = """
CREATE TABLE IF NOT EXISTS theses (
  id {ID},
  kind TEXT NOT NULL,                    -- macro | sector | company | idea
  subject TEXT NOT NULL,                 -- stance | XLE | SHEL | SHEL:long
  status TEXT NOT NULL,                  -- active | archived
  version INTEGER NOT NULL,
  as_of TEXT NOT NULL,                   -- the market day of the current version
  stale_after TEXT,                      -- when the course's cadence asks for a fresh look
  body {JSON} NOT NULL,
  rests_on {JSON},                       -- [{kind, subject, version}]
  updated_at {TS} NOT NULL,
  UNIQUE (kind, subject)
);
CREATE TABLE IF NOT EXISTS thesis_versions (
  id {ID},
  thesis_id BIGINT NOT NULL,
  version INTEGER NOT NULL,
  as_of TEXT NOT NULL,
  created_at {TS} NOT NULL,
  author TEXT NOT NULL,                  -- agents | session | code | ilay
  source TEXT,                           -- dossier:2026-10-20 | watchlist:12
  body {JSON} NOT NULL,
  pillars {JSON},
  rests_on {JSON},
  change TEXT,                           -- what changed, one line
  UNIQUE (thesis_id, version)
);
CREATE TABLE IF NOT EXISTS pillars (
  id {ID},
  thesis_id BIGINT NOT NULL,
  key TEXT NOT NULL,
  claim TEXT NOT NULL,
  kind TEXT,                             -- DC-12 kinds (kpi, risk_10k, estimates_surprises, liquidity_dilution,
                                         -- contract) or judgment / data_threshold
  check_text TEXT,
  status TEXT NOT NULL,                  -- holding | weakened | broken
  since_version INTEGER NOT NULL,
  last_checked TEXT,
  UNIQUE (thesis_id, key)
);
CREATE TABLE IF NOT EXISTS evidence (
  id {ID},
  subject TEXT NOT NULL,                 -- ticker, ETF or macro
  kind TEXT NOT NULL,                    -- 10-K | 10-Q | 8-K | transcript | news | macro_print | price_event
  url TEXT NOT NULL,
  published TEXT,
  title TEXT,
  summary TEXT,
  body TEXT,
  created_at {TS} NOT NULL,
  UNIQUE (url)
);
CREATE TABLE IF NOT EXISTS evidence_links (
  id {ID},
  evidence_id BIGINT NOT NULL,
  pillar_id BIGINT NOT NULL,
  stance TEXT NOT NULL,                  -- supports | against | neutral
  decided_by TEXT NOT NULL,
  decision_id BIGINT,
  created_at {TS} NOT NULL
);
"""

KINDS = ("macro", "sector", "company", "idea")
KIND_HE = {"macro": "מקרו", "sector": "סקטור", "company": "חברה", "idea": "רעיון"}
STATUS_HE = {"holding": "מחזיק", "weakened": "נחלש", "broken": "נשבר"}
# when the course's cadence asks for a fresh look (thesis-store spec 3.3)
STALE_DAYS = {"macro": 30,          # S02-R7, S13-R25: the stance monthly (the written research every two weeks)
              "sector": 7,          # S04-R29: the arena weekly (the full score monthly)
              "company": 90,        # not from course: the next quarterly report, at most 90 days
              "idea": 7}            # S13-R25, S08-R31: old ideas revisited weekly
MACRO = "stance"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _d(v: Any) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def _key(body: dict, pillars: list[dict], rests_on: list[dict]) -> str:
    """What makes two versions different: the content, not the day it was written."""
    ps = [{k: p.get(k) for k in ("key", "claim", "kind", "check")} for p in pillars]
    return json.dumps([body, ps, rests_on], sort_keys=True, ensure_ascii=False, default=str)


class Store:
    def __init__(self, db: DB):
        self.db = db
        db.ddl(SCHEMA)

    # ------------------------------------------------------------------ read
    def get(self, kind: str, subject: str) -> dict | None:
        t = self.db.one("SELECT * FROM theses WHERE kind = ? AND subject = ?", (kind, subject),
                        json_cols=("body", "rests_on"))
        if t is not None:
            t["pillars"] = self.pillars(t["id"])
        return t

    def pillars(self, thesis_id: int) -> list[dict]:
        return self.db.all("SELECT * FROM pillars WHERE thesis_id = ? ORDER BY id", (thesis_id,))

    def all(self, kind: str | None = None, status: str = "active") -> list[dict]:
        q, args = "SELECT * FROM theses WHERE status = ?", [status]
        if kind:
            q, args = q + " AND kind = ?", args + [kind]
        return self.db.all(q + " ORDER BY kind, subject", args, json_cols=("body", "rests_on"))

    def history(self, kind: str, subject: str) -> list[dict]:
        return self.db.all("SELECT v.* FROM thesis_versions v JOIN theses t ON t.id = v.thesis_id "
                           "WHERE t.kind = ? AND t.subject = ? ORDER BY v.version DESC", (kind, subject),
                           json_cols=("body", "pillars", "rests_on"))

    def dependents(self, kind: str, subject: str, version: int | None = None) -> list[dict]:
        """Active theses whose current version rests on (kind, subject), on `version` when given: after a change,
        the ones still built on the old version are the ones to look at again (spec 3.4)."""
        out = []
        for t in self.all():
            for r in t.get("rests_on") or []:
                if r["kind"] == kind and r["subject"] == subject and (version is None or r["version"] == version):
                    out.append(t)
                    break
        return out

    def stale(self, as_of: date) -> list[dict]:
        return [t for t in self.all() if t["stale_after"] and t["stale_after"] < _d(as_of).isoformat()]

    def chain(self, idea_subject: str) -> list[dict]:
        """macro -> sector -> company -> idea, each as stored now, with whether the idea rests on its current
        version (False: the upstream thesis changed after the idea was written)."""
        idea = self.get("idea", idea_subject)
        if idea is None:
            return []
        out = []
        for r in idea.get("rests_on") or []:
            t = self.get(r["kind"], r["subject"])
            if t is not None:
                out.append({**t, "current": t["version"] == r["version"], "rested_on": r["version"]})
        order = {k: i for i, k in enumerate(KINDS)}
        return sorted(out, key=lambda t: order[t["kind"]]) + [{**idea, "current": True, "rested_on": idea["version"]}]

    # ------------------------------------------------------------------ write
    def put(self, kind: str, subject: str, body: dict, pillars: Iterable[dict] = (), rests_on: Iterable[dict] = (),
            *, as_of: date | str, author: str, source: str | None = None, change: str | None = None,
            stale_after: date | str | None = None) -> int | None:
        """A new version when the content changed; None when it did not, or when `as_of` is older than the
        current version (a backfill replaying old reports never overwrites a newer thesis)."""
        assert kind in KINDS, kind
        pillars, rests_on = [dict(p) for p in pillars], [dict(r) for r in rests_on]
        as_of = _d(as_of)
        stale = _d(stale_after) if stale_after else as_of + timedelta(days=STALE_DAYS[kind])
        cur = self.db.one("SELECT * FROM theses WHERE kind = ? AND subject = ?", (kind, subject),
                          json_cols=("body", "rests_on"))
        now = _now()
        if cur is not None:
            if as_of.isoformat() < cur["as_of"]:
                return None
            last = self.db.one("SELECT pillars FROM thesis_versions WHERE thesis_id = ? AND version = ?",
                               (cur["id"], cur["version"]), json_cols=("pillars",))
            if _key(cur["body"], (last or {}).get("pillars") or [], cur.get("rests_on") or []) == _key(body, pillars, rests_on):
                if as_of.isoformat() > cur["as_of"]:           # the same thesis, looked at again: fresh, no version
                    self.db.execute("UPDATE theses SET as_of = ?, stale_after = ?, status = 'active' WHERE id = ?",
                                    (as_of.isoformat(), stale.isoformat(), cur["id"]))
                return None
            tid, version = cur["id"], cur["version"] + 1
            self.db.execute("UPDATE theses SET version = ?, as_of = ?, stale_after = ?, body = ?, rests_on = ?, "
                            "status = 'active', updated_at = ? WHERE id = ?",
                            (version, as_of.isoformat(), stale.isoformat(), dumps(body), dumps(rests_on), now, tid))
        else:
            version = 1
            tid = self.db.insert("theses", dict(kind=kind, subject=subject, status="active", version=1,
                                                as_of=as_of.isoformat(), stale_after=stale.isoformat(), body=dumps(body),
                                                rests_on=dumps(rests_on), updated_at=now))
        self.db.insert("thesis_versions", dict(thesis_id=tid, version=version, as_of=as_of.isoformat(), created_at=now,
                                               author=author, source=source, body=dumps(body), pillars=dumps(pillars),
                                               rests_on=dumps(rests_on),
                                               change=change or ("נוצרה" if version == 1 else _diff(cur["body"], body))))
        self._pillars(tid, version, pillars, as_of)
        return version

    def _pillars(self, tid: int, version: int, pillars: list[dict], as_of: date) -> None:
        """The current pillars: a pillar whose claim did not change keeps its state and its first version."""
        old = {p["key"]: p for p in self.pillars(tid)}
        self.db.execute("DELETE FROM pillars WHERE thesis_id = ?", (tid,))
        for p in pillars:
            o = old.get(p["key"])
            same = o is not None and o["claim"] == p["claim"]
            self.db.insert("pillars", dict(thesis_id=tid, key=p["key"], claim=p["claim"], kind=p.get("kind"),
                                           check_text=p.get("check"),
                                           status=p.get("status") or (o["status"] if same else "holding"),
                                           since_version=o["since_version"] if same else version,
                                           last_checked=as_of.isoformat()))

    def archive(self, kind: str, subject: str) -> None:
        self.db.execute("UPDATE theses SET status = 'archived', updated_at = ? WHERE kind = ? AND subject = ?",
                        (_now(), kind, subject))


def _diff(old: dict, new: dict) -> str:
    """One line: the fields that changed (the agents write their own line from phase C)."""
    keys = [k for k in new if old.get(k) != new.get(k)] + [k for k in old if k not in new]
    shown = []
    for k in keys[:4]:
        o, n = old.get(k), new.get(k)
        shown.append(f"{k}: {o} → {n}" if isinstance(n, (str, int, float, type(None))) and isinstance(o, (str, int, float, type(None)))
                     and len(str(o)) + len(str(n)) < 60 else k)
    return "; ".join(shown) + (f" (+{len(keys) - 4})" if len(keys) > 4 else "") if keys else "ללא שינוי בתוכן"


# ---------------------------------------------------------------------- from the weekly research dossier
def _author(d: dict) -> str:
    return "agents" if str((d.get("meta") or {}).get("judged_by") or "").startswith("Agents") else "session"


def _ref(kind: str, subject: str, version: int | None) -> dict | None:
    return {"kind": kind, "subject": subject, "version": version} if version else None


# what would move each A1 card (a pointer to the rule, not a new threshold)
A1_CHECKS = {"DC-01": "סולם הפעילות חוצה את גבול האזור (S02)", "DC-02": "כיוון ה-ISM או ה-CPI מתהפך ב-3 חודשים (S02)",
             "DC-03": "מצב השוק משתנה: שיא, תיקון או קו השוק הדובי (S03)", "DC-04": "מרווח ה-HY חוצה את ממוצע 250 הימים (S01-R2)"}
A1_NAMES = {"DC-01": "מצב", "DC-02": "מצפן", "DC-03": "מקרו מול שוק", "DC-04": "סיכון"}


def macro_thesis(d: dict) -> tuple[dict, list[dict]]:
    st = ((d.get("macro") or {}).get("stage2") or {}).get("stance") or {}
    a1 = (d.get("judgments") or {}).get("A1") or {}
    body = {"zone": (a1.get("DC-01") or {}).get("choice") or st.get("zone"), "bias": st.get("bias"),
            "compass_now": (a1.get("DC-02") or {}).get("choice") or st.get("compass_now"),
            "compass_6m": (a1.get("DC-02") or {}).get("in_six_months"),
            "market": (a1.get("DC-03") or {}).get("choice"), "risk": (a1.get("DC-04") or {}).get("choice"),
            "fed": (st.get("fed") or {}).get("period"), "phase": (d.get("sectors") or {}).get("phase"),
            "notes": a1.get("notes") or []}
    pillars = [{"key": c, "claim": f"{A1_NAMES[c]}: {(a1[c] or {}).get('choice')}. {(a1[c] or {}).get('why', '')}".strip(),
                "kind": "judgment", "check": A1_CHECKS[c]} for c in A1_CHECKS if a1.get(c)]
    return body, pillars


def sector_thesis(d: dict, etf: str) -> tuple[dict, list[dict]] | None:
    s = d.get("sectors") or {}
    sc = (s.get("scorecard") or {}).get(etf)
    if sc is None:
        return None
    side = next((k for k in ("long", "short", "neutral") if etf in (s.get(k) or [])), sc.get("bucket"))
    notes = ((d.get("judgments") or {}).get("A2") or {}).get("notes") or {}
    body = {"side": side, "total": sc.get("total"), "type": sc.get("type"), "rs_avg": sc.get("rs_avg"),
            "tech_rank": sc.get("tech_rank"), "fund_rank": sc.get("fund_rank"), "macro_score": sc.get("macro"),
            "growth": sc.get("growth"), "note": notes.get(etf) if isinstance(notes, dict) else None,
            "industries": {x["name"]: x.get("tag") for x in (s.get("industries") or {}).get(etf) or []
                           if isinstance(x, dict) and x.get("tag") and x["tag"] != "no_tag"} or None}
    side_he = {"long": "לונג", "short": "שורט", "neutral": "ניטרלי"}.get(side, side)
    pillars = [{"key": "side", "claim": f"הסקטור ב{side_he}: ציון {sc.get('total')}, RS ממוצע {round(sc.get('rs_avg') or 0, 1)} מול SPY",
                "kind": "data_threshold", "check": "הציון החודשי מעביר את הסקטור צד, או שה-RS הממוצע מתהפך (S04)"}]
    return body, pillars


def company_thesis(i: dict) -> tuple[dict, list[dict]]:
    j, v = i.get("judgment") or {}, i.get("valuation") or {}
    body = {"name": (i.get("profile") or {}).get("name") or (i.get("quote") or {}).get("name"),
            "industry": (i.get("profile") or {}).get("industry"), "etf": i.get("etf"),
            "profile": (i.get("row") or {}).get("profile"), "story_type": j.get("story_type"), "story": j.get("story"),
            "thesis": j.get("thesis"), "growth_engines": j.get("growth_engines") or [], "premium": j.get("premium"),
            "target": (i.get("plan") or {}).get("target"), "target_source": (i.get("plan") or {}).get("target_source"),
            "target_upside_pct": v.get("target_upside_pct"), "kpis": j.get("kpis") or [],
            "catalysts": j.get("catalysts") or [], "next_earnings": (i.get("catalysts") or {}).get("next_earnings")}
    pillars = [{"key": f"b{n}", "claim": b.get("text", ""), "kind": b.get("kind"), "check": b.get("check")}
               for n, b in enumerate(j.get("breakers") or [], 1) if isinstance(b, dict)]
    return body, pillars


def idea_thesis(i: dict, bucket: str) -> dict:
    j, p = i.get("judgment") or {}, i.get("plan") or {}
    tldr = j.get("tldr") or {}
    return {"bucket": bucket, "direction": i.get("direction"), "bucket_reason": i.get("bucket_reason") or None,
            "entry": p.get("entry"), "stop": p.get("stop"), "target": p.get("target"), "rr": p.get("rr"), "k": p.get("k"),
            "horizon_months": p.get("horizon_months"), "trigger": j.get("trigger"), "heat": j.get("heat"),
            "timing": (i.get("timing") or {}).get("combined"), "catalyst_chain": j.get("catalyst_chain"),
            "tldr": tldr or None, "line": tldr.get("line") or j.get("why")}


def sync_dossier(store: Store, d: dict, source: str | None = None) -> dict[str, int]:
    """Every link of one weekly research into the store: the stance, the 11 sectors, each deep-dived company and
    each idea, with what each rests on. Returns how many theses got a new version, by kind."""
    as_of, author = d["meta"]["as_of"], _author(d)
    source = source or f"dossier:{as_of}"
    out = dict.fromkeys(KINDS, 0)

    def put(kind, subject, body, pillars=(), rests=(), stale=None):
        v = store.put(kind, subject, body, pillars, [r for r in rests if r], as_of=as_of, author=author, source=source,
                      stale_after=stale)
        if v:
            out[kind] += 1
        t = store.get(kind, subject)
        return t["version"] if t else None

    mv = None
    if (d.get("macro") or {}).get("stage2"):
        mv = put("macro", MACRO, *macro_thesis(d))
    sv = {}
    for etf in ((d.get("sectors") or {}).get("scorecard") or {}):
        got = sector_thesis(d, etf)
        if got:
            sv[etf] = put("sector", etf, *got, rests=[_ref("macro", MACRO, mv)])
    for sym, i in (d.get("ideas") or {}).items():
        etf = i.get("etf")
        body, pillars = company_thesis(i)
        ne = body.get("next_earnings")
        stale = ne if ne and ne > as_of else None
        cv = put("company", sym, body, pillars, rests=[_ref("sector", etf, sv.get(etf))], stale=stale)
        subject = f"{sym}:{i.get('direction') or 'long'}"
        if i.get("bucket") == "reject":
            if store.get("idea", subject):
                put("idea", subject, idea_thesis(i, "reject"), rests=[_ref("macro", MACRO, mv),
                    _ref("sector", etf, sv.get(etf)), _ref("company", sym, cv)])
                store.archive("idea", subject)
            continue
        put("idea", subject, idea_thesis(i, i["bucket"]), rests=[_ref("macro", MACRO, mv), _ref("sector", etf, sv.get(etf)),
                                                                  _ref("company", sym, cv)])
    return out


def sync_watchlist(store: Store, wl: dict | None, as_of: date | str, source: str | None = None) -> int:
    """Watchlist entries with no idea thesis yet (a session by hand, an old list): a thesis from what the entry
    holds, by code. Entries the research already wrote are left to it."""
    n = 0
    for e in (wl or {}).get("entries") or []:
        subject = f"{e['symbol']}:{e.get('direction') or 'long'}"
        if store.get("idea", subject) is not None:
            continue
        body = {"bucket": e.get("bucket"), "direction": e.get("direction"), "entry": e.get("price_at_add"),
                "target": e.get("target"), "k": e.get("k"), "trigger": {"kind": e.get("trigger_kind"), "level": e.get("trigger_level")},
                "heat": e.get("heat"), "line": e.get("tldr") or e.get("why_not") or None, "added": e.get("added")}
        pillars = [{"key": f"b{k}", "claim": t, "kind": None, "check": None} for k, t in enumerate(e.get("breakers") or [], 1)]
        rests = [r for r in (_ref("sector", e["etf"], (store.get("sector", e["etf"]) or {}).get("version")) if e.get("etf") else None,
                             _ref("macro", MACRO, (store.get("macro", MACRO) or {}).get("version"))) if r]
        if store.put("idea", subject, body, pillars, rests, as_of=e.get("added") or as_of, author="code",
                     source=source or "watchlist"):
            n += 1
    return n


def backfill(db: DB) -> dict[str, Any]:
    """Every saved research report, oldest first, then the newest watchlist: the store's starting point and its
    history. Safe to run again: a thesis whose content did not change gets no new version."""
    import yaml
    store = Store(db)
    from qt.approvals import Queue
    Queue(db)                                                        # the uploads table, on a new database
    total, reports = dict.fromkeys(KINDS, 0), 0
    for r in db.all("SELECT id, name FROM uploads WHERE kind = 'dossier' ORDER BY id"):
        try:
            d = json.loads(db.one("SELECT content FROM uploads WHERE id = ?", (r["id"],))["content"])
            d["meta"]["as_of"]
        except (ValueError, KeyError, TypeError):
            continue
        for k, v in sync_dossier(store, d, f"dossier:{d['meta']['as_of']}").items():
            total[k] += v
        reports += 1
    wl = Queue(db).latest_upload("watchlist")
    extra = 0
    if wl:
        try:
            extra = sync_watchlist(store, yaml.safe_load(wl["content"]), date.today(), f"watchlist:{wl['id']}")
        except yaml.YAMLError:
            pass
    return {"reports": reports, "versions": total, "from_watchlist": extra,
            "theses": {k: len(store.all(k)) for k in KINDS}}
