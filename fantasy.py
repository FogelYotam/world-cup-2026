"""
מנוע FIFA Fantasy — חישוב Expected Points ובחירת הרכב אופטימלי.

לכל שחקן מחושב צפי נקודות לפי סיכוי לפתוח, סיכוי לשער/בישול/clean sheet
וסיכון דקות. מתוך כך נבחר הרכב מומלץ בכפוף למכסות עמדה ולתקציב,
כולל קפטן, סגן, מועמדים לחילוף ושחקנים שכדאי להימנע מהם.
"""
from __future__ import annotations

from math import exp

import config
import utils

log = utils.get_logger("fantasy")

# --------------------------------------------------------------------------- #
# חוקי FIFA Fantasy (World Cup)
# --------------------------------------------------------------------------- #
GOAL_POINTS = {"GK": 6, "DEF": 6, "MID": 5, "FWD": 4}
CLEAN_SHEET_POINTS = {"GK": 4, "DEF": 4, "MID": 1, "FWD": 0}
ASSIST_POINTS = 3
APPEARANCE_POINTS = 2  # 60+ דקות
FANTASY_BASE_XG = 1.35  # ממוצע שערים לקבוצה — בסיס לכיול תפוקה התקפית לפי המשחק

# הרכב פותח: 1 שוער, מינימום/מקסימום לכל קו (מערכים חוקיים של FIFA)
FORMATION = {
    "GK": (1, 1),
    "DEF": (3, 5),
    "MID": (2, 5),
    "FWD": (1, 3),
}
STARTING_SIZE = 11

# סגל מלא: 2 שוערים, 5 הגנה, 5 קישור, 3 חלוץ — סה"כ 15
SQUAD_COMPOSITION = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
SQUAD_SIZE = 15

DEFAULT_BUDGET = 100.0  # מיליון — לפי הגדרות המשחק
DEFAULT_PRICE = 5.0     # מחיר fallback כשאין נתון
MAX_PER_NATION = 3      # חוק FIFA Fantasy — מקסימום 3 שחקנים מאותה נבחרת

# טווחי מחיר אופייניים ל-FIFA Fantasy (מיליון) — לפי עמדה, לשימוש כשאין מחיר אמיתי
_PRICE_FLOOR = {"GK": 4.5, "DEF": 4.5, "MID": 5.0, "FWD": 5.5}
_PRICE_CEIL = {"GK": 6.0, "DEF": 7.5, "MID": 12.5, "FWD": 13.0}

# סטטוסים שמוציאים שחקן מהסגל (לא בכושר / לא זמין)
_UNFIT_INJURY = ("injured", "out", "doubtful")
_UNAVAILABLE_SUSPENSION = ("suspended", "banned")

_POSITION_ALIASES = {
    "goalkeeper": "GK", "keeper": "GK", "gk": "GK", "g": "GK",
    "defender": "DEF", "defence": "DEF", "def": "DEF", "df": "DEF", "cb": "DEF",
    "fullback": "DEF", "lb": "DEF", "rb": "DEF",
    "midfielder": "MID", "midfield": "MID", "mid": "MID", "mf": "MID",
    "winger": "MID", "am": "MID", "dm": "MID", "cm": "MID",
    "forward": "FWD", "striker": "FWD", "fwd": "FWD", "fw": "FWD",
    "attacker": "FWD", "cf": "FWD", "st": "FWD",
}


def normalize_position(pos) -> str:
    if not pos:
        return "MID"
    return _POSITION_ALIASES.get(str(pos).strip().lower(), "MID")


# --------------------------------------------------------------------------- #
# הסתברויות ברמת השחקן
# --------------------------------------------------------------------------- #
def start_probability(player: dict) -> float:
    """סיכוי לפתוח בהרכב."""
    if player.get("injury_status") in ("injured", "doubtful", "out"):
        return 0.1
    if player.get("suspension_status") in ("suspended", "banned"):
        return 0.0
    if player.get("expected_start") is True:
        return 0.9
    if player.get("expected_start") is False:
        return 0.4
    minutes = _safe(player.get("minutes"), 0)
    return 0.75 if minutes >= 180 else 0.55


def per_match_rate(total, minutes, fallback_per90: float) -> float:
    """ממיר סך תרומות לקצב למשחק (90 דק'); נשען על fallback בלי דקות."""
    minutes = _safe(minutes, 0)
    total = _safe(total, 0)
    if minutes >= 90:
        return total / (minutes / 90.0)
    return fallback_per90


def clean_sheet_probability(team_name: str, opponent_xg: dict) -> float:
    """P(הנבחרת לא תספוג) = פואסון(0) לפי צפי השערים של היריבה."""
    xg = opponent_xg.get(team_name)
    if xg is None:
        return 0.3
    return exp(-max(xg, 0.0))


# --------------------------------------------------------------------------- #
# Expected Points
# --------------------------------------------------------------------------- #
def attack_multiplier(team_name: str, team_xg: dict | None) -> float:
    """מכייל תפוקה התקפית לפי צפי השערים של הנבחרת במשחק הספציפי (קושי יריב)."""
    if not team_xg:
        return 1.0
    txg = team_xg.get(team_name)
    if not txg:
        return 1.0
    return max(0.5, min(2.0, txg / FANTASY_BASE_XG))


def expected_points(player: dict, opponent_xg: dict, team_xg: dict | None = None) -> float:
    """צפי נקודות פנטזי לשחקן במחזור הנתון.

    team_xg (אופציונלי) ממפה נבחרת → צפי השערים שלה באותו מחזור; כשהוא מסופק,
    התפוקה ההתקפית מכוילת לפי קושי המשחק — כך התוכנית משתנה ממחזור למחזור.
    """
    pos = normalize_position(player.get("position"))
    p_start = start_probability(player)
    att = attack_multiplier(player.get("team"), team_xg)

    goals_rate = per_match_rate(player.get("goals"), player.get("minutes"), _pos_goal_rate(pos))
    assists_rate = per_match_rate(player.get("assists"), player.get("minutes"), 0.1)

    pts = p_start * APPEARANCE_POINTS
    pts += goals_rate * GOAL_POINTS[pos] * p_start * att
    pts += assists_rate * ASSIST_POINTS * p_start * att

    cs_pts = CLEAN_SHEET_POINTS[pos]
    if cs_pts:
        cs_prob = clean_sheet_probability(player.get("team"), opponent_xg)
        pts += cs_prob * cs_pts * p_start

    return round(pts, 2)


def minutes_risk(player: dict) -> str:
    """סיווג סיכון דקות לתצוגה."""
    p = start_probability(player)
    if p >= 0.85:
        return "low"
    if p >= 0.5:
        return "medium"
    return "high"


def _pos_goal_rate(pos: str) -> float:
    return {"GK": 0.0, "DEF": 0.08, "MID": 0.18, "FWD": 0.35}[pos]


# --------------------------------------------------------------------------- #
# בחירת הרכב
# --------------------------------------------------------------------------- #
def estimate_price(player: dict, pos: str) -> float:
    """מעריך מחיר FIFA Fantasy ריאלי לפי עמדה ומעורבות התקפית, כשאין מחיר אמיתי.
    כך מגבלת התקציב (100M) באמת מחייבת, ונוצר פיזור בין שחקנים זולים ליקרים."""
    real = _safe(player.get("price"), 0.0)
    if real and real > 0:
        return round(real, 1)
    goals_rate = per_match_rate(player.get("goals"), player.get("minutes"), _pos_goal_rate(pos))
    assists_rate = per_match_rate(player.get("assists"), player.get("minutes"), 0.1)
    involvement = goals_rate + 0.6 * assists_rate          # מעורבות התקפית למשחק
    ref = {"GK": 0.2, "DEF": 0.35, "MID": 0.55, "FWD": 0.8}[pos]
    frac = max(0.0, min(1.0, involvement / ref)) if ref else 0.0
    floor, ceil = _PRICE_FLOOR[pos], _PRICE_CEIL[pos]
    return round(floor + (ceil - floor) * frac, 1)


def is_in_form(player: dict) -> bool:
    """שחקן 'בכושר' = לא פצוע/מוטל בספק/מורחק ולא בסיכון דקות גבוה."""
    if player.get("injury_status") in _UNFIT_INJURY:
        return False
    if player.get("suspension_status") in _UNAVAILABLE_SUSPENSION:
        return False
    if player.get("minutes_risk") == "high":
        return False
    return True


def score_players(db: dict, predictions: list[dict]) -> list[dict]:
    """מחשב EP, עמדה ומחיר לכל שחקן וממיין מהגבוה לנמוך.

    הניקוד רגיש למחזור: צפי השערים של כל נבחרת ושל יריבתה (מתוך predictions)
    מכייל את התפוקה ההתקפית ואת סיכויי שער נקי.
    """
    opponent_xg = _opponent_xg_map(predictions)
    team_xg = _team_xg_map(predictions)
    scored = []
    for p in db.get("players", []):
        pos = normalize_position(p.get("position"))
        scored.append(
            {
                "player_name": p.get("player_name"),
                "team": p.get("team"),
                "position": pos,
                "price": estimate_price(p, pos),
                "expected_points": expected_points(p, opponent_xg, team_xg),
                "minutes_risk": minutes_risk(p),
                "injury_status": p.get("injury_status", "fit"),
                "suspension_status": p.get("suspension_status", "available"),
            }
        )
    scored.sort(key=lambda x: x["expected_points"], reverse=True)
    return scored


def pick_squad(scored: list[dict], budget: float = DEFAULT_BUDGET) -> dict:
    """בוחר סגל מלא של 15 שחקנים (2/5/5/3) בכפוף לתקציב ולמכסת FIFA של
    מקסימום 3 שחקנים לנבחרת. בוחר חמדני לפי EP מבין השחקנים שבכושר.
    מחזיר {squad, cost}."""
    # שלב 1 — רק שחקנים בכושר (לא פצועים/מורחקים/סיכון דקות גבוה)
    eligible = [p for p in scored if is_in_form(p)]
    # רשת ביטחון: אם אין מספיק שחקנים בכושר למלא 15, נוסיף זמינים (לא מורחקים)
    if len(eligible) < SQUAD_SIZE:
        extra = [p for p in scored
                 if not is_in_form(p)
                 and p["suspension_status"] not in _UNAVAILABLE_SUSPENSION]
        eligible = eligible + extra

    squad: list[dict] = []
    spent = 0.0
    counts = {pos: 0 for pos in SQUAD_COMPOSITION}
    nation_counts: dict[str, int] = {}

    min_price = min(_PRICE_FLOOR.values())  # רצפת מחיר לשמירת תקציב לעמדות שנותרו

    def _try_add(p, enforce_budget: bool = True) -> bool:
        nonlocal spent
        pos = p["position"]
        if counts[pos] >= SQUAD_COMPOSITION[pos]:
            return False
        if nation_counts.get(p["team"], 0) >= MAX_PER_NATION:
            return False
        if enforce_budget:
            # שומרים תקציב מינימלי לכל יתר העמדות שצריך עוד למלא — כך כוכב יקר
            # לא "אוכל" את כל התקציב ומשאיר את הסגל בלי כיסוי
            remaining_slots = SQUAD_SIZE - len(squad) - 1
            if spent + p["price"] + remaining_slots * min_price > budget:
                return False
        squad.append(p)
        counts[pos] += 1
        nation_counts[p["team"]] = nation_counts.get(p["team"], 0) + 1
        spent += p["price"]
        return True

    chosen: set[int] = set()
    for p in eligible:  # eligible כבר ממוין לפי EP יורד
        if len(squad) >= SQUAD_SIZE:
            break
        if id(p) in chosen:
            continue
        if _try_add(p):
            chosen.add(id(p))

    # מילוי חוזר לעמדות שנותרו — קודם הזולים ביותר שעדיין נכנסים לתקציב,
    # רק אם אין ברירה חורגים מהתקציב (נרשמת אזהרה)
    if len(squad) < SQUAD_SIZE:
        by_price = sorted(eligible, key=lambda x: x["price"])
        for p in by_price:
            if len(squad) >= SQUAD_SIZE:
                break
            if id(p) in chosen:
                continue
            if _try_add(p):  # עם אילוץ תקציב
                chosen.add(id(p))
    if len(squad) < SQUAD_SIZE:  # מוצא אחרון — חריגה מתקציב כדי להשלים 15
        by_price = sorted(eligible, key=lambda x: x["price"])
        for p in by_price:
            if len(squad) >= SQUAD_SIZE:
                break
            if id(p) in chosen:
                continue
            if _try_add(p, enforce_budget=False):
                chosen.add(id(p))
    if spent > budget:
        log.warning("הסגל חרג מהתקציב (%.1fM > %.1fM) — אין מספיק שחקנים זולים בנתונים",
                    spent, budget)

    return {"squad": squad, "cost": round(spent, 2)}


def select_starting_eleven(squad: list[dict]) -> dict:
    """בוחר 11 פותחים מתוך הסגל: שוער + מינימום חובה לכל קו, השלמה ל-11
    לפי EP בכפוף למקסימום לקו. שאר השחקנים יוצאים לספסל."""
    chosen: set[int] = set()
    lineup: list[dict] = []
    counts = {pos: 0 for pos in FORMATION}

    gks = sorted((p for p in squad if p["position"] == "GK"),
                 key=lambda x: x["expected_points"], reverse=True)
    outfield = sorted((p for p in squad if p["position"] != "GK"),
                      key=lambda x: x["expected_points"], reverse=True)

    if gks:
        lineup.append(gks[0]); chosen.add(id(gks[0])); counts["GK"] = 1

    # מינימום חובה לכל קו חוץ (רק שחקנים מאותה עמדה)
    for pos in ("DEF", "MID", "FWD"):
        min_n = FORMATION[pos][0]
        for p in outfield:
            if counts[pos] >= min_n:
                break
            if id(p) in chosen or p["position"] != pos:
                continue
            lineup.append(p); chosen.add(id(p)); counts[pos] += 1

    # השלמה ל-11 לפי EP בכפוף למקסימום לקו
    for p in outfield:
        if len(lineup) >= STARTING_SIZE:
            break
        if id(p) in chosen:
            continue
        pos = p["position"]
        if counts[pos] >= FORMATION[pos][1]:
            continue
        lineup.append(p); chosen.add(id(p)); counts[pos] += 1

    lineup.sort(key=lambda x: x["expected_points"], reverse=True)
    bench = [p for p in squad if id(p) not in chosen]
    bench.sort(key=lambda x: (x["position"] != "GK", -x["expected_points"]))

    captain = lineup[0] if lineup else None
    vice = lineup[1] if len(lineup) > 1 else None

    return {
        "lineup": lineup,
        "bench": bench,
        "formation": _formation_string(counts),
        "captain": captain,
        "vice_captain": vice,
        "total_expected_points": round(
            sum(p["expected_points"] for p in lineup)
            + (captain["expected_points"] if captain else 0),  # קפטן כפול
            2,
        ),
    }


def squad_for_matchday(squad: list[dict], scored_md: list[dict]) -> list[dict]:
    """מחזיר את שחקני הסגל עם ניקוד EP של מחזור ספציפי (לפי שם+נבחרת)."""
    keys = {(p["player_name"], p["team"]) for p in squad}
    return [s for s in scored_md if (s["player_name"], s["team"]) in keys]


def pick_starting_eleven(scored: list[dict], budget: float = DEFAULT_BUDGET) -> dict:
    """בונה סגל 15 חוקי ובוחר ממנו 11 פותחים + ספסל. מחזיר את כל המידע."""
    eligible = [p for p in scored if p["suspension_status"] not in ("suspended", "banned")]
    squad_info = pick_squad(scored, budget)
    squad = squad_info["squad"]

    result = select_starting_eleven(squad)
    _attach_alternatives(result["lineup"], eligible)
    result["squad"] = squad
    result["total_cost"] = squad_info["cost"]
    return result


def _attach_alternatives(squad: list[dict], pool: list[dict]) -> None:
    """לכל שחקן בהרכב מצמיד את החלופה הטובה ביותר באותה עמדה שאינה בהרכב."""
    in_squad = {(p["player_name"], p["team"]) for p in squad}
    for player in squad:
        alt = next(
            (
                c for c in pool
                if c["position"] == player["position"]
                and (c["player_name"], c["team"]) not in in_squad
            ),
            None,
        )
        player["alternative"] = (
            {
                "player_name": alt["player_name"],
                "team": alt["team"],
                "expected_points": alt["expected_points"],
                "price": alt["price"],
            }
            if alt
            else None
        )


def transfer_suggestions(scored: list[dict], squad: list[dict], top: int = 5) -> list[dict]:
    """שחקנים מומלצים שאינם בסגל ה-15 — מועמדים אמיתיים להבאה (transfer in)."""
    in_squad = {(p["player_name"], p["team"]) for p in squad}
    out = [p for p in scored if (p["player_name"], p["team"]) not in in_squad]
    return out[:top]


def players_to_avoid(scored: list[dict], top: int = 5) -> list[dict]:
    """שחקנים מסוכנים: פצועים/מורחקים/סיכון דקות גבוה."""
    risky = [
        p for p in scored
        if p["injury_status"] in ("injured", "doubtful", "out")
        or p["suspension_status"] in ("suspended", "banned")
        or p["minutes_risk"] == "high"
    ]
    risky.sort(key=lambda x: x["expected_points"], reverse=True)
    return risky[:top]


def build_fantasy(db: dict, predictions: list[dict], budget: float = DEFAULT_BUDGET) -> dict:
    """תזמור מלא של מנוע הפנטזי. לעולם לא זורק חריגה."""
    try:
        scored = score_players(db, predictions)
        if not scored:
            log.warning("אין נתוני שחקנים — מדלג על המלצות פנטזי")
            return {"available": False}
        eleven = pick_starting_eleven(scored, budget)
        return {
            "available": True,
            "starting_eleven": eleven,
            "squad": eleven["squad"],
            "bench": eleven["bench"],
            "transfers": transfer_suggestions(scored, eleven["squad"]),
            "avoid": players_to_avoid(scored),
        }
    except Exception as exc:  # noqa: BLE001
        log.error("מנוע הפנטזי נכשל: %s", exc)
        return {"available": False}


# --------------------------------------------------------------------------- #
# עזרים
# --------------------------------------------------------------------------- #
def _opponent_xg_map(predictions: list[dict]) -> dict[str, float]:
    """ממפה כל נבחרת לצפי השערים של יריבתה במחזור הקרוב."""
    mapping: dict[str, float] = {}
    for pred in predictions:
        xg = pred.get("expected_goals", {})
        home, away = pred.get("home_team"), pred.get("away_team")
        if home and away:
            mapping[home] = xg.get("away", 1.3)  # מה שהיריבה צפויה לכבוש
            mapping[away] = xg.get("home", 1.3)
    return mapping


def _team_xg_map(predictions: list[dict]) -> dict[str, float]:
    """ממפה כל נבחרת לצפי השערים שלה עצמה במחזור הקרוב (כיול התקפי)."""
    mapping: dict[str, float] = {}
    for pred in predictions:
        xg = pred.get("expected_goals", {})
        home, away = pred.get("home_team"), pred.get("away_team")
        if home and away:
            mapping[home] = xg.get("home", FANTASY_BASE_XG)
            mapping[away] = xg.get("away", FANTASY_BASE_XG)
    return mapping


def _formation_string(counts: dict) -> str:
    return f"{counts.get('DEF',0)}-{counts.get('MID',0)}-{counts.get('FWD',0)}"


def _safe(value, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback
