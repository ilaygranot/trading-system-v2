"""The approval queue (runtime plan 7): what reaches Ilay, what he decided, and the paper trades it makes.

Every item has a validity: it expires at the next market open (7.2 rule 2). An item that expires unanswered
is journalled as "not reviewed" and counts in the discipline report. Approving sends nothing to a broker in v1
(rule 3): it opens a paper order filled at the next session's open. The premarket recheck (rule 4) returns an
item whose price moved past the threshold to Ilay as "changed" and cancels its unfilled paper order.
Exits on a stop or a hard block do not wait (7.3): they are standing orders, filled as paper and reported.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from qt.db import DB, dumps
from qt.journal import Journal
from qt.policy import load_policy
from qt.schedule import next_open

SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_queue (
  id {ID},
  run_id TEXT NOT NULL,
  as_of TEXT NOT NULL,
  item_key TEXT NOT NULL UNIQUE,         -- as_of + symbol + rule + kind: one item per day per action
  kind TEXT NOT NULL,                    -- enter | add | exit | reduce | sell | tighten_stop | block_new_risk | flag
  rule_id TEXT NOT NULL,
  symbol TEXT,
  status TEXT NOT NULL,                  -- pending | postponed | changed | approved | rejected | expired
                                         -- | standing_order | info
  payload {JSON} NOT NULL,
  ref_price {REAL},                      -- the price the proposal was built on (premarket recheck)
  valid_until {TS},                      -- next market open (7.2 rule 2)
  decision_id BIGINT,                    -- the journal entry this item asks Ilay about
  tg_message_id BIGINT,                  -- the Telegram message carrying the buttons
  created_at {TS} NOT NULL,
  decided_at {TS},
  decided_note TEXT
);
CREATE TABLE IF NOT EXISTS paper_orders (
  id {ID},
  item_id BIGINT NOT NULL UNIQUE,
  symbol TEXT NOT NULL,
  kind TEXT NOT NULL,
  qty {REAL},
  fill_on TEXT NOT NULL,                 -- the session whose open fills it
  status TEXT NOT NULL,                  -- open | filled | cancelled
  fill_price {REAL},
  created_at {TS} NOT NULL,
  filled_at {TS}
);
CREATE TABLE IF NOT EXISTS uploads (
  id {ID},
  kind TEXT NOT NULL,                    -- manual_macro (ISM / NFIB monthly file)
  name TEXT,
  content TEXT NOT NULL,
  created_at {TS} NOT NULL
);
"""

ORDER_KINDS = {"enter", "add", "exit", "reduce", "sell"}     # a paper order; tighten_stop / flags are not orders
OPEN_STATUSES = ("pending", "postponed", "changed")          # still waiting for Ilay
CHOICES = {"approve": ("approved", "approved"), "reject": ("rejected", "rejected"), "postpone": ("postponed", None)}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


class NotOpen(ValueError):
    """The item is not waiting for a decision (already decided, expired, or a standing order)."""


@dataclass
class Queue:
    db: DB

    def __post_init__(self):
        self.db.ddl(SCHEMA)
        self.journal = Journal(self.db)

    # ------------------------------------------------------------ adding
    def add(self, run_id: str, as_of: date, action: dict[str, Any], ref_price: float | None = None,
            decision_id: int | None = None) -> int | None:
        """One item per day, symbol, rule and kind; returns the new id, or None if it is already queued."""
        status = "standing_order" if action.get("preapproved") else "info" if action["kind"] in ("flag",) else "pending"
        key = f"{as_of.isoformat()}|{action.get('symbol') or '-'}|{action['rule_id']}|{action['kind']}"
        new = self.db.insert_new("approval_queue", dict(
            run_id=run_id, as_of=as_of.isoformat(), item_key=key, kind=action["kind"], rule_id=action["rule_id"],
            symbol=action.get("symbol"), status=status, payload=dumps(action), ref_price=ref_price,
            valid_until=_iso(next_open(as_of)), decision_id=decision_id, created_at=_iso(_now())), ["item_key"])
        if not new:
            return None
        item = self.db.one("SELECT id FROM approval_queue WHERE item_key = ?", (key,))
        if status == "standing_order" and action["kind"] in ORDER_KINDS:
            self._paper(item["id"], action, next_open(as_of).date())
        return item["id"]

    # ------------------------------------------------------------ reading
    def get(self, item_id: int) -> dict | None:
        return self.db.one("SELECT * FROM approval_queue WHERE id = ?", (item_id,), ["payload"])

    def open_items(self) -> list[dict]:
        marks = ",".join("?" * len(OPEN_STATUSES))
        return self.db.all(f"SELECT * FROM approval_queue WHERE status IN ({marks}) ORDER BY id", OPEN_STATUSES,
                           ["payload"])

    def for_day(self, as_of: date) -> list[dict]:
        return self.db.all("SELECT * FROM approval_queue WHERE as_of = ? ORDER BY id", (as_of.isoformat(),),
                           ["payload"])

    def set_message(self, item_id: int, message_id: int) -> None:
        self.db.execute("UPDATE approval_queue SET tg_message_id = ? WHERE id = ?", (message_id, item_id))

    # ------------------------------------------------------------ Ilay decides
    def decide(self, item_id: int, choice: str, note: str = "", now: datetime | None = None) -> dict:
        """approve | reject | postpone. Every answer is journalled against the item's decision (7.2 rule 5:
        a reason for a rejection is welcome, not required)."""
        now = now or _now()
        item = self.get(item_id)
        if item is None:
            raise KeyError(item_id)
        if item["status"] not in OPEN_STATUSES:
            raise NotOpen(f"item {item_id} is {item['status']}")
        if datetime.fromisoformat(item["valid_until"]) <= now:
            self.expire(now)
            raise NotOpen(f"item {item_id} expired at the open")
        status, verdict = CHOICES[choice]
        valid = item["valid_until"]
        if choice == "postpone":
            # "tomorrow": the item stays open one more session (not from course: one session)
            valid = _iso(next_open(datetime.fromisoformat(valid).date()))
        self.db.execute("UPDATE approval_queue SET status=?, decided_at=?, decided_note=?, valid_until=? WHERE id=?",
                        (status, _iso(now), note or None, valid, item_id))
        if verdict and item["decision_id"]:
            self.journal.review(item["decision_id"], verdict, note)
        if choice == "approve" and item["kind"] in ORDER_KINDS:
            self._paper(item_id, item["payload"], datetime.fromisoformat(item["valid_until"]).date())
        return self.get(item_id)

    def note(self, item_id: int, text: str) -> None:
        """A rejection reason sent after the button (a reply to the bot's question)."""
        self.db.execute("UPDATE approval_queue SET decided_note = ? WHERE id = ?", (text, item_id))

    # ------------------------------------------------------------ time and prices
    def expire(self, now: datetime | None = None) -> list[int]:
        """Open items past their validity become 'expired'; journalled as not reviewed (discipline metric)."""
        now = now or _now()
        gone = [r for r in self.open_items() if datetime.fromisoformat(r["valid_until"]) <= now]
        for r in gone:
            self.db.execute("UPDATE approval_queue SET status='expired', decided_at=? WHERE id=?", (_iso(now), r["id"]))
            if r["decision_id"]:
                self.journal.review(r["decision_id"], "not_reviewed", "expired at the open")
        return [r["id"] for r in gone]

    def recheck(self, prices: dict[str, float], hard_blocked: set[str] = frozenset(),
                now: datetime | None = None) -> list[int]:
        """Premarket (7.2 rule 4): an open or approved item whose price moved more than the threshold, or whose
        symbol now fails a hard rule, comes back as 'changed'; an approval's unfilled paper order is cancelled."""
        now = now or _now()
        thr = load_policy().runtime.recheck_move_pct
        changed = []
        rows = self.db.all("SELECT * FROM approval_queue WHERE status IN ('pending','postponed','approved') "
                           "AND valid_until > ?", (_iso(now),), ["payload"])
        for r in rows:
            p, ref = prices.get(r["symbol"]), r["ref_price"]
            moved = p is not None and ref and abs(p / ref - 1) * 100 > thr
            if not moved and r["symbol"] not in hard_blocked:
                continue
            why = (f"price {ref:g} -> {p:g} ({(p / ref - 1) * 100:+.1f}%)" if moved else "a hard rule fails now")
            self.db.execute("UPDATE approval_queue SET status='changed', decided_note=? WHERE id=?", (why, r["id"]))
            self.db.execute("UPDATE paper_orders SET status='cancelled' WHERE item_id=? AND status='open'", (r["id"],))
            if r["status"] == "approved" and r["decision_id"]:
                self.journal.review(r["decision_id"], "changed", why)
            changed.append(r["id"])
        return changed

    # ------------------------------------------------------------ paper trades
    def _paper(self, item_id: int, action: dict, fill_on: date) -> None:
        self.db.insert_new("paper_orders", dict(
            item_id=item_id, symbol=action["symbol"], kind=action["kind"], qty=action.get("qty"),
            fill_on=fill_on.isoformat(), status="open", created_at=_iso(_now())), ["item_id"])

    def open_orders(self) -> list[dict]:
        return self.db.all("SELECT * FROM paper_orders WHERE status='open' ORDER BY id")

    def fill(self, opens: dict[tuple[str, str], float]) -> list[dict]:
        """Fill open paper orders at the open of their session; opens maps (symbol, day) -> open price."""
        done = []
        for o in self.open_orders():
            px = opens.get((o["symbol"], o["fill_on"]))
            if px is None:
                continue
            self.db.execute("UPDATE paper_orders SET status='filled', fill_price=?, filled_at=? WHERE id=?",
                            (px, _iso(_now()), o["id"]))
            done.append({**o, "fill_price": px, "status": "filled"})
        return done

    # ------------------------------------------------------------ uploads (the monthly manual file)
    def upload(self, kind: str, name: str, content: str) -> int:
        return self.db.insert("uploads", dict(kind=kind, name=name, content=content, created_at=_iso(_now())))

    def latest_upload(self, kind: str) -> dict | None:
        return self.db.one("SELECT * FROM uploads WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,))
