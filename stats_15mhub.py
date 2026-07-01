"""
אינטגרציית סטטיסטיקות מתקדמות מ-15MHUB (wc.15mhub.com) — ה-API הציבורי מספק
xG / xGI / xA / npxG / xGOT פר-שחקן, וסיכויי שער/בישול/שער-נקי חזויים, ו-xG קבוצתי.
ממזגים אותם לבריכת השחקנים שלנו לפי שם (nameEn) כדי להעשיר את המלצות הפנטזי.

הערה: ה-API פתוח (בלי התחברות) אבל מסנן לפי User-Agent — לכן שולחים UA של דפדפן.
"""
from __future__ import annotations

import unicodedata

import requests

import config
import utils

log = utils.get_logger("stats_15mhub")

_BASE = getattr(config, "HUB15M_BASE", "https://wc.15mhub.com/api/v1")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_TIMEOUT = 20

# השדות שאנחנו שולפים לכל שחקן (שם-מקור → שם אצלנו)
_FIELDS = {
    "xg": "xg", "xgi": "xgi", "xa": "xa", "npxg": "npxg", "xgot": "xgot",
    "goals": "hub_goals", "assists": "hub_assists",
    "cleanSheetChance": "cs_chance", "scoreChance": "score_chance",
    "assistChance": "assist_chance", "involvementChance": "involvement_chance",
    "teamExpectedGoals": "team_xg", "minutes": "hub_minutes",
    "bigChancesCreated": "big_chances", "predictedStart": "hub_predicted_start",
}


def _norm(s) -> str:
    d = unicodedata.normalize("NFKD", str(s or "").lower())
    return "".join(c for c in d if not unicodedata.combining(c) and c.isalnum())


def _get(path: str) -> dict | list | None:
    try:
        r = requests.get(f"{_BASE}/{path}", headers={"User-Agent": _UA},
                         timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("שליפת 15mhub /%s נכשלה: %s", path, exc)
        return None


def fetch_players() -> list[dict]:
    data = _get("players")
    return (data or {}).get("players", []) if isinstance(data, dict) else []


def fetch_clean_sheets() -> dict[str, float]:
    """שם-נבחרת מנורמל → אחוז אי-ספיגה (cs) מ-15mhub."""
    data = _get("clean-sheet-odds/teams")
    out = {}
    for t in (data or {}).get("teams", []) if isinstance(data, dict) else []:
        if t.get("cs") is not None:
            out[_norm(t.get("nameEn"))] = t["cs"]
    return out


def stats_by_name() -> dict[str, dict]:
    """שם-שחקן מנורמל → dict הסטטיסטיקות המתקדמות."""
    out = {}
    for p in fetch_players():
        rec = {ours: p.get(src) for src, ours in _FIELDS.items() if p.get(src) is not None}
        if rec:
            out[_norm(p.get("nameEn"))] = rec
    return out


def merge_into_pool(pool: list[dict]) -> int:
    """ממזג xG/xGI/CS וכו' לכל שחקן בבריכה לפי שם. מחזיר כמה הועשרו (in-place)."""
    stats = stats_by_name()
    cs = fetch_clean_sheets()
    if not stats and not cs:
        return 0
    n = 0
    for pl in pool:
        s = stats.get(_norm(pl.get("player_name")))
        team_cs = cs.get(_norm(pl.get("team")))
        if s:
            pl.update(s)
            n += 1
        if team_cs is not None:
            pl["hub_team_cs"] = team_cs
    log.info("15mhub: הועשרו %d שחקנים ב-xG/xGI, ו-%d נבחרות בשער-נקי", n, len(cs))
    return n
