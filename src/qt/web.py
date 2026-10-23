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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs

from qt.approvals import NotOpen, Queue
from qt.db import DB

Start = Callable[[str, list[tuple[str, str]]], Any]

from qt import webui as ui
from qt.webui import REFRESH_S  # noqa: F401 - kept for callers

# The files that make the dashboard an installable app (22.10), served without the login: no data in them.
STATIC = {"/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
          "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
          "/icon-192.png": ("icon-192.png", "image/png"), "/icon-512.png": ("icon-512.png", "image/png"),
          "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"), "/favicon.png": ("favicon.png", "image/png"),
          "/favicon.ico": ("favicon.png", "image/png")}


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
                 prices: Callable[[list[str]], dict[str, float]] | None = None,
                 bars: Callable[[str], list] | None = None, news: Callable[[str], list[dict]] | None = None):
        self.db_url = db_url or os.environ.get("QT_DATABASE_URL", ":memory:")
        self.password = password if password is not None else os.environ.get("DASHBOARD_PASSWORD", "")
        from qt.telegram import webhook_token
        self.webhook_secret = webhook_token(webhook_secret if webhook_secret is not None
                                            else os.environ.get("TELEGRAM_WEBHOOK_SECRET", ""))
        self._bot = bot
        self._resume = resume
        self._prices = prices
        self._bars = bars
        self._news = news
        self._db: DB | None = None
        self._gw: tuple[float, tuple[str | None, str]] | None = None
        self._dossier: tuple[int, dict] | None = None
        self._web_cache: dict[str, tuple[float, Any]] = {}

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
        return env["wsgi.input"].read(min(n, 8_000_000)) if n else b""

    @staticmethod
    def _send(start: Start, status: str, body: str | bytes, ctype: str = "text/html; charset=utf-8",
              headers: Iterable[tuple[str, str]] = ()) -> list[bytes]:
        start(status, [("Content-Type", ctype), *headers])
        return [body if isinstance(body, bytes) else body.encode()]

    def _page(self, start: Start, title: str, body: str, active: str = "", nav_q: str = "") -> list[bytes]:
        return self._send(start, "200 OK", ui.page(title, body, active, nav_q=nav_q))

    def _live(self, env: dict, start: Start, title: str, body: Callable[[], str] | str,
              active: str = "", css: str = "") -> list[bytes]:
        """A page that refreshes itself; with ?fragment=1 only the refreshing part (what the script fetches)."""
        body = body() if callable(body) else body
        if "fragment" in parse_qs(env.get("QUERY_STRING", "")):
            return self._send(start, "200 OK", body, headers=[("Cache-Control", "no-store")])
        return self._send(start, "200 OK", ui.page(title, body, active, live=True, css=css))

    def _deny(self, start: Start) -> list[bytes]:
        return self._send(start, "401 Unauthorized", "auth required", "text/plain",
                          [("WWW-Authenticate", 'Basic realm="qt"')])

    # ------------------------------------------------------------ routing
    def __call__(self, env: dict, start: Start) -> list[bytes]:
        """Every answer of some size goes out gzipped when the browser takes it: the overview is the whole research
        report (~500KB of tables and charts) and a phone on a slow line showed a white screen until it came in."""
        if "gzip" not in env.get("HTTP_ACCEPT_ENCODING", ""):
            return self._route(env, start)
        got: dict = {}
        body = b"".join(self._route(env, lambda status, headers, *a: got.update(status=status, headers=list(headers))))
        headers = got["headers"]
        ctype = next((v for k, v in headers if k.lower() == "content-type"), "")
        if len(body) < 1024 or not ctype.startswith(("text/", "application/json", "application/javascript", "image/svg")):
            start(got["status"], headers)
            return [body]
        import gzip
        body = gzip.compress(body, compresslevel=6)
        start(got["status"], headers + [("Content-Encoding", "gzip"), ("Vary", "Accept-Encoding"),
                                        ("Content-Length", str(len(body)))])
        return [body]

    def _route(self, env: dict, start: Start) -> list[bytes]:
        path, method = env.get("PATH_INFO", "/"), env.get("REQUEST_METHOD", "GET")
        try:
            if path == "/health":
                return self._send(start, "200 OK", "ok", "text/plain")
            if path in STATIC and method == "GET":                     # the installed app's files: public, no data
                return self.static(start, path)
            if path == "/offline" and method == "GET":
                return self._send(start, "200 OK", ui.offline())
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
                return self._live(env, start, "פוזיציות פתוחות", self.positions_html, "/positions")
            if path == "/runs" and method == "GET":
                return self._live(env, start, "ריצות", self.runs_html, "/runs")
            m = re.fullmatch(r"/runs/([\w.-]+)", path)
            if m and method == "GET":
                return self._live(env, start, f"ריצה {m.group(1)}", lambda: self.run_html(m.group(1)), "/runs")
            m = re.fullmatch(r"/stock/([\w.\-]{1,12})", path)
            if m and method == "GET":
                sym = m.group(1).upper()
                return self._live(env, start, sym, lambda: self.stock_html(sym), "/", css=ui.report_css())
            if path == "/macro" and method == "GET":
                return self._live(env, start, "דוח מקרו", self.macro_html, "/", css=ui.report_css())
            if path == "/sectors" and method == "GET":
                return self._live(env, start, "סקטורים וסינון", self.sectors_html, "/", css=ui.report_css())
            if path == "/system" and method == "GET":
                return self._live(env, start, "מצב המערכת", self.system_html, "/system")
            if path == "/api/status" and method == "GET":
                return self._send(start, "200 OK", json.dumps(self.status(), ensure_ascii=False, default=str),
                                  "application/json; charset=utf-8", [("Cache-Control", "no-store")])
            if path == "/api/positions" and method == "GET":
                return self._send(start, "200 OK", json.dumps(self.positions(), ensure_ascii=False, default=str),
                                  "application/json; charset=utf-8", [("Cache-Control", "no-store")])
            if method == "POST" and not self._same_origin(env):
                return self._send(start, "403 Forbidden", "cross-site request", "text/plain")
            if path == "/api/dossier" and method == "POST":           # the research dossier, for the dashboard
                msg = self.upload("dossier.json", self._body(env).decode("utf-8"))
                return self._send(start, "200 OK" if msg.startswith("דוח") else "400 Bad Request", msg,
                                  "text/plain; charset=utf-8")
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
    def static(self, start: Start, path: str) -> list[bytes]:
        name, ctype = STATIC[path]
        data = (Path(__file__).parent / "static" / name).read_bytes()
        # the service worker must be re-checked on each visit, so a new version reaches the phone after a deploy
        cache = "no-cache" if name == "sw.js" else "public, max-age=86400"
        return self._send(start, "200 OK", data, ctype, [("Cache-Control", cache)])

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
        """A portfolio file replaces the portfolio, a watchlist file the watchlist, a research dossier (JSON) the report on the
        dashboard; anything else is the monthly manual macro file, and a monthly run waiting for it resumes."""
        import yaml
        if name.lower().startswith("dossier"):
            try:
                d = json.loads(content)
                d["meta"]["as_of"]
            except (ValueError, KeyError, TypeError) as e:
                return f"הקובץ לא נקרא כ-dossier של מחקר: {e}"
            Queue(self.db).upload("dossier", name, content)
            return f"דוח המחקר מ-{d['meta']['as_of']} עלה ללוח הבקרה"
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
        body = ui.section("ההודעה", f"<pre style='direction:rtl;text-align:right;font-family:var(--body);font-size:.95rem'>"
                                    f"{_e(item_text(it))}</pre>")
        if it.get("decision_id"):
            d = q.journal.get(it["decision_id"])
            if d:
                body += ui.section("יומן ההחלטות", "<pre>" + _e(json.dumps(d, ensure_ascii=False, indent=1, default=str)) + "</pre>")
        ag = (it["payload"] or {}).get("agent")
        if ag:
            body += ui.section("Agent ובודק", "<pre>" + _e(json.dumps(ag, ensure_ascii=False, indent=1)) + "</pre>")
        body += ui.section("הפריט", "<pre>" + _e(json.dumps(it, ensure_ascii=False, indent=1, default=str)) + "</pre>")
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
            tbl = (ui.table((_e(h) for _, h in cols), ([_e(r.get(k)) for k, _ in cols] for r in rows)) if rows
                   else ui.empty("עוד אין שורות"))
            body += ui.section(f"{names[name]} ({len(rows)})",
                               f"<p class=note><a href='/journal/{name}.csv{q}'>הורדה כ-CSV</a> לאקסל או ל-Google Sheets</p>"
                               + tbl)
        return self._page(start, "יומן המסחר", body, "/journal", q)

    def decide(self, env: dict, start: Start, item_id: int, choice: str) -> list[bytes]:
        form = parse_qs(self._body(env).decode())
        note = (form.get("note") or [""])[0]
        back = {"positions": "/positions"}.get((form.get("back") or [""])[0], "/")
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

    # ------------------------------------------------------------ live pages
    KINDS = (("daily", "יומית"), ("premarket", "לפני פתיחה"), ("open", "בפתיחה"), ("weekly", "שבועית"), ("monthly", "חודשית"))

    def _schemas(self) -> Queue:
        from qt.runs import SCHEMA
        self.db.ddl(SCHEMA)
        return Queue(self.db)

    @staticmethod
    def run_state(r: dict | None) -> tuple[str, str]:
        """(dot / pill class, Hebrew) for a run row."""
        if r is None:
            return "", "עוד לא רצה"
        if r["status"] == "done":
            return "ok", "הושלמה"
        if r["status"] == "running":
            return "warn", "רצה עכשיו"
        if "manual macro" in str(r.get("error") or ""):
            return "warn", "מחכה לקובץ מקרו"
        return "bad", "נכשלה"

    def gateway(self) -> tuple[str | None, str]:
        """Is IB Gateway reachable (a TCP connect to the API port, cached a minute). Read-only: nothing is sent."""
        import socket
        import time
        now = time.monotonic()
        if self._gw is not None and now - self._gw[0] < 60:
            return self._gw[1]
        host, port = os.environ.get("IBKR_HOST"), os.environ.get("IBKR_PORT", "4004")
        if not host:
            res: tuple[str | None, str] = (None, "לא מוגדר בשירות הזה")
        else:
            try:
                socket.create_connection((host, int(port)), timeout=1.5).close()
                res = ("ok", "עונה · paper, קריאה בלבד")
            except OSError:
                res = ("bad", "לא עונה")
        self._gw = (now, res)
        return res

    def costs(self) -> dict:
        now = datetime.now(timezone.utc)
        month = (now - timedelta(days=30)).isoformat(timespec="seconds")
        today = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
        tot = self.db.one("SELECT COALESCE(SUM(cost_usd), 0) AS c, COUNT(*) AS n FROM agent_calls WHERE created_at >= ?",
                          (month,))
        day = self.db.one("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM agent_calls WHERE created_at >= ?", (today,))
        by = self.db.all("SELECT agent, COALESCE(SUM(cost_usd), 0) AS c, COUNT(*) AS n FROM agent_calls "
                         "WHERE created_at >= ? GROUP BY agent ORDER BY c DESC", (month,))
        return {"month": float(tot["c"] or 0), "calls": int(tot["n"] or 0), "today": float(day["c"] or 0),
                "by_agent": [{"agent": r["agent"], "cost": float(r["c"] or 0), "calls": int(r["n"])} for r in by]}

    def status(self) -> dict:
        """Everything the overview shows, as data (also /api/status)."""
        from qt.schedule import next_run
        q = self._schemas()
        runs = {}
        for k, _ in self.KINDS:
            r = self.db.one("SELECT * FROM runs WHERE kind = ? ORDER BY started_at DESC LIMIT 1", (k,))
            nxt = next_run(k)
            runs[k] = {"last": r, "state": self.run_state(r), "next": nxt.isoformat() if nxt else None}
        try:
            book: dict | None = self.positions()
        except Exception as e:                                          # noqa: BLE001 - the rest of the page stands
            print(json.dumps({"level": "error", "path": "/positions", "error": f"{type(e).__name__}: {e}"}), flush=True)
            book = None
        return {"runs": runs, "queue": q.open_items(), "gateway": self.gateway(), "costs": self.costs(), "book": book}

    # ---- pieces
    def item_card(self, i: dict, equity: float | None, back: str = "") -> str:
        """One queue item: what the system proposes, the numbers, and the buttons. An entry gets the one big
        "open position" button: approving makes the paper order, filled at the next open and written to the book."""
        from qt.telegram import KIND
        p = i["payload"] or {}
        icon, name = KIND.get(i["kind"], ("•", i["kind"]))
        qty, entry, stop = p.get("qty"), p.get("price") or i.get("ref_price"), p.get("stop")
        risk = qty * abs(entry - stop) if qty and entry and stop else None
        facts = (("כיוון", ("לונג" if p.get("direction", "long") == "long" else "שורט") if i["kind"] in ("enter", "add") else ""),
                 ("כמות", _n(qty, 0)), ("כניסה", _n(entry)), ("סטופ", _n(stop)), ("יעד", _n(p.get("target"))),
                 ("סכום", _n(qty * entry, 0) if qty and entry else ""),
                 ("סיכון", f"{_n(risk, 0)}$" + (f" ({risk / equity * 100:.2f}%)" if equity else "") if risk else ""))
        hidden = f"<input type=hidden name=back value={back}>" if back else ""
        if i["kind"] in ("enter", "add"):
            buttons = (("approve", "✅ פתח פוזיציה", " class=go"), ("reject", "❌ דחה", ""))
        else:
            buttons = (("approve", "✅ אשר", " class=go"), ("reject", "❌ דחה", ""), ("postpone", "⏸ מחר", ""))
        st = {"changed": ui.pill("השתנה מאז ההצעה", "warn"), "postponed": ui.pill("נדחה למחר", "mute")}.get(i["status"], "")
        return (f"<div class=card><h3>{icon} {ui.sym(i['symbol'])} · {_e(name)} {st} "
                f"<a class=note href='/item/{i['id']}'>#{i['id']}</a></h3>"
                f"<div class=facts>" + "".join(f"<span>{t} <b>{v}</b></span>" for t, v in facts if v) + "</div>"
                + (f"<p>{_e(p.get('detail'))}</p>" if p.get("detail") else "")
                + f"<p class=note>{_e(i['rule_id'])} · בתוקף עד {ui.when(i['valid_until'])} (שעון ישראל)"
                + ("; אישור יוצר פקודת נייר שתתבצע בפתיחה הבאה" if i["kind"] in ("enter", "add") else "") + "</p>"
                + "<div class=acts>" + "".join(f"<form method=post action='/item/{i['id']}/{c}'>{hidden}<button{cls}>{t}"
                                                f"</button></form>" for c, t, cls in buttons) + "</div></div>")

    def ready_html(self, s: dict) -> str:
        """Entries the system prepared (a watchlist opportunity the daily run queued: size, stop, target)."""
        ready = [i for i in Queue(self.db).open_items() if i["kind"] in ("enter", "add")]
        if not ready:
            return ""
        return ui.section(f"מוכנות לפתיחה ({len(ready)})", "<div class=cards>"
                          + "".join(self.item_card(i, s.get("equity"), "positions") for i in ready) + "</div>")

    @staticmethod
    def book_strip(s: dict) -> str:
        dd = s["drawdown_pct"]
        return "<div class=strip>" + "".join((
            ui.cell(f"{_n(s['equity'], 0)}$", "שווי התיק", big=True),
            ui.cell(f"{_n(s['cash'], 0)}$" if s["cash"] is not None else "", "מזומן", big=True),
            ui.cell(_signed(s["unrealized"], 0), "רווח/הפסד פתוח", big=True),
            ui.cell(_signed(s["realized"], 0), "רווח ממומש", big=True),
            ui.cell(f"{_n(s['long_value'], 0)} / {_n(s['short_value'], 0)}", "לונג / שורט", big=True),
            ui.cell(_signed(dd, 1, "%") if dd else "0.0%", "ירידה מהשיא", big=True))) + "</div>"

    @staticmethod
    def book_table(s: dict) -> str:
        if not s["positions"]:
            return ui.empty("אין פוזיציות פתוחות: התיק כולו במזומן")
        return ui.table(
            ("מניה", "כיוון", "סקטור", "כמות", "כניסה", "מחיר", "רו\"ה $", "רו\"ה %", "סטופ", "מרחק לסטופ", "יעד",
             "משקל", "נפתחה"),
            ([f"<b>{ui.sym(p['symbol'])}</b>", ui.pill("לונג", "ok") if p["direction"] == "long" else ui.pill("שורט", "bad"),
              _e(p["sector"]), f"<td class=num>{_n(p['qty'], 0)}</td>", f"<td class=num>{_n(p['entry'])}</td>",
              f"<td class=num>{_n(p['last']) if p['priced'] else '<span class=muted>אין מחיר</span>'}</td>",
              f"<td class=num>{_signed(p['pl'], 0)}</td>", f"<td class=num>{_signed(p['pl_pct'], 2, '%')}</td>",
              f"<td class=num>{_n(p['stop'])}</td>",
              f"<td class=num>{_n(p['stop_dist_pct'], 1)}{'%' if p['stop_dist_pct'] is not None else ''}</td>",
              f"<td class=num>{_n(p['target'])}</td>", f"<td class=num>{_n(p['weight_pct'], 1)}%</td>",
              f"<td class=t>{_e(p['opened'])}</td>"] for p in s["positions"]))

    @staticmethod
    def pending_table(s: dict) -> str:
        if not s["pending"]:
            return ""
        return ("<h3>ממתינות לביצוע בפתיחה</h3>"
                + ui.table(("מניה", "סוג", "כמות", "בפתיחת"),
                           ([f"<b>{_e(o['symbol'])}</b>", _e(o["kind"]), f"<td class=num>{_e(o['qty'])}</td>",
                             f"<td class=t>{_e(o['fill_on'])}</td>"] for o in s["pending"])))

    def positions_html(self) -> str:
        s = self.positions()
        if not s["has_portfolio"]:
            return ui.section("פוזיציות פתוחות", ui.empty("עוד אין תיק: שלח לבוט קובץ portfolio.yaml."))
        return (self.ready_html(s)
                + ui.section(f"פוזיציות פתוחות ({len(s['positions'])})",
                             self.book_strip(s) + self.book_table(s) + self.pending_table(s)
                             + "<p class=note>מחירים מ-Yahoo (בעיכוב), נשמרים לדקה. התיק: תיק הנייר אחרי הריצה היומית האחרונה.</p>"))

    def run_li(self, r: dict) -> str:
        cls, text = self.run_state(r)
        kind = dict(self.KINDS).get(r["kind"], r["kind"])
        resume = (f"<form method=post action='/runs/{_e(r['id'])}/resume'><button>▶️ המשך ריצה</button></form>"
                  if r["status"] == "failed" else "")
        err = f"<p class=note>{_e(str(r['error'])[:240])}</p>" if r.get("error") else ""
        dur = ui.duration(r["started_at"], r["finished_at"])
        return (f"<li class={cls}><span class=d>{ui.when(r['started_at'])} · as of {_e(r['as_of'])}</span>"
                f"<b>{kind}</b> {ui.pill(text, cls)}"
                + (f" <span class=note>· {dur}</span>" if dur else "")
                + (f" <span class=note>· {r['resumes']} המשכים</span>" if r.get("resumes") else "")
                + f" <a class=note href='/runs/{_e(r['id'])}'>פירוט</a>{err}{resume}</li>")

    def schedule_table(self, st: dict | None = None) -> str:
        from qt.schedule import _r
        rt = _r()
        texts = self.status_doc().get("runs", {})
        ny_at = {"daily": rt.daily_at, "premarket": rt.premarket_at, "open": rt.open_at, "weekly": f"שבת {rt.weekly_at}",
                 "monthly": f"שבת בימים {rt.monthly_days[0]}–{rt.monthly_days[1]}, {rt.weekly_at}"}
        runs = (st or {}).get("runs") or {}
        if not runs:
            from qt.schedule import next_run
            runs = {k: {"next": (lambda n: n.isoformat() if n else None)(next_run(k))} for k, _ in self.KINDS}
        return ui.table(("ריצה", "ניו יורק", "הבאה (ישראל)", "מה קורה"),
                        ([f"<b>{he}</b>", f"<td class=t>{_e(ny_at[k])}</td>",
                          f"<td class=t>{ui.when(runs[k]['next'])}<br><span class=note>{ui.ago(runs[k]['next'])}</span></td>",
                          _e(texts.get(k, ""))] for k, he in self.KINDS))

    def runs_html(self) -> str:
        self._schemas()
        runs = self.db.all("SELECT * FROM runs ORDER BY started_at DESC LIMIT 60")
        if not runs:
            return ui.empty("עוד לא היו ריצות")
        rows = []
        for r in runs:
            cls, text = self.run_state(r)
            rows.append([f"<a href='/runs/{_e(r['id'])}'><b>{dict(self.KINDS).get(r['kind'], _e(r['kind']))}</b></a>",
                         f"<td class=t>{_e(r['as_of'])}</td>", ui.pill(text, cls),
                         f"<td class=t>{ui.when(r['started_at'])}</td>", f"<td class=t>{ui.duration(r['started_at'], r['finished_at'])}</td>",
                         _e(r["resumes"]), f"<span class=note>{_e(str(r['error'] or '')[:200])}</span>",
                         f"<form method=post action='/runs/{_e(r['id'])}/resume'><button>▶️ המשך</button></form>"
                         if r["status"] == "failed" else ""])
        return ui.section(f"{len(runs)} הריצות האחרונות", ui.table(
            ("ריצה", "as of", "מצב", "התחילה", "משך", "המשכים", "שגיאה", ""), rows))

    def run_html(self, run_id: str) -> str:
        self._schemas()
        r = self.db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if r is None:
            return ui.empty("אין ריצה כזאת")
        cls, text = self.run_state(r)
        body = "<div class=strip>" + "".join((
            ui.cell(dict(self.KINDS).get(r["kind"], _e(r["kind"])), f"as of {_e(r['as_of'])}"),
            ui.cell(text, f"{r['resumes']} המשכים", cls),
            ui.cell(ui.when(r["started_at"]), "התחילה"),
            ui.cell(ui.duration(r["started_at"], r["finished_at"]) or "…", "משך"))) + "</div>"
        if r.get("error"):
            body += f"<div class=verdict><p class=down>{_e(r['error'])}</p>" + (
                f"<form method=post action='/runs/{_e(r['id'])}/resume'><button class=go>▶️ המשך ריצה</button></form>"
                if r["status"] == "failed" else "") + "</div>"
        steps = self.db.all("SELECT step, subject, seq, status, started_at, finished_at, attempts, error FROM run_steps "
                            "WHERE run_id = ? ORDER BY seq, subject", (run_id,))
        body += ui.section(f"שלבים ({len(steps)})", ui.table(("#", "שלב", "על", "מצב", "משך", "ניסיונות", "שגיאה"), (
            [f"<td class=num>{x['seq']}</td>", f"<b>{_e(x['step'])}</b>", _e(x["subject"]),
             ui.pill(*{"done": ("הושלם", "ok"), "failed": ("נכשל", "bad")}.get(x["status"], (x["status"], "warn"))),
             f"<td class=t>{ui.duration(x['started_at'], x['finished_at'])}</td>", _e(x["attempts"]),
             f"<span class=note>{_e(str(x['error'] or '')[:300])}</span>"] for x in steps)) if steps else ui.empty("אין שלבים"))
        ev = self.db.all("SELECT * FROM run_events WHERE run_id = ? ORDER BY id", (run_id,))
        body += ui.section(f"אירועים ({len(ev)})", ("<ol class=timeline>" + "".join(
            f"<li class={ {'error': 'bad', 'warn': 'warn'}.get(x['level'], '') }><span class=d>{ui.when(x['at'])} · {_e(x['step'])}</span>"
            f"{_e(x['message'])}</li>" for x in ev) + "</ol>") if ev else ui.empty("אין אירועים"))
        calls = self.db.all("SELECT agent, model, step, subject, cost_usd, duration_ms, valid, critic, created_at "
                            "FROM agent_calls WHERE run_id = ? ORDER BY id", (run_id,))
        if calls:
            body += ui.section(f"קריאות ל-Agents ({len(calls)})", ui.table(
                ("Agent", "מודל", "שלב", "על", "עלות", "זמן", "תקין", "בודק"),
                ([f"<b>{_e(c['agent'])}</b>", f"<span class=mono>{_e(c['model'])}</span>", _e(c["step"]), _e(c["subject"]),
                  f"<td class=num>{(c['cost_usd'] or 0):.3f}$</td>", f"<td class=num>{(c['duration_ms'] or 0) / 1000:.0f}s</td>",
                  ui.pill("כן", "ok") if c["valid"] else ui.pill("לא", "bad"), _e(str(c["critic"] or "")[:160])] for c in calls)))
        return body

    # ---- the system status page (status.yaml plus live checks)
    @staticmethod
    def status_doc() -> dict:
        import yaml
        return yaml.safe_load((Path(__file__).parent / "status.yaml").read_text(encoding="utf-8")) or {}

    def system_checks(self) -> dict[str, tuple[str, str]]:
        """The waiting-for-Ilay lines the system can check itself: (state, what it found)."""
        from qt.app import waiting_monthly
        q = self._schemas()
        out: dict[str, tuple[str, str]] = {}
        pf = self.db.one("SELECT name, created_at FROM uploads WHERE kind = 'portfolio' AND name NOT LIKE ? "
                         "ORDER BY id DESC LIMIT 1", ("book-%",))
        start = os.environ.get("QT_START_EQUITY")
        out["portfolio"] = (("ok", f"נשלח {ui.when(pf['created_at'])} ({_e(pf['name'])})") if pf else
                            ("warn", "לא נשלח" + (f": הספר מתחיל ב-{float(start):,.0f}$ במזומן" if start else "")))
        out["fred"] = ("ok", "מוגדר") if os.environ.get("FRED_API_KEY") else ("bad", "חסר")
        mac = q.latest_upload("manual_macro")
        this_month = datetime.now(ui.IL).strftime("%Y-%m")
        if waiting_monthly(self.db):
            out["macro"] = ("bad", "הריצה החודשית מחכה לו")
        elif mac and ui.parse(mac["created_at"]) and ui.parse(mac["created_at"]).astimezone(ui.IL).strftime("%Y-%m") == this_month:
            out["macro"] = ("ok", f"התקבל {ui.when(mac['created_at'])}")
        else:
            out["macro"] = ("warn", "עוד לא התקבל החודש")
        return out

    def system_html(self, compact: bool = False) -> str:
        """The status page (status.yaml with live checks). Compact (inside the overview): without its own strip."""
        doc = self.status_doc()
        checks = self.system_checks()
        body = (f"<div class=eyebrow>עודכן ביד ב-{_e(doc.get('updated'))} · החלקים החיים מתעדכנים לבד</div>"
                f"<div class=verdict><p>{_e(doc.get('lead'))}</p></div>")
        gw_state, gw_text = self.gateway()
        st = {k: self.run_state(self.db.one("SELECT * FROM runs WHERE kind = ? ORDER BY started_at DESC LIMIT 1", (k,)))
              for k, _ in self.KINDS}
        body += "" if compact else "<div class=strip>" + "".join(
            [ui.cell("qt-web", "הבוט, ה-webhook ולוח הבקרה", "ok"),
             *(ui.cell(f"ריצה {he}", st[k][1], st[k][0]) for k, he in self.KINDS),
             ui.cell("qt-db", "Postgres עם גיבוי יומי" if self.db.pg else "SQLite מקומי", "ok"),
             ui.cell("qt-ibgateway", gw_text, gw_state or ""),
             ui.cell("Agents", "על המנוי שלך דרך Claude Code", "ok")]) + "</div>"
        body += ui.section("מה קרה עד עכשיו", "<ol class=timeline>" + "".join(
            f"<li><span class=d>{_e(x['d'])}</span><b>{_e(x['t'])}.</b> {_e(x['x'])}</li>" for x in doc.get("timeline", []))
            + "</ol>")
        flow = "<i>←</i>".join(f"<span>{_e(x)}</span>" for x in doc.get("flow", []))
        body += ui.section("איך זה עובד", self.schedule_table()
                           + f"<h3>המסלול של פריט אחד</h3><div class=flow>{flow}</div><p class=note>{_e(doc.get('flow_note'))}</p>")
        names = {"ok": "נבנה", "warn": "חלקי", "bad": "חסר"}
        body += ui.section("מה נבנה", ui.table(("מה תכננו", "מצב", "הערה"), (
            [_e(x["what"]), ui.pill(names[x["state"]], x["state"]), _e(x.get("note"))] for x in doc.get("built", [])))
            + f"<p class=note>{_e(doc.get('out_of_scope'))}</p>")
        wait = []
        for x in doc.get("waiting", []):
            state, found = checks.get(x.get("check"), (x.get("state", "warn"), ""))
            wait.append(f"<li><span class='dot {state}'></span><b>{_e(x['t'])}</b>"
                        + (f" {ui.pill(found, state)}" if found else "") + f" {_e(x['x'])}</li>")
        body += ui.section("מחכה להחלטה או לפעולה שלך", "<ul>" + "".join(wait) + "</ul>")
        body += ui.section("עוד לא נבדק על נתונים אמיתיים",
                           "<ul>" + "".join(f"<li>{_e(x)}</li>" for x in doc.get("untested", [])) + "</ul>")
        body += ui.section("סיכונים שכדאי לדעת", "<ul>" + "".join(
            f"<li><b>{_e(x['t'])}.</b> {_e(x['x'])}</li>" for x in doc.get("risks", [])) + "</ul>")
        body += ui.section("הצעדים הבאים", "<ol class=steps>" + "".join(
            f"<li><div><b>{_e(x['t'])}</b><span class=who>{_e(x['who'])}</span><span class=note>{_e(x['x'])}</span></div></li>"
            for x in doc.get("next", [])) + "</ol>")
        return body

    # ---- the overview: the research report's structure, with the live parts around it
    def report_doc(self) -> dict | None:
        """The newest research dossier (the weekly run saves it with its report), cached by upload id."""
        row = self.db.one("SELECT id FROM uploads WHERE kind = 'dossier' ORDER BY id DESC LIMIT 1")
        if row is None:
            return None
        if self._dossier is None or self._dossier[0] != row["id"]:
            up = self.db.one("SELECT content FROM uploads WHERE id = ?", (row["id"],))
            self._dossier = (row["id"], json.loads(up["content"]))
        return self._dossier[1]

    def report_parts(self) -> tuple[list[tuple[str, str]], str, str | None]:
        """(rail, sections, as of) of the newest research report: from its dossier, else cut out of the saved HTML."""
        from qt.research import report
        d = self.report_doc()
        if d is not None:
            nav, body = report.sections(d)
            return nav, body, d["meta"]["as_of"]
        row = self.db.one("SELECT name, content FROM uploads WHERE kind = 'report' ORDER BY id DESC LIMIT 1")
        if row is not None and '<nav class="rail">' in row["content"]:
            head, _, rest = row["content"].partition('<nav class="rail">')
            links, _, body = rest.partition("</nav>")
            nav = re.findall(r'<a href="#([\w-]+)">([^<]*)</a>', links)
            m = re.search(r"report-(\d{4}-\d\d-\d\d)", row["name"] or "")
            return [(k, html.unescape(t)) for k, t in nav], body.rstrip()[:-len("</div>")], m.group(1) if m else None
        return [], "", None

    def overview_report(self) -> tuple[list[tuple[str, str]], str, str | None]:
        """report_parts with the site's links in, kept until a newer report or dossier comes in."""
        Queue(self.db)                                                  # its tables
        key = self.db.one("SELECT MAX(id) AS id FROM uploads WHERE kind IN ('dossier', 'report')")
        key = key and key["id"]
        if key is not None and getattr(self, "_rep_cache", (None,))[0] == key:
            return self._rep_cache[1]
        nav, report, as_of = self.report_parts()
        if report:
            report = self.rep_links(report)
            for sec, href in (("macro", "/macro"), ("sectors", "/sectors"), ("screen", "/sectors")):
                report = re.sub(rf'(<section id="{sec}"><h2>[^<]*)', rf"\1 <a class=note href='{href}'>פתח בדף מלא ←</a>", report, count=1)
        self._rep_cache = (key, (nav, report, as_of))
        return nav, report, as_of

    def watch_html(self) -> str:
        """The watchlist: each idea as the research left it (trigger, stop width, target, why) with the latest daily
        check (close, distance to the trigger, reward:risk, the 078 trend columns)."""
        from qt.app import watchlist
        from qt.daily import STATUS_HE
        wl = watchlist(self.db) or {}
        entries = wl.get("entries") or []
        if not entries:
            return ui.empty("רשימת המעקב ריקה: היא מתמלאת מהמחקר השבועי (או מקובץ watchlist.yaml שנשלח לבוט)")
        row = self.db.one("SELECT s.output, r.as_of FROM run_steps s JOIN runs r ON r.id = s.run_id WHERE s.step = 'watch' "
                          "AND s.status = 'done' AND r.kind = 'daily' ORDER BY r.started_at DESC LIMIT 1", (), ["output"])
        checks = {c["symbol"]: c for c in ((row or {}).get("output") or {}).get("checks") or []}
        bucket = {"enter": ("כניסה", "ok"), "conditional": ("מותנה בטריגר", "warn"), "watch": ("מעקב", "")}
        rows = []
        for e in entries:
            c = checks.get(e["symbol"]) or {}
            b, bc = bucket.get(e.get("bucket"), (e.get("bucket") or "", ""))
            status = ("בתיק" if c.get("held") else STATUS_HE.get(c.get("status"), "")) if c else "עוד לא נבדקה"
            trend = " / ".join(f"{_signed(c.get(k), 1, '%')}" if c.get(k) is not None else "–"
                               for k in ("trend_1y_pct", "trend_3m_pct", "since_add_pct"))
            rows.append([f"<b>{ui.sym(e['symbol'])}</b> <span class=note>{'לונג' if e.get('direction') == 'long' else 'שורט'} · "
                         f"{_e(e.get('etf'))}</span>", ui.pill(b, bc), _e(status),
                         f"<td class=num>{_n(c.get('close'))}</td>",
                         f"<td class=num>{_n(e.get('trigger_level'))}"
                         + (f" <span class=note>({_n(c['dist_to_trigger_pct'], 1)}%)</span>"
                            if c.get("dist_to_trigger_pct") is not None and not c.get("triggered") else "") + "</td>",
                         f"<td class=num>{_n(c.get('stop'))}</td>", f"<td class=num>{_n(e.get('target'))}</td>",
                         f"<td class=num>{_n(c.get('rr'), 1)}</td>", f"<td class=num>{trend}</td>",
                         f"<td class=t>{_e(c.get('days_on_list', ''))}</td>", f"<td class=t>{_e(e.get('next_earnings'))}</td>",
                         f"<td class=note style='min-width:260px'>{_e(e.get('tldr'))}</td>"])
        when = f"הבדיקה היומית מ-{_e(row['as_of'])}" if row else "עוד לא הייתה בדיקה יומית"
        return (ui.table(("מניה", "סטטוס", "מצב היום", "סגירה", "טריגר", "עצירה", "יעד", "יחס", "שנה / 3ח' / מאז", "ימים",
                          "דוח", "למה"), rows)
                + f"<p class=note>{len(entries)} רעיונות · {_e(wl.get('source') or '')} · {when}</p>")

    def summaries_html(self) -> str:
        """What each run last told Ilay in Telegram (its notify step), word for word."""
        out = []
        for k, he in self.KINDS:
            row = self.db.one("SELECT s.output, r.as_of, r.id FROM run_steps s JOIN runs r ON r.id = s.run_id "
                              "WHERE s.step = 'notify' AND s.status = 'done' AND r.kind = ? ORDER BY r.started_at DESC LIMIT 1",
                              (k,), ["output"])
            text = ((row or {}).get("output") or {}).get("text") if row else None
            if text:
                out.append(f"<div class=card><h3>{he} · {_e(row['as_of'])} <a class=note href='/runs/{_e(row['id'])}'>"
                           f"לריצה</a></h3><p style='white-space:pre-line;max-width:none'>{_e(text)}</p></div>")
        return "<div class=cards>" + "".join(out) + "</div>" if out else ui.empty("עוד אין סיכומים: הם מגיעים אחרי הריצות")

    def overview_parts(self) -> dict[str, str]:
        """The live parts of the overview, by name (each one is refreshed in place)."""
        st = self.status()
        s, items, costs = st["book"], st["queue"], st["costs"]
        equity = s.get("equity") if s else None
        # the bottom line: what needs Ilay, how the last daily run went, what runs next
        daily = st["runs"]["daily"]
        cls, text = daily["state"]
        lines = [f"<strong>{len(items)} פריטים מחכים לך</strong>" if items else "אין כרגע פריטים שמחכים לך."]
        if daily["last"]:
            d = daily["last"]
            lines.append(f"הריצה היומית האחרונה ({_e(d['as_of'])}) {ui.pill(text, cls)} "
                         f"{ui.ago(d['finished_at'] or d['started_at'])}.")
        upcoming = sorted(((v["next"], k) for k, v in st["runs"].items() if v["next"]))
        if upcoming:
            n, k = upcoming[0]
            lines.append(f"הריצה הבאה: {dict(self.KINDS)[k]}, {ui.when(n)} ({ui.ago(n)}).")
        top = "<div class=verdict>" + "".join(f"<p>{x}</p>" for x in lines) + "</div>"
        gw_state, gw_text = st["gateway"]
        cells = [ui.cell(f"ריצה {he}", f"{st['runs'][k]['state'][1]} {ui.ago((st['runs'][k]['last'] or {}).get('finished_at') or (st['runs'][k]['last'] or {}).get('started_at'))}"
                         f" · הבאה {ui.when(st['runs'][k]['next'])}", st["runs"][k]["state"][0])
                 for k, he in self.KINDS]
        cells.append(ui.cell("IB Gateway", gw_text, gw_state or ""))
        if s and s["has_portfolio"]:
            cells.append(ui.cell(f"{_n(equity, 0)}$", f"שווי התיק · פתוח {_signed(s['unrealized'], 0)}", "ok"))
        else:
            cells.append(ui.cell("תיק", "לא הצלחתי לטעון את התיק" if s is None else "עוד אין תיק", "bad" if s is None else "warn"))
        cells.append(ui.cell(f"Agents {costs['month']:.2f}$", f"30 יום · היום {costs['today']:.2f}$"))
        top += "<div class=strip aria-label='מצב השירותים'>" + "".join(cells) + "</div>"
        top += ui.section(f"מחכה לך ({len(items)})", "<div class=cards>" + "".join(
            self.item_card(i, equity) for i in items) + "</div>" if items else ui.empty("אין כרגע פריטים בתור."), id="queue")
        # the book
        if s is None:
            book = ui.section("פוזיציות פתוחות", "<p class=failed>לא הצלחתי לטעון את התיק</p>", id="book")
        elif not s["has_portfolio"]:
            book = ui.section("פוזיציות פתוחות", ui.empty("עוד אין תיק: שלח לבוט קובץ portfolio.yaml."), id="book")
        else:
            book = ui.section(f"פוזיציות פתוחות ({len(s['positions'])})", self.book_strip(s) + self.book_table(s)
                              + self.pending_table(s) + "<p class=note><a href='/positions'>למסך הפוזיציות</a></p>", id="book")
        watch = ui.section("רשימת מעקב", self.watch_html(), id="watch")
        # runs and everything they put out
        runs = self.db.all("SELECT * FROM runs ORDER BY started_at DESC LIMIT 8")
        out = ui.section("ריצות", ("<ol class=timeline>" + "".join(self.run_li(r) for r in runs) + "</ol>"
                                   if runs else ui.empty("עוד לא היו ריצות"))
                         + "<p class=note><a href='/runs'>כל הריצות</a></p>", id="runs")
        out += ui.section("מה הריצות אמרו", self.summaries_html())
        orders = self.db.all("SELECT * FROM paper_orders ORDER BY id DESC LIMIT 10")
        otbl = ui.table(("מניה", "סוג", "כמות", "בפתיחת", "מצב", "מחיר"), (
            [f"<b>{_e(o['symbol'])}</b>", _e(o["kind"]), f"<td class=num>{_e(o['qty'])}</td>", f"<td class=t>{_e(o['fill_on'])}</td>",
             ui.pill({"filled": "בוצעה", "open": "ממתינה", "cancelled": "בוטלה"}.get(o["status"], o["status"]),
                     {"filled": "ok", "open": "warn"}.get(o["status"], "mute")),
             f"<td class=num>{_n(o['fill_price'])}</td>"] for o in orders)) if orders else ui.empty("עוד אין עסקאות נייר")
        ev = self.db.all("SELECT * FROM run_events WHERE level IN ('warn', 'error') ORDER BY id DESC LIMIT 8")
        etbl = ("<ol class=timeline>" + "".join(
            f"<li class={'bad' if x['level'] == 'error' else 'warn'}><span class=d>{ui.when(x['at'])} · {_e(x['step'])}</span>"
            f"{_e(x['message'])} <a class=note href='/runs/{_e(x['run_id'])}'>לריצה</a></li>" for x in ev) + "</ol>"
                if ev else ui.empty("אין התראות"))
        out += f"<div class=two>{ui.section('עסקאות נייר', otbl)}{ui.section('התראות מהריצות', etbl)}</div>"
        dec = self.db.all("SELECT * FROM decisions ORDER BY id DESC LIMIT 10", (), ["deviations"])
        out += ui.section("החלטות אחרונות", ui.table(("מתי", "מי", "על מה", "פעולה", "החלטה", "ביטחון", "כרטיס"), (
            [f"<td class=t>{ui.when(d['created_at'])}</td>", _e(d["decided_by"]), f"<b>{_e(d['subject'])}</b>",
             _e(d["action"]), _e(str(d["choice"])[:160]), _e(d["confidence"]), _e(d["card_id"])] for d in dec))
            + "<p class=note><a href='/journal'>ליומן המסחר המלא</a></p>" if dec else ui.empty("עוד אין החלטות ביומן"))
        reps = self.db.all("SELECT id, name, created_at FROM uploads WHERE kind = 'report' ORDER BY id DESC LIMIT 5")
        files = []
        for kind, he in (("portfolio", "תיק / ספר נייר"), ("watchlist", "רשימת מעקב"), ("manual_macro", "קובץ מקרו ידני"),
                         ("dossier", "נתוני המחקר")):
            u = self.db.one("SELECT name, created_at FROM uploads WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,))
            files.append(f"<li><b>{he}</b>: " + (f"{_e(u['name'])} <span class=note>{ui.when(u['created_at'])}</span>"
                                                 if u else "<span class=note>עוד לא התקבל</span>") + "</li>")
        rep = ("<ul>" + "".join(f"<li><a href='/report/{r['id']}'>{_e(r['name'])}</a> <span class=note>"
                                f"{ui.when(r['created_at'])}</span></li>" for r in reps) + "</ul>"
               if reps else ui.empty("הדוח השבועי הראשון יגיע אחרי הריצה השבועית"))
        peak = max((a["cost"] for a in costs["by_agent"]), default=0) or 1
        bars = ("<div class=bars>" + "".join(
            f"<div><span>{_e(a['agent'])}</span><i style='width:{max(a['cost'] / peak * 100, 1):.0f}%'></i>"
            f"<span class=num>{a['cost']:.2f}$</span></div>" for a in costs["by_agent"]) + "</div>"
                if costs["by_agent"] else ui.empty("אין קריאות ל-Agents ב-30 הימים האחרונים"))
        cost = (f"<p><b>{costs['month']:.2f}$</b> ב-30 יום, {costs['calls']} קריאות. היום {costs['today']:.2f}$.</p>"
                + bars + "<p class=note>הערכה לפי מחיר API. בפועל רץ על המנוי.</p>")
        files_html = rep + "<h3>הקבצים האחרונים</h3><ul>" + "".join(files) + "</ul>"
        out += f"<div class=two>{ui.section('דוחות וקבצים', files_html)}{ui.section('עלות Agents', cost)}</div>"
        out += ui.section("לוח זמנים", self.schedule_table(st)
                          + "<p class=note>לפי שעון ניו יורק, בימי מסחר בלבד. כשהשעון בישראל ובארה\"ב זז בתאריכים שונים, השעה בישראל זזה בהתאם.</p>")
        system = ui.section("מצב המערכת", self.system_html(compact=True), id="system")
        return {"top": top, "book": book, "watch": watch, "runs": out, "system": system}

    def overview_html(self) -> str:
        """The live parts only (what the page's script re-fetches)."""
        return "".join(f"<div data-live={k} class=wrap style='padding:0;width:100%'>{v}</div>"
                       for k, v in self.overview_parts().items())

    # ---- drill-down pages: one stock, the macro report, the sectors (Ilay, 22.10: "click a stock, see everything")
    @staticmethod
    def rep_links(body: str) -> str:
        """The report's links to an idea on the same page open that stock's page instead; every ticker in the
        screen table becomes a link too."""
        body = re.sub(r'href="#idea-([\w.\-]+)"', r'href="/stock/\1"', body)
        body = re.sub(r'(<article class="idea[^"]*" id="idea-([\w.\-]+)">.*?<h3>)([\w.\-]+)( <span>)',
                      r'\1<a href="/stock/\2">\3</a>\4', body, flags=re.S)          # the idea's header too
        def one(m: re.Match) -> str:
            s = m.group(1)
            href = "/sectors" if re.fullmatch(r"XL[A-Z]{1,2}", s) else f"/stock/{s}"   # a sector ETF row: the sectors page
            return f'<tr><td><b><a href="{href}">{s}</a></b></td>'
        return re.sub(r'<tr><td><b>([A-Z][A-Z0-9.\-]{0,9})</b></td>', one, body)

    def _yahoo(self, key: str, url: str, ttl: float, parse: Callable[[Any], Any]) -> Any:
        """One quick Yahoo read for a page (6s, no retries, kept in memory for `ttl`): a slow Yahoo must not hold the
        page. None when it fails."""
        import time
        import requests
        from qt.data.providers import BROWSER_UA
        hit = self._web_cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        try:
            r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=6)
            r.raise_for_status()
            val = parse(r.json())
        except Exception:                                               # noqa: BLE001 - the page says so
            return None
        self._web_cache[key] = (time.monotonic(), val)
        return val

    def bars(self, symbol: str) -> list:
        """About 400 days of daily bars for one symbol (Yahoo), [] when there are none."""
        if self._bars is not None:
            return self._bars(symbol)
        from qt.calc.volatility import parse_yahoo_chart
        from qt.data.providers import Yahoo
        return self._yahoo(f"bars:{symbol}", f"{Yahoo.BASE}/v8/finance/chart/{symbol}?range=13mo&interval=1d", 600,
                           parse_yahoo_chart) or []

    def news(self, symbol: str) -> list[dict]:
        if self._news is not None:
            return self._news(symbol)
        from qt.data.providers import Yahoo

        def parse(body: Any) -> list[dict]:
            return [dict(title=x.get("title") or "", publisher=x.get("publisher") or "", link=x.get("link") or "",
                         published=datetime.fromtimestamp(x.get("providerPublishTime", 0), timezone.utc).date().isoformat())
                    for x in body.get("news", [])]
        return self._yahoo(f"news:{symbol}", f"{Yahoo.BASE}/v1/finance/search?q={symbol}&newsCount=8&quotesCount=0",
                           1800, parse) or []

    @staticmethod
    def chart_from_bars(bars: list) -> dict:
        """The research report's chart input (dates, closes, moving averages 21/63/252) from daily bars."""
        close = [float(b.close) for b in bars]

        def ma(n: int) -> list[float | None]:
            return [None if i + 1 < n else sum(close[i + 1 - n:i + 1]) / n for i in range(len(close))]
        return {"dates": [b.day.isoformat() for b in bars], "close": close, "ma": {"21": ma(21), "63": ma(63), "252": ma(252)}}

    def stock_html(self, symbol: str) -> str:
        from qt.app import watchlist
        from qt.daily import STATUS_HE
        from qt.research import report
        sym = symbol.upper()
        d = self.report_doc() or {}
        idea = (d.get("ideas") or {}).get(sym)
        timing = next((t for t in d.get("timing") or [] if t.get("sym") == sym), None)
        entry = next((x for x in (watchlist(self.db) or {}).get("entries") or [] if x["symbol"] == sym), None)
        row = self.db.one("SELECT s.output FROM run_steps s JOIN runs r ON r.id = s.run_id WHERE s.step = 'watch' "
                          "AND s.status = 'done' AND r.kind = 'daily' ORDER BY r.started_at DESC LIMIT 1", (), ["output"])
        check = next((c for c in ((row or {}).get("output") or {}).get("checks") or [] if c["symbol"] == sym), None)
        try:
            book = self.positions()
        except Exception:                                               # noqa: BLE001 - the rest of the page stands
            book = {"positions": [], "equity": None}
        pos = next((p for p in book["positions"] if p["symbol"] == sym), None)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(2) as ex:                               # Yahoo only, no database on these threads
            fb, fn = ex.submit(self.bars, sym), ex.submit(self.news, sym)
            bars, news = fb.result(), fn.result()
        last = float(bars[-1].close) if bars else None
        prev = float(bars[-2].close) if len(bars) > 1 else None
        # the strip: price now, since the research, the plan, the light, the position
        plan = (idea or {}).get("plan") or {}
        ref = ((idea or {}).get("row") or {}).get("price") or (entry or {}).get("price_at_add")
        cells = [ui.cell(f"{_n(last)}" if last else "אין מחיר", "סגירה אחרונה" + (f" · {_signed((last / prev - 1) * 100, 1, '%')} היום"
                                                                              if last and prev else "")),
                 ui.cell(_signed((last / ref - 1) * 100, 1, "%") if last and ref else "–", f"מאז המחקר ({_n(ref)})")]
        if plan or entry:
            e = entry or {}
            cells.append(ui.cell(f"{_n(plan.get('entry') or e.get('trigger_level'))} / {_n(plan.get('stop'))} / "
                                 f"{_n(plan.get('target') or e.get('target'))}", "כניסה / עצירה / יעד"))
        if idea:
            cells.append(ui.cell(report.BUCKET_HE.get(idea["bucket"], idea["bucket"]),
                                 f"רמזור {report.COLOR_HE.get(idea['timing'].get('combined'), '')} · R:R {_n(plan.get('rr'), 1)}"))
        if check:
            cells.append(ui.cell("בתיק" if check.get("held") else STATUS_HE.get(check["status"], check["status"]),
                                 f"הבדיקה היומית · יחס {_n(check.get('rr'), 1)}"))
        if pos:
            cells.append(ui.cell(_signed(pos["pl"], 0), f"בתיק: {_n(pos['qty'], 0)} יח' · {_signed(pos['pl_pct'], 2, '%')}", "ok"))
        out = "<div class=strip>" + "".join(cells) + "</div>"
        # the live chart with the plan's lines
        if bars:
            c = self.chart_from_bars(bars)
            lines = {"entry": plan.get("entry") or (entry or {}).get("trigger_level") or (pos or {}).get("entry") or last,
                     "stop": plan.get("stop") or (pos or {}).get("stop") or (check or {}).get("stop") or last,
                     "target": plan.get("target") or (entry or {}).get("target") or (pos or {}).get("target")}
            fig = report.price_chart(c, lines, (idea or {}).get("levels") or {}, (idea or {}).get("direction", "long"))
            out += ui.section("המחיר עכשיו", f"<div class=rep><figure>{fig}</figure></div>"
                              "<p class=note>שנה אחרונה מ-Yahoo, עם ממוצעים 21/63/252 וקווי התוכנית. מתעדכן בכל טעינה.</p>")
        else:
            out += ui.section("המחיר עכשיו", ui.empty("לא הצלחתי להביא מחירים מ-Yahoo כרגע"))
        # what the research found: the whole dossier of the idea, as in the report
        if idea:
            checks = [r for r in (d.get("verification") or {}).get("checks", []) if r["subject"] == sym]
            out += ui.section(f"מה המחקר מצא ({_e(d['meta']['as_of'])})",
                              "<div class=rep>" + self.rep_links(report._idea(idea, checks)) + "</div>")
            if sym in ((d.get("portfolio") or {}).get("syms") or []):      # S11: how it sits with the other trades
                out += f"<div class=rep>{self.rep_links(report._portfolio(d))}</div>"
        elif timing:
            out += ui.section(f"בסינון וברמזור ({_e(d['meta']['as_of'])})", ui.data(timing)
                              + "<p class=note>המניה עברה את הסינון אבל לא נבחרה לניתוח עמוק.</p>")
        else:
            out += ui.section("מה המחקר מצא", ui.empty("המניה לא הופיעה בדוח המחקר האחרון"))
        if entry or check:
            out += ui.section("ברשימת המעקב", (ui.data({k: v for k, v in (entry or {}).items() if k != "breakers"})
                                               + ("<h3>מה ישבור את התזה</h3><ul>" + "".join(f"<li>{_e(b)}</li>" for b in entry["breakers"])
                                                  + "</ul>" if (entry or {}).get("breakers") else "")
                                               + ("<h3>הבדיקה היומית האחרונה</h3>" + ui.data(check) if check else "")))
        if pos:
            out += ui.section("הפוזיציה", self.book_table({"positions": [pos]}))
        items = self.db.all("SELECT * FROM approval_queue WHERE symbol = ? ORDER BY id DESC LIMIT 10", (sym,), ["payload"])
        if items:
            out += ui.section("בתור ובהחלטות", ui.table(("#", "יום", "מה", "מצב", "פירוט"), (
                [f"<a href='/item/{i['id']}'>#{i['id']}</a>", f"<td class=t>{_e(i['as_of'])}</td>",
                 f"{_e(i['kind'])} · {_e(i['rule_id'])}", _e(i["status"]), _e((i["payload"] or {}).get("detail"))] for i in items)))
        orders = self.db.all("SELECT * FROM paper_orders WHERE symbol = ? ORDER BY id DESC LIMIT 10", (sym,))
        if orders:
            out += ui.section("עסקאות נייר", ui.table(("סוג", "כמות", "בפתיחת", "מצב", "מחיר"), (
                [_e(o["kind"]), f"<td class=num>{_e(o['qty'])}</td>", f"<td class=t>{_e(o['fill_on'])}</td>", _e(o["status"]),
                 f"<td class=num>{_n(o['fill_price'])}</td>"] for o in orders)))
        dec = self.db.all("SELECT * FROM decisions WHERE subject = ? ORDER BY id DESC LIMIT 10", (sym,))
        if dec:
            out += ui.section("ביומן ההחלטות", ui.table(("מתי", "מי", "פעולה", "החלטה", "ביטחון", "כרטיס"), (
                [f"<td class=t>{ui.when(x['created_at'])}</td>", _e(x["decided_by"]), _e(x["action"]), _e(str(x["choice"])[:200]),
                 _e(x["confidence"]), _e(x["card_id"])] for x in dec)))
        out += ui.section("חדשות", "<ul>" + "".join(
            f"<li><span class=note>{_e(n['published'])} · {_e(n['publisher'])}</span> <a href='{_e(n['link'])}' target=_blank "
            f"rel=noopener>{_e(n['title'])}</a></li>" for n in news[:8]) + "</ul>" if news else ui.empty("אין חדשות מ-Yahoo כרגע"))
        return out

    def _latest_out(self, kind: str, step: str) -> tuple[Any, dict | None]:
        row = self.db.one("SELECT s.output, r.id, r.as_of FROM run_steps s JOIN runs r ON r.id = s.run_id WHERE s.step = ? "
                          "AND s.status = 'done' AND r.kind = ? ORDER BY r.started_at DESC LIMIT 1", (step, kind), ["output"])
        return (row or {}).get("output"), row

    def runs_said(self, kind: str, steps: Iterable[tuple[str, str]]) -> str:
        """What the newest run of `kind` put out at each step: its summary text first, then the data."""
        self._schemas()
        text, row = self._latest_out(kind, "notify")
        out = ""
        if row and isinstance(text, dict) and text.get("text"):
            out += (f"<div class=card><h3>{_e(row['as_of'])} <a class=note href='/runs/{_e(row['id'])}'>לריצה</a></h3>"
                    f"<p style='white-space:pre-line;max-width:none'>{_e(text['text'])}</p></div>")
        for step, he in steps:
            o, r = self._latest_out(kind, step)
            if o:
                out += f"<h3>{he} <span class=note>{_e(r['as_of'])}</span></h3>" + ui.data(o)
        return out or ui.empty("הריצה עוד לא רצה")

    def macro_html(self) -> str:
        from qt.research import report
        d = self.report_doc()
        out = (f"<div class=rep>{self.rep_links(report._macro(d))}</div>" if d else ui.empty("עוד אין דוח מחקר"))
        return out + ui.section("הריצה החודשית האחרונה", self.runs_said("monthly", (
            ("stance", "עמדת מקרו"), ("a1", "A1"), ("ism", "ISM לפי סקטור"), ("sentiment", "סנטימנט"),
            ("calendar", "יומן אירועים"), ("season", "עונתיות"), ("exposure", "חשיפה מול תקרות"))))

    def sectors_html(self) -> str:
        from qt.research import report
        d = self.report_doc()
        out = (f"<div class=rep>{self.rep_links(report._sectors(d) + report._screen(d))}</div>" if d
               else ui.empty("עוד אין דוח מחקר"))
        return out + ui.section("הריצה השבועית האחרונה", self.runs_said("weekly", (
            ("arena", "זירת הסקטורים"), ("revisit", "ביקור ברעיונות ישנים"), ("week", "סיכום השבוע"))))

    def stale(self, as_of: str | None) -> str:
        """The report is weekly: past 8 days the header says how old it is and why the last research did not write one."""
        try:
            days = (datetime.now(timezone.utc).date() - date.fromisoformat(str(as_of)[:10])).days
        except ValueError:
            return ""
        if days <= 8:
            return ""
        out = self._latest_out("weekly", "research")[0] or {}
        why = f": חסר {', '.join(out['skipped'])}" if isinstance(out, dict) and out.get("skipped") else ""
        return f" · <b class=bad>בן {days} ימים, המחקר השבועי לא הפיק דוח חדש{ui.e(why)}</b>"

    def dashboard(self, env: dict, start: Start) -> list[bytes]:
        """The overview, in the research report's structure (Ilay, 22.10): the bottom line and what waits for Ilay,
        then the newest report (macro, sectors, screens, ideas, charts), then the book, the watchlist, the runs and
        what they said, and the system's status. The report changes once a week, so only the parts around it refresh."""
        if "fragment" in parse_qs(env.get("QUERY_STRING", "")):
            return self._send(start, "200 OK", self.overview_html(), headers=[("Cache-Control", "no-store")])
        nav, report, as_of = self.overview_report()
        if "part=report" in env.get("QUERY_STRING", ""):
            return self._send(start, "200 OK", f"<div class=rep>{report}</div>" if report else "")
        parts = self.overview_parts()
        live = lambda k: f"<div data-live={k} class=wrap style='padding:0;width:100%'>{parts[k]}</div>"  # noqa: E731
        if report:
            # the report is most of the page (~500KB): the live parts paint first and the report comes in right after
            rep = ("<div id=rep-slot><p class=note style='padding:1rem'>טוען את דוח המחקר…</p></div><script>"
                   "fetch('/?part=report',{credentials:'same-origin'}).then(function(r){return r.text()}).then(function(t){"
                   "var s=document.getElementById('rep-slot');s.outerHTML=t;var h=location.hash&&document.getElementById("
                   "location.hash.slice(1));if(h)h.scrollIntoView()})</script>")
            sub = f"דוח המחקר האחרון: {ui.e(as_of or '')}{self.stale(as_of)} · הפוזיציות, התור והריצות מתעדכנים לבד"
        else:
            rep = ui.section("דוח המחקר", ui.empty("עוד אין דוח מחקר: הריצה השבועית מפיקה אותו בשבת (צריך מפתח FRED, "
                                                   "קובץ מקרו ו-Claude), או שלח לבוט dossier.json של מחקר"), id="summary")
            sub = "הפוזיציות, התור והריצות מתעדכנים לבד"
        rail = ([("queue", "מחכה לך")] + list(nav) + [("book", "פוזיציות"), ("watch", "רשימת מעקב"), ("runs", "ריצות ופלט"),
                                                     ("system", "מצב המערכת")])
        body = live("top") + rep + live("book") + live("watch") + live("runs") + live("system")
        return self._send(start, "200 OK", ui.page("Quantum Trader · לוח בקרה", body, "/", live=True, sub=sub, rail=rail,
                                                   css=ui.report_css() if report else ""))


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
