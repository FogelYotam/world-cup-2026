"""מרכז ההגדרות של המערכת. טוען משתנים מקובץ .env."""
from pathlib import Path
import json
import os

BASE_DIR = Path(__file__).resolve().parent
# python-dotenv אופציונלי: בסביבה טרייה (claude.ai/code בנייד, ללא pip install)
# הוא לא קיים — הליבה (kickoff_predictions/predictor) חייבת לרוץ בלעדיו. הסודות
# נטענים אז ישירות מ-os.environ אם הוגדרו.
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

# --- נתיבים ---
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"
LOGS_DIR = BASE_DIR / "logs"
DB_PATH = DATA_DIR / "db.json"
MY_TEAM_PATH = DATA_DIR / "my_team.json"   # הקבוצה האישית שלך ב-FIFA Fantasy

for _d in (DATA_DIR, OUTPUT_DIR, LOGS_DIR):
    _d.mkdir(exist_ok=True)

def _clean_key(value: str) -> str:
    """מתעלם מערכי placeholder שנשארו בקובץ .env."""
    value = (value or "").strip()
    if not value or value.upper().startswith("PASTE_"):
        return ""
    return value


# --- מפתחות ---
GEMINI_API_KEY = _clean_key(os.getenv("GEMINI_API_KEY"))
PERPLEXITY_API_KEY = _clean_key(os.getenv("PERPLEXITY_API_KEY"))

# --- טלגרם (ערוץ ההפצה המועדף) ---
TELEGRAM_BOT_TOKEN = _clean_key(os.getenv("TELEGRAM_BOT_TOKEN"))
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
# קליטת הבוט (קריאת צילומים/צ'אט) כבויה — הניחושים נעשים מ-claude.ai/code בנייד.
# הדוח היומי עדיין נשלח (זה ג'וב נפרד ב-main.py). הפוך ל-True כדי להפעיל מחדש.
TELEGRAM_INTAKE_ENABLED = os.getenv("TELEGRAM_INTAKE_ENABLED", "0").strip() in ("1", "true", "True")

# --- מייל (אופציונלי) ---
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").strip()
MAIL_TO = os.getenv("MAIL_TO", "").strip()
REPORT_PUBLIC_URL = os.getenv("REPORT_PUBLIC_URL", "").strip()

# --- פרמטרים של המודל ---
# מונדיאל = רוב המשחקים במגרש ניטרלי, לכן יתרון ביתי נמוך (אומת ב-backtest auto-tune:
# 0.10 נתן 1.421 ppg מול 1.316 ב-0.25, עקבי לאורך כל ערכי MAX_XG).
HOME_ADVANTAGE = 0.10          # תוספת לכושר התקפי ביתי במגרש ניטרלי (פואסון)
# יתרון ביתי מותנה: המארחות (ארה"ב/קנדה/מקסיקו) משחקות בביתן האמיתי מול קהל ביתי,
# לכן מקבלות יתרון גבוה יותר כשהן הקבוצה הביתית.
HOST_NATIONS = {
    "USA", "United States", "United States of America", "US",
    "Mexico", "México", "Canada",
}
# בונוס *מעל* היתרון הניטרלי (יחסי) — כך גם אם auto-tune מעלה את HOME_ADVANTAGE,
# מארחת תמיד גבוהה ממנו. host_adv = HOME_ADVANTAGE + HOST_HOME_BONUS.
HOST_HOME_BONUS = 0.20
DEFAULT_GOALS_FOR = 1.3        # ערך fallback ממוצע שערים למשחק
DEFAULT_GOALS_AGAINST = 1.3
MAX_GOALS_GRID = 6             # תקרת שערים בחישוב מטריצת ההסתברויות
MAX_XG = 4.5                   # תקרת צפי שערים לקבוצה (מונע over-fit לתוצאות קיצון)
# ניחוש הדוח (predicted_score) — מגוון/ריאלי. תיקו נבחר כשהסתברות התיקו ≥ הסף הזה
# (כי תיקו כמעט אף פעם לא ה'הכי סביר' אך קורה ~30%). כיול ל-~30% תיקו בטורניר.
DRAW_PREDICT_THRESHOLD = 0.25
MIN_XG = 0.2                   # רצפת צפי שערים
MIN_CONFIDENCE = 0.0           # סף אמון מינימלי להצגה (0 = הצג הכל)
REPORT_WINDOW_DAYS = 2         # (לא בשימוש בדוח כעת — נשמר לתאימות)
REPORT_UPCOMING_COUNT = 5      # (לא בשימוש — הוחלף בחלון ימים)
REPORT_UPCOMING_DAYS = 5       # מציגים בדוח את כל המשחקים ב-5 הימים הקרובים

# --- שיטת הניקוד של קבוצת הניחושים (KICKOFF) ---
# ההמלצה נבחרת כך שתמקסם את תוחלת הנקודות תחת השיטה הזו (לא רק הסבירה ביותר).
PREDICTION_SCORING = {
    "exact": 3,                # תוצאה מדויקת
    "direction": 1,            # כיוון נכון (ניצחון/תיקו)
    "reversed": -1,            # קנס תוצאה הפוכה
    "goals_bonus": 1,          # בונוס אם המשחק בפועל מעל הסף (לא תלוי בניחוש)
    "goals_bonus_threshold": 3,
}
POSITION_PICKS_PER_POS = 3     # כמה שחקנים מומלצים להציג בכל עמדה
TRANSFER_CANDIDATES_PER_POS = 2  # כמה מועמדי חילוף להציג לכל עמדה
# --- בחירת קפטן לפי תקרה (ceiling) ולא רק תוחלת ---
# קפטן מכפיל נקודות, לכן עדיף שחקן עם פוטנציאל שיא (שערים) על פני יציב-ממוצע.
CAPTAIN_CEILING_WEIGHT = 0.5     # משקל רכיב התקרה ההתקפי בבחירת הקפטן
PENALTY_TAKER_GOAL_BONUS = 0.15  # תוספת קצב-שער לבועט פנדלים (תקרה גבוהה יותר)
# תקרת בעלות לדיפרנציאל — מועלתה מ-5% ל-15% כדי לא להגביל לשחקנים "שאיש לא בחר";
# הדירוג מתעדף סיכוי לנקד (תוחלת נקודות + קלות משחק), ובעלות נמוכה היא יתרון.
DIFFERENTIAL_MAX_OWNERSHIP = 15.0  # סף בעלות (%) לשחקן "דיפרנציאל"
# משקלי ניקוד דיפרנציאל — דגש על תוחלת נקודות וקלות המשחק (סיכוי לנצח/לנקד)
DIFFERENTIAL_WEIGHTS = {"points": 1.0, "form": 0.5, "fixture": 3.0,
                        "ownership": 1.5, "starter": 2.0}
# כמה דיפרנציאלים להציג לכל עמדה — מבנה סגל מלא (מתמקדים במובטחי-דקות)
DIFFERENTIAL_COUNTS = {"GK": 3, "DEF": 5, "MID": 5, "FWD": 3}

# --- מקור רשמי של FIFA World Cup Fantasy (מקור האמת לבריכת השחקנים) ---
# קבצי JSON ציבוריים (ללא אימות) — הסגלים הרשמיים בלבד, עם מחיר/בעלות/כושר/נקודות.
FIFA_FANTASY_PLAYERS_URL = "https://play.fifa.com/json/fantasy/players.json"
FIFA_FANTASY_SQUADS_URL = "https://play.fifa.com/json/fantasy/squads.json"
FIFA_FANTASY_ROUNDS_URL = "https://play.fifa.com/json/fantasy/rounds.json"

# --- תחרות ---
COMPETITION = "FIFA World Cup 2026"
SEASON = "2026"
TOURNAMENT_START = "2026-06-11"   # עד תאריך זה — עדכון יומי; אחריו — רק בשינוי

# --- שקלול אודדס וטריגרים לעדכון ---
# משקל השוק מול המודל (0=מודל בלבד, 1=שוק בלבד). הועלה ל-0.6 — הבוקמייקרים החדים
# (Pinnacle/Betfair) מדויקים יותר מהמודל הפנימי, אז נותנים להם יותר אמון בניחוש.
MARKET_BLEND_WEIGHT = 0.6
# 5 מקורות האודדס המובילים לדיוק ניבוי — מובילים בחדות (קרובים להסתברות האמיתית).
# Pinnacle הוא ה'עוגן החד' (מרווח ~2%, קווי פתיחה קרובים לאמת); Betfair = בורסה
# (מחירי שוק אמיתיים); Oddschecker מצרף קונצנזוס מכל הבוקמייקרים.
ODDS_SOURCES = [
    "Pinnacle",            # החד ביותר — סטנדרט הזהב לדיוק
    "Betfair Exchange",    # בורסת ההימורים הנזילה ביותר — מחירי שוק אמיתיים
    "Bet365",              # הבוקמייקר הגדול והנזיל בעולם
    "Circa Sports",        # קובע-שוק (sharp, לאס וגאס)
    "Oddschecker",         # מצרף קונצנזוס מכל הבוקמייקרים
]
ODDS_CHANGE_THRESHOLD = 0.10      # שינוי בהסתברות הפייבוריט שמפעיל עדכון
ODDS_REVEAL_HOURS = 2             # המלצת ההימור נחשפת רק כך וכך שעות לפני פתיחה

# --- מקורות פנטזי מומלצים (לאיסוף טפסים/מחירים/בעלות/xG) ---
# הרשימה שבחר המשתמש — מקורות אלה נמסרים ל-Gemini לחיפוש מעוגן.
# האתר הרשמי (play.fifa.com/fantasy) הוא המקור לאחוזי הבעלות (ownership).
FANTASY_SOURCES = [
    "official FIFA World Cup Fantasy (play.fifa.com/fantasy) — ownership %",
    "Fantasy Football Scout", "WhoScored", "FBref", "Flashscore",
    "Reddit r/FantasyPL", "FotMob", "#FPL on Twitter/X",
    # אתרי ניתוח דיפרנציאלים שאומתו בשיחה
    "allaboutfpl.com", "RotoWire", "onsidearena.com", "chaseyoursport.com",
]

# --- מקורות נתונים ---
USE_GEMINI = bool(GEMINI_API_KEY)
USE_PERPLEXITY = bool(PERPLEXITY_API_KEY)


def gemini_enabled() -> bool:
    return USE_GEMINI


def mail_enabled() -> bool:
    return bool(GMAIL_ADDRESS and GMAIL_APP_PASSWORD and MAIL_TO)


def telegram_enabled() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


# --- כיוונון אוטומטי שמור (data/tuning.json) — דורס ידיות מודל בתחום שפוי בלבד ---
_TUNING_BOUNDS = {"MAX_XG": (3.0, 5.5), "HOME_ADVANTAGE": (0.0, 0.4)}


def _apply_saved_tuning() -> None:
    """טוען ערכי כיוונון שנשמרו על-ידי auto-tune היומי ומחיל אותם (בגבולות שפויים)."""
    path = DATA_DIR / "tuning.json"
    if not path.exists():
        return
    try:
        t = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    g = globals()
    for key, (lo, hi) in _TUNING_BOUNDS.items():
        v = t.get(key)
        if isinstance(v, (int, float)) and lo <= float(v) <= hi:
            g[key] = float(v)


_apply_saved_tuning()
