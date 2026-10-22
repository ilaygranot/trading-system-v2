"""The dashboard as an installed app (22.10): manifest, icons and service worker without the login, the app's head
on every page, and table cells labelled for the phone layout."""
from __future__ import annotations

import json

from qt import webui as ui
from test_web_positions import _call, web  # noqa: F401 - the fixture


def test_app_files_are_served_without_the_login(web):
    st, h, body = _call(web, "/manifest.webmanifest", auth=None)
    m = json.loads(body)
    assert st.startswith("200") and h["Content-Type"] == "application/manifest+json"
    assert m["display"] == "standalone" and m["start_url"] == "/" and m["dir"] == "rtl"
    assert {"192x192", "512x512"} <= {i["sizes"] for i in m["icons"]}
    st, h, body = _call(web, "/sw.js", auth=None)
    assert st.startswith("200") and "javascript" in h["Content-Type"] and h["Cache-Control"] == "no-cache"
    assert "/offline" in body and "navigate" in body
    st, _, page = _call(web, "/offline", auth=None)
    assert st.startswith("200") and "אין חיבור" in page


def test_icons_are_png(web):
    import io
    env = {"PATH_INFO": "/icon-512.png", "REQUEST_METHOD": "GET", "QUERY_STRING": "", "wsgi.input": io.BytesIO(b"")}
    out = {}
    body = b"".join(web(env, lambda s, h: out.update(status=s, headers=dict(h))))
    assert out["status"].startswith("200") and out["headers"]["Content-Type"] == "image/png"
    assert body[:8] == b"\x89PNG\r\n\x1a\n"


def test_every_page_carries_the_app_head_and_the_data_stays_behind_the_login(web):
    page = _call(web, "/positions")[2]
    for part in ("rel=manifest", "apple-touch-icon", "theme-color", "serviceWorker", "viewport-fit=cover", "<svg"):
        assert part in page, part
    assert _call(web, "/positions", auth=None)[0].startswith("401")


def test_table_cells_carry_their_label_for_the_phone():
    t = ui.table(("מניה", "<b>רו\"ה</b>"), [["MSFT", "<td class=num>+10</td>"]])
    assert "<td data-label='מניה'>MSFT</td>" in t
    assert "<td data-label='רו&quot;ה' class=num>+10</td>" in t
