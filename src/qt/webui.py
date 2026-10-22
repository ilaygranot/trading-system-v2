"""The dashboard's look (qt.web): one reading column, a status strip up top, then sections, in the style of the
status page Ilay liked (22.10). Plain HTML and CSS, no build step; the live part of a page is swapped in by a small
script every REFRESH_S seconds while the tab is visible.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

IL = ZoneInfo("Asia/Jerusalem")
REFRESH_S = 30

FONTS = ("<link rel=preconnect href='https://fonts.gstatic.com' crossorigin><link rel=stylesheet "
         "href='https://fonts.googleapis.com/css2?family=Assistant:wght@400;500;600;700&family=Frank+Ruhl+Libre:wght@400;600;700"
         "&family=IBM+Plex+Mono:wght@400;600;700&display=swap'>")

_DARK = """--bg:#0f151b;--surface:#162029;--fg:#e4eaf0;--muted:#97a4b2;--line:#26333f;--accent:#4cc2c4;--accent-soft:#16363a;
--ok:#4cc27e;--ok-soft:#173726;--warn:#f0a63a;--warn-soft:#3a2c13;--bad:#f07167;--bad-soft:#3d1d1b;color-scheme:dark"""

CSS = """:root{--bg:#f6f7f9;--surface:#fff;--fg:#15202b;--muted:#5b6876;--line:#dfe3e8;--accent:#0b6e75;--accent-soft:#e3f1f1;
--ok:#1f8a4c;--ok-soft:#e2f3e8;--warn:#b86e00;--warn-soft:#fbefd9;--bad:#c23b32;--bad-soft:#f9e1df;color-scheme:light;
--display:"Frank Ruhl Libre","David Libre",Georgia,serif;--body:"Assistant",system-ui,sans-serif;--mono:"IBM Plex Mono",ui-monospace,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){%(dark)s}}
:root[data-theme="dark"]{%(dark)s}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--body);font-size:16px;line-height:1.6;direction:rtl}
.wrap{max-width:1000px;margin:0 auto;padding:20px 16px 64px;display:flex;flex-direction:column;gap:36px}
h1,h2,h3{font-family:var(--display);line-height:1.25;margin:0;text-wrap:balance}
h1{font-size:2rem}h2{font-size:1.5rem;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;padding-top:.6rem;
border-top:2px solid var(--fg)}
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
.rail{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);display:flex;gap:4px;
overflow-x:auto;padding:8px 0;margin-top:-20px}
.rail a{white-space:nowrap;text-decoration:none;color:var(--muted);padding:4px 10px;border-radius:4px;font-size:.9rem}
.rail a:hover{background:var(--accent-soft);color:var(--fg)}
html{scroll-behavior:smooth;scroll-padding-top:56px}
.sub{color:var(--muted);margin:0}
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
nav svg{display:none;width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
@media (max-width:640px){
body{font-size:15px}
.wrap{padding:calc(12px + env(safe-area-inset-top)) max(16px,env(safe-area-inset-right)) calc(96px + env(safe-area-inset-bottom)) max(16px,env(safe-area-inset-left));gap:28px}
h1{font-size:1.5rem}
nav{position:fixed;bottom:0;left:0;right:0;z-index:10;flex-wrap:nowrap;gap:0;justify-content:space-around;
background:var(--surface);border-top:1px solid var(--line);padding:6px 4px calc(6px + env(safe-area-inset-bottom));
box-shadow:0 -2px 12px rgba(0,0,0,.06)}
nav a{flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;border:0;background:none;border-radius:10px;
padding:4px 2px;font-size:.72rem;color:var(--muted);min-height:48px;justify-content:center}
nav a.on{background:var(--accent-soft);color:var(--accent);font-weight:700}
nav svg{display:block}
.verdict{padding:14px 16px;font-size:1rem}
.cell .big{font-size:1.15rem}
button{min-height:40px;padding:6px 14px}button.go{min-height:44px}
.acts button{flex:1}.acts form{display:flex;flex:1}
.tbl{border:0;background:none;overflow:visible}
table,table tbody,table tr,table td{display:block;min-width:0;width:100%%}
table{font-size:.92rem}
table tr:first-child{display:none}
table tr{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:4px 14px;margin-bottom:10px}
table td{display:flex;justify-content:space-between;align-items:baseline;gap:14px;padding:7px 0;border-bottom:1px dashed var(--line);
text-align:left;white-space:normal}
table td.num{flex-direction:row-reverse}
table td::before{content:attr(data-label);font-size:.78rem;font-weight:700;color:var(--muted);text-align:right;flex:none;max-width:45%%}
table td:first-child{font-family:var(--display);font-size:1.02rem;border-bottom:1px solid var(--line)}
table td:last-child{border-bottom:0}
table td:empty{display:none}
.bars div{grid-template-columns:56px 1fr 60px}
.two{grid-template-columns:1fr}
}
@media (display-mode:standalone){.brand{padding-top:2px}}
""" % {"dark": _DARK}

# Live refresh (runtime plan 7.1): the page re-reads its own fragment every REFRESH_S seconds while it is visible,
# and swaps in each live part ([data-live]), so positions, the queue and the runs stay current without a reload.
# The parts that change once a week (the research report) are not in the fragment and are not re-fetched.
LIVE_JS = """<script>(function(){var st=document.getElementById('stamp'),pu=document.getElementById('pulse');
function tick(){if(document.visibilityState!=='visible')return;
var u=location.pathname+(location.search?location.search+'&':'?')+'fragment=1';
fetch(u,{credentials:'same-origin',cache:'no-store'}).then(function(r){if(!r.ok)throw r.status;return r.text()})
.then(function(t){var d=new DOMParser().parseFromString(t,'text/html'),parts=d.querySelectorAll('[data-live]');
if(!parts.length){document.querySelector('[data-live=main]').innerHTML=t}
parts.forEach(function(n){var el=document.querySelector('[data-live="'+n.getAttribute('data-live')+'"]');if(el)el.innerHTML=n.innerHTML});
st.textContent='עודכן '+new Date().toLocaleTimeString('he-IL');pu.className='pulse'})
.catch(function(){st.textContent='אין חיבור לשרת, מנסה שוב';pu.className='pulse off'})}
setInterval(tick,%d*1000);document.addEventListener('visibilitychange',tick)})()</script>""" % REFRESH_S


def scope(css: str, pre: str) -> str:
    """Prefix every rule of a stylesheet with `pre`, so a page made for itself (the research report) can sit inside
    the dashboard without restyling it: body becomes `pre`, :root (the tokens) stays, html and body::before go."""
    out, i = [], 0
    while True:
        j = css.find("{", i)
        if j < 0:
            break
        head = css[i:j].strip()
        if head.startswith("@media") or head.startswith("@supports"):
            depth, k = 1, j + 1
            while depth:
                depth += {"{": 1, "}": -1}.get(css[k], 0)
                k += 1
            out.append(f"{head}{{{scope(css[j + 1:k - 1], pre)}}}")
            i = k
            continue
        k = css.index("}", j)
        sels = []
        for sel in head.split(","):
            sel = sel.strip()
            if sel.startswith(":root"):
                sels.append(sel)
            elif sel == "body":
                sels.append(pre)
            elif sel not in ("html", "body::before") and not sel.startswith("@"):
                sels.append(f"{pre} {sel}")
        if sels:
            out.append(f"{','.join(sels)}{{{css[j + 1:k]}}}")
        i = k + 1
    return "".join(out)


def report_css() -> str:
    """The research report's stylesheet inside .rep, with its tables kept as tables on a phone (they scroll)."""
    from qt.research.report import CSS as REPORT
    return (scope(REPORT, ".rep") + ".rep{background:none;padding:0;font-size:15.5px}"
            ".rep h2{border-top:2px solid var(--ink)}"
            "@media (max-width:640px){.rep .tbl{overflow-x:auto}.rep table{display:table;width:100%}"
            ".rep thead{display:table-header-group}.rep tbody{display:table-row-group}"
            ".rep tr,.rep table tr:first-child{display:table-row;background:none;border:0;padding:0;margin:0}"
            ".rep td,.rep th{display:table-cell;width:auto;border-bottom:1px solid var(--line);padding:6px 8px;"
            "text-align:right;font-family:var(--body);font-size:inherit}.rep td::before{content:none}}")


NAV = (("/", "סקירה"), ("/positions", "פוזיציות"), ("/runs", "ריצות"), ("/journal", "יומן"), ("/system", "מצב המערכת"))

# Tab icons for the phone's bottom bar (24px line icons, currentColor).
_ICON = {"/": "<path d='M4 11l8-7 8 7v9h-5v-6H9v6H4z'/>",
         "/positions": "<path d='M4 19V9m6 10V5m6 14v-7m4 7H2'/>",
         "/runs": "<circle cx='12' cy='12' r='8'/><path d='M12 8v4l3 2'/>",
         "/journal": "<path d='M6 3h10l3 3v15H6zM9 9h7M9 13h7M9 17h4'/>",
         "/system": "<path d='M3 12h4l3-7 4 14 3-7h4'/>"}

# The installed app (22.10): manifest, icons, theme colour, and the service worker (static/sw.js).
APP_HEAD = ("<meta name=theme-color content='#0f5f73' media='(prefers-color-scheme: light)'>"
            "<meta name=theme-color content='#0f161c' media='(prefers-color-scheme: dark)'>"
            "<link rel=manifest href='/manifest.webmanifest'><link rel=icon href='/favicon.png' type='image/png'>"
            "<link rel=apple-touch-icon href='/apple-touch-icon.png'><meta name=apple-mobile-web-app-capable content=yes>"
            "<meta name=mobile-web-app-capable content=yes><meta name=apple-mobile-web-app-title content=Quantum>"
            "<meta name=apple-mobile-web-app-status-bar-style content=default>")
SW_JS = ("<script>if('serviceWorker' in navigator)addEventListener('load',function(){"
         "navigator.serviceWorker.register('/sw.js').catch(function(){})})</script>")


def e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def page(title: str, body: str, active: str = "", live: bool = False, nav_q: str = "", sub: str = "",
         rail: Iterable[tuple[str, str]] = (), css: str = "") -> str:
    """The whole document: brand and tabs, the title, the section rail, then the body. A live page refreshes the
    parts marked [data-live] (the whole body when none is marked). On a phone the tabs move to a bar at the bottom,
    like an app."""
    nav = "".join(f"<a href='{h}{nav_q if h == '/journal' else ''}'{' class=on aria-current=page' if h == active else ''}>"
                  f"<svg viewBox='0 0 24 24' aria-hidden=true>{_ICON[h]}</svg><span>{t}</span></a>" for h, t in NAV)
    stamp = (f"<div class=eyebrow><span id=pulse class=pulse></span><span id=stamp>מתעדכן כל {REFRESH_S} שניות</span>"
             f"</div>" if live else "")
    if live and "data-live=" not in body:
        body = f"<main data-live=main class=wrap style='padding:0;width:100%'>{body}</main>"
    links = "".join(f"<a href='#{k}'>{e(t)}</a>" for k, t in rail)
    return (f"<!doctype html><html lang=he dir=rtl><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1,viewport-fit=cover'><title>{e(title)}</title>"
            f"{APP_HEAD}{FONTS}<style>{CSS}{css}</style><body><div class=wrap><header class=top><div class=bar><span class=brand>"
            f"Quantum Trader</span><nav>{nav}</nav></div>{stamp}<h1>{e(title)}</h1>"
            + (f"<p class=sub>{sub}</p>" if sub else "") + "</header>"
            + (f"<div class=rail role=navigation aria-label='חלקי הדף'>{links}</div>" if links else "")
            + f"{body}<footer>תיק נייר בלבד. שום פקודה לא נשלחת ל-IBKR. מחירים מ-Yahoo בעיכוב.</footer></div>"
            + (LIVE_JS if live else "") + SW_JS + "</body></html>")


def offline() -> str:
    """What the installed app shows when the server cannot be reached (cached by the service worker)."""
    return (f"<!doctype html><html lang=he dir=rtl><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1,viewport-fit=cover'><title>אין חיבור</title>"
            f"{APP_HEAD}<style>{CSS}</style><body><div class=wrap><header class=top><span class=brand>Quantum Trader</span>"
            f"<h1>אין חיבור לשרת</h1></header><div class=verdict><p>לא הצלחתי להגיע ללוח הבקרה. ייתכן שאין רשת, או "
            f"שהשירות ב-Render עולה מחדש אחרי פריסה. שום פקודה לא נשלחה.</p>"
            f"<div class=acts><button class=go onclick='location.reload()'>נסה שוב</button></div></div></div></body></html>")


def section(title: str, body: str, tag: str = "", id: str = "") -> str:
    return f"<section{f' id={id}' if id else ''}><h2>{title}{f' <span class=q>{tag}</span>' if tag else ''}</h2>{body}</section>"


def table(heads: Iterable[str], rows: Iterable[Iterable[Any]]) -> str:
    """Cells are HTML already (escape with e())."""
    heads = list(heads)
    th = "".join(f"<th>{h}</th>" for h in heads)
    labels = [e(re.sub(r"<[^>]+>", "", str(h))) for h in heads]                # each cell's label on a phone

    def td(c: Any, i: int) -> str:
        lab = f" data-label='{labels[i]}'" if i < len(labels) else ""
        return f"<td{lab}{str(c)[3:]}" if str(c).startswith("<td") else f"<td{lab}>{c}</td>"
    trs = "".join("<tr>" + "".join(td(c, i) for i, c in enumerate(r)) + "</tr>" for r in rows)
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


def sym(symbol: Any) -> str:
    """A ticker that opens its stock page."""
    return f"<a href='/stock/{e(symbol)}'>{e(symbol)}</a>" if symbol else ""


def data(obj: Any, depth: int = 0) -> str:
    """Any run output (dicts, lists of rows, scalars) as tables a person can read: a list of flat dicts is a table,
    a dict is label / value rows, anything nested goes one level down."""
    if isinstance(obj, dict):
        if not obj:
            return "<span class=note>ריק</span>"
        rows = [[f"<b>{e(LABELS.get(k, k))}</b>", data(v, depth + 1) if isinstance(v, (dict, list)) and depth < 3 else e(_fmt(v))]
                for k, v in obj.items()]
        return table(("שדה", "ערך"), rows)
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            cols = list(dict.fromkeys(k for x in obj for k in x))[:14]
            return table((e(LABELS.get(c, c)) for c in cols), ([e(_fmt(x.get(c))) if not isinstance(x.get(c), (dict, list))
                                                 else data(x.get(c), depth + 1) for c in cols] for x in obj[:200]))
        return e(", ".join(_fmt(x) for x in obj))
    return e(_fmt(obj))


# Hebrew labels for the fields the runs put out (the watchlist and its daily check, most of all)
LABELS = {"symbol": "מניה", "direction": "כיוון", "etf": "סקטור", "bucket": "סטטוס במחקר", "added": "נוסף", "trigger_kind": "סוג טריגר",
          "trigger_level": "טריגר", "k": "k (רוחב עצירה ב-ADR)", "target": "יעד", "target_source": "מקור היעד",
          "invalidate_level": "רמת ביטול", "time_stop": "עצירת זמן", "next_earnings": "דוח הבא", "first_size_pct": "גודל ראשון %",
          "price_at_add": "מחיר בהוספה", "tldr": "בקצרה", "close": "סגירה", "change_1d_pct": "שינוי יומי %",
          "dist_to_trigger_pct": "מרחק לטריגר %", "triggered": "הטריגר הופעל", "invalidated": "בוטל", "expired": "פג",
          "stop": "עצירה", "rr": "יחס סיכוי-סיכון", "rr_ok": "יחס מעל המינימום", "potential_pct": "עד היעד %",
          "potential_at_add_pct": "עד היעד בהוספה %", "days_on_list": "ימים ברשימה", "review_day60": "יום 60: לבדוק",
          "earnings_in_days": "ימים לדוח", "boiling": "רותחת", "status": "מצב", "notes": "הערות", "held": "בתיק",
          "trend_1y_pct": "מגמה שנה %", "trend_3m_pct": "מגמה 3 חודשים %", "since_add_pct": "מאז ההוספה %",
          "sym": "מניה", "profile": "פרופיל", "stock": "רמזור מניה", "sector": "רמזור סקטור", "market": "רמזור שוק",
          "combined": "רמזור משולב", "sources": "מקורות", "rr_extreme": "R:R לשיא", "adr": "ADR %"}


def _fmt(v: Any) -> str:
    if v is True:
        return "כן"
    if v is False:
        return "לא"
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)
