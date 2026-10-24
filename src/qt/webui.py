"""The dashboard's look (qt.web), as an app (Ilay, 24.10: "if you built an app for users it would not look like
this"): four tabs (today, ideas, portfolio, research), a status dot up top that opens the system's page and turns
red when something failed, and cards that read at a glance. Plain HTML and CSS, no build step; the live part of a
page is swapped in by a small script every REFRESH_S seconds while the tab is visible.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

IL = ZoneInfo("Asia/Jerusalem")
REFRESH_S = 30

# Assistant for the app; the research report and the articles keep their serif headings (Frank Ruhl Libre).
FONTS = ("<link rel=preconnect href='https://fonts.gstatic.com' crossorigin><link rel=stylesheet "
         "href='https://fonts.googleapis.com/css2?family=Assistant:wght@400;500;600;700;800&family=Frank+Ruhl+Libre:wght@400;600;700"
         "&family=IBM+Plex+Mono:wght@400;600;700&display=swap'>")

_DARK = """--bg:#0e1419;--surface:#172028;--soft:#1f2a33;--fg:#e8edf2;--muted:#9eabb8;--line:#2a3743;--accent:#52c4c6;
--accent-soft:#16363a;--ink-strong:#0f2a2c;--ok:#4ade80;--ok-soft:#173726;--warn:#f5b041;--warn-soft:#3a2c13;--bad:#f87171;
--bad-soft:#3d1d1b;--on-accent:#0b1a1c;--shadow:none;color-scheme:dark"""

CSS = """:root{--bg:#f4f5f7;--surface:#fff;--soft:#eef0f2;--fg:#111827;--muted:#5b6472;--line:#e3e6ea;--accent:#0b6e75;
--accent-soft:#e6f1f1;--ink-strong:#0f2a2c;--ok:#15803d;--ok-soft:#e4f4ea;--warn:#9a4a00;--warn-soft:#fdf1e3;--bad:#b91c1c;
--bad-soft:#fbe7e7;--on-accent:#fff;--shadow:0 1px 2px rgba(17,24,39,.06);color-scheme:light;
--display:"Frank Ruhl Libre","David Libre",Georgia,serif;--body:"Assistant",system-ui,sans-serif;--mono:"IBM Plex Mono",ui-monospace,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){%(dark)s}}
:root[data-theme="dark"]{%(dark)s}
*{box-sizing:border-box}
html{scroll-behavior:smooth;scroll-padding-top:120px}
body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--body);font-size:16px;line-height:1.55;direction:rtl}
a{color:var(--accent)}p{margin:0;max-width:72ch}
h1,h2,h3{font-family:var(--body);line-height:1.25;margin:0;text-wrap:balance}
h1{font-size:1.85rem;font-weight:800;letter-spacing:-.01em}
h2{font-size:1.08rem;font-weight:700;display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
h2 .q{font-weight:600;font-size:.85rem;color:var(--muted)}h3{font-size:1rem;font-weight:700}
code,.mono{font-family:var(--mono);font-size:.86em;direction:ltr;unicode-bidi:isolate}
.appbar{position:sticky;top:0;z-index:10;background:var(--surface);border-bottom:1px solid var(--line)}
.appbar .in{max-width:1120px;margin:0 auto;height:60px;display:flex;align-items:center;gap:28px;padding:0 20px}
.brand{font-weight:800;font-size:1.05rem;color:var(--accent);text-decoration:none;white-space:nowrap}
nav.tabs{display:flex;gap:4px;flex:1}
nav.tabs a{text-decoration:none;color:var(--fg);padding:7px 14px;border-radius:10px;font-weight:600;font-size:.95rem;
display:flex;align-items:center;gap:6px}
nav.tabs a:hover{background:var(--soft)}
nav.tabs a.on{background:var(--accent-soft);color:var(--accent);font-weight:700}
nav.tabs svg{display:none;width:24px;height:24px;fill:none;stroke:currentColor;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}
.health{display:flex;align-items:center;gap:8px;border:1px solid var(--line);border-radius:999px;padding:6px 14px;min-height:40px;
color:var(--fg);font-size:.88rem;font-weight:600;text-decoration:none;white-space:nowrap;background:var(--surface)}
.health:hover{border-color:var(--accent)}
.health.on{border-color:var(--accent);color:var(--accent)}
.wrap{max-width:1120px;margin:0 auto;padding:24px 20px 64px;display:flex;flex-direction:column;gap:20px}
.wrap>*,.two>*,.today>*{min-width:0}
.head{display:flex;flex-direction:column;gap:4px}
.sub{color:var(--muted);margin:0;font-size:.95rem}
.eyebrow{font-size:.8rem;color:var(--muted);display:flex;align-items:center;gap:8px}
.pulse{width:8px;height:8px;border-radius:50%%;background:var(--ok);animation:p 2.4s ease-in-out infinite}
.pulse.off{background:var(--bad);animation:none}
@keyframes p{0%%,100%%{opacity:1}50%%{opacity:.3}}
@media (prefers-reduced-motion:reduce){.pulse{animation:none}html{scroll-behavior:auto}}
section{display:flex;flex-direction:column;gap:12px;min-width:0}
.panel,.verdict,.card{background:var(--surface);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow)}
.panel{padding:16px 18px;display:flex;flex-direction:column;gap:12px}
.panel>.ph{display:flex;justify-content:space-between;align-items:center;gap:10px}
.panel>.ph a{font-size:.9rem;font-weight:600;text-decoration:none}
.verdict{padding:16px 18px;display:flex;flex-direction:column;gap:8px}
.verdict strong{color:var(--accent)}
.banner{border-radius:16px;padding:14px 18px;display:flex;flex-direction:column;gap:8px;border:1px solid}
.banner.bad{background:var(--bad-soft);border-color:var(--bad);color:var(--fg)}
.banner.warn{background:var(--warn-soft);border-color:var(--warn);color:var(--fg)}
.banner b{font-size:1.02rem}.banner ul{gap:4px}
.banner .acts{margin-top:2px}
.strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.cell{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:12px 14px;display:flex;
flex-direction:column;gap:2px;min-width:0;box-shadow:var(--shadow)}
.cell b{font-size:1rem;font-weight:700}.cell .big{font-size:1.3rem;font-weight:800;font-variant-numeric:tabular-nums}
.cell>span{font-size:.84rem;color:var(--muted)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px}
.stat{background:var(--soft);border-radius:12px;padding:8px 12px;display:flex;flex-direction:column;min-width:0}
.stat>span{font-size:.78rem;color:var(--muted)}.stat b{font-size:1rem;font-variant-numeric:tabular-nums}
.money{font-size:2rem;font-weight:800;font-variant-numeric:tabular-nums;direction:ltr;text-align:right;line-height:1.15}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%%;margin-inline-end:6px;vertical-align:middle;background:var(--muted);flex:none}
.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}.dot.bad{background:var(--bad)}
.tbl{max-width:100%%;overflow-x:auto;border:1px solid var(--line);border-radius:14px;background:var(--surface)}
table{border-collapse:collapse;width:100%%;font-size:.92rem;min-width:520px}
th,td{text-align:right;padding:9px 12px;border-bottom:1px solid var(--soft);vertical-align:top}
th{font-weight:700;font-size:.78rem;color:var(--muted);background:var(--bg);white-space:nowrap}
tr:last-child td{border-bottom:0}
.num,td.t{white-space:nowrap;font-variant-numeric:tabular-nums}.num{text-align:left;direction:ltr}
.pill{display:inline-block;font-size:.75rem;font-weight:700;padding:1px 9px;border-radius:999px;white-space:nowrap;
background:var(--accent-soft);color:var(--accent);line-height:1.6}
.pill.ok{background:var(--ok-soft);color:var(--ok)}.pill.warn{background:var(--warn-soft);color:var(--warn)}
.pill.bad{background:var(--bad-soft);color:var(--bad)}.pill.mute{background:var(--soft);color:var(--muted)}
.up{color:var(--ok)!important}.down{color:var(--bad)!important}
.timeline{list-style:none;margin:0 6px 0 0;padding:0;border-inline-start:2px solid var(--line);display:flex;flex-direction:column}
.timeline li{position:relative;padding:0 18px 16px 0}
.timeline li::before{content:"";position:absolute;right:-7px;top:.5em;width:12px;height:12px;border-radius:50%%;
background:var(--surface);border:2px solid var(--accent)}
.timeline li.bad::before{border-color:var(--bad)}.timeline li.warn::before{border-color:var(--warn)}
.timeline .d{font-family:var(--mono);font-size:.76rem;color:var(--muted);display:block;direction:ltr;text-align:right}
.cards{display:flex;flex-direction:column;gap:10px}
.card{padding:14px 16px;display:flex;flex-direction:column;gap:8px}
.card .facts{display:grid;grid-template-columns:repeat(auto-fill,minmax(92px,1fr));gap:6px 12px;font-size:.9rem}
.card .facts span{display:flex;flex-direction:column;font-size:.78rem;color:var(--muted)}
.card .facts span b{font-size:1rem;color:var(--fg);font-variant-numeric:tabular-nums}
.acts{display:flex;flex-wrap:wrap;gap:8px}form{display:inline;margin:0}
button{font:inherit;font-size:.92rem;font-weight:600;min-height:40px;padding:6px 16px;border-radius:12px;border:1px solid var(--line);
background:var(--surface);color:var(--fg);cursor:pointer}
button:hover{border-color:var(--accent)}
button.go{background:var(--accent);border-color:var(--accent);color:var(--on-accent);font-weight:700;min-height:44px;padding:6px 22px}
.rows{display:flex;flex-direction:column}
.row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid var(--soft);
color:var(--fg);text-decoration:none;min-height:48px}
.row:last-child{border-bottom:0}.row:hover b{color:var(--accent)}
.row .l{display:flex;flex-direction:column;min-width:0}.row .l>span{font-size:.8rem;color:var(--muted)}
.row .r{display:flex;flex-direction:column;align-items:flex-end;text-align:left;font-variant-numeric:tabular-nums;flex:none}
.row .r>span{font-size:.8rem;color:var(--muted)}
.prog{height:6px;border-radius:3px;background:var(--soft);overflow:hidden;display:flex}
.prog i{display:block;border-radius:3px;background:var(--accent)}
.prog i.ok{background:var(--ok)}.prog i.warn{background:var(--warn)}.prog i.mute{background:var(--muted)}
.ends{display:flex;justify-content:space-between;font-size:.78rem;color:var(--muted);font-variant-numeric:tabular-nums}
.ends b{color:var(--fg)}
.report-card{background:var(--ink-strong);color:#fff;border-radius:16px;padding:16px 18px;display:flex;flex-direction:column;gap:6px;
text-decoration:none}
.report-card span{font-size:.78rem;font-weight:700;color:#8fd3d5}.report-card b{font-size:1.05rem;line-height:1.4}
.report-card i{font-style:normal;font-size:.9rem;color:#cfe9ea;font-weight:600}.report-card .bad{color:#ffb4b4}
.bars{display:flex;flex-direction:column;gap:6px}
.bars div{display:grid;grid-template-columns:70px 1fr 70px;gap:10px;align-items:center;font-size:.9rem}
.bars i{display:block;height:10px;border-radius:5px;background:var(--accent);min-width:2px}
.note,.muted{font-size:.86rem;color:var(--muted)}
.filters{display:flex;gap:6px;flex-wrap:wrap}
.filters a{min-height:36px;display:flex;align-items:center;padding:0 14px;border-radius:999px;border:1px solid var(--line);
background:var(--surface);color:var(--fg);font-size:.9rem;font-weight:600;text-decoration:none}
.filters a.on{background:var(--fg);border-color:var(--fg);color:var(--surface)}
.wlist{display:flex;flex-direction:column;gap:10px}
.wl-g{font-size:.8rem;font-weight:700;color:var(--muted);padding:6px 4px 0}
details.wl{background:var(--surface);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow);margin:0}
details.wl>summary{list-style:none;cursor:pointer;color:var(--fg);padding:14px 16px 12px 36px;position:relative;display:grid;
grid-template-columns:minmax(0,1fr) auto;gap:6px 14px;align-items:start}
details.wl>summary::-webkit-details-marker{display:none}
details.wl>summary::after{content:"";position:absolute;left:16px;top:21px;width:7px;height:7px;
border-left:2px solid var(--muted);border-bottom:2px solid var(--muted);transform:rotate(45deg);transition:transform .15s}
details.wl[open]>summary::after{transform:rotate(-45deg)}
details.wl>summary:hover b{color:var(--accent)}
.wl-s{min-width:0;display:flex;flex-direction:column}.wl-s>span:first-child{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.wl-s b{font-size:1.1rem;font-weight:800}.wl-s .note{font-size:.8rem}
.wl-p{display:flex;gap:4px;flex-wrap:wrap}
.wl-n{display:flex;flex-direction:column;align-items:flex-end;text-align:left;font-variant-numeric:tabular-nums}
.wl-n b{font-size:1.05rem}.wl-n i{font-style:normal;color:var(--muted);font-size:.78rem}
.wl-d{grid-column:1/-1;display:flex;flex-direction:column;gap:4px}.wl-d span{font-size:.8rem;font-weight:600}
.wl-o{grid-column:1/-1;font-size:.92rem;color:var(--fg);opacity:.85;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
overflow:hidden;max-width:none;line-height:1.5}
details.wl[open] .wl-o{-webkit-line-clamp:unset;opacity:1}
.wl-a{grid-column:1/-1;font-size:.78rem;color:var(--muted)}
.wl-b{padding:2px 16px 16px;display:flex;flex-direction:column;gap:10px;border-top:1px solid var(--soft)}
.wl-b dl{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px 16px;margin:10px 0 0}
.wl-b dl div{min-width:0}.wl-b dt{font-size:.76rem;font-weight:700;color:var(--muted)}
.wl-b dd{margin:0;font-variant-numeric:tabular-nums;font-size:.92rem}
.wl-b h4{margin:0;font-size:.9rem}.wl-go{font-weight:700;text-decoration:none}
details.more>summary,details.fold>summary{cursor:pointer;color:var(--accent);padding:8px 0;font-weight:600}
details.fold{background:var(--surface);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow)}
details.fold>summary{list-style:none;padding:0 18px;min-height:52px;display:flex;align-items:center;justify-content:space-between;
color:var(--fg);font-size:1rem}
details.fold>summary::-webkit-details-marker{display:none}
details.fold>summary::after{content:"‹";color:var(--muted);font-size:1.3rem;transition:transform .15s}
details.fold[open]>summary::after{transform:rotate(-90deg)}
details.fold>.in{padding:4px 18px 18px;display:flex;flex-direction:column;gap:12px}
.empty{background:var(--surface);border:1px dashed var(--line);border-radius:14px;padding:14px 16px;color:var(--muted)}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px}
.today{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,1fr);grid-template-areas:"main side";gap:20px;align-items:start}
.lv:empty{display:none}
.today>.col-main{grid-area:main;display:flex;flex-direction:column;gap:20px}
.today>.col-side{grid-area:side;display:flex;flex-direction:column;gap:20px}
.hero{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap}
.hero h1{font-size:2.1rem}.hero .px{display:flex;flex-direction:column;align-items:flex-end}
.hero .px b{font-size:1.8rem;font-weight:800;font-variant-numeric:tabular-nums}
.brief{display:flex;flex-direction:column;gap:12px}
.brief p.lead{font-size:1.08rem;font-weight:700;line-height:1.5}
.brief div{display:flex;flex-direction:column;gap:2px}
.brief div>span{font-size:.8rem;font-weight:700;color:var(--accent)}.brief div.wrong>span{color:var(--bad)}
pre{white-space:pre-wrap;word-break:break-word;background:var(--surface);border:1px solid var(--line);border-radius:12px;
padding:12px;font-family:var(--mono);font-size:.8rem;direction:ltr;text-align:left;margin:0}
.flow{display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:.92rem}
.flow span{background:var(--accent-soft);padding:5px 11px;border-radius:8px}.flow i{font-style:normal;color:var(--muted)}
.steps{counter-reset:s;list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:10px}
.steps li{counter-increment:s;display:grid;grid-template-columns:34px 1fr;gap:12px;background:var(--surface);
border:1px solid var(--line);border-radius:14px;padding:14px 16px}
.steps li::before{content:counter(s);font-weight:800;color:var(--accent);font-size:1.3rem;line-height:1.2}
.steps li div{min-width:0;display:flex;flex-direction:column;gap:4px}.who{font-size:.8rem;color:var(--muted)}
ul{margin:0;padding-inline-start:1.3em;display:flex;flex-direction:column;gap:8px}
.failed{color:var(--bad)}
.rail{position:sticky;top:60px;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);display:flex;gap:4px;
overflow-x:auto;padding:8px 0}
.rail a{white-space:nowrap;text-decoration:none;color:var(--muted);padding:4px 10px;border-radius:8px;font-size:.88rem}
.rail a:hover{background:var(--accent-soft);color:var(--fg)}
footer{font-size:.82rem;color:var(--muted);border-top:1px solid var(--line);padding-top:14px}
@media (max-width:860px){.today{display:flex;flex-direction:column;align-items:stretch;gap:16px}.today>.col-main,.today>.col-side{display:contents}.o1{order:1}.o2{order:2}.o3{order:3}.o4{order:4}.o5{order:5}.o6{order:6}}
@media (max-width:640px){
body{font-size:15px}
html{scroll-padding-top:70px}
.appbar .in{height:56px;padding:0 max(16px,env(safe-area-inset-right)) 0 max(16px,env(safe-area-inset-left));gap:12px;
justify-content:space-between}
.appbar{padding-top:env(safe-area-inset-top)}
.wrap{padding:16px max(16px,env(safe-area-inset-right)) calc(96px + env(safe-area-inset-bottom)) max(16px,env(safe-area-inset-left));gap:16px}
h1{font-size:1.6rem}.hero h1{font-size:1.8rem}
nav.tabs{position:fixed;bottom:0;left:0;right:0;z-index:10;gap:0;justify-content:space-around;background:var(--surface);
border-top:1px solid var(--line);padding:6px 4px calc(6px + env(safe-area-inset-bottom));box-shadow:0 -2px 12px rgba(0,0,0,.06)}
nav.tabs a{flex:1;flex-direction:column;gap:2px;border-radius:10px;padding:4px 2px;font-size:.72rem;color:var(--muted);
min-height:50px;justify-content:center}
nav.tabs a.on{background:none;color:var(--accent)}
nav.tabs svg{display:block}
.health{padding:6px 12px;font-size:.82rem}
.panel,.verdict{padding:14px 16px}
.money{font-size:1.75rem}
.cell .big{font-size:1.15rem}
.acts button{flex:1}.acts form{display:flex;flex:1}
.tbl{border:0;background:none;overflow:visible}
table,table tbody,table tr,table td{display:block;min-width:0;width:100%%}
table{font-size:.92rem}
table tr:first-child{display:none}
table tr{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:4px 14px;margin-bottom:10px}
table td{display:flex;justify-content:space-between;align-items:baseline;gap:14px;padding:7px 0;border-bottom:1px dashed var(--line);
text-align:left;white-space:normal}
table td.num{flex-direction:row-reverse}
table td::before{content:attr(data-label);font-size:.78rem;font-weight:700;color:var(--muted);text-align:right;flex:none;max-width:45%%}
table td:first-child{font-weight:700;font-size:1.02rem;border-bottom:1px solid var(--line)}
table td:last-child{border-bottom:0}
table td:empty{display:none}
.bars div{grid-template-columns:56px 1fr 60px}
details.wl>summary{padding:12px 14px 10px 32px}
.wl-b dl{grid-template-columns:1fr 1fr}
.two{grid-template-columns:1fr}
}
@media (max-width:380px){.health span:last-child{display:none}}
""" % {"dark": _DARK}

# Live refresh (runtime plan 7.1): the page re-reads its own fragment every REFRESH_S seconds while it is visible,
# and swaps in each live part ([data-live]), keeping open the rows a person opened, so positions, the queue and the
# runs stay current without a reload. The status dot up top is a live part on every page.
# The parts that change once a week (the research report) are not in the fragment and are not re-fetched.
LIVE_JS = """<script>(function(){var st=document.getElementById('stamp'),pu=document.getElementById('pulse');
function tick(){if(document.visibilityState!=='visible')return;
var u=location.pathname+(location.search?location.search+'&':'?')+'fragment=1';
fetch(u,{credentials:'same-origin',cache:'no-store'}).then(function(r){if(!r.ok)throw r.status;return r.text()})
.then(function(t){var d=new DOMParser().parseFromString(t,'text/html'),parts=d.querySelectorAll('[data-live]');
if(!parts.length){document.querySelector('[data-live=main]').innerHTML=t}
parts.forEach(function(n){var el=document.querySelector('[data-live="'+n.getAttribute('data-live')+'"]');if(!el)return;
var o=[].map.call(el.querySelectorAll('details[open][id]'),function(x){return x.id});el.innerHTML=n.innerHTML;
if(n.className)el.className=n.className;
o.forEach(function(i){var x=document.getElementById(i);if(x)x.open=true})});
if(st)st.textContent='עודכן '+new Date().toLocaleTimeString('he-IL');if(pu)pu.className='pulse'})
.catch(function(){if(st)st.textContent='אין חיבור לשרת, מנסה שוב';if(pu)pu.className='pulse off'})}
setInterval(tick,%d*1000);document.addEventListener('visibilitychange',tick)})()</script>""" % REFRESH_S


def scope(css: str, pre: str) -> str:
    """Prefix every rule of a stylesheet with `pre`, so a page made for itself (the research report) can sit inside
    the dashboard without restyling it: body becomes `pre`, its tokens (:root) move onto `pre` so they do not
    repaint the app around it, html and body::before go."""
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
            if sel == ":root":
                sels.append(pre)
            elif sel.startswith(":root"):
                sels.append(f"{sel} {pre}")
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
    from qt.research.magazine import CSS as MAG
    from qt.research.report import CSS as REPORT
    return (scope(REPORT + MAG, ".rep") + ".rep{background:none;padding:0;font-size:15.5px;color:var(--ink)}"
            ".rep h2{border-top:2px solid var(--ink);font-family:var(--display)}"
            "@media (max-width:640px){.rep .tbl{overflow-x:auto}.rep table{display:table;width:100%}"
            ".rep thead{display:table-header-group}.rep tbody{display:table-row-group}"
            ".rep tr,.rep table tr:first-child{display:table-row;background:none;border:0;padding:0;margin:0}"
            ".rep td,.rep th{display:table-cell;width:auto;border-bottom:1px solid var(--line);padding:6px 8px;"
            "text-align:right;font-family:var(--body);font-size:inherit}.rep td::before{content:none}}")


NAV = (("/", "היום"), ("/ideas", "רעיונות"), ("/positions", "תיק"), ("/research", "מחקר"))

# Tab icons (24px line icons, currentColor); on a phone the tabs are a bar at the bottom, like an app.
_ICON = {"/": "<path d='M4 11l8-7 8 7v9h-5v-6H9v6H4z'/>",
         "/ideas": "<path d='M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z'/><circle cx='12' cy='12' r='3'/>",
         "/positions": "<path d='M4 19V9m6 10V5m6 14v-7m4 7H2'/>",
         "/research": "<path d='M6 3h10l3 3v15H6zM9 9h7M9 13h7M9 17h4'/>"}

# The installed app (22.10): manifest, icons, theme colour, and the service worker (static/sw.js).
APP_HEAD = ("<meta name=theme-color content='#ffffff' media='(prefers-color-scheme: light)'>"
            "<meta name=theme-color content='#172028' media='(prefers-color-scheme: dark)'>"
            "<link rel=manifest href='/manifest.webmanifest'><link rel=icon href='/favicon.png' type='image/png'>"
            "<link rel=apple-touch-icon href='/apple-touch-icon.png'><meta name=apple-mobile-web-app-capable content=yes>"
            "<meta name=mobile-web-app-capable content=yes><meta name=apple-mobile-web-app-title content=Quantum>"
            "<meta name=apple-mobile-web-app-status-bar-style content=default>")
SW_JS = ("<script>if('serviceWorker' in navigator)addEventListener('load',function(){"
         "navigator.serviceWorker.register('/sw.js').catch(function(){})})</script>")


def e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def health_link(health: tuple[str, str] | None, on: bool = False) -> str:
    """The status dot up top: green when all is well, red when a run failed (the reason in words); opens /system."""
    state, text = health or ("", "מצב המערכת")
    return (f"<a href='/system' data-live=health class='health{' on' if on else ''}' aria-label='מצב המערכת: {e(text)}'>"
            f"<span class='dot {e(state)}'></span><span>{e(text)}</span></a>")


def page(title: str, body: str, active: str = "", live: bool = False, sub: str = "",
         rail: Iterable[tuple[str, str]] = (), css: str = "", health: tuple[str, str] | None = None,
         head: bool = True) -> str:
    """The whole document: the app bar (brand, the four tabs, the status dot), the title, the section rail, then the
    body. A live page refreshes the parts marked [data-live] (the whole body when none is marked)."""
    nav = "".join(f"<a href='{h}'{' class=on aria-current=page' if h == active else ''}>"
                  f"<svg viewBox='0 0 24 24' aria-hidden=true>{_ICON[h]}</svg><span>{t}</span></a>" for h, t in NAV)
    stamp = (f"<div class=eyebrow><span id=pulse class=pulse></span><span id=stamp>מתעדכן כל {REFRESH_S} שניות</span>"
             f"</div>" if live else "")
    if live and "data-live=" not in body:
        body = f"<main data-live=main class=wrap style='padding:0;width:100%'>{body}</main>"
    links = "".join(f"<a href='#{k}'>{e(t)}</a>" for k, t in rail)
    top = (f"<header class=head><h1>{e(title)}</h1>" + (f"<p class=sub>{sub}</p>" if sub else "") + stamp + "</header>"
           if head else stamp)
    return (f"<!doctype html><html lang=he dir=rtl><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1,viewport-fit=cover'><title>{e(title)}</title>"
            f"{APP_HEAD}{FONTS}<style>{CSS}{css}</style><body><div class=appbar><div class=in>"
            f"<a class=brand href='/'>Quantum Trader</a><nav class=tabs aria-label='ניווט ראשי'>{nav}</nav>"
            f"{health_link(health, active == '/system')}</div></div><div class=wrap>{top}"
            + (f"<div class=rail role=navigation aria-label='חלקי הדף'>{links}</div>" if links else "")
            + f"{body}<footer>תיק נייר בלבד. שום פקודה לא נשלחת ל-IBKR. מחירים מ-Yahoo בעיכוב.</footer></div>"
            + (LIVE_JS if live else "") + SW_JS + "</body></html>")


def offline() -> str:
    """What the installed app shows when the server cannot be reached (cached by the service worker)."""
    return (f"<!doctype html><html lang=he dir=rtl><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1,viewport-fit=cover'><title>אין חיבור</title>"
            f"{APP_HEAD}<style>{CSS}</style><body><div class=appbar><div class=in><span class=brand>Quantum Trader</span>"
            f"</div></div><div class=wrap><header class=head><h1>אין חיבור לשרת</h1></header><div class=verdict><p>לא הצלחתי "
            f"להגיע ללוח הבקרה. ייתכן שאין רשת, או שהשירות ב-Render עולה מחדש אחרי פריסה. שום פקודה לא נשלחה.</p>"
            f"<div class=acts><button class=go onclick='location.reload()'>נסה שוב</button></div></div></div></body></html>")


def section(title: str, body: str, tag: str = "", id: str = "") -> str:
    return f"<section{f' id={id}' if id else ''}><h2>{title}{f' <span class=q>{tag}</span>' if tag else ''}</h2>{body}</section>"


def panel(title: str, body: str, link: tuple[str, str] | None = None, id: str = "", label: str = "") -> str:
    """A card with a heading (and a link at its end): the building block of the app's screens."""
    a = f"<a href='{link[0]}'>{link[1]}</a>" if link else ""
    return (f"<section class=panel{f' id={id}' if id else ''}{f' aria-label={chr(39)}{e(label)}{chr(39)}' if label else ''}>"
            f"<div class=ph><h2>{title}</h2>{a}</div>{body}</section>")


def fold(title: str, body: str, open: bool = False, id: str = "") -> str:
    """A part of a page that opens on a tap (the full article, the news, the numbers behind a card)."""
    return (f"<details class=fold{f' id={id}' if id else ''}{' open' if open else ''}><summary>{title}</summary>"
            f"<div class=in>{body}</div></details>")


def prog(pct: float | None, cls: str = "") -> str:
    """A thin progress bar, 0–100."""
    if pct is None:
        return ""
    return f"<div class=prog aria-hidden=true><i class='{cls}' style='width:{max(0.0, min(100.0, pct)):.0f}%'></i></div>"


def plain(text: Any) -> str:
    """A status line from the runs without its leading emoji (the Telegram messages keep theirs)."""
    return re.sub(r"^[^\w֐-׿(]+", "", "" if text is None else str(text)).strip()


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
