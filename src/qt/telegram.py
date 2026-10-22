"""The Telegram bot (runtime plan 7.1-7.2): one message per queue item with buttons, the daily summary, the
"resume run" button after a failure, and the monthly manual file. It answers only Ilay's chat id.

Item layout (7.2): the numbers come from code, the reasoning from the agent, deviations always stand out, and
every item shows until when it is valid. The details button opens a signed link to the dashboard's item page.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests

from qt.approvals import NotOpen, Queue

API = "https://api.telegram.org"

KIND = {"enter": ("🟢", "כניסה מוצעת"), "add": ("🟢", "הגדלה מוצעת"), "exit": ("🔴", "יציאה"),
        "reduce": ("🟠", "הקטנה"), "sell": ("🟠", "מכירת אופציה"), "tighten_stop": ("🟡", "הידוק סטופ"),
        "block_new_risk": ("⛔", "חסימת סיכון חדש"), "flag": ("🔔", "דגל")}
STATUS = {"standing_order": "✔️ פקודה עומדת (אושרה עם העסקה): נרשמה כעסקת נייר",
          "info": "לידיעה בלבד", "approved": "✅ אושר", "rejected": "❌ נדחה", "postponed": "⏸ נדחה למחר",
          "changed": "🔄 השתנה מאז ההצעה: צריך החלטה מחדש", "expired": "⌛ פג תוקף: נרשם כ\"לא נבדק\""}


def sign(item_id: int, secret: str) -> str:
    return hmac.new(secret.encode(), f"item:{item_id}".encode(), hashlib.sha256).hexdigest()[:24]


def verify(item_id: int, sig: str, secret: str) -> bool:
    return bool(secret) and hmac.compare_digest(sign(item_id, secret), sig or "")


def _ny(ts: str) -> str:
    return datetime.fromisoformat(ts).astimezone(ZoneInfo("America/New_York")).strftime("%d.%m")


def item_text(item: dict) -> str:
    p = item["payload"]
    icon, name = KIND.get(item["kind"], ("•", item["kind"]))
    lines = [f"{icon} {name} · {item['symbol'] or 'תיק'} · {item['rule_id']}  #{item['id']}", p.get("detail", "")]
    nums = []
    if p.get("qty") is not None:
        nums.append(f"כמות {p['qty']:g}")
    if p.get("price") is not None:
        nums.append(f"מחיר {p['price']:g}")
    if item.get("ref_price") is not None and p.get("price") is None:
        nums.append(f"מחיר אחרון {item['ref_price']:g}")
    if nums:
        lines.append(" · ".join(nums))
    ag = p.get("agent")
    if ag:
        lines.append(f"Agent {ag.get('agent', '')}: {ag.get('option')} ({ag.get('confidence', '?')}) · {ag.get('reasoning', '')}")
        for d in ag.get("deviations", []):
            lines.append(f"⚠️ חריגה: {d['rule_id']} (ברירת מחדל) ← J: {d['principle']}, עובדות {', '.join(d['fact_ids'])}")
        if ag.get("status") == "agent_undecided":
            lines.append("⚠️ Agent לא הכריע: נבחרה ברירת המחדל של הכרטיס")
        crit = ag.get("critic") or {}
        lines.append("בודק: עבר ✓" if crit.get("verdict") == "pass" else f"בודק: {crit.get('verdict', 'לא רץ')} ✗")
    if item["status"] in STATUS:
        lines.append(STATUS[item["status"]])
    if item["status"] in ("pending", "postponed", "changed") and item.get("valid_until"):
        lines.append(f"בתוקף עד: פתיחת המסחר {_ny(item['valid_until'])}")
    if item.get("decided_note") and item["status"] in ("changed", "rejected"):
        lines.append(f"הערה: {item['decided_note']}")
    return "\n".join(x for x in lines if x)


def item_buttons(item: dict, link: str | None) -> list[list[dict]] | None:
    row = []
    if item["status"] in ("pending", "postponed", "changed"):
        row = [{"text": "✅ אשר", "callback_data": f"a:{item['id']}"},
               {"text": "❌ דחה", "callback_data": f"r:{item['id']}"},
               {"text": "⏸ מחר", "callback_data": f"p:{item['id']}"}]
    if link:
        row.append({"text": "🔎 פרטים", "url": link})
    return [row] if row else None


@dataclass
class Telegram:
    token: str
    chat_id: str
    http: Any = None

    @classmethod
    def from_env(cls) -> "Telegram | None":
        t, c = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        return cls(t, c) if t and c else None

    def _api(self, method: str, **payload: Any) -> Any:
        http = self.http or requests
        r = http.post(f"{API}/bot{self.token}/{method}", json=payload, timeout=20)
        body = r.json()
        if not body.get("ok"):
            raise RuntimeError(f"telegram {method}: {body.get('description')}")    # never echo the token
        return body["result"]

    def send(self, text: str, buttons: list[list[dict]] | None = None, force_reply: bool = False) -> int:
        payload: dict[str, Any] = {"chat_id": self.chat_id, "text": text[:4096]}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        elif force_reply:
            payload["reply_markup"] = {"force_reply": True}
        return self._api("sendMessage", **payload)["message_id"]

    def edit(self, message_id: int, text: str, buttons: list[list[dict]] | None = None) -> None:
        self._api("editMessageText", chat_id=self.chat_id, message_id=message_id, text=text[:4096],
                  reply_markup={"inline_keyboard": buttons or []})

    def answer(self, callback_id: str, text: str) -> None:
        self._api("answerCallbackQuery", callback_query_id=callback_id, text=text[:200])

    def download(self, file_id: str) -> bytes:
        path = self._api("getFile", file_id=file_id)["file_path"]
        http = self.http or requests
        return http.get(f"{API}/file/bot{self.token}/{path}", timeout=30).content

    def set_webhook(self, url: str, secret: str) -> None:
        self._api("setWebhook", url=url, secret_token=secret, allowed_updates=["message", "callback_query"])


def webhook_token(secret: str) -> str:
    """Telegram takes only A-Z, a-z, 0-9, _ and - in a webhook secret (and our /telegram/<x> route the same);
    Render's generateValue is base64, so anything else is replaced by its sha256."""
    if re.fullmatch(r"[A-Za-z0-9_-]{1,256}", secret or ""):
        return secret
    return hashlib.sha256(secret.encode()).hexdigest() if secret else ""


def sign_report(upload_id: int, secret: str) -> str:
    return hmac.new(secret.encode(), f"report:{upload_id}".encode(), hashlib.sha256).hexdigest()[:24]


def verify_report(upload_id: int, sig: str, secret: str) -> bool:
    return bool(secret) and hmac.compare_digest(sign_report(upload_id, secret), sig or "")


def report_link(upload_id: int) -> str | None:
    """The signed link to a research report saved by the weekly run (served by qt.web)."""
    base = os.environ.get("QT_PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    secret = os.environ.get("DASHBOARD_PASSWORD")
    if not base or not secret:
        return None
    if not base.startswith("http"):
        base = "https://" + base
    return f"{base.rstrip('/')}/report/{upload_id}?sig={sign_report(upload_id, secret)}"


def public_link(item_id: int) -> str | None:
    base = os.environ.get("QT_PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    secret = os.environ.get("DASHBOARD_PASSWORD")
    if not base or not secret:
        return None
    if not base.startswith("http"):
        base = "https://" + base
    return f"{base.rstrip('/')}/item/{item_id}?sig={sign(item_id, secret)}"


def send_item(bot: Telegram, queue: Queue, item: dict) -> int:
    mid = bot.send(item_text(item), item_buttons(item, public_link(item["id"])))
    queue.set_message(item["id"], mid)
    return mid


def refresh_item(bot: Telegram, queue: Queue, item_id: int) -> None:
    item = queue.get(item_id)
    if item and item.get("tg_message_id"):
        bot.edit(item["tg_message_id"], item_text(item), item_buttons(item, public_link(item_id)))


ANSWER = {"a": ("approve", "אושר: עסקת נייר בפתיחה הבאה"), "r": ("reject", "נדחה"), "p": ("postpone", "נדחה למחר")}
REASON_Q = re.compile(r"#(\d+)")


def handle_update(update: dict, bot: Telegram, queue: Queue, on_resume: Callable[[str], None] | None = None,
                  on_upload: Callable[[str, str], str] | None = None) -> str:
    """One webhook update. Returns what happened (for the log). Anything not from Ilay's chat is ignored."""
    cb = update.get("callback_query")
    msg = update.get("message")
    chat = str(((cb or {}).get("message") or msg or {}).get("chat", {}).get("id", ""))
    if chat != str(bot.chat_id):
        return "ignored: not Ilay's chat"
    if cb:
        data = cb.get("data", "")
        if data.startswith("resume:"):
            if on_resume is None:
                bot.answer(cb["id"], "המשך ריצה לא זמין כאן")
                return "resume unavailable"
            on_resume(data.split(":", 1)[1])
            bot.answer(cb["id"], "ממשיך את הריצה מהצעד שנפל")
            return f"resume {data}"
        code, _, sid = data.partition(":")
        if code not in ANSWER or not sid.isdigit():
            bot.answer(cb["id"], "?")
            return "unknown button"
        choice, said = ANSWER[code]
        try:
            queue.decide(int(sid), choice)
        except NotOpen as e:
            bot.answer(cb["id"], "הפריט כבר לא פתוח")
            refresh_item(bot, queue, int(sid))
            return f"not open: {e}"
        bot.answer(cb["id"], said)
        refresh_item(bot, queue, int(sid))
        if choice == "reject":
            bot.send(f"סיבה לדחיית #{sid}? מילה או שתיים, לא חובה (השב להודעה הזו)", force_reply=True)
        return f"{choice} #{sid}"
    if msg is None:
        return "ignored"
    reply = (msg.get("reply_to_message") or {}).get("text", "")
    m = REASON_Q.search(reply)
    if m and "סיבה" in reply and msg.get("text"):
        queue.note(int(m.group(1)), msg["text"][:500])
        bot.send(f"נרשמה סיבה ל-#{m.group(1)}")
        return f"reason #{m.group(1)}"
    doc = msg.get("document")
    if doc:
        if on_upload is None or not doc.get("file_name", "").endswith((".yaml", ".yml")):
            bot.send("אני מקבל רק את קובץ המקרו החודשי (YAML)")
            return "upload refused"
        text = bot.download(doc["file_id"]).decode("utf-8")
        result = on_upload(doc["file_name"], text)
        bot.send(result)
        return f"upload {doc['file_name']}"
    if (msg.get("text") or "").strip() == "/queue":
        items = queue.open_items()
        for it in items:
            send_item(bot, queue, it)
        if not items:
            bot.send("אין פריטים פתוחים בתור")
        return f"queue resent ({len(items)})"
    return "ignored text"
