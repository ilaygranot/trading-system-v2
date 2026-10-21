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
@media (prefers-color-scheme:dark){body{background:#161616;color:#ddd}pre{background:#222;border-color:#444}td,th{border-color:#333}}"""


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


class App:
    def __init__(self, db_url: str | None = None, bot: Any = None, password: str | None = None,
                 webhook_secret: str | None = None, resume: Callable[[str], Any] | None = None):
        self.db_url = db_url or os.environ.get("QT_DATABASE_URL", ":memory:")
        self.password = password if password is not None else os.environ.get("DASHBOARD_PASSWORD", "")
        self.webhook_secret = webhook_secret if webhook_secret is not None else os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
        self._bot = bot
        self._resume = resume
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
            if not self._authed(env):
                return self._deny(start)
            if path == "/" and method == "GET":
                return self.dashboard(start)
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

    def decide(self, env: dict, start: Start, item_id: int, choice: str) -> list[bytes]:
        note = (parse_qs(self._body(env).decode()).get("note") or [""])[0]
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
        return self._send(start, "303 See Other", "", headers=[("Location", "/")])

    def dashboard(self, start: Start) -> list[bytes]:
        from qt.runs import SCHEMA
        self.db.ddl(SCHEMA)
        q = Queue(self.db)
        runs = self.db.all("SELECT * FROM runs ORDER BY started_at DESC LIMIT 15")
        rows = "".join(
            f"<tr><td>{_e(r['id'])}</td><td class={_e(r['status'])}>{_e(r['status'])}</td><td>{_e(r['resumes'])}</td>"
            f"<td>{_e(r['error'])}</td><td>" + (f"<form method=post action='/runs/{_e(r['id'])}/resume'>"
                                              f"<button>המשך ריצה</button></form>" if r["status"] == "failed" else "")
            + "</td></tr>" for r in runs)
        body = f"<h2>ריצות</h2><table><tr><th>ריצה</th><th>מצב</th><th>המשכים</th><th>שגיאה</th><th></th></tr>{rows}</table>"
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
        return self._page(start, "Quantum Trader · לוח בקרה", body)


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
