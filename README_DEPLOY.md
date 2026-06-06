# הרצה בענן עם GitHub Actions (בלי תלות במחשב)

המערכת יכולה לרוץ על השרתים של GitHub — הדוח היומי והבוט פועלים גם כשהמחשב כבוי.

## מה רץ בענן
| Workflow | מתי | מה עושה |
|---|---|---|
| `Daily Report` | 08:00 UTC = **11:00 שעון ישראל (קיץ)** | דוח יומי מלא לטלגרם |
| `Bot Poll` | כל **15 דקות** | קולט צילומי הרכב/ניחושים ומגיב |

הנתונים הנלמדים (`data/*.json` — מודל, הקבוצה שלך, offset) נשמרים חזרה לריפו אוטומטית בין הרצות.

---

## שלבים (פעם אחת)

### 1. זהות Git (אתה מריץ ב-PowerShell)
```powershell
git config --global user.name "Yotam Fogel"
git config --global user.email "yotamfogel@gmail.com"
```

### 2. יצירת ריפו ב-GitHub
היכנס ל-https://github.com/new
- שם: `world-cup-2026`
- **Public** או **Private** — ראה "מכסת דקות" למטה
- אל תסמן "Add a README"
- העתק את כתובת ה-`.git`

### 3. דחיפת הקוד (אני אעשה איתך)
```
git remote add origin https://github.com/USERNAME/world-cup-2026.git
git commit -m "..." && git push -u origin main
```

### 4. הדבקת הסודות (אתה — ב-GitHub)
בריפו: **Settings → Secrets and variables → Actions → New repository secret**.
צור שלושה:
| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | הטוקן של הבוט |
| `TELEGRAM_CHAT_ID` | ה-chat id שלך |
| `GEMINI_API_KEY` | מפתח Gemini |

> 🔒 הסודות מוצפנים אצל GitHub. קובץ `.env` **לעולם לא נדחף** (הוא ב-.gitignore).

### 5. זהו
ה-workflows ירוצו אוטומטית לפי הלו"ז. אפשר גם להריץ ידנית:
**Actions → בחר workflow → Run workflow**.

---

## מכסת דקות חינמית — חשוב
- **ריפו Public** → דקות Actions **בלתי מוגבלות בחינם**. מומלץ — הבוט יכול לרוץ כל 15 דק' (או מהר יותר) בלי דאגה. הקוד ייחשף, אבל **הסודות לא** (הם מוצפנים בנפרד).
- **ריפו Private** → **2,000 דקות חינם בחודש**. כל 15 דק' ≈ 2,880 דקות → חורג. אם בחרת Private, שנה ב-`.github/workflows/bot-poll.yml` את השורה ל-`cron: "*/30 * * * *"` (כל 30 דק' ≈ 1,440 דקות, בתוך המכסה). הדוח היומי זניח ממילא.

כשנגמרות הדקות בריפו פרטי — ההרצות פשוט נעצרות עד תחילת החודש (אין חיוב אוטומטי).

---

## להעביר לגמרי לענן? לכבות את משימות המחשב
כדי שלא יהיו שתי מערכות שמושכות את אותן הודעות (כפילויות), כבה את משימות ה-Windows:
```powershell
Disable-ScheduledTask -TaskName "WorldCup2026Bot"
Disable-ScheduledTask -TaskName "WorldCup2026DailyReport"
```
(אפשר להחזיר בכל רגע עם `Enable-ScheduledTask`.)
