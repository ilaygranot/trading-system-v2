# הקמה ב-Render (צעד אחר צעד)

כל מה שבקוד כבר מוכן: `render.yaml` מגדיר שירות web, ארבע משימות cron ו-Postgres. אין מפתחות בקוד; כולם נכנסים בלוח של Render.

## מה צריך לפני

1. **בוט טלגרם:** ב-BotFather שולחים `/newbot`, מקבלים טוקן. שולחים לבוט החדש הודעה כלשהי, ואז פותחים
   `https://api.telegram.org/bot<הטוקן>/getUpdates` ומעתיקים את `chat.id` (זה ה-chat id שלך).
2. **מפתח Anthropic API** מ-console.anthropic.com (חיוב נפרד ממנוי Claude). תקרה חודשית: 150$ (הוגדרה גם בקוד: 10$ ליום).
3. **מפתח FRED** (חינם) ו-**FMP** (גיבוי למחירים).
4. **סיסמה ללוח הבקרה** (משמשת גם לחתימת קישורי "פרטים").

## ההקמה

1. ב-Render: New → Blueprint → בוחרים את הריפו `trading-system-v2`. Render קורא את `render.yaml`.
2. Render מבקש את הערכים שמסומנים `sync: false`: `ANTHROPIC_API_KEY`, `FRED_API_KEY`, `FMP_API_KEY`,
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DASHBOARD_PASSWORD` (בשירות ה-web ובכל אחת מארבע משימות ה-cron).
   `TELEGRAM_WEBHOOK_SECRET` נוצר לבד.
3. אחרי שה-web עלה: Shell של `qt-web` → `qt set-webhook`. מעכשיו הכפתורים בטלגרם עובדים.
4. שולחים לבוט את קובץ התיק `portfolio.yaml` (מבנה כמו `examples/portfolio.yaml`). הבוט עונה "התיק עודכן".
5. בדיקה: Shell → `qt run daily --as-of <יום מסחר אחרון>`. אמורה להגיע הודעת סיכום ופריטים עם כפתורים.
6. לוח הבקרה: הכתובת של `qt-web`, משתמש כלשהו + הסיסמה.

## כל חודש

הריצה החודשית (שבת בשבוע השני) מחכה לקובץ המקרו הידני (ISM PMI/NMI, NFIB) בפורמט YAML:
`ism_pmi: {'2026-09': 49.1, ...}`. הבוט מזכיר; שולחים לו את הקובץ והריצה ממשיכה לבד.

## עלות צפויה

web starter 7$ · Postgres basic-256mb כ-6$ · ארבע משימות cron לפי דקות ריצה (מעט דולרים) · בסה"כ מתחת ל-25$ לחודש.

## אופציונלי

דופק (healthchecks.io): יוצרים בדיקה לכל ריצה ומוסיפים `HEALTHCHECK_DAILY`, `HEALTHCHECK_PREMARKET`,
`HEALTHCHECK_WEEKLY`, `HEALTHCHECK_MONTHLY` עם הכתובת. אם ריצה לא דיווחה בזמן, מגיעה התראה.
