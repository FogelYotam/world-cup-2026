"""כלי עזר משותפים: לוגים, קריאה/כתיבה של JSON, ובקשות HTTP בטוחות."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import logging
import time

import config

# requests מיובא עצלן בתוך safe_get — כך הליבה (kickoff_predictions/predictor)
# רצה בלי התקנת requests (סביבת נייד טרייה ב-claude.ai/code).

# --------------------------------------------------------------------------- #
# לוגים
# --------------------------------------------------------------------------- #
_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def get_logger(name: str) -> logging.Logger:
    """מחזיר logger שכותב גם למסוף וגם לקובץ יומי תחת logs/."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(_LOG_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    log_file = config.LOGS_DIR / f"{datetime.now():%Y-%m-%d}.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


log = get_logger("utils")


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #
def load_json(path: Path, default=None):
    """טוען JSON. אם הקובץ חסר או פגום — מחזיר ערך ברירת מחדל ולא קורס."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        log.warning("קובץ JSON לא נמצא: %s — מחזיר ברירת מחדל", path)
        return default
    except json.JSONDecodeError as exc:
        log.error("JSON פגום ב-%s: %s — מחזיר ברירת מחדל", path, exc)
        return default


def save_json(path: Path, data) -> None:
    """שומר JSON בצורה אטומית (כתיבה לקובץ זמני ואז החלפה)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def now_iso() -> str:
    """חותמת זמן ISO ב-UTC."""
    return datetime.now(timezone.utc).isoformat()


def _local_tz():
    """אזור-הזמן של המשתמש (config.LOCAL_TZ, ברירת מחדל ישראל). None אם לא זמין."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(getattr(config, "LOCAL_TZ", "Asia/Jerusalem"))
    except Exception:  # noqa: BLE001
        return None


def now_local() -> datetime:
    """עכשיו לפי שעון המשתמש (ישראל), naive — תואם לפלט של _parse_dt."""
    tz = _local_tz()
    n = datetime.now(tz) if tz else datetime.now()
    return n.replace(tzinfo=None)


def _parse_dt(value):
    """מפרסר ISO datetime/date ל-naive **לפי שעון המשתמש** (config.LOCAL_TZ).
    כך 'היום'/'עכשיו' עקביים עם השעון שלך, לא עם UTC של השרת. None אם לא תקין."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        tz = _local_tz()
        dt = (dt.astimezone(tz) if tz else dt.astimezone()).replace(tzinfo=None)
    return dt


def odds_revealed(match: dict, reveal_hours: float, now: datetime | None = None) -> tuple:
    """
    קובע אם המלצת ההימור נחשפת. מחזיר (revealed: bool, kickoff_label: str).

    כלל: אם יש שעת פתיחה (kickoff) — נחשף רק reveal_hours שעות לפניה.
    אם אין שעה אך יש תאריך — נחשף ביום המשחק ואילך. אחרת — לא נחשף.
    """
    from datetime import timedelta
    now = now or datetime.now()

    kickoff = _parse_dt(match.get("kickoff"))
    if kickoff is not None:
        reveal_at = kickoff - timedelta(hours=reveal_hours)
        return now >= reveal_at, kickoff.strftime("%d/%m %H:%M")

    day = _parse_dt(match.get("date"))
    if day is not None:
        return now.date() >= day.date(), ""

    return False, ""


# --------------------------------------------------------------------------- #
# בקשות HTTP בטוחות
# --------------------------------------------------------------------------- #
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,he;q=0.8",
}


def safe_get(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = 15,
    retries: int = 3,
    backoff: float = 2.0,
) -> requests.Response | None:
    """
    GET עם טיפול בשגיאות רשת, rate-limit (429) ו-retry עם backoff.
    מחזיר Response במקרה הצלחה, או None אם כל הניסיונות נכשלו.
    """
    import requests  # עצלן — נדרש רק כאן (HTTP), לא בליבת החישוב
    merged_headers = {**_DEFAULT_HEADERS, **(headers or {})}

    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                url, params=params, headers=merged_headers, timeout=timeout
            )
            if resp.status_code == 429:
                wait = backoff ** attempt
                log.warning(
                    "Rate limit (429) על %s — ממתין %.1f שנ' (ניסיון %d/%d)",
                    url, wait, attempt, retries,
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            wait = backoff ** attempt
            log.warning(
                "שגיאת רשת על %s: %s — ניסיון %d/%d, ממתין %.1f שנ'",
                url, exc, attempt, retries, wait,
            )
            if attempt < retries:
                time.sleep(wait)

    log.error("כל הניסיונות נכשלו עבור %s", url)
    return None


def safe_get_json(url: str, **kwargs) -> dict | list | None:
    """כמו safe_get אך מפרסר JSON. מחזיר None אם נכשל או אם הפורמט לא תקין."""
    resp = safe_get(url, **kwargs)
    if resp is None:
        return None
    try:
        return resp.json()
    except ValueError as exc:
        log.error("תשובת JSON לא תקינה מ-%s: %s", url, exc)
        return None


def coalesce(*values, default=None):
    """מחזיר את הערך הראשון שאינו None/ריק — שימושי ל-fallback."""
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return default
