"""
התראות הרכב — מודיע מיד כשאחד משחקני הקבוצה שלך (data/my_team.json) **לא בהרכב
הפותח** או **לא זמין**, ברגע שהרכב המשחק מתפרסם.

מבוסס על הפיד הרשמי של FIFA (`fetch_official_pool` → injury_status / suspension_status
/ expected_start):
- `expected_start is False`  → הרכב פורסם והשחקן על הספסל / לא בסגל.
- `injury_status == "injured"` / `"out"`, `suspension_status == "suspended"` → לא זמין.
- `expected_start is None`   → ההרכב עדיין לא פורסם — לא מתריעים.

רץ בלולאת ה-bot-poll (כל ~15 דק') ושולח **פעם אחת** לכל אירוע (dedup ב-state).
לעולם לא זורק חריגה.
"""
from __future__ import annotations

import json
import unicodedata

import config
import utils

log = utils.get_logger("lineup_alerts")


def _norm(s) -> str:
    d = unicodedata.normalize("NFKD", str(s or "").lower())
    return "".join(c for c in d if not unicodedata.combining(c)).strip()


def load_my_squad(path=None) -> list[dict]:
    """קורא את 15 השחקנים מ-data/my_team.json. מחזיר [] אם חסר/שגוי."""
    path = path or (config.DATA_DIR / "my_team.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        return []
    return [p for p in (data.get("squad") or []) if p.get("player_name")]


_REASON = {
    "suspended": "🚫 מורחק — לא ישחק",
    "injured": "🩹 פצוע — לא צפוי לשחק",
    "out": "❌ לא בסגל למשחק",
    "bench": "🪑 על הספסל — לא בהרכב הפותח",
}


def _bad_status(p: dict):
    """מחזיר (code, label) אם השחקן לא-מתחיל/לא-זמין; אחרת None."""
    if p.get("suspension_status") == "suspended":
        return "suspended", _REASON["suspended"]
    if p.get("injury_status") == "injured":
        return "injured", _REASON["injured"]
    if p.get("injury_status") == "out":
        return "out", _REASON["out"]
    if p.get("expected_start") is False:      # ההרכב פורסם והוא לא פותח
        return "bench", _REASON["bench"]
    return None                               # מתחיל, או שההרכב עוד לא פורסם


def _match(name: str, pool: list[dict], by_norm: dict) -> dict | None:
    """מתאים שם מהקבוצה שלך לשחקן בבריכה הרשמית. עמיד גם לשמות **קטועים**
    (e.g. 'Nuno Men...', 'Bruno Fer_') — חיתוך זנב לא-אלפבתי והתאמת תחילית."""
    n = _norm(name).strip("._ ").replace("...", "").strip()
    if not n:
        return None
    if n in by_norm:
        return by_norm[n]
    for p in pool:
        pn = _norm(p.get("player_name"))
        if pn == n or n in pn.split():
            return p
    # תחילית (לשמות קטועים) — דורש לפחות 4 תווים כדי לצמצם התאמות-שווא
    if len(n) >= 4:
        for p in pool:
            pn = _norm(p.get("player_name"))
            if pn.startswith(n) or any(tok.startswith(n) for tok in pn.split()) or n in pn:
                return p
    return None


def check_alerts(pool: list[dict], squad: list[dict], already: dict) -> list[dict]:
    """מחזיר התראות חדשות לשחקנים שלך שלא פותחים/לא זמינים (לא דווחו עדיין).
    מעדכן את `already` (dedup): שם→code. שחקן שחזר להרכב מתאפס."""
    by_norm = {}
    for p in pool:
        by_norm.setdefault(_norm(p.get("player_name")), p)
    out = []
    for sp in squad:
        nm = sp.get("player_name")
        pl = _match(nm, pool, by_norm)
        key = _norm(nm)
        if not pl:
            continue
        bad = _bad_status(pl)
        if bad:
            code, label = bad
            if already.get(key) != code:        # אירוע חדש / השתנה
                out.append({"name": pl.get("player_name") or nm,
                            "team": pl.get("team"), "label": label})
                already[key] = code
        elif key in already:                    # חזר להרכב הפותח — אפס
            del already[key]
    return out


def format_alert(alerts: list[dict]) -> str:
    lines = ["<b>⚠️ התראת הרכב — שחקנים שלך שלא פותחים</b>"]
    for a in alerts:
        team = f" <i>({a['team']})</i>" if a.get("team") else ""
        lines.append(f"• <b>{a['name']}</b>{team} — {a['label']}")
    lines.append("")
    lines.append("<i>💡 שקול להחליף אותו לפני שהמשחק מתחיל. זכור: חילוף ידני מבטל "
                 "את ה-auto-subs לכל הסבב.</i>")
    return "\n".join(lines)


def run_lineup_alerts(send_fn, state: dict, pool: list[dict] | None = None) -> int:
    """מתזמר: טוען סגל, מושך בריכה רשמית, מאתר שחקנים שלך שלא פותחים, ושולח התראה
    אחת לכל אירוע. מחזיר כמה התראות נשלחו. לעולם לא זורק."""
    try:
        squad = load_my_squad()
        if not squad:
            return 0
        if pool is None:
            import scraper
            pool = scraper.fetch_official_pool()
        if not pool:
            return 0
        already = state.setdefault("lineup_alerts", {})
        alerts = check_alerts(pool, squad, already)
        if alerts:
            send_fn(format_alert(alerts))
            log.info("נשלחו %d התראות הרכב", len(alerts))
        return len(alerts)
    except Exception as exc:  # noqa: BLE001
        log.error("התראות הרכב נכשלו: %s", exc)
        return 0
