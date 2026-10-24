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

    def _back(self, env: dict, body: str | None = None) -> str:
        """Where a button sends the page back to: the screen it was pressed on, from a closed list (never a free
        address, so a form cannot redirect anywhere else)."""
        b = (parse_qs(self._body(env).decode() if body is None else body).get("back") or [""])[0]
        m = re.fullmatch(r"stock:([\w.\-]{1,12})", b)
        if m:
            return f"/stock/{m.group(1).upper()}"
        return {"positions": "/positions", "system": "/system", "ideas": "/ideas", "runs": "/runs"}.get(b, "/")

    @staticmethod
    def _body(env: dict) -> bytes:
        n = int(env.get("CONTENT_LENGTH") or 0)
        return env["wsgi.input"].read(min(n, 8_000_000)) if n else b""

    @staticmethod
    def _send(start: Start, status: str, body: str | bytes, ctype: str = "text/html; charset=utf-8",
              headers: Iterable[tuple[str, str]] = ()) -> list[bytes]:
        start(status, [("Content-Type", ctype), *headers])
        return [body if isinstance(body, bytes) else body.encode()]

    def _page(self, start: Start, title: str, body: str, active: str = "") -> list[bytes]:
        """A page that does not refresh (an item, the journal): these also open from a signed Telegram link, so they
        carry no status dot (nothing about the runs shows without the login)."""
        return self._send(start, "200 OK", ui.page(title, body, active))

    def _live(self, env: dict, start: Start, title: str, body: Callable[[], str] | str,
              active: str = "", css: str = "", sub: str = "", head: bool = True) -> list[bytes]:
        """A page that refreshes itself; with ?fragment=1 only the refreshing part (what the script fetches): the body
        and the status dot up top."""
        self._schemas()                                                 # a fresh database: every screen still opens
        body = body() if callable(body) else body
        h = self.health()
        if "fragment" in parse_qs(env.get("QUERY_STRING", "")):
            return self._send(start, "200 OK", f"<div data-live=main>{body}</div>"
                              + ui.health_link((h["state"], h["text"]), active == "/system"),
                              headers=[("Cache-Control", "no-store")])
        return self._send(start, "200 OK", ui.page(title, body, active, live=True, css=css, sub=sub,
                                                   health=(h["state"], h["text"]), head=head))

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
            if path == "/ideas" and method == "GET":
                f = (parse_qs(env.get("QUERY_STRING", "")).get("f") or [""])[0]
                return self._live(env, start, "רעיונות", lambda: self.ideas_page(f), "/ideas")
            if path == "/positions" and method == "GET":
                return self._live(env, start, "תיק", self.positions_html, "/positions", sub="תיק הנייר, לפי המחירים האחרונים")
            if path == "/research" and method == "GET":
                return self.research(env, start)
            if path == "/runs" and method == "GET":
                return self._live(env, start, "ריצות", self.runs_html, "/system")
            m = re.fullmatch(r"/runs/([\w.-]+)", path)
            if m and method == "GET":
                return self._live(env, start, f"ריצה {m.group(1)}", lambda: self.run_html(m.group(1)), "/system")
            m = re.fullmatch(r"/stock/([\w.\-]{1,12})", path)
            if m and method == "GET":
                sym = m.group(1).upper()
                return self._live(env, start, sym, lambda: self.stock_html(sym), "/ideas", css=ui.report_css(), head=False)
            if path == "/macro" and method == "GET":
                return self._live(env, start, "דוח מקרו", self.macro_html, "/research", css=ui.report_css())
            if path == "/sectors" and method == "GET":
                return self._live(env, start, "סקטורים וסינון", self.sectors_html, "/research", css=ui.report_css())
            if path == "/system" and method == "GET":
                return self._live(env, start, "מצב המערכת", self.system_html, "/system")
            if path == "/api/status" and method == "GET":
                return self._send(start, "200 OK", json.dumps(dict(self.status(), health=self.health()), ensure_ascii=False,
                                                              default=str),
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
                return self._send(start, "303 See Other", "", headers=[("Location", self._back(env))])
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
        row = self.db.one("SELECT content FROM uploads WHERE id = ? AND kind IN ('report', 'dry_report')", (upload_id,))
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
        back = "" if sig else "<p class=note><a href='/positions'>← חזרה לתיק</a></p>"
        return self._page(start, "יומן המסחר", back + body, "" if sig else "/positions")

    def decide(self, env: dict, start: Start, item_id: int, choice: str) -> list[bytes]:
        body = self._body(env).decode()
        form = parse_qs(body)
        note = (form.get("note") or [""])[0]
        back = self._back(env, body)
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

    # ---- the status dot: one answer to "is anything wrong?" for every page (Ilay 24.10: a run like today's must not
    # go by quietly). Red: the newest run of a kind failed, a run that is not done logged an error in the last day,
    # the book did not load, or IB Gateway is set up and does not answer. Amber: a run waits for the macro file, a run
    # that finished logged an error in the last day, or the research report is over 8 days old.
    _SKIP = object()

    def health(self, book: Any = _SKIP) -> dict:
        """{"state": ok|warn|bad|"", "text": the dot's words, "issues": [(state, text)], "failed": [failed run rows]}."""
        self._schemas()
        issues: list[tuple[str, str]] = []
        failed: list[dict] = []
        running: list[str] = []
        latest = {}
        for k, he in self.KINDS:
            r = self.db.one("SELECT * FROM runs WHERE kind = ? ORDER BY started_at DESC LIMIT 1", (k,))
            latest[k] = r
            if r is None or r["status"] == "done":
                continue
            if r["status"] == "running":
                running.append(he)
                continue
            cls, text = self.run_state(r)
            if cls == "bad":
                failed.append(r)
                issues.append(("bad", f"הריצה ה{he} של {r['as_of']} נכשלה"
                                      + (f": {str(r['error'])[:160]}" if r.get("error") else "")))
            else:
                issues.append(("warn", f"הריצה ה{he} של {r['as_of']} {text}"))
        day = datetime.now(timezone.utc) - timedelta(days=1)
        seen: set[str] = {r["id"] for r in failed}
        ev = self.db.all("SELECT e.run_id, e.at, e.step, e.message, r.status, r.kind FROM run_events e "
                         "JOIN runs r ON r.id = e.run_id WHERE e.level = 'error' ORDER BY e.id DESC LIMIT 50")
        for x in ev:
            t = ui.parse(x["at"])
            if t is None or t < day or x["run_id"] in seen:
                continue
            seen.add(x["run_id"])
            he = dict(self.KINDS).get(x["kind"], x["kind"])
            if x["status"] == "done":
                issues.append(("warn", f"בריצה ה{he} הייתה שגיאה ב-{x['step'] or '?'}, והיא הסתיימה: {str(x['message'])[:140]}"))
            elif x["status"] != "running":
                issues.append(("bad", f"שגיאה בריצה ה{he} ב-{x['step'] or '?'}: {str(x['message'])[:140]}"))
        gw_state, _ = self.gateway()
        if gw_state == "bad":
            issues.append(("bad", "IB Gateway לא עונה"))
        if book is None:
            issues.append(("bad", "לא הצלחתי לטעון את התיק"))
        d = self.report_doc()
        as_of = ((d or {}).get("meta") or {}).get("as_of")
        try:
            days = (datetime.now(timezone.utc).date() - date.fromisoformat(str(as_of)[:10])).days if as_of else None
        except ValueError:
            days = None
        if days is not None and days > 8:
            issues.append(("warn", f"דוח המחקר בן {days} ימים: המחקר השבועי לא הפיק דוח חדש"))
        states = {s for s, _ in issues}
        if "bad" in states:
            state, text = "bad", "ריצה נכשלה" if failed else "יש תקלה"
        elif "warn" in states:
            state, text = "warn", "דורש תשומת לב"
        elif running:
            state, text = "", f"ריצה {running[0]} רצה עכשיו"
        else:
            state, text = "ok", "המערכת תקינה"
        return {"state": state, "text": text, "issues": issues, "failed": failed, "running": running}

    def health_banner(self, h: dict, back: str = "") -> str:
        """The red (or amber) card on top of a page when something needs a look, with the button to resume a failed run."""
        if h["state"] not in ("bad", "warn"):
            return ""
        hidden = f"<input type=hidden name=back value={back}>" if back else ""
        acts = "".join(f"<form method=post action='/runs/{_e(r['id'])}/resume'>{hidden}<button class=go>המשך את הריצה ה"
                       f"{dict(self.KINDS).get(r['kind'], _e(r['kind']))}</button></form>" for r in h["failed"])
        return (f"<div class='banner {h['state']}' role=alert><b>{'משהו נכשל' if h['state'] == 'bad' else 'כדאי לבדוק'}</b>"
                "<ul>" + "".join(f"<li>{_e(t)}</li>" for _, t in h["issues"]) + "</ul>"
                + (f"<div class=acts>{acts}</div>" if acts else "")
                + "<a href='/system'>לפרטים במצב המערכת ←</a></div>")

    # ---- pieces
    def item_card(self, i: dict, equity: float | None, back: str = "") -> str:
        """One queue item: what the system proposes, the numbers, and the buttons. An entry gets the one big
        "open position" button: approving makes the paper order, filled at the next open and written to the book."""
        from qt.telegram import KIND
        p = i["payload"] or {}
        _, name = KIND.get(i["kind"], ("", i["kind"]))
        qty, entry, stop = p.get("qty"), p.get("price") or i.get("ref_price"), p.get("stop")
        risk = qty * abs(entry - stop) if qty and entry and stop else None
        facts = (("כיוון", ("לונג" if p.get("direction", "long") == "long" else "שורט") if i["kind"] in ("enter", "add") else ""),
                 ("כמות", _n(qty, 0)), ("כניסה", _n(entry)), ("סטופ", _n(stop)), ("יעד", _n(p.get("target"))),
                 ("סכום", _n(qty * entry, 0) if qty and entry else ""),
                 ("סיכון", f"{_n(risk, 0)}$" + (f" ({risk / equity * 100:.2f}%)" if equity else "") if risk else ""))
        hidden = f"<input type=hidden name=back value={back}>" if back else ""
        if i["kind"] in ("enter", "add"):
            buttons = (("approve", "פתח פוזיציה", " class=go"), ("reject", "דחה", ""))
        else:
            buttons = (("approve", "אשר", " class=go"), ("reject", "דחה", ""), ("postpone", "מחר", ""))
        st = {"changed": ui.pill("השתנה מאז ההצעה", "warn"), "postponed": ui.pill("נדחה למחר", "mute")}.get(i["status"], "")
        return (f"<div class=card><h3>{_e(name)} · {ui.sym(i['symbol'])} {st} "
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
        """The book in one card: its value, and the open, realized, cash, long / short and drawdown under it."""
        dd = s["drawdown_pct"]
        tiles = (("פתוח", _signed(s["unrealized"], 0)), ("ממומש", _signed(s["realized"], 0)),
                 ("מזומן", f"{_n(s['cash'], 0)}$" if s["cash"] is not None else ""),
                 ("לונג / שורט", f"{_n(s['long_value'], 0)} / {_n(s['short_value'], 0)}"),
                 ("ירידה מהשיא", _signed(dd, 1, "%") if dd else "0.0%"))
        return ("<div class=panel><span class=note>שווי התיק</span>"
                f"<div class=money>{_n(s['equity'], 0)}$</div>"
                "<div class=stats>" + "".join(f"<div class=stat><span>{k}</span><b>{v}</b></div>" for k, v in tiles if v)
                + "</div></div>")

    @staticmethod
    def _between(lo: Any, x: Any, hi: Any) -> float | None:
        """How far x is on the way from lo to hi, in percent (a long's stop below and target above, or a short's the
        other way round: the same formula)."""
        if lo is None or x is None or hi is None or hi == lo:
            return None
        return (x - lo) / (hi - lo) * 100

    def position_card(self, p: dict) -> str:
        """One open position: what it is, the P&L, and where the price sits between the stop and the target."""
        last = p["last"] if p["priced"] else None
        where = self._between(p["stop"], last if last is not None else p["entry"], p["target"])
        pl = (f"<b>{_signed(p['pl'], 0)}</b><span>{_signed(p['pl_pct'], 2, '%')}</span>" if p["priced"]
              else "<b class=muted>אין מחיר</b><span>לפי מחיר הכניסה</span>")
        if p["target"] is not None and p["stop"] is not None:
            bar = (ui.prog(where, "ok" if (where or 0) >= 50 else "warn" if (where or 0) >= 0 else "mute")
                   + f"<div class=ends><span>עצירה {_n(p['stop'])}</span><b>{_n(last) if last is not None else _n(p['entry'])}</b>"
                     f"<span>יעד {_n(p['target'])}</span></div>")
        elif p["stop"] is not None:
            bar = (f"<div class=ends><span>עצירה {_n(p['stop'])}</span>"
                   f"<b>{_n(p['stop_dist_pct'], 1)}% עד העצירה</b><span>אין יעד</span></div>"
                   if p["stop_dist_pct"] is not None else "")
        else:
            bar = "<div class=ends><span class=down>אין עצירה בספר</span></div>"
        side = "לונג" if p["direction"] == "long" else "שורט"
        sector = f" · {_e(p['sector'])}" if p.get("sector") else ""
        return (f"<a class=card href='/stock/{_e(p['symbol'])}' style='text-decoration:none;color:inherit'>"
                f"<div class=row style='border:0;padding:0;min-height:0'><div class=l><b style='font-size:1.1rem'>{_e(p['symbol'])}</b>"
                f"<span>{side} · {_n(p['qty'], 0)} יח' · מ-{_n(p['entry'])}{sector}"
                f"</span></div><div class=r>{pl}</div></div>{bar}</a>")

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
        return ui.section(f"ממתינות לביצוע בפתיחה ({len(s['pending'])})",
                          ui.table(("מניה", "סוג", "כמות", "בפתיחת"),
                                   ([f"<b>{ui.sym(o['symbol'])}</b>", _e(o["kind"]), f"<td class=num>{_e(o['qty'])}</td>",
                                     f"<td class=t>{_e(o['fill_on'])}</td>"] for o in s["pending"])))

    def orders_html(self, limit: int = 10) -> str:
        orders = self.db.all("SELECT * FROM paper_orders ORDER BY id DESC LIMIT ?", (limit,))
        if not orders:
            return ui.empty("עוד אין עסקאות נייר")
        return ui.table(("מניה", "סוג", "כמות", "בפתיחת", "מצב", "מחיר"), (
            [f"<b>{ui.sym(o['symbol'])}</b>", _e(o["kind"]), f"<td class=num>{_e(o['qty'])}</td>", f"<td class=t>{_e(o['fill_on'])}</td>",
             ui.pill({"filled": "בוצעה", "open": "ממתינה", "cancelled": "בוטלה"}.get(o["status"], o["status"]),
                     {"filled": "ok", "open": "warn"}.get(o["status"], "mute")),
             f"<td class=num>{_n(o['fill_price'])}</td>"] for o in orders))

    def decisions_html(self, limit: int = 10) -> str:
        dec = self.db.all("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,), ["deviations"])
        if not dec:
            return ui.empty("עוד אין החלטות ביומן")
        return ui.table(("מתי", "מי", "על מה", "פעולה", "החלטה", "ביטחון", "כרטיס"), (
            [f"<td class=t>{ui.when(d['created_at'])}</td>", _e(d["decided_by"]), f"<b>{ui.sym(d['subject'])}</b>",
             _e(d["action"]), _e(str(d["choice"])[:160]), _e(d["confidence"]), _e(d["card_id"])] for d in dec))

    def positions_html(self) -> str:
        """The portfolio tab: the book's value, what is ready to open, a card per position, the orders waiting for the
        open, and the paper trades and decisions behind them (moved here from the old overview)."""
        Queue(self.db)                                                  # its tables
        s = self.positions()
        if not s["has_portfolio"]:
            return (ui.empty("עוד אין תיק: שלח לבוט קובץ portfolio.yaml.")
                    + ui.section("עסקאות נייר", self.orders_html()))
        cards = ("<div class=cards>" + "".join(self.position_card(p) for p in s["positions"]) + "</div>"
                 if s["positions"] else ui.empty("אין פוזיציות פתוחות: התיק כולו במזומן"))
        return (self.book_strip(s) + self.ready_html(s)
                + ui.section(f"פוזיציות פתוחות ({len(s['positions'])})", cards)
                + self.pending_table(s)
                + ui.fold("כל המספרים בטבלה", self.book_table(s), id="book-table")
                + ui.fold("עסקאות נייר", self.orders_html(), id="orders")
                + ui.fold("החלטות אחרונות", self.decisions_html()
                          + "<p class=note><a href='/journal'>ליומן המסחר המלא</a></p>", id="decisions")
                + "<p class=note>מחירים מ-Yahoo (בעיכוב), נשמרים לדקה. התיק: תיק הנייר אחרי הריצה היומית האחרונה. "
                  "<a href='/journal'>יומן המסחר</a></p>")

    def run_li(self, r: dict, back: str = "") -> str:
        cls, text = self.run_state(r)
        kind = dict(self.KINDS).get(r["kind"], r["kind"])
        hidden = f"<input type=hidden name=back value={back}>" if back else ""
        resume = (f"<form method=post action='/runs/{_e(r['id'])}/resume'>{hidden}<button>המשך ריצה</button></form>"
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

    def files_html(self) -> str:
        """The last file of each kind the system received (portfolio, watchlist, macro, research data)."""
        files = []
        for kind, he in (("portfolio", "תיק / ספר נייר"), ("watchlist", "רשימת מעקב"), ("manual_macro", "קובץ מקרו ידני"),
                         ("dossier", "נתוני המחקר")):
            u = self.db.one("SELECT name, created_at FROM uploads WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,))
            files.append(f"<li><b>{he}</b>: " + (f"{_e(u['name'])} <span class=note>{ui.when(u['created_at'])}</span>"
                                                 if u else "<span class=note>עוד לא התקבל</span>") + "</li>")
        return "<ul>" + "".join(files) + "</ul><p class=note><a href='/research#reports'>הדוחות השבועיים במחקר</a></p>"

    def costs_html(self, costs: dict) -> str:
        peak = max((a["cost"] for a in costs["by_agent"]), default=0) or 1
        bars = ("<div class=bars>" + "".join(
            f"<div><span>{_e(a['agent'])}</span><i style='width:{max(a['cost'] / peak * 100, 1):.0f}%'></i>"
            f"<span class=num>{a['cost']:.2f}$</span></div>" for a in costs["by_agent"]) + "</div>"
                if costs["by_agent"] else ui.empty("אין קריאות ל-Agents ב-30 הימים האחרונים"))
        return (f"<p><b>{costs['month']:.2f}$</b> ב-30 יום, {costs['calls']} קריאות. היום {costs['today']:.2f}$.</p>"
                + bars + "<p class=note>הערכה לפי מחיר API. בפועל רץ על המנוי.</p>")

    def system_html(self) -> str:
        """The system's page, behind the status dot: what failed and the button to resume it, every run's state, the
        runs and what they said, the alerts, the schedule, the costs and the files (moved here from the old overview),
        then the status doc (status.yaml) with what still waits for Ilay."""
        st = self.status()
        h = self.health(st["book"])
        costs = st["costs"]
        body = self.health_banner(h, "system") or (
            f"<div class=verdict><p><span class='dot {h['state']}'></span><b>{_e(h['text'])}.</b> "
            f"{len(st['queue'])} פריטים מחכים לך.</p></div>" if h["state"] != "ok" or st["queue"] else
            "<div class=verdict><p><span class='dot ok'></span><b>המערכת תקינה.</b> כל הריצות האחרונות הסתיימו ואין התראות.</p></div>")
        gw_state, gw_text = st["gateway"]
        cells = [ui.cell(f"ריצה {he}", f"{st['runs'][k]['state'][1]} {ui.ago((st['runs'][k]['last'] or {}).get('finished_at') or (st['runs'][k]['last'] or {}).get('started_at'))}"
                         f" · הבאה {ui.when(st['runs'][k]['next'])}", st["runs"][k]["state"][0])
                 for k, he in self.KINDS]
        cells += [ui.cell("IB Gateway", gw_text, gw_state or ""),
                  ui.cell("qt-db", "Postgres עם גיבוי יומי" if self.db.pg else "SQLite מקומי", "ok"),
                  ui.cell(f"Agents {costs['month']:.2f}$", f"30 יום · היום {costs['today']:.2f}$")]
        body += "<div class=strip aria-label='מצב השירותים'>" + "".join(cells) + "</div>"
        runs = self.db.all("SELECT * FROM runs ORDER BY started_at DESC LIMIT 8")
        body += ui.section("ריצות", ("<ol class=timeline>" + "".join(self.run_li(r, "system") for r in runs) + "</ol>"
                                     if runs else ui.empty("עוד לא היו ריצות"))
                           + "<p class=note><a href='/runs'>כל הריצות</a></p>", id="runs")
        ev = self.db.all("SELECT * FROM run_events WHERE level IN ('warn', 'error') ORDER BY id DESC LIMIT 8")
        etbl = ("<ol class=timeline>" + "".join(
            f"<li class={'bad' if x['level'] == 'error' else 'warn'}><span class=d>{ui.when(x['at'])} · {_e(x['step'])}</span>"
            f"{_e(x['message'])} <a class=note href='/runs/{_e(x['run_id'])}'>לריצה</a></li>" for x in ev) + "</ol>"
                if ev else ui.empty("אין התראות"))
        body += ui.section("התראות מהריצות", etbl, id="alerts")
        body += ui.fold("מה הריצות אמרו", self.summaries_html(), id="said")
        body += ui.section("לוח זמנים", self.schedule_table(st)
                           + "<p class=note>לפי שעון ניו יורק, בימי מסחר בלבד. כשהשעון בישראל ובארה\"ב זז בתאריכים שונים, "
                             "השעה בישראל זזה בהתאם.</p>", id="schedule")
        body += f"<div class=two>{ui.section('עלות Agents', self.costs_html(costs))}{ui.section('קבצים שהתקבלו', self.files_html())}</div>"
        return body + self.status_doc_html()

    def status_doc_html(self) -> str:
        """The status doc (status.yaml, updated by hand) with the lines the system checks itself."""
        doc = self.status_doc()
        checks = self.system_checks()
        body = (f"<div class=verdict><span class=eyebrow>עודכן ביד ב-{_e(doc.get('updated'))}</span>"
                f"<p>{_e(doc.get('lead'))}</p></div>")
        wait = []
        for x in doc.get("waiting", []):
            state, found = checks.get(x.get("check"), (x.get("state", "warn"), ""))
            wait.append(f"<li><span class='dot {state}'></span><b>{_e(x['t'])}</b>"
                        + (f" {ui.pill(found, state)}" if found else "") + f" {_e(x['x'])}</li>")
        body += ui.section("מחכה להחלטה או לפעולה שלך", "<ul>" + "".join(wait) + "</ul>")
        flow = "<i>←</i>".join(f"<span>{_e(x)}</span>" for x in doc.get("flow", []))
        body += ui.fold("איך זה עובד", f"<h3>המסלול של פריט אחד</h3><div class=flow>{flow}</div>"
                                       f"<p class=note>{_e(doc.get('flow_note'))}</p>")
        names = {"ok": "נבנה", "warn": "חלקי", "bad": "חסר"}
        body += ui.fold("מה נבנה", ui.table(("מה תכננו", "מצב", "הערה"), (
            [_e(x["what"]), ui.pill(names[x["state"]], x["state"]), _e(x.get("note"))] for x in doc.get("built", [])))
            + f"<p class=note>{_e(doc.get('out_of_scope'))}</p>")
        body += ui.fold("עוד לא נבדק על נתונים אמיתיים",
                        "<ul>" + "".join(f"<li>{_e(x)}</li>" for x in doc.get("untested", [])) + "</ul>")
        body += ui.fold("סיכונים שכדאי לדעת", "<ul>" + "".join(
            f"<li><b>{_e(x['t'])}.</b> {_e(x['x'])}</li>" for x in doc.get("risks", [])) + "</ul>")
        body += ui.fold("הצעדים הבאים", "<ol class=steps>" + "".join(
            f"<li><div><b>{_e(x['t'])}</b><span class=who>{_e(x['who'])}</span><span class=note>{_e(x['x'])}</span></div></li>"
            for x in doc.get("next", [])) + "</ol>")
        body += ui.fold("מה קרה עד עכשיו", "<ol class=timeline>" + "".join(
            f"<li><span class=d>{_e(x['d'])}</span><b>{_e(x['t'])}.</b> {_e(x['x'])}</li>" for x in doc.get("timeline", []))
            + "</ol>")
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

    def idea_doc(self, sym: str) -> tuple[dict, dict | None]:
        """(dossier, idea) of the newest research that analysed the stock (Ilay 24.10: a watchlist stock from an
        earlier week showed nothing once a newer report came in), else (the newest dossier, None)."""
        rows = self.db.all("SELECT id FROM uploads WHERE kind = 'dossier' ORDER BY id DESC LIMIT 8")
        cache = self.__dict__.setdefault("_dossiers", {})
        newest = None
        for r in rows:
            if r["id"] not in cache:
                up = self.db.one("SELECT content FROM uploads WHERE id = ?", (r["id"],))
                try:
                    cache[r["id"]] = json.loads(up["content"])
                except ValueError:
                    cache[r["id"]] = {}
            d = cache[r["id"]]
            newest = d if newest is None else newest
            idea = (d.get("ideas") or {}).get(sym)
            if idea:
                return d, idea
        return newest or {}, None

    def report_parts(self) -> tuple[list[tuple[str, str]], str, str | None]:
        """(rail, sections, as of) of the newest research report: from its dossier, else cut out of the saved HTML."""
        from qt.research import report
        d = self.report_doc()
        if d is not None:
            nav, body = report.sections(d, self.ideas_html(d))
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

    @staticmethod
    def overview(entry: dict | None, idea: dict | None, limit: int = 340) -> str:
        """The stock in two or three sentences for the scannable list: the research's one line (its tldr) and why now,
        else the thesis or the reason, cut at a sentence end before `limit` characters."""
        j = (idea or {}).get("judgment") or {}
        t = j.get("tldr") or {}
        parts = [t.get("line") or (entry or {}).get("tldr") or j.get("thesis") or j.get("why") or j.get("story") or ""]
        if t.get("why_now"):
            parts.append(t["why_now"])
        text = " ".join(p.strip() for p in parts if p and p.strip())
        if len(text) <= limit:
            return text
        cut = max(text.rfind(". ", 0, limit), text.rfind(": ", 0, limit))
        return text[:cut + 1] if cut > limit // 3 else text[:limit].rsplit(" ", 1)[0] + "…"

    @staticmethod
    def stock_row(sym: str, head: str, pills: str, nums: list[tuple[str, str]], text: str, facts: list[tuple[str, str]],
                  more: str = "", pre: str = "wl", dist: str = "", analysed: str = "") -> str:
        """One stock in the scannable list (Ilay, 24.10): a card that reads at a glance (ticker, status, the price and
        the few numbers, how far the trigger is, two sentences, the date of its analysis) and opens in place to the
        plan's numbers, with a link to the stock's full page."""
        nums = [(k, v) for k, v in nums if v]
        n = (f"<b class=num>{nums[0][1]}</b><i>{' · '.join([nums[0][0]] + [f'{k} {v}' for k, v in nums[1:]])}</i>"
             if nums else "")
        f = "".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in facts if v not in (None, "", "–"))
        return (f"<details class=wl id='{pre}-{_e(sym)}'><summary><span class=wl-s><span><b>{_e(sym)}</b>"
                f"<span class=wl-p>{pills}</span></span><span class=note>{head}</span></span><span class=wl-n>{n}</span>"
                + (f"<span class=wl-d>{dist}</span>" if dist else "")
                + (f"<span class=wl-o>{_e(text)}</span>" if text else "")
                + (f"<span class=wl-a>{analysed}</span>" if analysed else "") + "</summary>"
                f"<div class=wl-b>{f'<dl>{f}</dl>' if f else ''}{more}<a class=wl-go href='/stock/{_e(sym)}'>"
                f"לניתוח המלא של {_e(sym)}: המאמר, הגרפים, החדשות ←</a></div></details>")

    BUCKET = {"enter": ("כניסה", "ok"), "conditional": ("מותנה בטריגר", "warn"), "watch": ("מעקב", "")}

    def watch_data(self) -> dict:
        """The watchlist, the newest daily check of each entry, and the newest research that analysed each stock (with
        its date: Ilay 24.10, a stock from an earlier week keeps its analysis)."""
        from qt.app import watchlist
        wl = watchlist(self.db) or {}
        entries = wl.get("entries") or []
        row = self.db.one("SELECT s.output, r.as_of FROM run_steps s JOIN runs r ON r.id = s.run_id WHERE s.step = 'watch' "
                          "AND s.status = 'done' AND r.kind = 'daily' ORDER BY r.started_at DESC LIMIT 1", (), ["output"])
        checks = {c["symbol"]: c for c in ((row or {}).get("output") or {}).get("checks") or []}
        docs = {}
        for e in entries:
            try:
                docs[e["symbol"]] = self.idea_doc(e["symbol"])
            except Exception:                                           # noqa: BLE001 - the list stands without it
                docs[e["symbol"]] = ({}, None)
        return {"wl": wl, "entries": entries, "checks": checks, "docs": docs, "row": row}

    @staticmethod
    def trigger_line(e: dict, c: dict) -> tuple[str, float | None, str]:
        """(words, bar 0–100, bar class) for how far a stock is from its trigger, from the newest daily check."""
        level = e.get("trigger_level")
        if c.get("invalidated"):
            return "הטריגר בוטל", None, ""
        if c.get("held"):
            return "בתיק", 100, "ok"
        if c.get("triggered"):
            return f"הטריגר הופעל ({_n(level)})" if level else "באזור הכניסה", 100, "ok"
        if level is None:
            return "אין טריגר: ממתינה", None, ""
        d = c.get("dist_to_trigger_pct")
        if d is None:
            return f"טריגר {_n(level)} · עוד לא נבדקה", None, ""
        return f"עוד {abs(d):.1f}% לטריגר ({_n(level)})", max(0.0, 100 - abs(d) * 5), "warn"

    def watch_html(self, f: str = "") -> str:
        """The watchlist as a list to scroll through (Ilay, 24.10): every idea in one card with the latest daily check
        (status, close, distance to the trigger, reward:risk, 3 months), two sentences and the date of its analysis; the
        plan, the trend and what breaks the thesis open on a click, and the stock's page has the full analysis."""
        from qt.daily import STATUS_HE
        w = self.watch_data()
        entries, checks, docs, row, wl = w["entries"], w["checks"], w["docs"], w["row"], w["wl"]
        if not entries:
            return ui.empty("רשימת המעקב ריקה: היא מתמלאת מהמחקר השבועי (או מקובץ watchlist.yaml שנשלח לבוט)")
        order = {"enter": 0, "conditional": 1, "watch": 2}
        hot = {"opportunity": 0, "ready": 0, "recheck": 1, "waiting": 2}

        def key(e: dict) -> tuple:
            c = checks.get(e["symbol"]) or {}
            return (order.get(e.get("bucket"), 9), hot.get(c.get("status"), 3),
                    abs(c["dist_to_trigger_pct"]) if c.get("dist_to_trigger_pct") is not None else 1e9, e["symbol"])
        counts: dict[str | None, int] = {}
        for e in entries:
            counts[e.get("bucket")] = counts.get(e.get("bucket"), 0) + 1
        chips = [("", f"הכל {len(entries)}")] + [(b, f"{self.BUCKET[b][0].split()[0]} {counts[b]}")
                                                for b in ("enter", "conditional", "watch") if counts.get(b)]
        f = f if f in dict(chips) else ""
        filt = ("<nav class=filters aria-label='סינון'>" + "".join(
            f"<a href='/ideas{f'?f={b}' if b else ''}'{' class=on aria-current=true' if b == f else ''}>{_e(t)}</a>"
            for b, t in chips) + "</nav>")
        rows, cur = [], None
        for e in sorted(entries, key=key):
            if f and e.get("bucket") != f:
                continue
            if e.get("bucket") != cur:
                cur = e.get("bucket")
                rows.append(f"<div class=wl-g>{_e(self.BUCKET.get(cur, (cur or 'אחר', ''))[0])} · {counts.get(cur, 0)}</div>")
            sym, c = e["symbol"], checks.get(e["symbol"]) or {}
            d, idea = docs.get(sym) or ({}, None)
            b, bc = self.BUCKET.get(e.get("bucket"), (e.get("bucket") or "", ""))
            st = c.get("status")
            status = ("בתיק" if c.get("held") else ui.plain(STATUS_HE.get(st, st or ""))) if c else "עוד לא נבדקה"
            sc = "ok" if c.get("held") or st in ("opportunity", "ready") else "bad" if st in ("invalidated", "stale") else "mute"
            name = ((idea or {}).get("profile") or {}).get("name")
            head = " · ".join(_e(x) for x in (name, "לונג" if e.get("direction") == "long" else "שורט", e.get("etf")) if x)
            words, pct, pcls = self.trigger_line(e, c)
            dist = ui.prog(pct, pcls) + f"<span>{_e(words)}</span>"
            nums = [("סגירה", _n(c.get("close"))), ("יחס", _n(c.get("rr"), 1)), ("3ח'", _signed(c.get("trend_3m_pct"), 1, "%"))]
            trend = " / ".join(_signed(c.get(k), 1, "%") or "–" for k in ("trend_1y_pct", "trend_3m_pct", "since_add_pct"))
            facts = [("טריגר", _n(e.get("trigger_level"))), ("עצירה", _n(c.get("stop"))), ("יעד", _n(e.get("target"))),
                     ("יחס סיכוי-סיכון", _n(c.get("rr"), 1)), ("עד היעד", _signed(c.get("potential_pct"), 1, "%")),
                     ("שנה / 3ח' / מאז ההוספה", trend if c else ""), ("ימים ברשימה", _e(c.get("days_on_list", ""))),
                     ("דוח הבא", _e(e.get("next_earnings"))), ("רמת ביטול", _n(e.get("invalidate_level"))),
                     ("עצירת זמן", _e(e.get("time_stop"))), ("נוספה", _e(e.get("added")))]
            more = ""
            if e.get("breakers"):
                more += "<h4>מה ישבור את התזה</h4><ul>" + "".join(
                    f"<li>{_e(x.get('text') if isinstance(x, dict) else x)}</li>" for x in e["breakers"]) + "</ul>"
            if c.get("notes"):
                more += "<p class=note>" + _e("; ".join(map(str, c["notes"])) if isinstance(c["notes"], list) else c["notes"]) + "</p>"
            as_of = (d.get("meta") or {}).get("as_of") if idea else None
            analysed = f"ניתוח מ-{_e(as_of)}" if as_of else "אין עדיין ניתוח מחקר"
            rows.append(self.stock_row(sym, head, ui.pill(b, bc) + (ui.pill(status, sc) if status else ""), nums,
                                       self.overview(e, idea), facts, more, dist=dist, analysed=analysed))
        when = f"הבדיקה היומית מ-{_e(row['as_of'])}" if row else "עוד לא הייתה בדיקה יומית"
        return (filt + f"<div class=wlist>{''.join(rows)}</div>"
                f"<p class=note>{len(entries)} רעיונות · {_e(wl.get('source') or '')} · {when} · לחיצה על כרטיס פותחת את "
                f"התוכנית, והקישור בתוכו את הניתוח המלא</p>")

    def ideas_page(self, f: str = "") -> str:
        return self.watch_html(f)

    def top_ideas(self, n: int = 3) -> str:
        """The stocks closest to their trigger, for the today screen: the ones whose trigger fired first, then by distance;
        a stock whose trigger was cancelled, expired or has no data is left out."""
        from qt.daily import STATUS_HE
        w = self.watch_data()
        entries, checks, docs = w["entries"], w["checks"], w["docs"]
        if not entries:
            return ui.empty("רשימת המעקב ריקה: היא מתמלאת מהמחקר השבועי")
        cand = []
        for e in entries:
            c = checks.get(e["symbol"]) or {}
            if e.get("bucket") not in ("enter", "conditional") or c.get("status") in ("invalidated", "expired", "stale", "frozen"):
                continue
            fired = bool(c.get("triggered") or c.get("status") in ("ready", "opportunity"))
            d = c.get("dist_to_trigger_pct")
            cand.append(((0 if fired else 1, abs(d) if d is not None else 1e9, e["symbol"]), e, c))
        cand.sort(key=lambda x: x[0])
        if not cand:
            return ui.empty("אין כרגע מניות עם טריגר פעיל ברשימה")
        rows = []
        for (rank, _, _), e, c in cand[:n]:
            fired = rank == 0
            sym = e["symbol"]
            name = (((docs.get(sym) or ({}, None))[1] or {}).get("profile") or {}).get("name") or e.get("etf") or ""
            words, _, _ = self.trigger_line(e, c)
            if c.get("held"):
                right = ui.pill("בתיק", "ok")
            elif not fired:
                right = (f"<b class=num style='color:var(--warn)'>עוד {abs(c['dist_to_trigger_pct']):.1f}%</b>"
                         if c.get("dist_to_trigger_pct") is not None else "<span>עוד לא נבדקה</span>")
            else:
                right = ui.pill(ui.plain(STATUS_HE.get(c.get("status"), "")) or self.BUCKET[e["bucket"]][0], "ok")
            rows.append(f"<a class=row href='/stock/{_e(sym)}'><span class=l><b>{_e(sym)}</b><span>{_e(name)} · {_e(words)}"
                        f"</span></span><span class=r>{right}</span></a>")
        return "<div class=rows>" + "".join(rows) + "</div>"

    def ideas_html(self, d: dict) -> str:
        """The report's ideas as the same scannable cards (Ilay, 24.10); each stock's article is on its page
        (/stock/SYM), so the report stays a list you can scroll through."""
        from qt.research import report
        order = {"enter": 0, "conditional": 1, "watch": 2, "reject": 3}
        ideas = sorted(d["ideas"].values(), key=lambda i: (order.get(i["bucket"], 9), -(i["plan"].get("rr") or 0)))
        tone = {"enter": "ok", "conditional": "warn", "reject": "bad"}
        rows, cur = [], None
        for i in ideas:
            if i["bucket"] != cur:
                cur = i["bucket"]
                n = sum(1 for x in ideas if x["bucket"] == cur)
                rows.append(f"<div class=wl-g>{_e(report.BUCKET_HE.get(cur, cur))} · {n}</div>")
            p, tl = i.get("plan") or {}, i.get("timing") or {}
            head = " · ".join(_e(x) for x in ((i.get("profile") or {}).get("name"), report.DIR_HE.get(i["direction"]),
                                               i.get("etf")) if x)
            pills = (ui.pill(report.BUCKET_HE.get(i["bucket"], i["bucket"]), tone.get(i["bucket"], ""))
                     + (ui.pill("רמזור " + report.COLOR_HE.get(tl.get("combined"), ""),
                                {"green": "ok", "orange": "warn", "red": "bad"}.get(tl.get("combined"), "mute"))
                        if tl.get("combined") else ""))
            nums = [("כניסה", _n(p.get("entry"))), ("עצירה", _n(p.get("stop"))), ("יעד", _n(p.get("target"))),
                    ("יחס", _n(p.get("rr"), 1))]
            j = i.get("judgment") or {}
            facts = [("סיפור", _e(j.get("story"))), ("תזה", _e(j.get("thesis") or j.get("why"))),
                     ("שרשרת קטליזטורים", _e(j.get("catalyst_chain"))), ("רמות", _e(j.get("levels_note"))),
                     ("למה בסל הזה", _e(i.get("bucket_reason")))]
            rows.append(self.stock_row(i["sym"], head, pills, nums, self.overview(None, i), facts, pre="ir"))
        fit = report._portfolio(d)
        return ('<section id="ideas"><h2>רעיונות: שלבים 7–11</h2>'
                f"<div class=wlist>{''.join(rows)}</div>"
                + (f"<details class=more><summary>התאמה לתיק (S11)</summary>{fit}</details>" if fit else "")
                + "</section>")

    DAYS = ("שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון")
    MONTHS = ("ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני", "יולי", "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר", "דצמבר")

    def book_card(self, s: dict | None) -> str:
        """The portfolio in one card for the today screen: its value, the open P&L and each position's %."""
        if s is None:
            return ui.panel("תיק", "<p class=failed>לא הצלחתי לטעון את התיק</p>", ("/positions", "לתיק ←"))
        if not s["has_portfolio"]:
            return ui.panel("תיק", ui.empty("עוד אין תיק: שלח לבוט קובץ portfolio.yaml."), ("/positions", "לתיק ←"))
        pos = "".join(f"<a class=row href='/stock/{_e(p['symbol'])}'><span class=l><b>{_e(p['symbol'])}</b>"
                      f"<span>{'לונג' if p['direction'] == 'long' else 'שורט'} · {_n(p['qty'], 0)} יח'</span></span><span class=r>"
                      + (f"<b>{_signed(p['pl_pct'], 1, '%')}</b>" if p["priced"] else "<span>אין מחיר</span>")
                      + "</span></a>" for p in s["positions"][:4])
        more = f"<p class=note>ועוד {len(s['positions']) - 4} בתיק</p>" if len(s["positions"]) > 4 else ""
        pend = f"<p class=note>{len(s['pending'])} פקודות ממתינות לפתיחה</p>" if s["pending"] else ""
        return ui.panel("שווי התיק", f"<div class=money>{_n(s['equity'], 0)}$</div>"
                        f"<span class=note>פתוח {_signed(s['unrealized'], 0)} · {len(s['positions'])} פוזיציות פתוחות</span>"
                        + (f"<div class=rows>{pos}</div>" if pos else "") + more + pend, ("/positions", "לתיק ←"), id="book")

    def next_runs(self, st: dict) -> str:
        upcoming = sorted(((v["next"], k) for k, v in st["runs"].items() if v["next"]))[:3]
        if not upcoming:
            return ""
        return ui.panel("הריצות הבאות", "<div class=rows>" + "".join(
            f"<div class=row><span class=l><b>{dict(self.KINDS)[k]}</b></span><span class=r><b>{ui.when(n)}</b>"
            f"<span>{ui.ago(n)}</span></span></div>" for n, k in upcoming) + "</div>", ("/system#schedule", "לוח הזמנים ←"))

    def overview_parts(self) -> dict[str, str]:
        """The live parts of the today screen, by name (each one is refreshed in place): what failed, what waits for
        Ilay, the book, the stocks closest to their trigger, and the next runs."""
        st = self.status()
        s, items = st["book"], st["queue"]
        h = self.health(s)
        equity = s.get("equity") if s else None
        queue = ui.panel(f"מחכה לאישורך ({len(items)})", "<div class=cards>" + "".join(
            self.item_card(i, equity) for i in items) + "</div>", id="queue", label="מחכה לאישורך") if items else ui.panel(
            "מחכה לאישורך", "<p class=note>אין כרגע פריטים שמחכים לך.</p>", id="queue")
        n = len((self.watch_data())["entries"])
        watch = ui.panel("קרובות לטריגר", self.top_ideas(), ("/ideas", f"כל הרעיונות ({n}) ←"), id="watch")
        return {"alert": self.health_banner(h), "queue": queue, "book": self.book_card(s), "watch": watch,
                "next": self.next_runs(st), "_health": h}

    LIVE_ORDER = {"alert": "o1", "queue": "o2", "book": "o3", "watch": "o4", "next": "o6"}

    def overview_html(self) -> str:
        """The live parts only (what the page's script re-fetches), and the status dot."""
        parts = self.overview_parts()
        h = parts.pop("_health")
        return ("".join(f"<div data-live={k} class='lv {self.LIVE_ORDER[k]}'>{v}</div>" for k, v in parts.items())
                + ui.health_link((h["state"], h["text"])))

    def report_card(self) -> str:
        """The newest research report in one card: its date, its first line and what it found; opens the research tab."""
        d = self.report_doc()
        if d is None:
            row = self.db.one("SELECT name FROM uploads WHERE kind = 'report' ORDER BY id DESC LIMIT 1")
            if row is None:
                return ui.panel("דוח המחקר", ui.empty("עוד אין דוח מחקר: הריצה השבועית מפיקה אותו בשבת"), ("/research", "למחקר ←"))
            return (f"<a class=report-card href='/research'><span>דוח המחקר השבועי</span><b>{_e(row['name'])}</b>"
                    f"<i>לקריאת הדוח ←</i></a>")
        as_of = d["meta"]["as_of"]
        story = (d.get("judgments") or {}).get("A1", {}).get("story") or []
        line = str(story[0] if isinstance(story, list) and story else story or "")
        cut = line.find(". ")
        line = line[:cut + 1] if 0 < cut < 160 else line[:160]
        n: dict[str, int] = {}
        for i in (d.get("ideas") or {}).values():
            n[i["bucket"]] = n.get(i["bucket"], 0) + 1
        found = " · ".join(f"{n[b]} {he}" for b, he in (("enter", "לכניסה"), ("conditional", "מותנים"), ("watch", "במעקב"))
                           if n.get(b))
        stale = self.stale(as_of)
        return (f"<a class=report-card href='/research'><span>דוח המחקר השבועי · {_e(as_of)}</span>"
                + (f"<b>{_e(line)}</b>" if line else "") + (f"<span>{_e(found)}</span>" if found else "")
                + (f"<span class=bad>{re.sub(r'<[^>]+>', '', stale).strip(' ·')}</span>" if stale else "")
                + "<i>לקריאת הדוח ←</i></a>")

    def dashboard(self, env: dict, start: Start) -> list[bytes]:
        """The today screen (Ilay 24.10, the approved design): what failed and what waits for Ilay first, then the book,
        the stocks closest to their trigger, the newest report and the next runs. Everything else moved to its own tab:
        the watchlist to /ideas, the book to /positions, the report to /research, the runs to /system."""
        qs = env.get("QUERY_STRING", "")
        self._schemas()
        if "fragment" in parse_qs(qs):
            return self._send(start, "200 OK", self.overview_html(), headers=[("Cache-Control", "no-store")])
        if "part=report" in qs:                                         # kept for links made before the tabs
            nav, report, as_of = self.overview_report()
            return self._send(start, "200 OK", f"<div class=rep>{report}</div>" if report else "")
        parts = self.overview_parts()
        h = parts.pop("_health")
        live = lambda k: f"<div data-live={k} class='lv {self.LIVE_ORDER[k]}'>{parts[k]}</div>"  # noqa: E731
        body = ("<div class=today><div class=col-main>" + live("alert") + live("queue") + live("watch") + "</div>"
                "<div class=col-side>" + live("book") + f"<div class=o5>{self.report_card()}</div>" + live("next")
                + "</div></div>")
        # the old one-page overview's anchors (/#watch, /#macro …) open the tab that now holds them
        body += ("<script>(function(){var h=location.hash.slice(1);if(!h||document.getElementById(h))return;"
                 "var m={watch:'/ideas',book:'/positions',runs:'/system#runs',system:'/system',summary:'/research'};"
                 "location.replace(m[h]||'/research#'+h)})()</script>")
        now = datetime.now(ui.IL)
        sub = f"{self.DAYS[now.weekday()]}, {now.day} ב{self.MONTHS[now.month - 1]}"
        return self._send(start, "200 OK", ui.page("היום", body, "/", live=True, sub=sub, health=(h["state"], h["text"])))

    def research(self, env: dict, start: Start) -> list[bytes]:
        """The research tab: the newest weekly report (macro, sectors, screens, ideas, charts), and every report before
        it. The report is most of the page (~500KB), so it comes in right after the page paints."""
        qs = env.get("QUERY_STRING", "")
        self._schemas()
        nav, report, as_of = self.overview_report()
        if "part=report" in qs:
            return self._send(start, "200 OK", f"<div class=rep>{report}</div>" if report else "")
        if report:
            rep = ("<div id=rep-slot><p class=note style='padding:1rem'>טוען את דוח המחקר…</p></div><script>"
                   "fetch('/research?part=report',{credentials:'same-origin'}).then(function(r){return r.text()}).then(function(t){"
                   "var s=document.getElementById('rep-slot');s.outerHTML=t;var h=location.hash&&document.getElementById("
                   "location.hash.slice(1));if(h)h.scrollIntoView()})</script>")
            sub = f"דוח המחקר האחרון: {ui.e(as_of or '')}{self.stale(as_of)}"
        else:
            rep = ui.section("דוח המחקר", ui.empty("עוד אין דוח מחקר: הריצה השבועית מפיקה אותו בשבת (צריך מפתח FRED, "
                                                   "קובץ מקרו ו-Claude), או שלח לבוט dossier.json של מחקר"), id="summary")
            sub = ""
        reps = self.db.all("SELECT id, name, created_at FROM uploads WHERE kind = 'report' ORDER BY id DESC LIMIT 20")
        past = ui.section("כל הדוחות", ("<div class='panel'><div class=rows>" + "".join(
            f"<a class=row href='/report/{r['id']}'><span class=l><b>{_e(r['name'])}</b></span><span class=r>"
            f"<span>{ui.when(r['created_at'])}</span></span></a>" for r in reps) + "</div></div>")
            if reps else ui.empty("הדוח השבועי הראשון יגיע אחרי הריצה השבועית"), id="reports")
        rail = list(nav) + [("reports", "כל הדוחות")] if nav else []
        h = self.health()
        return self._send(start, "200 OK", ui.page("מחקר", rep + past, "/research", sub=sub, rail=rail,
                                                   css=ui.report_css() if report else "", health=(h["state"], h["text"])))

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
        """One stock (the approved design): the ticker, the price and the plan's numbers, what to know in three lines
        (the research's one line, why now, what can go wrong), the live chart, and then the full article, the
        watchlist data, the position, the queue, the trades and the news, each opening on a tap."""
        from qt.app import watchlist
        from qt.daily import STATUS_HE
        from qt.research import report
        sym = symbol.upper()
        latest = self.report_doc() or {}
        d, idea = self.idea_doc(sym)
        timing = next((t for t in latest.get("timing") or [] if t.get("sym") == sym), None)
        older = bool(idea) and (d.get("meta") or {}).get("as_of") != (latest.get("meta") or {}).get("as_of")
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
        plan = (idea or {}).get("plan") or {}
        as_of = (d.get("meta") or {}).get("as_of") if idea else None
        # the head: ticker, name, side, the research's basket and the daily check's status, the price
        name = ((idea or {}).get("profile") or {}).get("name")
        direction = (idea or {}).get("direction") or (entry or {}).get("direction") or (pos or {}).get("direction")
        etf = (idea or {}).get("etf") or (entry or {}).get("etf") or (pos or {}).get("sector")
        bucket = (idea or {}).get("bucket") or (entry or {}).get("bucket")
        pills = ""
        if bucket:
            pills += ui.pill(report.BUCKET_HE.get(bucket, bucket),
                             {"enter": "ok", "conditional": "warn", "reject": "bad"}.get(bucket, "mute"))
        if check:
            st = check.get("status")
            pills += ui.pill("בתיק" if check.get("held") else ui.plain(STATUS_HE.get(st, st)),
                             "ok" if check.get("held") or st in ("opportunity", "ready") else "bad" if st in ("invalidated", "stale")
                             else "mute")
        sub = " · ".join(_e(x) for x in (name, etf, {"long": "לונג", "short": "שורט"}.get(direction or "")) if x)
        out = (f"<div class=hero><div class=head><a class=note href='/ideas'>→ רעיונות</a>"
               f"<div style='display:flex;align-items:center;gap:10px;flex-wrap:wrap'><h1>{_e(sym)}</h1>{pills}</div>"
               f"<span class=sub>{sub}</span>" + (f"<span class=note>ניתוח מ-{_e(as_of)}</span>" if as_of else "")
               + "</div><div class=px>"
               + (f"<b>{_n(last)}</b><span class=note>סגירה אחרונה" + (f" · {_signed((last / prev - 1) * 100, 1, '%')} היום"
                                                                      if prev else "") + "</span>" if last else
                  "<b class=muted>אין מחיר</b>") + "</div></div>")
        # what waits for Ilay on this stock, with its buttons
        items = [i for i in Queue(self.db).open_items() if i.get("symbol") == sym]
        if items:
            out += ui.section(f"מחכה לאישורך ({len(items)})", "<div class=cards>" + "".join(
                self.item_card(i, book.get("equity"), f"stock:{sym}") for i in items) + "</div>")
        # the numbers: since the research, the plan, the basket, the daily check, the position
        ref = ((idea or {}).get("row") or {}).get("price") or (entry or {}).get("price_at_add")
        cells = [ui.cell(_signed((last / ref - 1) * 100, 1, "%") if last and ref else "–", f"מאז המחקר ({_n(ref)})")]
        if plan or entry:
            e = entry or {}
            cells += [ui.cell(_n(v), k) for k, v in (("כניסה", plan.get("entry") or e.get("trigger_level")),
                                                     ("עצירה", plan.get("stop")), ("יעד", plan.get("target") or e.get("target")))
                      if v is not None]
        if idea:
            cells.append(ui.cell(f"R:R {_n(plan.get('rr'), 1)}", f"רמזור {report.COLOR_HE.get(idea['timing'].get('combined'), '')}"))
        if check:
            cells.append(ui.cell(f"יחס {_n(check.get('rr'), 1)}", "הבדיקה היומית · " + _e(self.trigger_line(entry or {}, check)[0])))
        if (entry or {}).get("next_earnings"):
            cells.append(ui.cell(_e(entry["next_earnings"]), "דוח הבא"))
        if pos:
            cells.append(ui.cell(_signed(pos["pl"], 0), f"בתיק: {_n(pos['qty'], 0)} יח' · {_signed(pos['pl_pct'], 2, '%')}", "ok"))
        out += "<div class=strip>" + "".join(cells) + "</div>"
        # what to know, in three lines
        t = ((idea or {}).get("judgment") or {}).get("tldr") or {}
        lead = t.get("line") or self.overview(entry, idea)
        if lead:
            out += ("<div class='panel brief'>" + f"<p class=lead>{_e(lead)}</p>"
                    + (f"<div><span>למה עכשיו</span><p>{_e(t['why_now'])}</p></div>" if t.get("why_now") else "")
                    + (f"<div class=wrong><span>מה יכול להשתבש</span><p>{_e(t['wrong'])}</p></div>" if t.get("wrong") else "")
                    + "</div>")
        # the live chart with the plan's lines
        if bars:
            c = self.chart_from_bars(bars)
            lines = {"entry": plan.get("entry") or (entry or {}).get("trigger_level") or (pos or {}).get("entry") or last,
                     "stop": plan.get("stop") or (pos or {}).get("stop") or (check or {}).get("stop") or last,
                     "target": plan.get("target") or (entry or {}).get("target") or (pos or {}).get("target")}
            fig = report.price_chart(c, lines, (idea or {}).get("levels") or {}, (idea or {}).get("direction", "long"))
            out += ui.section("המחיר עכשיו", f"<div class=panel><div class=rep><figure>{fig}</figure></div></div>"
                              "<p class=note>שנה אחרונה מ-Yahoo, עם ממוצעים 21/63/252 וקווי התוכנית. מתעדכן בכל טעינה.</p>")
        else:
            out += ui.section("המחיר עכשיו", ui.empty("לא הצלחתי להביא מחירים מ-Yahoo כרגע"))
        # what the research found: the whole dossier of the idea, as in the report
        if idea:
            checks = [r for r in (d.get("verification") or {}).get("checks", []) if r["subject"] == sym]
            note = (f"<p class=note>מהמחקר של {_e(d['meta']['as_of'])}: המניה לא נותחה במחקר האחרון "
                    f"({_e((latest.get('meta') or {}).get('as_of'))}).</p>" if older else "")
            art = "<div class=rep>" + self.rep_links(report.article_html(idea, checks, d["meta"]["as_of"])) + "</div>"
            if sym in ((d.get("portfolio") or {}).get("syms") or []):      # S11: how it sits with the other trades
                art += f"<div class=rep>{self.rep_links(report._portfolio(d))}</div>"
            out += note + ui.fold(f"המאמר המלא · מהמחקר של {_e(d['meta']['as_of'])}", art, id="article")
        elif timing:
            out += ui.fold(f"בסינון וברמזור ({_e(d['meta']['as_of'])})", ui.data(timing)
                           + "<p class=note>המניה עברה את הסינון אבל לא נבחרה לניתוח עומק.</p>", open=True)
        else:
            out += ui.empty("המניה לא הופיעה בדוח המחקר האחרון")
        if entry or check:
            out += ui.fold("ברשימת המעקב", (ui.data({k: v for k, v in (entry or {}).items() if k != "breakers"})
                                           + ("<h3>מה ישבור את התזה</h3><ul>" + "".join(f"<li>{_e(b)}</li>" for b in entry["breakers"])
                                              + "</ul>" if (entry or {}).get("breakers") else "")
                                           + ("<h3>הבדיקה היומית האחרונה</h3>" + ui.data(check) if check else "")))
        if pos:
            out += ui.fold("הפוזיציה", self.position_card(pos) + self.book_table({"positions": [pos]}), open=True)
        qitems = self.db.all("SELECT * FROM approval_queue WHERE symbol = ? ORDER BY id DESC LIMIT 10", (sym,), ["payload"])
        if qitems:
            out += ui.fold("בתור ובהחלטות", ui.table(("#", "יום", "מה", "מצב", "פירוט"), (
                [f"<a href='/item/{i['id']}'>#{i['id']}</a>", f"<td class=t>{_e(i['as_of'])}</td>",
                 f"{_e(i['kind'])} · {_e(i['rule_id'])}", _e(i["status"]), _e((i["payload"] or {}).get("detail"))] for i in qitems)))
        orders = self.db.all("SELECT * FROM paper_orders WHERE symbol = ? ORDER BY id DESC LIMIT 10", (sym,))
        if orders:
            out += ui.fold("עסקאות נייר", ui.table(("סוג", "כמות", "בפתיחת", "מצב", "מחיר"), (
                [_e(o["kind"]), f"<td class=num>{_e(o['qty'])}</td>", f"<td class=t>{_e(o['fill_on'])}</td>", _e(o["status"]),
                 f"<td class=num>{_n(o['fill_price'])}</td>"] for o in orders)))
        dec = self.db.all("SELECT * FROM decisions WHERE subject = ? ORDER BY id DESC LIMIT 10", (sym,))
        if dec:
            out += ui.fold("ביומן ההחלטות", ui.table(("מתי", "מי", "פעולה", "החלטה", "ביטחון", "כרטיס"), (
                [f"<td class=t>{ui.when(x['created_at'])}</td>", _e(x["decided_by"]), _e(x["action"]), _e(str(x["choice"])[:200]),
                 _e(x["confidence"]), _e(x["card_id"])] for x in dec)))
        out += ui.fold("חדשות", "<ul>" + "".join(
            f"<li><span class=note>{_e(n['published'])} · {_e(n['publisher'])}</span> <a href='{_e(n['link'])}' target=_blank "
            f"rel=noopener>{_e(n['title'])}</a></li>" for n in news[:8]) + "</ul>" if news else ui.empty("אין חדשות מ-Yahoo כרגע"),
            id="news")
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
