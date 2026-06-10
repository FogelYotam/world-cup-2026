"""מרכז ההגדרות של המערכת. טוען משתנים מקובץ .env."""
from pathlib import Path
import os

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

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

# --- מייל (אופציונלי) ---
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").strip()
MAIL_TO = os.getenv("MAIL_TO", "").strip()
REPORT_PUBLIC_URL = os.getenv("REPORT_PUBLIC_URL", "").strip()

# --- פרמטרים של המודל ---
HOME_ADVANTAGE = 0.25          # תוספת לכושר התקפי ביתי (פואסון)
DEFAULT_GOALS_FOR = 1.3        # ערך fallback ממוצע שערים למשחק
DEFAULT_GOALS_AGAINST = 1.3
MAX_GOALS_GRID = 6             # תקרת שערים בחישוב מטריצת ההסתברויות
MIN_CONFIDENCE = 0.0           # סף אמון מינימלי להצגה (0 = הצג הכל)
REPORT_WINDOW_DAYS = 2         # (לא בשימוש בדוח כעת — נשמר לתאימות)
REPORT_UPCOMING_COUNT = 5      # כמה משחקים קרובים להציג בניחושים שבדוח
POSITION_PICKS_PER_POS = 3     # כמה שחקנים מומלצים להציג בכל עמדה
TRANSFER_CANDIDATES_PER_POS = 2  # כמה מועמדי חילוף להציג לכל עמדה

# --- תחרות ---
COMPETITION = "FIFA World Cup 2026"
SEASON = "2026"
TOURNAMENT_START = "2026-06-11"   # עד תאריך זה — עדכון יומי; אחריו — רק בשינוי

# --- שקלול אודדס וטריגרים לעדכון ---
MARKET_BLEND_WEIGHT = 0.5         # משקל השוק מול המודל (0=מודל בלבד, 1=שוק בלבד)
ODDS_SOURCES = [
    "Bet365", "Pinnacle", "William Hill", "Betfair", "FanDuel",
    "DraftKings", "Oddschecker", "Opta supercomputer", "Caesars", "Unibet",
]
ODDS_CHANGE_THRESHOLD = 0.10      # שינוי בהסתברות הפייבוריט שמפעיל עדכון
ODDS_REVEAL_HOURS = 2             # המלצת ההימור נחשפת רק כך וכך שעות לפני פתיחה

# --- מקורות פנטזי מומלצים (לאיסוף טפסים/מחירים/בעלות/xG) ---
# הרשימה שבחר המשתמש — מקורות אלה נמסרים ל-Gemini לחיפוש מעוגן.
FANTASY_SOURCES = [
    "Fantasy Football Scout", "WhoScored", "FBref", "Flashscore",
    "Reddit r/FantasyPL", "FotMob", "#FPL on Twitter/X",
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
