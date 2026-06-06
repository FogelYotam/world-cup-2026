"""
זיהוי שינויים וקצב עדכונים.

שומר תמונת-מצב של הריצה האחרונה (data/state.json) ומשווה אליה כדי
להחליט אם לשלוח עדכון. עד תחילת המונדיאל — עדכון יומי. אחריו — רק כשיש
טריגר: מחזור חדש, פציעה/הרחקה מהותית, או שינוי אודדס משמעותי.
"""
from __future__ import annotations

from datetime import date

import config
import utils

log = utils.get_logger("state")

STATE_PATH = config.DATA_DIR / "state.json"

_INJURY_FLAGS = ("injured", "doubtful", "out")
_SUSPENSION_FLAGS = ("suspended", "banned")


# --------------------------------------------------------------------------- #
# בניית תמונת-מצב
# --------------------------------------------------------------------------- #
def build_snapshot(db: dict, predictions: list[dict]) -> dict:
    """תמונת-מצב תמציתית להשוואה בין ריצות."""
    fixtures = sorted(str(p.get("match_id")) for p in predictions if p.get("match_id"))

    injured = sorted(
        f"{p.get('player_name')}|{p.get('team')}"
        for p in db.get("players", [])
        if p.get("injury_status") in _INJURY_FLAGS
        or p.get("suspension_status") in _SUSPENSION_FLAGS
    )

    odds = {}
    for p in predictions:
        mid = p.get("match_id")
        probs = p.get("outcome_probabilities") or {}
        if not mid or not probs:
            continue
        fav = max(probs, key=lambda k: probs.get(k, 0.0))
        odds[str(mid)] = {"favorite": fav, "prob": round(probs.get(fav, 0.0), 3)}

    return {
        "generated_at": utils.now_iso(),
        "fixtures": fixtures,
        "injured": injured,
        "odds": odds,
    }


# --------------------------------------------------------------------------- #
# זיהוי טריגרים
# --------------------------------------------------------------------------- #
def detect_triggers(prev: dict | None, curr: dict) -> list[str]:
    """מחזיר רשימת סיבות (בעברית) לעדכון. ריק = אין שינוי מהותי."""
    if not prev:
        return []
    triggers: list[str] = []

    prev_fix, curr_fix = set(prev.get("fixtures", [])), set(curr.get("fixtures", []))
    if curr_fix - prev_fix:
        triggers.append("מחזור חדש — נוספו משחקים חדשים")

    prev_inj, curr_inj = set(prev.get("injured", [])), set(curr.get("injured", []))
    new_inj = curr_inj - prev_inj
    healed = prev_inj - curr_inj
    if new_inj:
        names = ", ".join(s.split("|")[0] for s in sorted(new_inj))
        triggers.append(f"פציעה/הרחקה חדשה: {names}")
    if healed:
        names = ", ".join(s.split("|")[0] for s in sorted(healed))
        triggers.append(f"חזרה מפציעה: {names}")

    moved = _odds_moves(prev.get("odds", {}), curr.get("odds", {}))
    if moved:
        triggers.append(f"שינוי אודדס משמעותי ב-{len(moved)} משחקים")

    return triggers


def _odds_moves(prev_odds: dict, curr_odds: dict) -> list[str]:
    """משחקים שבהם הפייבוריט התחלף או ההסתברות זזה מעל הסף."""
    moved = []
    for mid, curr in curr_odds.items():
        prev = prev_odds.get(mid)
        if not prev:
            continue
        flipped = prev.get("favorite") != curr.get("favorite")
        delta = abs(curr.get("prob", 0.0) - prev.get("prob", 0.0))
        if flipped or delta >= config.ODDS_CHANGE_THRESHOLD:
            moved.append(mid)
    return moved


# --------------------------------------------------------------------------- #
# החלטת קצב
# --------------------------------------------------------------------------- #
def is_pre_tournament(today: date | None = None) -> bool:
    today = today or date.today()
    try:
        start = date.fromisoformat(config.TOURNAMENT_START)
    except ValueError:
        return False
    return today < start


def decide(prev: dict | None, curr: dict, today: date | None = None,
           force: bool = False) -> tuple[bool, list[str]]:
    """מחליט אם לשלוח עדכון ומחזיר (should_send, reasons)."""
    if force:
        return True, ["שליחה ידנית (--force)"]
    if prev is None:
        return True, ["ריצה ראשונה"]
    if is_pre_tournament(today):
        return True, ["עדכון יומי (טרם תחילת המונדיאל)"]
    triggers = detect_triggers(prev, curr)
    return bool(triggers), triggers


# --------------------------------------------------------------------------- #
# התמדה
# --------------------------------------------------------------------------- #
def load_state() -> dict | None:
    return utils.load_json(STATE_PATH, default=None)


def save_state(snapshot: dict) -> None:
    utils.save_json(STATE_PATH, snapshot)
