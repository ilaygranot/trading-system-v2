"""Approval queue, Telegram, web service, schedule and the premarket / weekly / monthly runs (runtime plan 4-7),
on SQLite and Postgres. No network: Telegram, prices and FRED are fakes."""
import base64
import io
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from qt import app as A
from qt.approvals import NotOpen, Queue
from qt.journal import Decision, Journal
from qt.pipelines import PIPELINES, ManualFileMissing
from qt.runs import Runner
from qt.schedule import due, is_trading_day, next_open
from qt.telegram import handle_update, item_buttons, item_text, sign
from qt.web import App
from test_runs_daily import AS_OF, PF, Prices

UTC = timezone.utc


class Bot:
    chat_id = "42"

    def __init__(self):
        self.sent, self.edits, self.answers, self.n = [], [], [], 100

    def send(self, text, buttons=None, force_reply=False):
        self.n += 1
        self.sent.append((self.n, text, buttons))
        return self.n

    def edit(self, mid, text, buttons=None):
        self.edits.append((mid, text, buttons))

    def answer(self, cid, text):
        self.answers.append(text)

    def download(self, file_id):
        return b"ism_pmi: {'2026-09': 49.0}\n"


def _action(kind="reduce", symbol="BA", pre=False, qty=50):
    return dict(kind=kind, rule_id="S12-R11", symbol=symbol, qty=qty, price=None, preapproved=pre, detail="test")


def _decision(db):
    return Journal(db).record(Decision(decided_by="code", subject="BA", choice="reduce", playbook_version="t"))


# ------------------------------------------------------------------ schedule

def test_schedule_new_york_time_dst_and_holidays():
    assert due("daily", datetime(2026, 10, 20, 22, 40, tzinfo=UTC))[0]         # EDT: 18:40 NY
    assert not due("daily", datetime(2026, 10, 20, 23, 40, tzinfo=UTC))[0]
    assert due("daily", datetime(2026, 12, 1, 23, 35, tzinfo=UTC))[0]          # EST: the other UTC hour
    assert not due("daily", datetime(2026, 11, 26, 23, 35, tzinfo=UTC))[0]     # Thanksgiving
    assert due("premarket", datetime(2026, 10, 21, 12, 30, tzinfo=UTC))[0]
    ok, as_of = due("weekly", datetime(2026, 10, 24, 14, 5, tzinfo=UTC))
    assert ok and as_of == date(2026, 10, 23)                                 # Saturday, as of Friday
    assert due("monthly", datetime(2026, 10, 10, 14, 5, tzinfo=UTC))[0]
    assert not due("monthly", datetime(2026, 10, 17, 14, 5, tzinfo=UTC))[0]   # only the second week
    assert next_open(date(2026, 7, 2)).date() == date(2026, 7, 6)             # over the July 3 holiday
    assert not is_trading_day(date(2026, 12, 25))


# ------------------------------------------------------------------ the queue

def test_queue_approve_makes_a_paper_order_filled_at_the_open(db):
    q = Queue(db)
    did = _decision(db)
    iid = q.add("r1", date(2026, 10, 20), _action(), 200.0, did)
    assert q.add("r1", date(2026, 10, 20), _action(), 200.0, did) is None            # one item per day and rule
    now = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)
    it = q.decide(iid, "approve", now=now)
    assert it["status"] == "approved"
    assert db.one("SELECT verdict FROM ilay_reviews WHERE decision_id = ?", (did,))["verdict"] == "approved"
    [o] = q.open_orders()
    assert o["fill_on"] == "2026-10-21" and o["qty"] == 50
    filled = q.fill({("BA", "2026-10-21"): 201.5})
    assert filled[0]["fill_price"] == 201.5 and not q.open_orders()
    with pytest.raises(NotOpen):
        q.decide(iid, "reject", now=now)


def test_queue_reject_postpone_and_expire_as_not_reviewed(db):
    q = Queue(db)
    a, b, c = (q.add("r1", date(2026, 10, 20), _action(symbol=s), 10.0, _decision(db)) for s in ("A", "B", "C"))
    now = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)
    q.decide(a, "reject", "too late", now=now)
    assert db.one("SELECT verdict, note FROM ilay_reviews ORDER BY id DESC LIMIT 1") == \
        {"verdict": "rejected", "note": "too late"}
    p = q.decide(b, "postpone", now=now)
    assert p["status"] == "postponed" and p["valid_until"].startswith("2026-10-22T13:30")
    after_open = datetime(2026, 10, 21, 14, 0, tzinfo=UTC)
    assert q.expire(after_open) == [c]                                           # b still valid tomorrow
    assert db.one("SELECT verdict FROM ilay_reviews ORDER BY id DESC LIMIT 1")["verdict"] == "not_reviewed"
    with pytest.raises(NotOpen):
        q.decide(c, "approve", now=after_open)


def test_standing_order_exit_is_paper_filled_without_waiting(db):
    q = Queue(db)
    iid = q.add("r1", date(2026, 10, 20), _action(kind="exit", pre=True), 100.0)
    assert q.get(iid)["status"] == "standing_order" and q.open_orders()[0]["kind"] == "exit"


def test_premarket_recheck_sends_a_moved_item_back_as_changed(db):
    q = Queue(db)
    moved = q.add("r1", date(2026, 10, 20), _action(symbol="BA"), 100.0, _decision(db))
    still = q.add("r1", date(2026, 10, 20), _action(symbol="XOM"), 100.0)
    q.decide(moved, "approve", now=datetime(2026, 10, 21, 1, 0, tzinfo=UTC))
    bot = Bot()
    svc = {"quote": lambda s: ({"BA": 103.5, "XOM": 100.5}[s], "fake"), "telegram": bot, "portfolio": {},
           "now": lambda: datetime(2026, 10, 21, 12, 35, tzinfo=UTC)}
    Runner(db, PIPELINES, svc).start("premarket", date(2026, 10, 21))
    assert q.get(moved)["status"] == "changed" and "+3.5%" in q.get(moved)["decided_note"]
    assert q.get(still)["status"] == "pending"
    assert db.one("SELECT status FROM paper_orders")["status"] == "cancelled"
    assert any("השתנו" in t for _, t, _ in bot.sent) and any(b for _, _, b in bot.sent)


# ------------------------------------------------------------------ Telegram

def test_item_text_shows_numbers_deviation_critic_and_validity(db):
    q = Queue(db)
    a = _action()
    a["agent"] = dict(agent="A7", option="reduce", confidence="medium", reasoning="event against the thesis",
                      deviations=[dict(rule_id="S12-R11", principle="S12-J2", fact_ids=["F-001"], reason="x")],
                      critic={"verdict": "pass", "issues": []}, status="decided")
    it = q.get(q.add("r1", date(2026, 10, 20), a, 200.0))
    t = item_text(it)
    assert "🟠 הקטנה · BA" in t and "כמות 50" in t and "⚠️ חריגה: S12-R11" in t and "בודק: עבר ✓" in t
    assert "בתוקף עד: פתיחת המסחר 21.10" in t
    assert [b["text"] for b in item_buttons(it, "https://x/item/1")[0]] == ["✅ אשר", "❌ דחה", "⏸ מחר", "🔎 פרטים"]


def _cb(data, chat=42):
    return {"callback_query": {"id": "c1", "data": data, "message": {"chat": {"id": chat}}}}


def test_bot_answers_only_ilay_and_records_buttons_and_reasons(db):
    q, bot = Queue(db), Bot()
    iid = q.add("r1", AS_OF + timedelta(days=5000), _action(), 1.0, _decision(db))
    assert handle_update(_cb(f"a:{iid}", chat=7), bot, q).startswith("ignored")
    assert q.get(iid)["status"] == "pending"
    assert handle_update(_cb(f"r:{iid}"), bot, q) == f"reject #{iid}"
    assert q.get(iid)["status"] == "rejected" and "סיבה לדחיית" in bot.sent[-1][1]
    reply = {"message": {"chat": {"id": 42}, "text": "חדשות",
                         "reply_to_message": {"text": bot.sent[-1][1]}}}
    assert handle_update(reply, bot, q) == f"reason #{iid}"
    assert q.get(iid)["decided_note"] == "חדשות"
    assert handle_update(_cb(f"a:{iid}"), bot, q).startswith("not open")
    resumed = []
    handle_update(_cb("resume:daily-x"), bot, q, on_resume=resumed.append)
    assert resumed == ["daily-x"]
    up = {"message": {"chat": {"id": 42}, "document": {"file_id": "f", "file_name": "ism.yaml"}}}
    got = []
    handle_update(up, bot, q, on_upload=lambda n, c: got.append((n, c)) or "ok")
    assert got and got[0][0] == "ism.yaml"


def test_daily_run_sends_summary_and_one_message_per_item(db):
    bot = Bot()
    Runner(db, PIPELINES, {"prices": Prices(), "portfolio": PF, "telegram": bot}).start("daily", AS_OF)
    assert bot.sent[0][1].startswith("יומית 2023-05-31")
    items = Queue(db).for_day(AS_OF)
    sent_items = [i for i in items if i["status"] != "info"]
    assert len(bot.sent) == 1 + len(sent_items) and all(i["tg_message_id"] for i in sent_items)
    assert all(i["decision_id"] for i in items)                       # every item points at its journal entry


# ------------------------------------------------------------------ web

def _call(app, path, method="GET", body=b"", auth=None, headers=None, query=""):
    env = {"PATH_INFO": path, "REQUEST_METHOD": method, "QUERY_STRING": query, "wsgi.input": io.BytesIO(body),
           "CONTENT_LENGTH": str(len(body)), "HTTP_HOST": "qt.test", **(headers or {})}
    if auth:
        env["HTTP_AUTHORIZATION"] = "Basic " + base64.b64encode(f"ilay:{auth}".encode()).decode()
    out = {}
    resp = b"".join(app(env, lambda s, h: out.update(status=s, headers=h)))
    return out["status"], resp.decode()


def test_web_auth_item_link_decide_and_webhook(db):
    bot = Bot()
    web = App(bot=bot, password="pw", webhook_secret="hook", resume=lambda rid: None)
    web._db = db
    q = Queue(db)
    iid = q.add("r1", AS_OF + timedelta(days=5000), _action(), 1.0, _decision(db))
    assert _call(web, "/health")[0].startswith("200")
    assert _call(web, "/")[0].startswith("401")
    st, html = _call(web, "/", auth="pw")
    assert st.startswith("200") and f"#{iid}" in html
    assert _call(web, f"/item/{iid}", query="sig=bad")[0].startswith("401")
    st, html = _call(web, f"/item/{iid}", query=f"sig={sign(iid, 'pw')}")
    assert st.startswith("200") and "יומן ההחלטות" in html
    evil = {"HTTP_ORIGIN": "https://evil.example"}
    assert _call(web, f"/item/{iid}/approve", "POST", auth="pw", headers=evil)[0].startswith("403")
    upd = json.dumps(_cb(f"p:{iid}")).encode()
    assert _call(web, "/telegram/hook", "POST", upd)[0].startswith("403")            # no secret header
    st, _ = _call(web, "/telegram/hook", "POST", upd, headers={"HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN": "hook"})
    assert st.startswith("200") and q.get(iid)["status"] == "postponed"
    assert _call(web, f"/item/{iid}/approve", "POST", auth="pw")[0].startswith("303")
    assert q.get(iid)["status"] == "approved"


def test_web_upload_portfolio_and_manual_file_resumes_monthly(db):
    resumed = []
    web = App(bot=Bot(), password="pw", webhook_secret="hook", resume=resumed.append)
    web._db = db
    assert "התיק עודכן" in web.upload("portfolio.yaml", "cash: 1000\npositions: []\n")
    assert A.portfolio(db) == {"cash": 1000, "positions": []}
    Runner(db, PIPELINES, {})                                         # schema
    db.insert("runs", dict(id="monthly-x", kind="monthly", as_of="2026-10-09", status="failed",
                           started_at="2026-10-10T14:00:00+00:00"), returning=None)
    assert "ממשיך" in web.upload("ism.yaml", "ism_pmi: {'2026-09': 49}\n") and resumed == ["monthly-x"]


# ------------------------------------------------------------------ weekly and monthly

def test_weekly_report_counts_discipline_and_cost(db):
    q = Queue(db)
    d = date(2026, 10, 20)
    q.add("r", d, _action(symbol="A"), 1.0)
    q.add("r", d, _action(symbol="B"), 1.0)
    q.expire(datetime(2026, 10, 22, tzinfo=UTC))
    bot = Bot()
    Runner(db, PIPELINES, {"telegram": bot, "now": lambda: datetime(2026, 10, 24, 14, 0, tzinfo=UTC)}) \
        .start("weekly", date(2026, 10, 23))
    rep = db.one("SELECT output FROM run_steps WHERE step='report'", json_cols=["output"])["output"]
    assert rep["not_reviewed"] == 2 and rep["not_reviewed_pct"] == 100.0
    assert "לא נבדקו (פגו): 2" in bot.sent[-1][1]


class Fred:
    def observations(self, sid, start, end, known_on=None):
        months = [date(2014 + i // 12, i % 12 + 1, 28) for i in range(150)]
        return [(m, 100 + i * (0.3 if sid != "FEDFUNDS" else -0.01)) for i, m in enumerate(months)], f"fake:{sid}"


def test_monthly_waits_for_the_manual_file_then_resumes(db):
    bot = Bot()
    r = Runner(db, PIPELINES, {"telegram": bot, "fred": Fred()})
    with pytest.raises(ManualFileMissing):
        r.start("monthly", date(2026, 10, 9))
    assert "מחכה לקובץ המקרו" in bot.sent[-1][1]
    months = [f"{2025 + (i + 8) // 12}-{(i + 8) % 12 + 1:02d}" for i in range(14)]            # 2025-09 .. 2026-10
    content = "ism_pmi: {" + ", ".join(f"'{m}': {48 + i * 0.3:.1f}" for i, m in enumerate(months)) + "}\n"
    Queue(db).upload("manual_macro", "ism.yaml", content)
    rid = A.waiting_monthly(db)
    r.resume(rid)
    st = db.one("SELECT output FROM run_steps WHERE run_id=? AND step='stance'", (rid,), ["output"])["output"]
    assert "ism_pmi" not in st["skipped_indicators"] and st["zone"]
    assert Queue(db).for_day(date(2026, 10, 9))[0]["rule_id"] == "S02-STANCE"


# ------------------------------------------------------------------ the cron entry point

def test_scheduled_runs_once_skips_done_and_auto_resumes_a_failure(db):
    bot = Bot()
    svc = {"telegram": bot, "now": lambda: datetime(2026, 10, 24, 14, 5, tzinfo=UTC)}
    sat = datetime(2026, 10, 24, 14, 5, tzinfo=UTC)
    assert A.scheduled("weekly", db, svc, datetime(2026, 10, 24, 16, 0, tzinfo=UTC)) == "not due: weekly"
    rid = A.scheduled("weekly", db, svc, sat)
    assert rid.startswith("weekly-2026-10-23")
    assert A.scheduled("weekly", db, svc, sat).startswith("skip")
    # a monthly run without FRED fails after the manual file: alerted with a resume button, resumed once later
    Queue(db).upload("manual_macro", "m.yaml", "ism_pmi: {'2026-09': 49}\n")
    out = A.scheduled("monthly", db, svc, datetime(2026, 10, 10, 14, 5, tzinfo=UTC))
    assert out.startswith("failed") and bot.sent[-1][2][0][0]["callback_data"].startswith("resume:monthly-")
    out = A.scheduled("monthly", db, svc, datetime(2026, 10, 10, 15, 5, tzinfo=UTC))
    assert out.startswith("failed again")
    assert A.scheduled("monthly", db, svc, datetime(2026, 10, 10, 15, 6, tzinfo=UTC)) == "not due: monthly"
