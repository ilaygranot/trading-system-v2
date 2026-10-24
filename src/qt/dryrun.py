"""The dry run (Ilay 24.10: "ריצה כמו היום לא יכולה לקרות יותר ... בלי קיצורי דרך"): the whole weekly run, on the
server, with the live data and the agents, a day before the real one, so what would break on Saturday breaks on
Friday while there is time to fix it.

It runs in a Postgres schema of its own (DRY_SCHEMA), emptied each time, so nothing it writes reaches the production
tables; it starts from a copy of the production portfolio, watchlist and monthly macro file. Two differences from
Saturday, both on cost only: two deep dives instead of up to twelve (LIMITS), and the messages are kept instead of
sent; one verdict goes to Telegram at the end, with the pre-flight check, each step, the report's completeness and a
link to the report. The universe it fetched, when every stock answered, is handed to the production research state of
the same week, so Saturday does not ask Yahoo for three thousand stocks again (the fetch moved to Friday).
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from typing import Any, Callable

from qt.approvals import Queue
from qt.db import DB

DRY_SCHEMA = "qt_dry"
LIMITS = {"max_deep": 2, "max_rescue": 2}          # cost only: every stage and card still runs (not from course)
COPY = ("portfolio", "watchlist", "manual_macro")
UNIVERSE = "stage:universe.pkl"


class Collector:
    """Telegram for the dry run: keeps what the run would have sent."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str, buttons: list[list[dict]] | None = None, force_reply: bool = False) -> int:
        self.sent.append(text)
        return len(self.sent)

    def edit(self, message_id: int, text: str, buttons: list[list[dict]] | None = None) -> None:
        self.sent.append(text)

    def answer(self, callback_id: str, text: str) -> None:
        pass


def prepare(prod: DB, dry: DB) -> list[str]:
    """Empty the dry schema and copy in what the weekly run reads from production."""
    if not dry.schema or dry.schema == prod.schema:
        raise ValueError("the dry run needs a schema of its own")
    dry.drop_schema()
    src, dst = Queue(prod), Queue(dry)
    copied = []
    for kind in COPY:
        up = src.latest_upload(kind)
        if up:
            dst.upload(kind, up["name"], up["content"])
            copied.append(f"{kind}: {up['name']}")
    return copied


def share_universe(dry: DB, prod: DB, as_of: date, dossier: dict | None) -> str:
    """The universe goes to Saturday's research only when the dry report says every stock answered."""
    u = ((dossier or {}).get("meta") or {}).get("universe")
    if not u:
        return "היקום לא הועבר: אין דוח עם מדידת היקום"
    bad = {s: e for s, e in (u.get("errors") or {}).items() if "HTTP 404" not in str(e)}
    if bad:
        return f"היקום לא הועבר: {len(bad)} מניות לא ענו"
    from qt.research.state import SCHEMA
    prod.ddl(SCHEMA)
    rows = dry.all("SELECT key, content, updated_at FROM research_state WHERE as_of = ? AND (key = ? OR key LIKE ?)",
                   (as_of.isoformat(), UNIVERSE, UNIVERSE + "#%"))
    if not any(r["key"] == UNIVERSE for r in rows):
        return "היקום לא הועבר: לא נשמר"
    head = next(r for r in rows if r["key"] == UNIVERSE)
    for r in sorted(rows, key=lambda r: r["key"] == UNIVERSE):         # the head row last, as State.put_blob
        prod.upsert("research_state", dict(as_of=as_of.isoformat(), **r), ["as_of", "key"])
    return f"היקום ({u.get('n')} מניות) הועבר למחקר של {as_of.isoformat()} ({json.loads(head['content'])['parts']} חלקים)"


def run(prod: DB, as_of: date, dry: DB | None = None, services: Callable[[DB], dict[str, Any]] | None = None,
        checks: Callable[[], list] | None = None, share: bool = True) -> dict[str, Any]:
    """The pre-flight check, then the weekly run in the dry schema. Returns what the verdict says."""
    from qt import app, preflight
    from qt.runs import AlreadyRunning
    pre = (checks or preflight.run)()
    dry = dry or DB(prod.url, schema=DRY_SCHEMA)
    copied = prepare(prod, dry)
    svc = (services or app.services)(dry)
    bot = Collector()
    svc.update(telegram=bot, research_limits=LIMITS)
    old_work = os.environ.get("QT_WORK_DIR")
    os.environ["QT_WORK_DIR"] = tempfile.mkdtemp(prefix="qt-dry-")       # no file cache from an earlier run
    error = None
    try:
        app.runner(dry, svc).start("weekly", as_of)
    except AlreadyRunning as e:
        error = f"AlreadyRunning: {e}"
    except Exception as e:                                              # noqa: BLE001 - the verdict reports it
        error = f"{type(e).__name__}: {e}"
    finally:
        if old_work is None:
            os.environ.pop("QT_WORK_DIR", None)
        else:
            os.environ["QT_WORK_DIR"] = old_work
    r = app.runner(dry, svc).status(1)
    steps = r[0]["steps"] if r else []
    dossier = Queue(dry).latest_upload("dossier")
    d = json.loads(dossier["content"]) if dossier else None
    rep = Queue(dry).latest_upload("report")
    report_id = Queue(prod).upload("dry_report", rep["name"], rep["content"]) if rep else None
    shared = share_universe(dry, prod, as_of, d) if share else "היקום לא הועבר (--no-share)"
    ok = error is None and all(p.ok for p in pre) and bool(r) and r[0]["status"] == "done" \
        and (d or {}).get("completeness", {}).get("status") == "complete"
    return {"ok": ok, "as_of": as_of.isoformat(), "preflight": pre, "copied": copied, "steps": steps, "error": error,
            "completeness": (d or {}).get("completeness"), "report_id": report_id, "shared": shared,
            "messages": bot.sent}


def text(res: dict[str, Any]) -> str:
    from qt.research import completeness
    out = [f"🧪 ריצת ניסיון {res['as_of']}: " + ("✅ עברה, השבת אמורה לרוץ מלאה" if res["ok"] else "❌ נכשלה, השבת לא תרוץ נקי")]
    bad = [p for p in res["preflight"] if not p.ok]
    out.append("בדיקת מוכנות: " + ("הכל תקין" if not bad else "; ".join(f"✗ {p.name}: {p.detail}" for p in bad)))
    for s in res["steps"]:
        mark = "✓" if s["status"] == "done" else "✗"
        out.append(f"{mark} {s['step']}" + (f": {str(s['error'])[:300]}" if s["status"] != "done" and s["error"] else ""))
    if res["error"] and not any(s["status"] == "failed" for s in res["steps"]):
        out.append(f"✗ {res['error'][:300]}")
    if res["completeness"]:
        out += completeness.lines(res["completeness"])
    if res["report_id"]:
        from qt.telegram import report_link
        link = report_link(res["report_id"])
        out.append(f"הדוח: {link}" if link else f"הדוח: העלאה #{res['report_id']}")
    out.append(res["shared"])
    out.append(f"{len(res['messages'])} הודעות נאספו ולא נשלחו (מוגבל ל-{LIMITS['max_deep']} ניתוחי עומק)")
    return "\n".join(out)


def main(as_of: date | None = None, notify: bool = True, share: bool = True) -> int:
    from qt import app
    prod = DB()
    if prod.url == ":memory:":
        print("set QT_DATABASE_URL: the dry run copies its inputs from production")
        return 1
    res = run(prod, as_of or _friday(app.as_of_today()), share=share)
    msg = text(res)
    print(msg, flush=True)
    if notify:
        from qt.telegram import Telegram
        bot = Telegram.from_env()
        if bot is not None:
            bot.send(msg[:4000])
    return 0 if res["ok"] else 1


def _friday(today: date) -> date:
    """The as-of of the coming Saturday's weekly run (qt.schedule.due), so the universe is shared with it."""
    from datetime import timedelta
    from qt.schedule import previous_trading_day
    sat = today + timedelta(days=(5 - today.weekday()) % 7)
    return previous_trading_day(sat + timedelta(days=1))
