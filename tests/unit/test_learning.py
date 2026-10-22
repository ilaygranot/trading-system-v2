"""The learning loop (architecture 7): the trade journal row, the measurements, the code's draft review, agent L
on DC-24, Ilay's confirmation and lesson in Telegram, the weekly and monthly reports, and the journal page."""
import io
from datetime import date, datetime, timezone
from types import SimpleNamespace as NS

from qt import learning as L
from qt import tradelog as T
from qt.approvals import Queue
from qt.runs import RunContext

TRADE = {"symbol": "XYZ", "qty": 50, "entry": 100.0, "exit": 92.0, "pnl": -400.0, "rule": "S12-R2",
         "direction": "long", "sector": "XLE", "opened": "2024-05-01", "closed": "2024-05-10", "entry_stop": 92.0,
         "target": 124.0, "max_qty": 50, "trade_pnl": -400.0,
         "plan": {"price": 100.0, "detail": "breakout", "size_pct": 5, "equity": 100_000, "decision_id": 3}}


def _rows(path: list[tuple[str, float, float]]) -> list[list]:
    return [[d, (h + lo) / 2, h, lo, (h + lo) / 2] for d, h, lo in path]


IN_TRADE = _rows([("2024-05-01", 101, 99), ("2024-05-03", 106, 100), ("2024-05-10", 99, 91)])


# ---------------------------------------------------------------- the row and its numbers

def test_r_excursions_and_plan():
    assert T.r_multiple(TRADE) == -1.0
    ex = T.excursions(TRADE, IN_TRADE)
    assert (ex["mfe_r"], ex["mae_r"], ex["target_hit"]) == (0.75, -1.12, False)
    assert T.plan_check(TRADE) == []                                   # stopped by the rule, as planned


def test_a_loss_by_the_plan_is_a_loss_not_a_mistake():
    """S13-J4 and S08-J23: execution good, idea unclear until the window after the exit closes."""
    rv = T.code_review(TRADE, T.excursions(TRADE, IN_TRADE), {"target_after_exit": None}, [])
    assert (rv["idea_quality"], rv["execution_quality"], rv["loss_or_mistake"]) == ("unclear", "good", "loss")


def test_off_plan_loss_is_a_mistake():
    c = dict(TRADE, entry=103.0, rule=None, max_qty=60, plan=dict(TRADE["plan"]))
    off = T.plan_check(c)
    assert len(off) == 3                                               # slippage, size, a manual exit
    rv = T.code_review(c, {}, {}, off)
    assert (rv["execution_quality"], rv["loss_or_mistake"]) == ("bad", "mistake")


def test_stopped_then_the_target_came_is_noise_and_a_good_idea():
    after = _rows([("2024-05-13", 110, 95), ("2024-05-14", 125, 110)])
    ax = T.after_exit(TRADE, IN_TRADE + after)
    assert ax == {"target_after_exit": True, "complete": True, "sessions_seen": 2}
    rv = T.code_review(TRADE, {}, ax, [])
    assert rv["idea_quality"] == "good" and rv["stopped_by_noise"]


def test_after_exit_waits_for_the_window():
    after = _rows([("2024-05-13", 101, 95)])
    assert T.after_exit(TRADE, IN_TRADE + after)["complete"] is False
    full = _rows([(f"2024-06-{d:02d}", 101, 95) for d in range(1, 21)])
    assert T.after_exit(TRADE, IN_TRADE + full) == {"target_after_exit": False, "complete": True, "sessions_seen": 20}


def test_short_trade_signs():
    c = dict(TRADE, direction="short", entry=100.0, entry_stop=108.0, target=80.0, exit=90.0, trade_pnl=500.0)
    rows = _rows([("2024-05-01", 101, 99), ("2024-05-05", 104, 79)])
    assert T.r_multiple(c) == 1.25
    assert T.excursions(c, rows)["target_hit"] is True


def test_csv_has_hebrew_headers_and_a_bom():
    text = T.to_csv([T.row(TRADE)], T.COLUMNS)
    assert text.startswith("﻿תאריך כניסה,סימול") and "XYZ" in text and "סטופ" in text


# ---------------------------------------------------------------- weekly pieces

def test_first_touch_is_conservative_on_a_day_with_both():
    rows = _rows([("2024-05-02", 105, 99), ("2024-05-03", 125, 91)])
    assert L.first_touch(rows, "long", 92.0, 124.0, "2024-05-01", "2024-07-01") == ("stop", "2024-05-03")
    assert L.first_touch(rows[:1], "long", 92.0, 124.0, "2024-05-01", "2024-07-01") == (None, None)


def test_reentries_after_stops_are_flagged():
    """S13-R6 with D-S13-1: two re-entries are allowed, the third is a revenge sign."""
    stop = dict(TRADE, rule="S12-R2")
    pf = {"closed": [dict(stop, opened="2024-05-01"), dict(stop, opened="2024-05-08"), dict(stop, opened="2024-05-15")],
          "positions": [{"symbol": "XYZ"}]}
    assert L.reentries(pf, date(2024, 5, 20)) == [{"symbol": "XYZ", "entries": 4, "stops": 3}]
    pf["positions"] = []
    assert L.reentries(pf, date(2024, 5, 20)) == []


def test_escaped_ideas():
    e = NS(symbol="ABC", direction="long", target=50.0, added="2024-05-01")
    rows = {"ABC": _rows([("2024-05-02", 45, 40), ("2024-05-09", 51, 46)])}
    assert L.escaped([e], rows, set()) == ["ABC"]
    assert L.escaped([e], rows, {"ABC"}) == []


# ---------------------------------------------------------------- monthly numbers

def test_trade_stats_against_the_course_targets():
    win = dict(TRADE, trade_pnl=1200.0, rule="S12-R11")
    s = L.trade_stats([TRADE, win, win])
    assert (s["n"], s["win_rate"], s["avg_win_r"], s["avg_loss_r"], s["rr"]) == (3, 66.7, 3.0, -1.0, 3.0)
    assert s["streak"] == 2 and s["streak_kind"] == "win" and s["followed_pct"] == 100.0


def test_risk_metrics_from_daily_equity():
    eq = [("d1", 100.0), ("d2", 102.0), ("d3", 99.0), ("d4", 103.0)]
    m = L.risk_metrics(eq)
    assert m["return_pct"] == 3.0 and m["max_dd_pct"] == round((99 / 102 - 1) * 100, 2) and m["sharpe"] > 0
    assert L.risk_metrics(eq[:2]) == {"days": 2}


def test_monthly_lines_warn_on_a_losing_streak():
    out = {"month": {"n": 0}, "all": dict(L.trade_stats([TRADE] * 3)), "risk_month": {"days": 0}, "ideas": {},
           "agreement": [], "critic": [], "calibration": [], "deviations": [], "stages": {}, "proposals": []}
    text = "\n".join(L.monthly_lines(out))
    assert "S13-J6" in text and "סף 10" in text and "הצלחה 0%" in text


# ---------------------------------------------------------------- agent L, Ilay, the page

class AgentL:
    def __init__(self, db):
        from qt.journal import Decision, Journal
        self.calls = []
        self.did = Journal(db).record(Decision(decided_by="agent:L", subject="XYZ", choice="playbook",
                                               playbook_version="t", card_id="DC-24"))

    def decide(self, agent, card, fs, world, **kw):
        self.calls.append((agent, card, world))
        return NS(option="playbook", decision_id=self.did, status="decided",
                  output={"option": "playbook", "stage": "12", "idea_quality": "good", "execution_quality": "good",
                          "loss_or_mistake": "loss", "worked": "חיכינו לטריגר", "not_worked": "הסטופ צמוד לרעש",
                          "lesson": "לבדוק k מול ATR", "proposal": "", "confidence": "medium", "reasoning": "x"})


def test_l_reviews_the_trade_and_ilay_confirms_with_a_lesson(db):
    from qt.telegram import handle_update
    ag = AgentL(db)
    ctx = RunContext(db, "d1", "daily", date(2024, 5, 10), services={"agents": ag})
    out = L.review_trade(ctx, TRADE, IN_TRADE)
    assert out["by"] == "agent:L" and ag.calls[0][:2] == ("L", "DC-24")
    assert ag.calls[0][2]["code_draft"]["loss_or_mistake"] == "loss"
    assert L.review_trade(ctx, TRADE, IN_TRADE)["skipped"]                         # once per trade
    q = Queue(db)
    item = q.get(out["item"])
    assert item["kind"] == "review" and item["decision_id"] == ag.did and "שלב 12 סיכון תגובתי" in item["payload"]["detail"]

    class Bot:
        chat_id = "42"
        sent, answers = [], []

        def send(self, text, buttons=None, force_reply=False):
            self.sent.append(text)
            return 1

        def answer(self, cid, text):
            self.answers.append(text)

        def edit(self, *a, **k):
            pass

    bot = Bot()
    cb = {"callback_query": {"id": "c", "data": f"a:{item['id']}", "message": {"chat": {"id": 42}}}}
    from unittest.mock import patch
    with patch("qt.approvals._now", return_value=datetime(2024, 5, 10, 21, tzinfo=timezone.utc)):
        assert handle_update(cb, bot, q) == f"approve #{item['id']}"
    assert bot.answers[-1] == "התחקיר אושר" and "מה למדת" in bot.sent[-1]
    reply = {"message": {"chat": {"id": 42}, "text": "לא להדק סטופ לפני דוח",
                         "reply_to_message": {"text": bot.sent[-1]}}}
    assert handle_update(reply, bot, q) == f"lesson #{item['id']}"
    rv = L.reviews(db)[T.trade_key(TRADE)]
    assert (rv["status"], rv["failure_class"], rv["lesson"]) == ("אושר", "playbook", "לא להדק סטופ לפני דוח")
    row = T.row(TRADE, rv, rv)
    assert row["stage"] == "12 סיכון תגובתי" and row["review_status"] == "אושר" and row["idea_quality"] == "טוב"


def test_journal_page_and_csv_need_the_signature(db):
    from qt.telegram import sign_journal
    from qt.web import App
    ctx = RunContext(db, "d1", "daily", date(2024, 5, 10))
    L.review_trade(ctx, TRADE, IN_TRADE)
    Queue(db).upload("portfolio", "book-2024-05-10.yaml",
                     "cash: 1\npositions: []\nclosed:\n- {symbol: XYZ, qty: 50, entry: 100.0, exit: 92.0, pnl: -400.0, "
                     "rule: S12-R2, direction: long, opened: '2024-05-01', closed: '2024-05-10', entry_stop: 92.0, "
                     "target: 124.0, max_qty: 50, trade_pnl: -400.0}\n")
    web = App(bot=None, password="pw", webhook_secret="hook", resume=lambda rid: None)
    web._db = db

    def call(path, query=""):
        env = {"PATH_INFO": path, "REQUEST_METHOD": "GET", "QUERY_STRING": query, "wsgi.input": io.BytesIO(b""),
               "CONTENT_LENGTH": "0", "HTTP_HOST": "qt.test"}
        out = {}
        body = b"".join(web(env, lambda s, h: out.update(status=s, headers=h)))
        return out["status"], body.decode()

    assert call("/journal")[0].startswith("401")
    st, page = call("/journal", f"sig={sign_journal('pw')}")
    assert st.startswith("200") and "עסקאות (1)" in page and "XYZ" in page and "הפסד (לא טעות)" in page
    st, csv = call("/journal/trades.csv", f"sig={sign_journal('pw')}")
    assert st.startswith("200") and csv.startswith("﻿תאריך כניסה") and "-1.0" in csv
