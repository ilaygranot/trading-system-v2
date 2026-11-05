"""The morning digest (qt.digest): the last run, the health line, the scanned stocks with their news, the macro."""
from datetime import datetime, timezone

from test_runs_daily import AS_OF, _runner

from qt import digest as D

RSS = ("<rss><channel><item><title>Fed holds rates steady</title><link>https://x/fed</link>"
       "<pubDate>{d}</pubDate><source>Reuters</source></item></channel></rss>")
NOW = datetime(2023, 6, 1, 4, 10, tzinfo=timezone.utc)            # 07:10 Israel (summer)


class Bot:
    def __init__(self):
        self.sent = []

    def send(self, text, buttons=None, force_reply=False):
        self.sent.append((text, buttons))
        return len(self.sent)


def _get(url):
    return RSS.format(d="Thu, 01 Jun 2023 01:00:00 GMT")


def _news(sym):
    return [{"title": f"{sym} wins a big order", "link": f"https://n/{sym}", "published": AS_OF.isoformat(),
             "publisher": "Wire", "related": [sym]}], "fake"


class News:
    news = staticmethod(_news)


def test_digest_reads_the_last_daily_run_and_the_macro(db, monkeypatch):
    monkeypatch.setenv("QT_PUBLIC_URL", "https://qt.example")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    monkeypatch.delenv("IBKR_HOST", raising=False)
    _runner(db, news=News()).start("daily", AS_OF)
    d = D.build(db, NOW, _get)
    assert d["fresh"] and [r["kind"] for r in d["runs"]] == ["daily"]
    r = d["runs"][0]
    assert r["status"] == "done" and r["market"]["spx"] and r["market"]["vix"] == 21.0
    ba = next(x for x in d["stocks"]["rows"] if x["symbol"] == "BA")
    assert ba["held"] and ba["news"][0]["title"] == "BA wins a big order"
    assert d["macro"]["headlines"][0]["title"] == "Fed holds rates steady"
    assert d["health"]["runs"][0]["kind"] == "daily"
    text = "\n".join(D.telegram_lines(d, "https://l"))
    assert "יומית 31.05 ✓" in text and "Fed holds rates steady" in text and "https://l" in text
    html = D.page(d)
    assert "BA wins a big order" in html and "סטטוס מערכת" in html and "מקרו ושוק" in html


def test_scheduled_sends_once_a_morning_with_the_signed_link(db, monkeypatch):
    monkeypatch.setenv("QT_PUBLIC_URL", "https://qt.example")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    _runner(db).start("daily", AS_OF)
    bot = Bot()
    assert D.scheduled(db, bot, datetime(2023, 6, 1, 6, 0, tzinfo=timezone.utc), _get) == "not due: digest"
    out = D.scheduled(db, bot, NOW, _get)
    assert out.startswith("digest 2023-06-01")
    assert D.scheduled(db, bot, NOW, _get).startswith("skip")         # the second UTC firing
    _, buttons = bot.sent[-1]
    uid = db.one("SELECT id FROM uploads WHERE kind = 'digest'")["id"]
    assert buttons[0][0]["url"] == f"https://qt.example/digest/{uid}?sig={D.sign(uid, 'pw')}"
    assert bot.sent[0][0].startswith("☀️ בוקר טוב")


def test_a_failed_run_and_a_quiet_feed_still_make_a_digest(db):
    from qt.runs import Runner, Step

    def boom(ctx, _):
        raise RuntimeError("Yahoo down")
    r = Runner(db, {"daily": (Step("fetch", boom),)}, {})
    try:
        r.start("daily", AS_OF)
    except RuntimeError:
        pass

    def down(url):
        raise ConnectionError("no route")
    d = D.build(db, NOW, down)
    assert d["runs"][0]["status"] == "failed" and d["runs"][0]["failed_step"] == "fetch"
    assert d["health"]["state"] == "bad"
    assert d["macro"]["headlines"] == [] and d["macro"]["headline_errors"]
    assert "נכשלה" in "\n".join(D.telegram_lines(d, None))
    assert "כותרות המקרו לא נטענו" in D.page(d)


def test_the_web_page_opens_only_with_its_signature(db, monkeypatch):
    from qt.approvals import Queue
    from qt.web import App
    d = D.build(db, NOW, _get)
    import json
    uid = Queue(db).upload("digest", "digest-2023-06-01.json", json.dumps(d, ensure_ascii=False))
    app = App(db.url, password="pw")
    app._db = db
    got = {}

    def start(status, headers, *a):
        got["status"] = status
    body = b"".join(app({"PATH_INFO": f"/digest/{uid}", "REQUEST_METHOD": "GET",
                         "QUERY_STRING": f"sig={D.sign(uid, 'pw')}"}, start))
    assert got["status"] == "200 OK" and "Digest בוקר".encode() in body
    app({"PATH_INFO": f"/digest/{uid}", "REQUEST_METHOD": "GET", "QUERY_STRING": "sig=bad"}, start)
    assert got["status"].startswith("401")
