"""The web service (runtime plan 7.1, 9): the Telegram webhook, the item detail page, and a small dashboard.

Plain WSGI (gunicorn on Render, wsgiref locally). The dashboard and its actions need the dashboard password
(HTTP basic auth); an item page also opens from the signed link in its Telegram message; the webhook accepts
only Telegram's secret header and only Ilay's chat id (checked in qt.telegram).
"""
from __future__ import annotations

import base64
import hmac
import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs

from qt.approvals import NotOpen, Queue
from qt.db import DB

Start = Callable[[str, list[tuple[str, str]]], Any]

CSS = """body{font-family:system-ui,sans-serif;max-width:960px;margin:0 auto;padding:16px;background:#fafafa;color:#222}
table{border-collapse:collapse;width:100%;margin:8px 0 24px}td,th{border-bottom:1px solid #ddd;padding:6px;text-align:start;
vertical-align:top;font-size:14px}pre{white-space:pre-wrap;background:#fff;border:1px solid #ddd;padding:8px;font-size:13px}
.failed{color:#b00}.done{color:#070}form{display:inline}button{margin:2px}
.up{color:#070}.down{color:#b00}.num{text-align:end;font-variant-numeric:tabular-nums}.muted{color:#888;font-size:13px}
.cards{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 16px}.card{border:1px solid #ddd;padding:6px 10px;border-radius:6px}
.card b{display:block;font-size:18px}.wrap{overflow-x:auto}nav a{margin-inline-end:12px}
@media (prefers-color-scheme:dark){body{background:#161616;color:#ddd}pre{background:#222;border-color:#444}td,th{border-color:#333}
.card{border-color:#333}.up{color:#5c5}.down{color:#f66}}"""

# Live refresh (runtime plan 7.1): the page re-reads its own fragment every REFRESH_S seconds while it is visible,
# and swaps it in, so positions, the queue and the runs stay current without a reload.
REFRESH_S = 30
LIVE_JS = """<script>(function(){var el=document.getElementById('live'),st=document.getElementById('stamp');
function tick(){if(document.visibilityState!=='visible')return;
var u=location.pathname+(location.search?location.search+'&':'?')+'fragment=1';
fetch(u,{credentials:'same-origin',cache:'no-store'}).then(function(r){if(!r.ok)throw r.status;return r.text()})
.then(function(t){el.innerHTML=t;st.textContent='עודכן '+new Date().toLocaleTimeString('he-IL');st.className='muted'})
.catch(function(){st.textContent='אין חיבור לשרת, מנסה שוב';st.className='down'})}
setInterval(tick,%d*1000);document.addEventListener('visibilitychange',tick)})()</script>"""


def _n(v: Any, d: int = 2) -> str:
    return "" if v is None else f"{v:,.{d}f}"


def _signed(v: Any, d: int = 2, suffix: str = "") -> str:
    if v is None:
        return ""
    if round(v, d) == 0:
        return f"<span dir=ltr>{0:.{d}f}{suffix}</span>"
    return f"<span dir=ltr class={'up' if v > 0 else 'down'}>{v:+,.{d}f}{suffix}</span>"


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


class App:
    def __init__(self, db_url: str | None = None, bot: Any = None, password: str | None = None,
                 webhook_secret: str | None = None, resume: Callable[[str], Any] | None = None,
                 prices: Callable[[list[str]], dict[str, float]] | None = None):
        self.db_url = db_url or os.environ.get("QT_DATABASE_URL", ":memory:")
        self.password = password if password is not None else os.environ.get("DASHBOARD_PASSWORD", "")
        from qt.telegram import webhook_token
        self.webhook_secret = webhook_token(webhook_secret if webhook_secret is not None
                                            else os.environ.get("TELEGRAM_WEBHOOK_SECRET", ""))
        self._bot = bot
        self._resume = resume
        self._prices = prices
        self._db: DB | None = None

    # ------------------------------------------------------------ plumbing
    @property
    def db(self) -> DB:
        if self._db is None:
            self._db = DB(self.db_url)
        return self._db

    @property
    def bot(self):
        if self._bot is None:
            from qt.telegram import Telegram
            self._bot = Telegram.from_env()
        return self._bot

    @property
    def prices(self) -> Callable[[list[str]], dict[str, float]]:
        if self._prices is None:
            from qt.data.store import Store
            from qt.positions import yahoo_prices
            self._prices = yahoo_prices(Store(self.db))
        return self._prices

    def resume(self, run_id: str) -> None:
        if self._resume is not None:
            self._resume(run_id)
        else:
            from qt.app import resume_in_background
            resume_in_background(self.db_url, run_id)

    def _authed(self, env: dict) -> bool:
        h = env.get("HTTP_AUTHORIZATION", "")
        if not self.password or not h.startswith("Basic "):
            return False
        try:
            _, _, pw = base64.b64decode(h[6:]).decode().partition(":")
        except ValueError:
            return False
        return hmac.compare_digest(pw, self.password)

    @staticmethod
    def _same_origin(env: dict) -> bool:
        origin = env.get("HTTP_ORIGIN") or env.get("HTTP_REFERER")
        return not origin or re.sub(r"^https?://", "", origin).split("/")[0] == env.get("HTTP_HOST")

    @staticmethod
    def _body(env: dict) -> bytes:
        n = int(env.get("CONTENT_LENGTH") or 0)
        return env["wsgi.input"].read(min(n, 2_000_000)) if n else b""

    @staticmethod
    def _send(start: Start, status: str, body: str, ctype: str = "text/html; charset=utf-8",
              headers: Iterable[tuple[str, str]] = ()) -> list[bytes]:
        start(status, [("Content-Type", ctype), *headers])
        return [body.encode()]

    def _page(self, start: Start, title: str, body: str) -> list[bytes]:
        return self._send(start, "200 OK", f"<!doctype html><html lang=he dir=rtl><meta charset=utf-8>"
                          f"<meta name=viewport content='width=device-width,initial-scale=1'><title>{_e(title)}</title>"
                          f"<style>{CSS}</style><h1>{_e(title)}</h1>{body}</html>")

    def _live(self, env: dict, start: Start, title: str, body: str) -> list[bytes]:
        """A page that refreshes itself; with ?fragment=1 only the refreshing part (what the script fetches)."""
        if "fragment" in parse_qs(env.get("QUERY_STRING", "")):
            return self._send(start, "200 OK", body, headers=[("Cache-Control", "no-store")])
        nav = "<nav><a href='/'>לוח בקרה</a><a href='/positions'>פוזיציות</a><a href='/journal'>יומן</a></nav>"
        return self._page(start, title, f"{nav}<p id=stamp class=muted>מתעדכן כל {REFRESH_S} שניות</p>"
                                        f"<div id=live>{body}</div>" + LIVE_JS % REFRESH_S)

    def _deny(self, start: Start) -> list[bytes]:
        return self._send(start, "401 Unauthorized", "auth required", "text/plain",
                          [("WWW-Authenticate", 'Basic realm="qt"')])

    # ------------------------------------------------------------ routing
    def __call__(self, env: dict, start: Start) -> list[bytes]:
        path, method = env.get("PATH_INFO", "/"), env.get("REQUEST_METHOD", "GET")
        try:
            if path == "/health":
                return self._send(start, "200 OK", "ok", "text/plain")
            m = re.fullmatch(r"/telegram/([\w-]+)", path)
            if m and method == "POST":
                return self.telegram(env, start, m.group(1))
            m = re.fullmatch(r"/item/(\d+)", path)
            if m and method == "GET":
                return self.item(env, start, int(m.group(1)))
            m = re.fullmatch(r"/report/(\d+)", path)
            if m and method == "GET":
                return self.report(env, start, int(m.group(1)))
            m = re.fullmatch(r"/journal(?:/(trades|ideas|equity)\.csv)?", path)
            if m and method == "GET":
                return self.journal(env, start, m.group(1))
            if not self._authed(env):
                return self._deny(start)
            if path == "/" and method == "GET":
                return self.dashboard(env, start)
            if path == "/positions" and method == "GET":
                return self._live(env, start, "פוזיציות פתוחות", self.positions_html())
            if path == "/api/positions" and method == "GET":
                return self._send(start, "200 OK", json.dumps(self.positions(), ensure_ascii=False, default=str),
                                  "application/json; charset=utf-8", [("Cache-Control", "no-store")])
            if method == "POST" and not self._same_origin(env):
                return self._send(start, "403 Forbidden", "cross-site request", "text/plain")
            m = re.fullmatch(r"/item/(\d+)/(approve|reject|postpone)", path)
            if m and method == "POST":
                return self.decide(env, start, int(m.group(1)), m.group(2))
            m = re.fullmatch(r"/runs/([\w.-]+)/resume", path)
            if m and method == "POST":
                self.resume(m.group(1))
                return self._send(start, "303 See Other", "", headers=[("Location", "/")])
            return self._send(start, "404 Not Found", "not found", "text/plain")
        except Exception as e:                                          # noqa: BLE001 - never leak a trace
            print(json.dumps({"level": "error", "path": path, "error": f"{type(e).__name__}: {e}"}), flush=True)
            return self._send(start, "500 Internal Server Error", "error", "text/plain")

    # ------------------------------------------------------------ handlers
    def telegram(self, env: dict, start: Start, secret: str) -> list[bytes]:
        header = env.get("HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN", "")
        if not self.webhook_secret or not (hmac.compare_digest(secret, self.webhook_secret)
                                           and hmac.compare_digest(header, self.webhook_secret)):
            return self._send(start, "403 Forbidden", "", "text/plain")
        from qt.telegram import handle_update
        update = json.loads(self._body(env) or b"{}")
        what = handle_update(update, self.bot, Queue(self.db), on_resume=self.resume, on_upload=self.upload)
        print(json.dumps({"level": "info", "telegram": what}, ensure_ascii=False), flush=True)
        return self._send(start, "200 OK", "ok", "text/plain")

    def upload(self, name: str, content: str) -> str:
        """A portfolio file replaces the portfolio, a watchlist file the watchlist; anything else is the monthly manual macro file, and a monthly
        run waiting for it resumes."""
        import yaml
        try:
            yaml.safe_load(content)
        except yaml.YAMLError as e:
            return f"הקובץ לא נקרא כ-YAML: {e}"
        q = Queue(self.db)
        if name.lower().startswith("portfolio"):
            q.upload("portfolio", name, content)
            return "התיק עודכן: הריצה הבאה תשתמש בו"
        if name.lower().startswith("watchlist"):
            q.upload("watchlist", name, content)
            return "רשימת המעקב עודכנה: הריצה היומית הבאה תבדוק אותה"
        q.upload("manual_macro", name, content)
        from qt.app import waiting_monthly
        rid = waiting_monthly(self.db)
        if rid:
            self.resume(rid)
            return f"קובץ המקרו נשמר: ממשיך את הריצה החודשית {rid}"
        return "קובץ המקרו נשמר: ישמש בריצה החודשית הבאה"

    def item(self, env: dict, start: Start, item_id: int) -> list[bytes]:
        from qt.telegram import item_text, verify
        sig = (parse_qs(env.get("QUERY_STRING", "")).get("sig") or [""])[0]
        if not (verify(item_id, sig, self.password) or self._authed(env)):
            return self._deny(start)
        q = Queue(self.db)
        it = q.get(item_id)
        if it is None:
            return self._send(start, "404 Not Found", "not found", "text/plain")
        body = f"<pre>{_e(item_text(it))}</pre>"
        if it.get("decision_id"):
            d = q.journal.get(it["decision_id"])
            if d:
                body += "<h2>יומן ההחלטות</h2><pre>" + _e(json.dumps(d, ensure_ascii=False, indent=1, default=str)) + "</pre>"
        ag = (it["payload"] or {}).get("agent")
        if ag:
            body += "<h2>Agent ובודק</h2><pre>" + _e(json.dumps(ag, ensure_ascii=False, indent=1)) + "</pre>"
        body += "<h2>הפריט</h2><pre>" + _e(json.dumps(it, ensure_ascii=False, indent=1, default=str)) + "</pre>"
        return self._page(start, f"פריט #{item_id}", body)

    def report(self, env: dict, start: Start, upload_id: int) -> list[bytes]:
        """A research report the weekly run saved: by its signed link from Telegram, or after login."""
        from qt.telegram import verify_report
        sig = (parse_qs(env.get("QUERY_STRING", "")).get("sig") or [""])[0]
        if not (verify_report(upload_id, sig, self.password) or self._authed(env)):
            return self._deny(start)
        row = self.db.one("SELECT content FROM uploads WHERE id = ? AND kind = 'report'", (upload_id,))
        if row is None:
            return self._send(start, "404 Not Found", "not found", "text/plain")
        return self._send(start, "200 OK", row["content"])

    def journal(self, env: dict, start: Start, csv_name: str | None) -> list[bytes]:
        """The trade journal (qt.learning.tables): the page with every table, or one table as CSV for Excel or
        Google Sheets. Opens from the signed link in Telegram (/journal) or after login."""
        from qt.app import portfolio
        from qt.learning import tables
        from qt.telegram import verify_journal
        from qt.tradelog import to_csv
        sig = (parse_qs(env.get("QUERY_STRING", "")).get("sig") or [""])[0]
        if not (verify_journal(sig, self.password) or self._authed(env)):
            return self._deny(start)
        t = tables(self.db, portfolio(self.db) or {})
        if csv_name:
            rows, cols = t[csv_name]
            return self._send(start, "200 OK", to_csv(rows, cols), "text/csv; charset=utf-8",
                              [("Content-Disposition", f'attachment; filename="qt-{csv_name}.csv"')])
        q = f"?sig={_e(sig)}" if sig else ""
        names = {"trades": "עסקאות", "ideas": "רעיונות כניסה ומה קרה איתם", "equity": "Equity יומי"}
        body = ""
        for name, (rows, cols) in t.items():
            head = "".join(f"<th>{_e(h)}</th>" for _, h in cols)
            trs = "".join("<tr>" + "".join(f"<td>{_e('' if r.get(k) is None else r.get(k))}</td>" for k, _ in cols)
                          + "</tr>" for r in rows)
            body += (f"<h2>{names[name]} ({len(rows)})</h2><p><a href='/journal/{name}.csv{q}'>הורדה כ-CSV</a></p>"
                     f"<div style='overflow-x:auto'><table><tr>{head}</tr>{trs}</table></div>")
        return self._page(start, "יומן המסחר", body)

    def decide(self, env: dict, start: Start, item_id: int, choice: str) -> list[bytes]:
        form = parse_qs(self._body(env).decode())
        note = (form.get("note") or [""])[0]
        back = "/positions" if (form.get("back") or [""])[0] == "positions" else "/"
        try:
            Queue(self.db).decide(item_id, choice, note)
        except NotOpen as e:
            return self._send(start, "409 Conflict", f"not open: {e}", "text/plain")
        if self.bot is not None:
            from qt.telegram import refresh_item
            try:
                refresh_item(self.bot, Queue(self.db), item_id)
            except Exception:                                       # noqa: BLE001 - the decision stands
                pass
        return self._send(start, "303 See Other", "", headers=[("Location", back)])

    def positions(self) -> dict:
        """The paper book (the newest portfolio upload, saved by the daily run after the morning's fills) marked
        to the latest prices, with the paper orders still waiting for the next open."""
        from qt.app import portfolio
        from qt.positions import snapshot
        q = Queue(self.db)
        pf = portfolio(self.db)
        symbols = sorted({p["symbol"] for p in (pf or {}).get("positions", [])
                          if p.get("instrument", "stock") == "stock"})
        return snapshot(pf, self.prices(symbols), q.open_orders())

    def positions_html(self) -> str:
        s = self.positions()
        if not s["has_portfolio"]:
            return "<h2>פוזיציות פתוחות</h2><p>עוד אין תיק: שלח לבוט קובץ portfolio.yaml.</p>"
        cards = "".join(f"<div class=card>{t}<b>{v}</b></div>" for t, v in (
            ("שווי תיק", _n(s["equity"], 0)), ("מזומן", _n(s["cash"], 0)),
            ("רווח/הפסד פתוח", _signed(s["unrealized"], 0)), ("רווח ממומש", _signed(s["realized"], 0)),
            ("לונג / שורט", f"{_n(s['long_value'], 0)} / {_n(s['short_value'], 0)}"),
            ("ירידה מהשיא", _signed(s["drawdown_pct"], 1, "%"))))
        rows = "".join(
            f"<tr><td><b>{_e(p['symbol'])}</b></td><td>{'לונג' if p['direction'] == 'long' else 'שורט'}</td>"
            f"<td>{_e(p['sector'])}</td><td class=num>{_n(p['qty'], 0)}</td><td class=num>{_n(p['entry'])}</td>"
            f"<td class=num>{_n(p['last']) if p['priced'] else '<span class=muted>אין מחיר</span>'}</td>"
            f"<td class=num>{_signed(p['pl'], 0)}</td><td class=num>{_signed(p['pl_pct'], 2, '%')}</td>"
            f"<td class=num>{_n(p['stop'])}</td><td class=num>{_n(p['stop_dist_pct'], 1)}{'%' if p['stop_dist_pct'] is not None else ''}</td>"
            f"<td class=num>{_n(p['target'])}</td><td class=num>{_n(p['weight_pct'], 1)}%</td><td>{_e(p['opened'])}</td></tr>"
            for p in s["positions"])
        body = (f"<h2>פוזיציות פתוחות ({len(s['positions'])})</h2><div class=cards>{cards}</div>"
                f"<div class=wrap><table><tr><th>מניה</th><th>כיוון</th><th>סקטור</th><th>כמות</th><th>כניסה</th>"
                f"<th>מחיר</th><th>רו\"ה $</th><th>רו\"ה %</th><th>סטופ</th><th>מרחק לסטופ</th><th>יעד</th><th>משקל</th>"
                f"<th>נפתחה</th></tr>{rows}</table></div>")
        if s["pending"]:
            body += ("<h3>ממתינות לביצוע בפתיחה</h3><table><tr><th>מניה</th><th>סוג</th><th>כמות</th><th>בפתיחת</th></tr>"
                     + "".join(f"<tr><td>{_e(o['symbol'])}</td><td>{_e(o['kind'])}</td><td class=num>{_e(o['qty'])}</td>"
                               f"<td>{_e(o['fill_on'])}</td></tr>" for o in s["pending"]) + "</table>")
        return self.ready_html(s) + body + "<p class=muted>מחירים מ-Yahoo (בעיכוב), נשמרים לדקה. התיק: תיק הנייר אחרי הריצה היומית האחרונה.</p>"

    def ready_html(self, s: dict) -> str:
        """Entries the system prepared (a watchlist opportunity the daily run queued: size, stop, target) with the one
        button that opens them: approving makes the paper order, filled at the next open and written to the book."""
        ready = [i for i in Queue(self.db).open_items() if i["kind"] in ("enter", "add")]
        if not ready:
            return ""
        out = f"<h2>מוכנות לפתיחה ({len(ready)})</h2>"
        for i in ready:
            p = i["payload"]
            qty, entry, stop = p.get("qty"), p.get("price") or i["ref_price"], p.get("stop")
            risk = qty * abs(entry - stop) if qty and entry and stop else None
            facts = (("כיוון", "לונג" if p.get("direction", "long") == "long" else "שורט"), ("כמות", _n(qty, 0)),
                     ("כניסה", _n(entry)), ("סטופ", _n(stop)), ("יעד", _n(p.get("target"))),
                     ("סכום", _n(qty * entry, 0) if qty and entry else ""),
                     ("סיכון", f"{_n(risk, 0)}$" + (f" ({risk / s['equity'] * 100:.2f}%)" if risk and s.get("equity") else "")
                      if risk else ""))
            out += (f"<div class=card><b>{_e(i['symbol'])} · <a href='/item/{i['id']}'>#{i['id']}</a></b>"
                    + " · ".join(f"{t} {v}" for t, v in facts if v)
                    + f"<p>{_e(p.get('detail'))}</p><p class=muted>בתוקף עד {_e(str(i['valid_until'])[:16].replace('T', ' '))} UTC; "
                    f"אישור יוצר פקודת נייר שתתבצע בפתיחה הבאה</p>"
                    f"<form method=post action='/item/{i['id']}/approve'><input type=hidden name=back value=positions>"
                    f"<button style='font-size:16px;padding:6px 14px'>✅ פתח פוזיציה</button></form>"
                    f"<form method=post action='/item/{i['id']}/reject'><input type=hidden name=back value=positions>"
                    f"<button>❌ דחה</button></form></div>")
        return out

    def dashboard(self, env: dict, start: Start) -> list[bytes]:
        from qt.runs import SCHEMA
        self.db.ddl(SCHEMA)
        q = Queue(self.db)
        try:
            body = self.positions_html()
        except Exception as e:                                          # noqa: BLE001 - the rest of the page stands
            print(json.dumps({"level": "error", "path": "/positions", "error": f"{type(e).__name__}: {e}"}), flush=True)
            body = "<h2>פוזיציות פתוחות</h2><p class=failed>לא הצלחתי לטעון את התיק</p>"
        runs = self.db.all("SELECT * FROM runs ORDER BY started_at DESC LIMIT 15")
        rows = "".join(
            f"<tr><td>{_e(r['id'])}</td><td class={_e(r['status'])}>{_e(r['status'])}</td><td>{_e(r['resumes'])}</td>"
            f"<td>{_e(r['error'])}</td><td>" + (f"<form method=post action='/runs/{_e(r['id'])}/resume'>"
                                              f"<button>המשך ריצה</button></form>" if r["status"] == "failed" else "")
            + "</td></tr>" for r in runs)
        body += f"<h2>ריצות</h2><table><tr><th>ריצה</th><th>מצב</th><th>המשכים</th><th>שגיאה</th><th></th></tr>{rows}</table>"
        items = q.open_items()
        irows = "".join(
            f"<tr><td><a href='/item/{i['id']}'>#{i['id']}</a></td><td>{_e(i['as_of'])}</td><td>{_e(i['symbol'])}</td>"
            f"<td>{_e(i['kind'])} · {_e(i['rule_id'])}</td><td>{_e(i['status'])}</td><td>{_e(i['payload'].get('detail'))}</td><td>"
            + "".join(f"<form method=post action='/item/{i['id']}/{c}'><button>{t}</button></form>"
                      for c, t in (("approve", "✅ אשר"), ("reject", "❌ דחה"), ("postpone", "⏸ מחר")))
            + "</td></tr>" for i in items)
        body += (f"<h2>תור פתוח ({len(items)})</h2><table><tr><th></th><th>יום</th><th>מניה</th><th>מה</th><th>מצב</th>"
                 f"<th>פירוט</th><th></th></tr>{irows}</table>")
        orders = self.db.all("SELECT * FROM paper_orders ORDER BY id DESC LIMIT 20")
        orows = "".join(f"<tr><td>{_e(o['symbol'])}</td><td>{_e(o['kind'])}</td><td>{_e(o['qty'])}</td>"
                        f"<td>{_e(o['fill_on'])}</td><td>{_e(o['status'])}</td><td>{_e(o['fill_price'])}</td></tr>"
                        for o in orders)
        body += (f"<h2>עסקאות נייר</h2><table><tr><th>מניה</th><th>סוג</th><th>כמות</th><th>בפתיחת</th><th>מצב</th>"
                 f"<th>מחיר</th></tr>{orows}</table>")
        month = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
        cost = self.db.one("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM agent_calls WHERE created_at >= ?", (month,))
        body += f"<p>עלות Agents ב-30 הימים האחרונים: {float(cost['c'] or 0):.2f}$</p>"
        return self._live(env, start, "Quantum Trader · לוח בקרה", body)


_APP: App | None = None


def application(env: dict, start: Start) -> list[bytes]:
    """gunicorn entry point: `gunicorn qt.web:application`."""
    global _APP
    if _APP is None:
        _APP = App()
    return _APP(env, start)


def serve(port: int = 8000) -> None:
    from wsgiref.simple_server import make_server
    with make_server("", port, App()) as srv:
        print(f"qt web on :{port}")
        srv.serve_forever()
