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
on the old one. Evidence (phase B): every headline and 8-K that A5 classifies is kept once, and A5 links it to the
pillars it bears on (DC-14 `pillars`: supports / weakens / breaks / neutral). A pillar that breaks or weakens
changes state here; the thesis itself changes only through a new version.

Phase C: the agents work from the store. A4 gets the current company thesis, its pillars and their evidence before
it writes the week's cards, and writes what changed (DC-12 `change`); a pillar whose claim stays keeps its key, its
state and its evidence. A7 (DC-21) gets the thesis and its pillars with every event on a position (the course
card's first input). A new version upstream marks the theses still built on the old one as due for a fresh look.
A material change to the thesis of an open position (a pillar dropped or rewritten, the side, bucket, story type or
target changed) waits for Ilay in the approval queue (DC-12: "changing or deleting a breaker is flagged to Ilay").

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
  stance TEXT NOT NULL,                  -- the DC-14 effect: supports | weakens | breaks | neutral
  decided_by TEXT NOT NULL,
  decision_id BIGINT,
  created_at {TS} NOT NULL
);
CREATE TABLE IF NOT EXISTS thesis_pending (
  id {ID},
  kind TEXT NOT NULL,
  subject TEXT NOT NULL,
  as_of TEXT NOT NULL,
  author TEXT NOT NULL,
  source TEXT,
  body {JSON} NOT NULL,
  pillars {JSON},
  rests_on {JSON},
  stale_after TEXT,
  change TEXT,                           -- what would change, one line (shown to Ilay)
  status TEXT NOT NULL,                  -- pending | approved | rejected | expired | superseded
  created_at {TS} NOT NULL,
  decided_at {TS}
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
        """The current pillars, updated in place (their evidence links stay attached): a pillar whose claim did not
        change keeps its state and its first version; a new claim starts holding; a dropped key is removed."""
        old = {p["key"]: p for p in self.pillars(tid)}
        keep = {p["key"] for p in pillars}
        for k, o in old.items():
            if k not in keep:
                self.db.execute("DELETE FROM pillars WHERE id = ?", (o["id"],))
        for p in pillars:
            o = old.get(p["key"])
            same = o is not None and o["claim"] == p["claim"]
            row = dict(claim=p["claim"], kind=p.get("kind"), check_text=p.get("check"),
                       status=p.get("status") or (o["status"] if same else "holding"),
                       since_version=o["since_version"] if same else version, last_checked=as_of.isoformat())
            if o is None:
                self.db.insert("pillars", dict(thesis_id=tid, key=p["key"], **row))
            else:
                self.db.execute("UPDATE pillars SET " + ", ".join(f"{c} = ?" for c in row) + " WHERE id = ?",
                                (*row.values(), o["id"]))

    # ------------------------------------------------------------------ evidence (phase B)
    def ticker_pillars(self, symbol: str) -> list[dict]:
        """The pillars a headline about `symbol` can bear on: its company thesis and its active ideas, each with a
        qualified key (company:b1, idea:b2) so A5 can name it."""
        out = []
        for kind, subject in [("company", symbol)] + [("idea", t["subject"]) for t in self.all("idea")
                                                       if t["subject"].split(":")[0] == symbol]:
            t = self.get(kind, subject)
            for p in (t or {}).get("pillars") or []:
                out.append({**p, "qkey": f"{kind}:{p['key']}", "thesis_kind": kind, "subject": subject})
        return out

    def add_evidence(self, subject: str, kind: str, url: str, published: str | None, title: str | None,
                     summary: str | None = None, body: str | None = None) -> int:
        self.db.insert_new("evidence", dict(subject=subject, kind=kind, url=url, published=published, title=title,
                                            summary=summary, body=body, created_at=_now()), ["url"])
        return self.db.one("SELECT id FROM evidence WHERE url = ?", (url,))["id"]

    def link(self, evidence_id: int, pillar: dict, effect: str, decided_by: str, decision_id: int | None,
             as_of: date | str) -> str | None:
        """Record what the evidence does to the pillar. breaks -> broken; weakens -> weakened (a broken pillar stays
        broken). Supports never restores a pillar: that takes a new thesis version. Returns the new state when it
        changed."""
        self.db.insert("evidence_links", dict(evidence_id=evidence_id, pillar_id=pillar["id"], stance=effect,
                                              decided_by=decided_by, decision_id=decision_id, created_at=_now()))
        new = {"breaks": "broken", "weakens": "weakened"}.get(effect)
        if new is None or pillar["status"] == "broken" or pillar["status"] == new:
            self.db.execute("UPDATE pillars SET last_checked = ? WHERE id = ?", (_d(as_of).isoformat(), pillar["id"]))
            return None
        self.db.execute("UPDATE pillars SET status = ?, last_checked = ? WHERE id = ?",
                        (new, _d(as_of).isoformat(), pillar["id"]))
        return new

    def evidence_for(self, pillar_id: int, limit: int = 10) -> list[dict]:
        return self.db.all("SELECT e.*, l.stance AS effect, l.decided_by, l.created_at AS linked_at FROM evidence_links l "
                           "JOIN evidence e ON e.id = l.evidence_id WHERE l.pillar_id = ? ORDER BY l.id DESC LIMIT ?",
                           (pillar_id, limit))

    def archive(self, kind: str, subject: str) -> None:
        self.db.execute("UPDATE theses SET status = 'archived', updated_at = ? WHERE kind = ? AND subject = ?",
                        (_now(), kind, subject))

    # ------------------------------------------------------------------ phase C: cascade and Ilay's approval
    def behind(self, t: dict | None) -> list[dict]:
        """The links of `t`'s rests_on whose thesis has a newer version than the one `t` was written on."""
        return [r for r in (t or {}).get("rests_on") or []
                if (self.get(r["kind"], r["subject"]) or {}).get("version", r["version"]) != r["version"]]

    def mark_due(self, kind: str, subject: str, old_version: int, as_of: date | str) -> list[dict]:
        """After (kind, subject) got a new version: the active theses still resting on `old_version` are due for a
        fresh look now (spec 3.4). Returns them."""
        due = self.dependents(kind, subject, version=old_version)
        day = _d(as_of).isoformat()
        for t in due:
            if not t["stale_after"] or t["stale_after"] > day:
                self.db.execute("UPDATE theses SET stale_after = ? WHERE id = ?", (day, t["id"]))
        return due

    def material(self, kind: str, subject: str, body: dict, pillars: list[dict]) -> str | None:
        """What Ilay has to see before a new version of an open position's thesis goes in, or None: a pillar dropped
        or rewritten (DC-12: changing or deleting a breaker is flagged), or the side, bucket, story type or target
        changed. A new pillar alone, or new wording elsewhere, goes in directly."""
        cur = self.get(kind, subject)
        if cur is None:
            return None
        new = {p["key"]: p["claim"] for p in pillars}
        out = [f"עמוד הוסר: {p['claim']}" if p["key"] not in new else f"עמוד שונה: {p['claim']} → {new[p['key']]}"
               for p in cur["pillars"] if new.get(p["key"]) != p["claim"]]
        for k in MATERIAL_KEYS:
            if k in body and (cur["body"] or {}).get(k) != body.get(k) and (cur["body"] or {}).get(k) is not None:
                out.append(f"{k}: {(cur['body'] or {}).get(k)} → {body.get(k)}")
        return "; ".join(out) or None

    def hold(self, kind: str, subject: str, body: dict, pillars: Iterable[dict] = (), rests_on: Iterable[dict] = (),
             *, as_of: date | str, author: str, source: str | None = None, change: str | None = None,
             stale_after: date | str | None = None) -> int:
        """A new version kept aside until Ilay answers (resolve). An earlier pending one for the thesis is replaced."""
        self.db.execute("UPDATE thesis_pending SET status = 'superseded', decided_at = ? WHERE kind = ? AND subject = ? "
                        "AND status = 'pending'", (_now(), kind, subject))
        return self.db.insert("thesis_pending", dict(
            kind=kind, subject=subject, as_of=_d(as_of).isoformat(), author=author, source=source, body=dumps(body),
            pillars=dumps(list(pillars)), rests_on=dumps(list(rests_on)),
            stale_after=_d(stale_after).isoformat() if stale_after else None, change=change, status="pending",
            created_at=_now()))

    def pending(self, pending_id: int | None = None) -> list[dict]:
        q, args = "SELECT * FROM thesis_pending WHERE status = 'pending'", []
        if pending_id is not None:
            q, args = "SELECT * FROM thesis_pending WHERE id = ?", [pending_id]
        return self.db.all(q + " ORDER BY id", args, json_cols=("body", "pillars", "rests_on"))

    def resolve(self, pending_id: int, verdict: str) -> int | None:
        """approved -> the version goes in (by Ilay's hand, as the author of record says); rejected or expired -> it
        is kept for the record only. Returns the new version, if one went in."""
        rows = self.pending(pending_id)
        if not rows or rows[0]["status"] != "pending":
            return None
        p = rows[0]
        self.db.execute("UPDATE thesis_pending SET status = ?, decided_at = ? WHERE id = ?", (verdict, _now(), pending_id))
        if verdict != "approved":
            return None
        cur = self.get(p["kind"], p["subject"])
        v = self.put(p["kind"], p["subject"], p["body"], p["pillars"] or [], p["rests_on"] or [], as_of=p["as_of"],
                     author=f"{p['author']}+ilay", source=p["source"], change=p["change"], stale_after=p["stale_after"])
        if v and cur is not None:
            self.mark_due(p["kind"], p["subject"], cur["version"], p["as_of"])
        return v

    def keep_keys(self, kind: str, subject: str, pillars: list[dict]) -> list[dict]:
        """A pillar whose claim is already in the thesis keeps its key (so its state and evidence stay attached),
        whatever order the agent wrote it in this week; a new claim gets the next free key."""
        cur = {p["claim"]: p["key"] for p in (self.get(kind, subject) or {}).get("pillars") or []}
        if not cur:
            return pillars
        used = set(cur.values())
        out, n = [], 0
        for p in pillars:
            k = cur.get(p["claim"])
            if k is None:
                n += 1
                while f"b{n}" in used:
                    n += 1
                k = f"b{n}"
            used.add(k)
            out.append({**p, "key": k})
        return out

    def prior(self, symbol: str, direction: str | None = None, evidence: int = 3) -> dict | None:
        """The standing company thesis of `symbol` and its idea, with each pillar's state and latest evidence: what
        A4 updates and what A7 weighs an event against. None when the store has neither."""
        c = self.get("company", symbol)
        ideas = [t for t in self.all("idea") if t["subject"].split(":")[0] == symbol
                 and (direction is None or t["subject"].endswith(f":{direction}"))]
        if c is None and not ideas:
            return None

        def ps(t):
            return [{"key": p["key"], "claim": p["claim"], "kind": p["kind"], "check": p["check_text"],
                     "status": p["status"], "since_version": p["since_version"],
                     "evidence": [{"effect": e["effect"], "title": e.get("title") or e.get("summary"),
                                   "published": (e.get("published") or "")[:10]} for e in self.evidence_for(p["id"], evidence)]}
                    for p in self.pillars(t["id"])]
        out: dict[str, Any] = {}
        if c is not None:
            b = c["body"] or {}
            out["company"] = {"version": c["version"], "as_of": c["as_of"], "story_type": b.get("story_type"),
                              "story": b.get("story"), "thesis": b.get("thesis"), "target": b.get("target"),
                              "growth_engines": b.get("growth_engines") or [], "pillars": ps(c)}
        for t in ideas:
            b = t["body"] or {}
            out.setdefault("ideas", []).append({"subject": t["subject"], "version": t["version"], "as_of": t["as_of"],
                                                "bucket": b.get("bucket"), "line": b.get("line"), "pillars": ps(t)})
        return out


MATERIAL_KEYS = ("direction", "bucket", "story_type", "target", "stop")
MAX_REVISIT = 4      # standing theses re-researched a week, beside the new ideas' deep dives (not from course: cost)
REVISIT_HE = {"pillar": "עמוד נשבר או נחלש מאז הגרסה האחרונה", "report": "דוח רבעוני פורסם מאז",
              "upstream": "התזה שמעליה השתנתה"}


def due_for_review(store: Store, as_of: date | str, held: Iterable[str] = (), limit: int = MAX_REVISIT) -> list[dict]:
    """The standing theses the weekly research looks at again (thesis-store spec 3.4, phase D), open positions first:
    a pillar that broke or weakened after the thesis was last written, a quarterly report since (the company thesis
    is stale on its next-earnings date), or a thesis upstream that changed. Only stocks with an active idea or an
    open position: a rejected stock is not re-researched. [{sym, etf, dir, revisit: [reason keys], held}]"""
    day, held = _d(as_of).isoformat(), set(held)
    ideas = {t["subject"].split(":")[0]: t for t in store.all("idea")}
    out = []
    for sym in sorted(set(ideas) | held):
        c = store.get("company", sym)
        i = store.get("idea", ideas[sym]["subject"]) if sym in ideas else None
        if c is None and i is None:
            continue
        why = []
        for t in [x for x in (c, i) if x]:
            for p in t["pillars"]:
                if p["status"] != "holding" and any((e.get("linked_at") or "")[:10] > t["as_of"]
                                                    for e in store.evidence_for(p["id"], 3)):
                    why.append("pillar")
        if c is not None and c["stale_after"] and c["stale_after"] <= day and (c["body"] or {}).get("next_earnings") \
                and c["body"]["next_earnings"] <= day:
            why.append("report")
        if any(store.behind(t) for t in (c, i) if t):
            why.append("upstream")
        if not why:
            continue
        why = sorted(set(why), key=list(REVISIT_HE).index)
        etf = ((c or {}).get("body") or {}).get("etf") or next((r["subject"] for r in (i or {}).get("rests_on") or []
                                                               if r["kind"] == "sector"), None)
        out.append({"sym": sym, "etf": etf, "dir": i["subject"].split(":")[1] if i else "long", "revisit": why,
                    "held": sym in held})
    out.sort(key=lambda x: (not x["held"], list(REVISIT_HE).index(x["revisit"][0]), x["sym"]))
    return out[:limit]


def thesis_facts(fs, prior: dict | None, symbol: str, as_of: date | str) -> int:
    """The standing thesis on a fact sheet (A4's update, A7's event): the story and each pillar with its state, its
    check and its latest evidence. Returns how many pillars went in."""
    if not prior:
        return 0
    n, src = 0, "code:theses"
    day = _d(as_of)
    c = prior.get("company")
    if c:
        fs.add(f"{symbol}.thesis.company", f"v{c['version']} ({c['as_of']}) {c.get('story_type') or ''}: "
               f"{c.get('story') or ''} | {c.get('thesis') or ''}"[:900], src, day)
    for kind, t in ([("company", c)] if c else []) + [("idea", i) for i in prior.get("ideas") or []]:
        if kind == "idea":
            fs.add(f"{symbol}.thesis.idea", f"{t['subject']} v{t['version']}: {t.get('bucket') or ''} · {t.get('line') or ''}"[:600],
                   src, day)
        for p in t["pillars"]:
            ev = " · ".join(f"{e['effect']}: {e['title']} ({e['published']})" for e in p["evidence"])
            fs.add(f"{symbol}.pillar.{kind}:{p['key']}",
                   f"[{STATUS_HE.get(p['status'], p['status'])}] {p['claim']}" + (f" · בדיקה: {p['check']}" if p.get("check") else "")
                   + (f" · ראיות: {ev}" if ev else ""), src, day)
            n += 1
    return n


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


def sync_dossier(store: Store, d: dict, source: str | None = None, held: Iterable[str] = ()) -> dict[str, Any]:
    """Every link of one weekly research into the store: the stance, the 11 sectors, each deep-dived company and
    each idea, with what each rests on. Returns how many theses got a new version, by kind; under "pending" the
    open positions' changes that wait for Ilay (held: the symbols of the open positions) and under "due" the
    theses a change upstream left on an old version."""
    as_of, author = d["meta"]["as_of"], _author(d)
    source = source or f"dossier:{as_of}"
    held = set(held)
    out: dict[str, Any] = dict.fromkeys(KINDS, 0)
    pending: list[dict] = []
    due: dict[tuple[str, str], dict] = {}

    def put(kind, subject, body, pillars=(), rests=(), stale=None, change=None):
        pillars = store.keep_keys(kind, subject, list(pillars)) if kind in ("company", "idea") else list(pillars)
        rests = [r for r in rests if r]
        sym = subject.split(":")[0]
        if kind in ("company", "idea") and sym in held:
            what = store.material(kind, subject, body, pillars)
            if what:
                pid = store.hold(kind, subject, body, pillars, rests, as_of=as_of, author=author, source=source,
                                 change=change or what, stale_after=stale)
                pending.append({"id": pid, "kind": kind, "subject": subject, "symbol": sym, "what": what,
                                "change": change})
                t = store.get(kind, subject)
                return t["version"] if t else None
        cur = store.get(kind, subject)
        v = store.put(kind, subject, body, pillars, rests, as_of=as_of, author=author, source=source,
                      stale_after=stale, change=change)
        if v:
            out[kind] += 1
            if cur is not None:
                for t in store.mark_due(kind, subject, cur["version"], as_of):
                    due[(t["kind"], t["subject"])] = t
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
        cv = put("company", sym, body, pillars, rests=[_ref("sector", etf, sv.get(etf))], stale=stale,
                 change=(i.get("judgment") or {}).get("thesis_change") or None)
        subject = f"{sym}:{i.get('direction') or 'long'}"
        if i.get("bucket") == "reject":
            if store.get("idea", subject):
                put("idea", subject, idea_thesis(i, "reject"), rests=[_ref("macro", MACRO, mv),
                    _ref("sector", etf, sv.get(etf)), _ref("company", sym, cv)])
                if not any(p["subject"] == subject for p in pending):
                    store.archive("idea", subject)
            continue
        put("idea", subject, idea_thesis(i, i["bucket"]), rests=[_ref("macro", MACRO, mv), _ref("sector", etf, sv.get(etf)),
                                                                  _ref("company", sym, cv)])
    # a thesis this report rewrote is not due any more (its rests_on is current)
    out["due"] = sorted(f"{k}:{s}" for (k, s) in due if store.behind(store.get(k, s)))
    out["pending"] = pending
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
        got = sync_dossier(store, d, f"dossier:{d['meta']['as_of']}")
        for k in KINDS:
            total[k] += got[k]
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
