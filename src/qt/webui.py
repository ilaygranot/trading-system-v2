"""The dashboard's look (qt.web): one reading column, a status strip up top, then sections, in the style of the
status page Ilay liked (22.10). Plain HTML and CSS, no build step; the live part of a page is swapped in by a small
script every REFRESH_S seconds while the tab is visible.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

IL = ZoneInfo("Asia/Jerusalem")
REFRESH_S = 30

FONTS = ("<link rel=preconnect href='https://fonts.gstatic.com' crossorigin><link rel=stylesheet "
         "href='https://fonts.googleapis.com/css2?family=Rubik:wght@500;700&family=Assistant:wght@400;600;700"
         "&family=IBM+Plex+Mono:wght@400;500&display=swap'>")

_DARK = """--bg:#0f161c;--surface:#16212a;--fg:#e4ebf0;--muted:#94a5b2;--line:#26343f;--accent:#5fb7cc;--accent-soft:#16323b;
--ok:#6cc995;--ok-soft:#173326;--warn:#e2ad5c;--warn-soft:#3a2b14;--bad:#ec8b84;--bad-soft:#3d1e1c;color-scheme:dark"""

CSS = """:root{--bg:#f5f7f8;--surface:#fff;--fg:#17212b;--muted:#5b6b78;--line:#dde3e8;--accent:#0f5f73;--accent-soft:#e3f0f3;
--ok:#1f7a46;--ok-soft:#e4f3ea;--warn:#9a5b00;--warn-soft:#fbf0dc;--bad:#a3302a;--bad-soft:#f8e4e2;color-scheme:light;
--display:"Rubik","Assistant",system-ui,sans-serif;--body:"Assistant",system-ui,sans-serif;--mono:"IBM Plex Mono",ui-monospace,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){%(dark)s}}
:root[data-theme="dark"]{%(dark)s}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--body);font-size:16px;line-height:1.6;direction:rtl}
.wrap{max-width:1000px;margin:0 auto;padding:20px 16px 64px;display:flex;flex-direction:column;gap:36px}
h1,h2,h3{font-family:var(--display);line-height:1.25;margin:0;text-wrap:balance}
h1{font-size:1.85rem}h2{font-size:1.3rem;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
h2 .q{font-family:var(--body);font-weight:600;font-size:.85rem;color:var(--accent)}h3{font-size:1.02rem}
p{margin:0;max-width:72ch}a{color:var(--accent)}
section{display:flex;flex-direction:column;gap:14px;min-width:0}
.wrap>*,.two>*{min-width:0}
code,.mono{font-family:var(--mono);font-size:.86em;direction:ltr;unicode-bidi:isolate}
.top{display:flex;flex-direction:column;gap:10px}
.bar{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between}
.brand{font-family:var(--display);font-weight:700;color:var(--accent);letter-spacing:.02em}
nav{display:flex;flex-wrap:wrap;gap:6px}
nav a{text-decoration:none;color:var(--fg);padding:5px 12px;border-radius:999px;border:1px solid var(--line);
background:var(--surface);font-size:.92rem}
nav a.on{background:var(--accent);border-color:var(--accent);color:var(--surface)}
.eyebrow{font-size:.82rem;color:var(--muted);letter-spacing:.03em;display:flex;align-items:center;gap:8px}
.pulse{width:8px;height:8px;border-radius:50%%;background:var(--ok);animation:p 2.4s ease-in-out infinite}
.pulse.off{background:var(--bad);animation:none}
@keyframes p{0%%,100%%{opacity:1}50%%{opacity:.3}}
@media (prefers-reduced-motion:reduce){.pulse{animation:none}}
.verdict{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:18px 20px;display:flex;
flex-direction:column;gap:8px;font-size:1.08rem}
.verdict strong{color:var(--accent)}
.strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
@media (max-width:520px){.strip{grid-template-columns:1fr 1fr}}
.cell{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:12px 14px;display:flex;
flex-direction:column;gap:2px;min-width:0}
.cell b{font-family:var(--display);font-size:1rem}.cell .big{font-size:1.35rem;font-variant-numeric:tabular-nums}
.cell span{font-size:.86rem;color:var(--muted)}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%%;margin-inline-end:6px;vertical-align:middle;background:var(--line)}
.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}.dot.bad{background:var(--bad)}
.tbl{max-width:100%%;overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--surface)}
table{border-collapse:collapse;width:100%%;font-size:.93rem;min-width:520px}
th,td{text-align:right;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:700;font-size:.8rem;color:var(--muted);letter-spacing:.03em;background:var(--bg);white-space:nowrap}
tr:last-child td{border-bottom:0}
.num,td.t{white-space:nowrap;font-variant-numeric:tabular-nums}.num{text-align:left;direction:ltr}
.pill{display:inline-block;font-size:.76rem;font-weight:700;padding:1px 9px;border-radius:999px;white-space:nowrap;
background:var(--accent-soft);color:var(--accent)}
.pill.ok{background:var(--ok-soft);color:var(--ok)}.pill.warn{background:var(--warn-soft);color:var(--warn)}
.pill.bad{background:var(--bad-soft);color:var(--bad)}.pill.mute{background:var(--bg);color:var(--muted)}
.up{color:var(--ok)}.down{color:var(--bad)}
.timeline{list-style:none;margin:0 6px 0 0;padding:0;border-inline-start:2px solid var(--line);display:flex;flex-direction:column}
.timeline li{position:relative;padding:0 18px 16px 0}
.timeline li::before{content:"";position:absolute;right:-7px;top:.5em;width:12px;height:12px;border-radius:50%%;
background:var(--surface);border:2px solid var(--accent)}
.timeline li.bad::before{border-color:var(--bad)}.timeline li.warn::before{border-color:var(--warn)}
.timeline .d{font-family:var(--mono);font-size:.78rem;color:var(--muted);display:block;direction:ltr;text-align:right}
.cards{display:flex;flex-direction:column;gap:10px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px 16px;display:flex;
flex-direction:column;gap:6px}
.card .facts{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:.92rem}.card .facts span b{font-variant-numeric:tabular-nums}
.acts{display:flex;flex-wrap:wrap;gap:6px}form{display:inline;margin:0}
button{font:inherit;font-size:.88rem;padding:4px 12px;border-radius:6px;border:1px solid var(--line);background:var(--surface);
color:var(--fg);cursor:pointer}
button:hover{border-color:var(--accent)}
button.go{background:var(--accent);border-color:var(--accent);color:var(--surface);font-weight:700;padding:6px 16px}
.bars{display:flex;flex-direction:column;gap:6px}
.bars div{display:grid;grid-template-columns:70px 1fr 70px;gap:10px;align-items:center;font-size:.9rem}
.bars i{display:block;height:10px;border-radius:5px;background:var(--accent);min-width:2px}
.note,.muted{font-size:.88rem;color:var(--muted)}
.empty{background:var(--surface);border:1px dashed var(--line);border-radius:10px;padding:14px 16px;color:var(--muted)}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:24px}
pre{white-space:pre-wrap;word-break:break-word;background:var(--surface);border:1px solid var(--line);border-radius:8px;
padding:12px;font-family:var(--mono);font-size:.8rem;direction:ltr;text-align:left;margin:0}
.flow{display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:.92rem}
.flow span{background:var(--accent-soft);padding:5px 11px;border-radius:6px}.flow i{font-style:normal;color:var(--muted)}
.steps{counter-reset:s;list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:10px}
.steps li{counter-increment:s;display:grid;grid-template-columns:34px 1fr;gap:12px;background:var(--surface);
border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.steps li::before{content:counter(s);font-family:var(--display);font-weight:700;color:var(--accent);font-size:1.3rem;line-height:1.2}
.steps li div{min-width:0;display:flex;flex-direction:column;gap:4px}.who{font-size:.8rem;color:var(--muted)}
ul{margin:0;padding-inline-start:1.3em;display:flex;flex-direction:column;gap:8px}
.failed{color:var(--bad)}
footer{font-size:.84rem;color:var(--muted);border-top:1px solid var(--line);padding-top:14px}
""" % {"dark": _DARK}

# Live refresh (runtime plan 7.1): the page re-reads its own fragment every REFRESH_S seconds while it is visible,
# and swaps it in, so positions, the queue and the runs stay current without a reload.
LIVE_JS = """<script>(function(){var el=document.getElementById('live'),st=document.getElementById('stamp'),
pu=document.getElementById('pulse');
function tick(){if(document.visibilityState!=='visible')return;
var u=location.pathname+(location.search?location.search+'&':'?')+'fragment=1';
fetch(u,{credentials:'same-origin',cache:'no-store'}).then(function(r){if(!r.ok)throw r.status;return r.text()})
.then(function(t){el.innerHTML=t;st.textContent='עודכן '+new Date().toLocaleTimeString('he-IL');pu.className='pulse'})
.catch(function(){st.textContent='אין חיבור לשרת, מנסה שוב';pu.className='pulse off'})}
setInterval(tick,%d*1000);document.addEventListener('visibilitychange',tick)})()</script>""" % REFRESH_S

NAV = (("/", "סקירה"), ("/positions", "פוזיציות"), ("/runs", "ריצות"), ("/journal", "יומן"), ("/system", "מצב המערכת"))


def e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def page(title: str, body: str, active: str = "", live: bool = False, nav_q: str = "") -> str:
    """The whole document: brand and tabs, the title, then the body (inside #live when it refreshes)."""
    nav = "".join(f"<a href='{h}{nav_q if h == '/journal' else ''}'{' class=on' if h == active else ''}>{t}</a>"
                  for h, t in NAV)
    stamp = (f"<div class=eyebrow><span id=pulse class=pulse></span><span id=stamp>מתעדכן כל {REFRESH_S} שניות</span>"
             f"</div>" if live else "")
    main = f"<main id=live class=wrap style='padding:0;width:100%'>{body}</main>" if live else body
    return (f"<!doctype html><html lang=he dir=rtl><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'><title>{e(title)}</title>{FONTS}"
            f"<style>{CSS}</style><body><div class=wrap><header class=top><div class=bar><span class=brand>"
            f"Quantum Trader</span><nav>{nav}</nav></div>{stamp}<h1>{e(title)}</h1></header>{main}"
            f"<footer>תיק נייר בלבד. שום פקודה לא נשלחת ל-IBKR. מחירים מ-Yahoo בעיכוב.</footer></div>"
            + (LIVE_JS if live else "") + "</body></html>")


def section(title: str, body: str, tag: str = "") -> str:
    return f"<section><h2>{title}{f' <span class=q>{tag}</span>' if tag else ''}</h2>{body}</section>"


def table(heads: Iterable[str], rows: Iterable[Iterable[Any]]) -> str:
    """Cells are HTML already (escape with e())."""
    th = "".join(f"<th>{h}</th>" for h in heads)
    trs = "".join("<tr>" + "".join(c if str(c).startswith("<td") else f"<td>{c}</td>" for c in r) + "</tr>"
                  for r in rows)
    return f"<div class=tbl><table><tr>{th}</tr>{trs}</table></div>"


def pill(text: str, cls: str = "") -> str:
    return f"<span class='pill {cls}'>{e(text)}</span>"


def cell(head: str, sub: str = "", state: str | None = None, big: bool = False) -> str:
    dot = f"<span class='dot {state}'></span>" if state is not None else ""
    return f"<div class=cell><b{' class=big' if big else ''}>{dot}{head}</b><span>{sub}</span></div>"


def empty(text: str) -> str:
    return f"<div class=empty>{e(text)}</div>"


def parse(ts: Any) -> datetime | None:
    if not ts:
        return None
    if isinstance(ts, datetime):
        t = ts
    else:
        try:
            t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def when(ts: Any) -> str:
    """Israel time, day.month hh:mm."""
    t = parse(ts)
    return t.astimezone(IL).strftime("%d.%m %H:%M") if t else ""


def ago(ts: Any, now: datetime | None = None) -> str:
    t = parse(ts)
    if t is None:
        return ""
    s = int(((now or datetime.now(timezone.utc)) - t).total_seconds())
    if s < 0:
        m = -s // 60
        return f"בעוד {m} דק'" if m < 60 else f"בעוד {m // 60} שע'" if m < 48 * 60 else f"בעוד {m // 1440} ימים"
    if s < 60:
        return "עכשיו"
    if s < 3600:
        return f"לפני {s // 60} דק'"
    if s < 48 * 3600:
        return f"לפני {s // 3600} שע'"
    return f"לפני {s // 86400} ימים"


def duration(a: Any, b: Any) -> str:
    ta, tb = parse(a), parse(b)
    if not ta or not tb:
        return ""
    s = int((tb - ta).total_seconds())
    return f"{s} שנ'" if s < 90 else f"{s // 60} דק'" if s < 5400 else f"{s // 3600} שע' {s % 3600 // 60} דק'"
