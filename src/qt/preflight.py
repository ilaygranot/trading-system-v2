"""The pre-flight check before the weekly research (Ilay 24.10: a run like 23.10 cannot happen again).

Each check is one thing the Saturday run needs, tried for real: the Playbook from the installed package, the keys
on this server, the database, the Claude CLI answering on the subscription token, and one live request to every
data source the research reads (Yahoo's screener and quoteSummary, FRED, SEC, roic.ai, FMP, the monthly macro
sources), with fresh caches so an old answer cannot pass for a live one. Then the agents' expected cost against the
run's cap, and against the load the Claude subscription was seen to stop at. `qt preflight` runs it (exit 1 on any
failure, `--notify` sends the result to Telegram); `--offline` keeps to the checks without network or keys (the CI
job that installs the package as Render does).
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

STAGES = [f"S{n:02d}" for n in range(1, 14)]
WEEKLY_KEYS = ("QT_DATABASE_URL", "FRED_API_KEY", "SEC_CONTACT", "ROIC_API_KEY", "FMP_API_KEY", "TELEGRAM_BOT_TOKEN",
               "TELEGRAM_CHAT_ID", "QT_PUBLIC_URL")
COST_PER_IDEA_USD = 8.0          # list price of one deep dive with its article, before any run measured it (24.10)
COST_SHARE_MAX = 0.8             # the estimate may use at most this share of the cap (not from course)
SUBSCRIPTION_SEEN_USD = 45.0     # list price after which the subscription stopped answering (weekly-2026-10-23-9ec812, 25.10)


@dataclass
class Result:
    name: str
    ok: bool
    detail: str
    ms: int = 0


def _run(name: str, fn: Callable[[], str]) -> Result:
    t0 = time.monotonic()
    try:
        detail = fn()
        ok = True
    except Exception as e:                                           # noqa: BLE001 - the check's answer
        detail, ok = f"{type(e).__name__}: {str(e)[:240]}", False
    return Result(name, ok, detail, int((time.monotonic() - t0) * 1000))


# ---------------------------------------------------------------- code (offline)

def playbook() -> str:
    from qt.playbook import _default_tiers_dir, load_playbook
    d = _default_tiers_dir()
    pb = load_playbook(str(d))
    missing = [s for s in STAGES if not (d / f"{s}.csv").exists() or not list((d / "chapters").glob(f"{s}-*.md"))]
    if missing or not (d / "chapters" / "decisions.md").exists():
        raise RuntimeError(f"snapshot {d} is missing {', '.join(missing) or 'decisions.md'}")
    if len(pb.rules) < 400 or len(pb.hard_rules()) < 40:
        raise RuntimeError(f"{len(pb.rules)} rules, {len(pb.hard_rules())} hard: expected 400+ and 40+")
    return f"{len(pb.rules)} כללים, {len(pb.hard_rules())} נוקשים, גרסה {pb.version}"


def cards() -> str:
    """Every card's request builds with its chapters, as the runner builds it (no call)."""
    from qt.agents.cards import load_cards
    from qt.agents.context import build_request
    from qt.agents.runner import config
    from qt.facts import FactSheet
    cfg = config()
    n = 0
    for cid, card in load_cards().items():
        a = cfg["agents"][card.agent]
        req = build_request(card.agent, card, FactSheet(), {}, cfg["models"][a["model"]], a["effort"], cfg["max_tokens"])
        if len(str(req.get("system"))) < 1000:
            raise RuntimeError(f"{cid}: the request has no Playbook chapters")
        n += 1
    return f"{n} כרטיסים נבנים עם פרקי ה-Playbook"


def policy() -> str:
    from qt.policy import load_policy
    p = load_policy()
    return f"policy {p.playbook_version}"


# ---------------------------------------------------------------- this server

def keys() -> str:
    missing = [k for k in WEEKLY_KEYS if not os.environ.get(k)]
    if not (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")):
        missing.append("CLAUDE_CODE_OAUTH_TOKEN")
    if missing:
        raise RuntimeError("חסרים: " + ", ".join(missing))
    return f"{len(WEEKLY_KEYS) + 1} מפתחות מוגדרים"


def database() -> str:
    from qt.db import DB
    url = os.environ.get("QT_DATABASE_URL", "")
    if not url:
        raise RuntimeError("QT_DATABASE_URL not set")
    db = DB(url)
    db.one("SELECT 1 AS x")
    from qt.approvals import Queue
    from qt.approvals import SCHEMA as QS
    db.ddl(QS)
    need = [k for k in ("manual_macro", "watchlist") if Queue(db).latest_upload(k) is None]
    if need:
        raise RuntimeError("אין במסד: " + ", ".join(need))
    return f"{db.dialect}, קובץ המקרו ורשימת המעקב קיימים"


def claude() -> str:
    """The CLI on the subscription token answers one short request (the agents' path, qt.agents.cli_client)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic
        from qt.agents.runner import config
        m = config()["models"]["haiku"]
        r = anthropic.Anthropic().messages.create(model=m, max_tokens=5, messages=[{"role": "user", "content": "OK?"}])
        return f"API: {r.model}"
    from qt.agents.cli_client import claude_bin
    b = claude_bin()
    v = subprocess.run([b, "--version"], capture_output=True, text=True, timeout=60)
    if v.returncode:
        raise RuntimeError(f"{b} --version: {v.stderr[:200]}")
    with tempfile.TemporaryDirectory() as cwd:
        r = subprocess.run([b, "-p", "Answer with the single word OK.", "--output-format", "json", "--max-turns", "1"],
                           capture_output=True, text=True, timeout=180, cwd=cwd)
    if r.returncode or '"is_error":true' in r.stdout.replace(" ", ""):
        raise RuntimeError(f"claude -p: {(r.stderr or r.stdout)[:240]}")
    return f"{v.stdout.strip()} עונה על הטוקן"


# ---------------------------------------------------------------- live data, fresh caches

def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="qt-preflight-"))


def _store():
    from qt.data.store import Store
    return Store(str(_tmp() / "store.sqlite"))


def yahoo() -> str:
    """The research's own client: the crumb, one screener page, one quoteSummary and one chart."""
    from qt.research.yahoo import BASE, SECTORS, Yahoo
    y = Yahoo(_tmp())
    sector = next(iter(SECTORS))
    body = {"size": 5, "offset": 0, "sortField": "intradaymarketcap", "sortType": "DESC", "quoteType": "EQUITY",
            "query": {"operator": "AND", "operands": [{"operator": "eq", "operands": ["region", "us"]},
                                                       {"operator": "eq", "operands": ["sector", sector]}]},
            "userId": "", "userIdType": "guid"}
    res = y._get(f"{BASE}/v1/finance/screener", key=f"preflight|{time.time()}", post=body)["finance"]["result"][0]
    if not res.get("quotes"):
        raise RuntimeError("screener answered with no stocks")
    s = y.summary("AAPL")
    if not s.get("earningsTrend"):
        raise RuntimeError("quoteSummary without earningsTrend")
    bars = y.bars("SPY", date.today() - timedelta(days=10), date.today())
    if not bars:
        raise RuntimeError("no SPY bars")
    return f"screener {res.get('total')} ב-{sector}, quoteSummary, גרף SPY עד {bars[-1].day}"


def fred() -> str:
    from qt.data.providers import FRED
    obs, _ = FRED(_store()).observations("UNRATE", date.today() - timedelta(days=120), date.today())
    if not obs:
        raise RuntimeError("UNRATE: no observations")
    return f"UNRATE {obs[-1][0]}"


def sec() -> str:
    from qt.data.providers import EDGAR
    cik = EDGAR(_store(), contact=os.environ.get("SEC_CONTACT")).cik("AAPL")
    if not cik:
        raise RuntimeError("no CIK for AAPL")
    return f"CIK {cik}"


def roic() -> str:
    from qt.data.transcripts import RoicTranscripts
    r = RoicTranscripts(_store())
    if not r.ok():
        raise RuntimeError("ROIC_API_KEY not set")
    calls = r.calls("AAPL")
    if not calls:
        raise RuntimeError("no calls listed for AAPL")
    return f"{len(calls)} שיחות ל-AAPL"


def fmp() -> str:
    from qt.data.providers import FMP
    bars, _ = FMP(_store()).daily_bars("SPY", date.today() - timedelta(days=10), date.today())
    if not bars:
        raise RuntimeError("no SPY bars")
    return f"SPY עד {bars[-1].day}"


def second_price() -> str:
    """The data check's second close for a small company (FMP's free plan answers only the largest; 24.10)."""
    from qt.research import verify
    from qt.schedule import previous_trading_day
    day = previous_trading_day(date.today())
    close, src = verify.default_sources(None)["second_close"]("EGY", day)
    return f"EGY {day}: {close} ({src})"


def news() -> str:
    """A1's market story (DC-27): every Google News query and the Fed's press feed answer, with headlines."""
    from qt.research import macro_story as MS
    got = MS.fetch_news(date.today())
    if got["errors"] or not got["items"]:
        raise RuntimeError("; ".join(f"{q}: {e[:80]}" for q, e in got["errors"].items()) or "no headlines")
    return f"{len(got['items'])} כותרות מ-{len(MS.QUERIES) + 1} מקורות"


def macro_sources(as_of: date | None = None) -> str:
    """The monthly macro file as the run will refresh it: nothing stale, nothing rejected (the research fails on both)."""
    from qt.approvals import Queue
    from qt.data import macro_auto as MA
    from qt.db import DB
    rep = MA.refresh(Queue(DB(os.environ["QT_DATABASE_URL"])).latest_upload("manual_macro"), as_of or date.today())
    if rep.get("stale") or rep.get("rejected"):
        raise RuntimeError("; ".join(MA.lines(rep)))
    return "עדכני: " + ", ".join(f"{k} {v}" for k, v in sorted((rep.get("latest") or {}).items()))


def _estimate() -> tuple[float, str]:
    """The research's expected list price: the last research's cost per deep dive, times the cap of deep dives, plus
    its fixed part (A1, the pick, DC-08)."""
    from qt.db import DB
    from qt.research.judge import MAX_DEEP
    per, fixed, basis = COST_PER_IDEA_USD, 2.0, "הערכה ראשונית"
    url = os.environ.get("QT_DATABASE_URL")
    if url:
        db = DB(url)
        row = db.one("SELECT run_id FROM agent_calls WHERE step = 'research' ORDER BY created_at DESC LIMIT 1")
        if row:
            rows = db.all("SELECT subject, SUM(cost_usd) AS usd FROM agent_calls WHERE run_id = ? AND step = 'research' "
                          "GROUP BY subject", (row["run_id"],))
            ideas = [float(r["usd"] or 0) for r in rows if r["subject"] not in ("macro", "screen")]
            if ideas:
                per = max(sum(ideas) / len(ideas), per / 2)
                fixed = sum(float(r["usd"] or 0) for r in rows if r["subject"] in ("macro", "screen"))
                basis = f"לפי {row['run_id']}"
    return per * MAX_DEEP + fixed, basis


def cost_estimate() -> str:
    """The expected list price against the run's cap. Over COST_SHARE_MAX of the cap fails: the cap must stay a guard
    against a runaway loop, never the thing that ends a normal run (23.10)."""
    from qt.agents.runner import config
    cap = float(config()["budget"]["weekly_research_usd"])
    est, basis = _estimate()
    if est > cap * COST_SHARE_MAX:
        raise RuntimeError(f"צפוי {est:.0f}$ ({basis}), התקרה {cap:.0f}$: הריצה עלולה להיחתך")
    return f"צפוי {est:.0f}$ מתוך תקרה של {cap:.0f}$ ({basis})"


def subscription() -> str:
    """The expected load against what the Claude subscription carried before it stopped answering (25.10: 395 calls,
    ~45$ at list price, then 0 tokens). The subscription's limit is not published, so the check uses the load it was
    seen to stop at; over it fails, since the run would stop in the middle and wait for a resume. On an API key the
    run pays per call and has no such limit."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "מפתח API: אין מגבלת מנוי"
    est, basis = _estimate()
    if est > SUBSCRIPTION_SEEN_USD:
        raise RuntimeError(f"צפוי עומס של {est:.0f}$ במחירון ({basis}), והמנוי הפסיק לענות אחרי כ-{SUBSCRIPTION_SEEN_USD:.0f}$ "
                           "ב-25.10: הריצה צפויה להיעצר באמצע ולחכות להמשך")
    return f"צפוי עומס של {est:.0f}$ במחירון, מתחת ל-{SUBSCRIPTION_SEEN_USD:.0f}$ שבהם המנוי נעצר ב-25.10"


OFFLINE: list[tuple[str, Callable[[], str]]] = [("Playbook", playbook), ("כרטיסים", cards), ("policy", policy)]
LIVE: list[tuple[str, Callable[[], str]]] = [("מפתחות", keys), ("מסד נתונים", database), ("Claude", claude),
                                             ("Yahoo", yahoo), ("FRED", fred), ("SEC", sec), ("roic.ai", roic),
                                             ("FMP", fmp), ("מחיר שני", second_price), ("חדשות", news), ("מקרו חודשי", macro_sources), ("עלות צפויה", cost_estimate),
                                             ("עומס מנוי", subscription)]


def run(offline: bool = False) -> list[Result]:
    return [_run(n, f) for n, f in OFFLINE + ([] if offline else LIVE)]


def text(results: list[Result], title: str = "בדיקת מוכנות") -> str:
    ok = all(r.ok for r in results)
    head = f"{'✅' if ok else '❌'} {title}: " + ("הכל תקין" if ok else f"{sum(not r.ok for r in results)} בדיקות נכשלו")
    return "\n".join([head] + [f"{'✓' if r.ok else '✗'} {r.name}: {r.detail}" for r in results])


def main(offline: bool = False, notify: bool = False) -> int:
    results = run(offline)
    out = text(results)
    print(out, flush=True)
    if notify:
        from qt.telegram import Telegram
        bot = Telegram.from_env()
        if bot is not None:
            bot.send(out)
    return 0 if all(r.ok for r in results) else 1


def as_dict(results: list[Result]) -> list[dict[str, Any]]:
    return [vars(r) for r in results]
