"""
מנוע חיזוי לניחושי 365 — מבוסס מודל פואסון.

לכל משחק מחושב צפי שערים (xG) לכל נבחרת לפי כושר התקפי/הגנתי,
יתרון ביתיות, פציעות, ודאות הרכב וחשיבות המשחק. מתוך מטריצת
הסתברויות פואסון נגזרים: תוצאה מומלצת, 3 חלופות, הסתברויות 1X2,
ציון אמון והסבר קצר.
"""
from __future__ import annotations

from math import exp, factorial

import config
import utils

log = utils.get_logger("predictor")

LEAGUE_AVG_GOALS = 1.35  # ממוצע שערים לקבוצה במשחק טורניר


# --------------------------------------------------------------------------- #
# פואסון
# --------------------------------------------------------------------------- #
def poisson_pmf(k: int, lam: float) -> float:
    """הסתברות פואסון ל-k אירועים בהינתן תוחלת lam."""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return (lam ** k) * exp(-lam) / factorial(k)


def expected_goals(home: dict, away: dict, context: dict | None) -> tuple[float, float]:
    """מחשב צפי שערים לכל נבחרת לפי כושר, יתרון ביתי והתאמות."""
    context = context or {}

    h_attack = _safe(home.get("goals_for"), config.DEFAULT_GOALS_FOR) / LEAGUE_AVG_GOALS
    h_defense = _safe(home.get("goals_against"), config.DEFAULT_GOALS_AGAINST) / LEAGUE_AVG_GOALS
    a_attack = _safe(away.get("goals_for"), config.DEFAULT_GOALS_FOR) / LEAGUE_AVG_GOALS
    a_defense = _safe(away.get("goals_against"), config.DEFAULT_GOALS_AGAINST) / LEAGUE_AVG_GOALS

    home_adv = context.get("home_advantage", config.HOME_ADVANTAGE)

    home_xg = h_attack * a_defense * LEAGUE_AVG_GOALS * (1 + home_adv)
    away_xg = a_attack * h_defense * LEAGUE_AVG_GOALS

    # התאמת פציעות: כל פציעה מורידה מעט מצפי השערים של הצד שנפגע
    injuries = context.get("injury_count", 0) or 0
    penalty = min(0.15 * injuries, 0.6)
    home_xg *= max(0.4, 1 - penalty / 2)
    away_xg *= max(0.4, 1 - penalty / 2)

    # שלבי נוק-אאוט נוטים לתוצאות הדוקות יותר
    if _is_knockout(context.get("stage")):
        home_xg *= 0.9
        away_xg *= 0.9

    # תקרה/רצפה — מונע over-fit ל-xG קיצוני מתוצאת בלאגן בודדת
    lo, hi = getattr(config, "MIN_XG", 0.2), getattr(config, "MAX_XG", 4.5)
    home_xg = min(max(home_xg, lo), hi)
    away_xg = min(max(away_xg, lo), hi)
    return round(home_xg, 3), round(away_xg, 3)


# --------------------------------------------------------------------------- #
# מטריצת הסתברויות
# --------------------------------------------------------------------------- #
def _dc_tau(i: int, j: int, lam: float, mu: float, rho: float) -> float:
    """תיקון Dixon-Coles לתלות בתוצאות-נמוכות (פואסון עצמאי מנבא שגוי 0-0/1-0/0-1/1-1).
    ρ שלילי מגביר תיקו (0-0,1-1) ומקטין 1-0/0-1 — כמו בכדורגל אמיתי."""
    if i == 0 and j == 0:
        return 1.0 - lam * mu * rho
    if i == 0 and j == 1:
        return 1.0 + lam * rho
    if i == 1 and j == 0:
        return 1.0 + mu * rho
    if i == 1 and j == 1:
        return 1.0 - rho
    return 1.0


def score_matrix(home_xg: float, away_xg: float, max_goals: int) -> list[list[float]]:
    """מטריצת הסתברות לכל תוצאה i:j עד max_goals שערים לכל צד.
    אם `config.DIXON_COLES_RHO` ≠ 0 — מוחל תיקון Dixon-Coles ל-4 התאים הנמוכים
    והמטריצה מנורמלת מחדש. ρ=0 → פואסון עצמאי כמקודם (תאימות לאחור)."""
    home_p = [poisson_pmf(i, home_xg) for i in range(max_goals + 1)]
    away_p = [poisson_pmf(j, away_xg) for j in range(max_goals + 1)]
    m = [[home_p[i] * away_p[j] for j in range(max_goals + 1)] for i in range(max_goals + 1)]
    rho = getattr(config, "DIXON_COLES_RHO", 0.0)
    if not rho:
        return m
    for i in range(min(2, max_goals + 1)):
        for j in range(min(2, max_goals + 1)):
            m[i][j] *= _dc_tau(i, j, home_xg, away_xg, rho)
    total = sum(p for row in m for p in row) or 1.0
    return [[max(0.0, p) / total for p in row] for row in m]


def blend_probabilities(model: dict, market: dict | None, weight: float) -> dict:
    """ממזג הסתברויות 1X2 של המודל עם קונצנזוס השוק לפי משקל (weight=חלק השוק)."""
    keys = ("home_win", "draw", "away_win")
    if not market or not all(k in market for k in keys):
        return dict(model)
    w = max(0.0, min(1.0, weight))
    blended = {k: (1 - w) * model.get(k, 0.0) + w * market.get(k, 0.0) for k in keys}
    total = sum(blended.values()) or 1.0
    return {k: round(blended[k] / total, 4) for k in keys}


def _favorite(probs: dict) -> str:
    return max(("home_win", "draw", "away_win"), key=lambda k: probs.get(k, 0.0))


def outcome_probabilities(matrix: list[list[float]]) -> dict:
    """הסתברויות 1X2 מתוך המטריצה."""
    home_win = draw = away_win = 0.0
    for i, row in enumerate(matrix):
        for j, p in enumerate(row):
            if i > j:
                home_win += p
            elif i == j:
                draw += p
            else:
                away_win += p
    total = home_win + draw + away_win or 1.0
    return {
        "home_win": round(home_win / total, 4),
        "draw": round(draw / total, 4),
        "away_win": round(away_win / total, 4),
    }


def clean_sheet_probabilities(matrix: list[list[float]]) -> dict:
    """
    הסתברות שער נקי לכל צד מתוך מטריצת התוצאות:
    - home = הסתברות שהאורחת לא כובשת (עמודה 0)
    - away = הסתברות שהמארחת לא כובשת (שורה 0)
    """
    total = sum(p for row in matrix for p in row) or 1.0
    home_cs = sum(row[0] for row in matrix)          # away scores 0
    away_cs = sum(matrix[0]) if matrix else 0.0       # home scores 0
    return {"home": round(home_cs / total, 4), "away": round(away_cs / total, 4)}


def ranked_scorelines(matrix: list[list[float]], top: int = 4) -> list[dict]:
    """התוצאות המדויקות בעלות ההסתברות הגבוהה ביותר."""
    cells = [
        {"score": f"{i}-{j}", "home": i, "away": j, "prob": round(p, 4)}
        for i, row in enumerate(matrix)
        for j, p in enumerate(row)
    ]
    cells.sort(key=lambda c: c["prob"], reverse=True)
    return cells[:top]


# --------------------------------------------------------------------------- #
# אופטימיזציה לפי שיטת הניקוד של קבוצת הניחושים (תוחלת נקודות)
# --------------------------------------------------------------------------- #
def _sign(h: int, a: int) -> int:
    return (h > a) - (h < a)   # 1 ביתי, -1 חוץ, 0 תיקו


def match_points(ph: int, pa: int, ah: int, aa: int, scoring: dict) -> float:
    """נקודות (תלויות-ניחוש) על ניחוש ph-pa כשהתוצאה בפועל ah-aa."""
    if ph == ah and pa == aa:
        return scoring.get("exact", 3)
    dp, da = _sign(ph, pa), _sign(ah, aa)
    if dp == da:                       # כיוון נכון (כולל תיקו נכון)
        return scoring.get("direction", 1)
    if ph == aa and pa == ah:          # קנס: **ההפך המדויק** של התוצאה (מראה)
        return scoring.get("reversed", -1)
    return 0.0                         # כיוון שגוי שאינו ההפך-המדויק = פספוס


def expected_points(ph: int, pa: int, matrix: list[list[float]], scoring: dict) -> float:
    """תוחלת הנקודות התלויות-ניחוש על פני כל התוצאות האפשריות."""
    return sum(
        p * match_points(ph, pa, i, j, scoring)
        for i, row in enumerate(matrix) for j, p in enumerate(row)
    )


def expected_goals_bonus(matrix: list[list[float]], scoring: dict) -> float:
    """תוחלת בונוס השערים (לא תלוי בניחוש) — קבוע לכל ההמלצות."""
    val = scoring.get("goals_bonus", 0)
    if not val:
        return 0.0
    thr = scoring.get("goals_bonus_threshold", 3)
    p = sum(p for i, row in enumerate(matrix)
            for j, p in enumerate(row) if i + j > thr)
    return p * val


def ranked_by_expected_points(matrix: list[list[float]], scoring: dict,
                              max_goals: int, top: int = 4) -> list[dict]:
    """כל הניחושים מדורגים לפי תוחלת נקודות (הבחירה המשתלמת ביותר)."""
    cands = [
        {"score": f"{ph}-{pa}", "home": ph, "away": pa,
         "ep": round(expected_points(ph, pa, matrix, scoring), 3)}
        for ph in range(max_goals + 1) for pa in range(max_goals + 1)
    ]
    cands.sort(key=lambda c: c["ep"], reverse=True)
    return cands[:top]


# --------------------------------------------------------------------------- #
# ציון אמון והסבר
# --------------------------------------------------------------------------- #
def confidence_score(probs: dict, scorelines: list[dict], context: dict | None) -> int:
    """ציון אמון 0-100 לפי בולטות התוצאה, פער הפייבוריט וודאות ההרכב."""
    context = context or {}
    favorite_edge = max(probs.values()) - sorted(probs.values())[-2]
    top_score_prob = scorelines[0]["prob"] if scorelines else 0

    base = 0.5 * favorite_edge + 0.5 * (top_score_prob / 0.15)
    base = min(base, 1.0)

    if context.get("lineup_confidence") == "low":
        base *= 0.8
    if (context.get("injury_count", 0) or 0) >= 4:
        base *= 0.9

    return int(round(base * 100))


def _explain(home_name, away_name, home_xg, away_xg, probs,
             recommended, most_likely, recommended_ep, context) -> str:
    """הסבר קצר בעברית."""
    if probs["home_win"] >= probs["away_win"] and probs["home_win"] >= probs["draw"]:
        lean = f"יתרון ל{home_name}"
    elif probs["away_win"] >= probs["draw"]:
        lean = f"יתרון ל{away_name}"
    else:
        lean = "נטייה לתיקו"
    note = ""
    if context and (context.get("injury_count", 0) or 0) >= 3:
        note = " הרכב מוחלש בשל פציעות."
    likely_note = "" if recommended == most_likely else f" (הכי סביר: {most_likely})"
    return (
        f"{lean}. צפי שערים {home_xg:.2f}-{away_xg:.2f}. "
        f"המלצה לניקוד: {recommended} — תוחלת {recommended_ep} נק'{likely_note}.{note}"
    )


# --------------------------------------------------------------------------- #
# חיזוי משחק בודד
# --------------------------------------------------------------------------- #
def _home_advantage_for(home_name) -> float:
    """יתרון ביתי מותנה: גבוה למארחת שמשחקת בביתה, אחרת ניטרלי (config.HOME_ADVANTAGE)."""
    hosts = {str(h).strip().lower() for h in getattr(config, "HOST_NATIONS", set())}
    if str(home_name or "").strip().lower() in hosts:
        # יחסי: תמיד מעל הניטרלי, גם אם auto-tune שינה את HOME_ADVANTAGE
        return config.HOME_ADVANTAGE + getattr(config, "HOST_HOME_BONUS", 0.20)
    return config.HOME_ADVANTAGE


def _round_goals(x: float) -> int:
    """עיגול שערים שמרני (חצי-למטה): מעגל למעלה רק כשהשבר *גדול* מ-0.5.
    כך xG=4.5 (תקרת המודל) הופך ל-4 ולא 5 — תואם את התוצאה הסבירה ביותר (מוד
    פואסון = floor(μ)) ולא מגזים במספר השערים לקבוצה."""
    return int(x) + (1 if (x - int(x)) > 0.5 else 0)


def _realistic_scoreline(home_xg: float, away_xg: float, probs: dict,
                         cap: int = 6) -> str:
    """ניחוש מגוון/ריאלי לדוח: ספירת השערים מעיגול (שמרני) של ה-xG, אך הכיוון לפי
    1X2 המשוקלל. תיקו נבחר כשההסתברות לתיקו גבוהה דיה (סף) — כי במודל הוא כמעט אף
    פעם לא ה'הכי סביר', אך קורה ~30%. פחות 1-0, יותר שערים, תיקו ריאלי, בלי הגזמה."""
    probs = probs or {}
    hw, dr, aw = (probs.get("home_win", 0.0), probs.get("draw", 0.0),
                  probs.get("away_win", 0.0))
    thr = getattr(config, "DRAW_PREDICT_THRESHOLD", 0.27)
    h, a = _round_goals(home_xg), _round_goals(away_xg)
    if dr >= thr or (dr >= hw and dr >= aw):           # תיקו סביר דיו
        g = min(_round_goals((home_xg + away_xg) / 2), cap)
        return f"{g}-{g}"
    if hw >= aw and h <= a:                              # פייבוריט ביתי מנצח
        h = a + 1
    elif aw > hw and a <= h:                             # פייבוריט חוץ מנצח
        a = h + 1
    return f"{min(h, cap)}-{min(a, cap)}"


def predict_match(match: dict, teams_by_name: dict[str, dict]) -> dict:
    """מחזיר חיזוי מלא למשחק אחד."""
    home_name = match.get("home_team")
    away_name = match.get("away_team")
    home = teams_by_name.get(home_name, {})
    away = teams_by_name.get(away_name, {})
    context = dict(match.get("context") or {})
    context.setdefault("stage", match.get("stage"))
    # יתרון ביתי מותנה — מארחת בביתה מקבלת יתרון גבוה, אחרת ניטרלי
    context["home_advantage"] = _home_advantage_for(home_name)

    home_xg, away_xg = expected_goals(home, away, context)
    matrix = score_matrix(home_xg, away_xg, config.MAX_GOALS_GRID)
    model_probs = outcome_probabilities(matrix)

    market_probs = match.get("market_probabilities")
    probs = blend_probabilities(model_probs, market_probs, config.MARKET_BLEND_WEIGHT)
    market_agrees = (
        _favorite(model_probs) == _favorite(market_probs) if market_probs else None
    )

    scorelines = ranked_scorelines(matrix, top=4)
    best = scorelines[0] if scorelines else {"score": "1-1"}
    confidence = confidence_score(probs, scorelines, context)
    # אם השוק חולק על המודל לגבי הפייבוריט — מורידים ביטחון
    if market_agrees is False:
        confidence = int(round(confidence * 0.8))

    # ההמלצה נבחרת כך שתמקסם תוחלת נקודות תחת שיטת הניקוד של הקבוצה
    scoring = getattr(config, "PREDICTION_SCORING", {})
    ev_ranked = ranked_by_expected_points(matrix, scoring, config.MAX_GOALS_GRID)
    ev_best = ev_ranked[0] if ev_ranked else best
    bonus_ep = expected_goals_bonus(matrix, scoring)
    recommended = ev_best["score"]
    recommended_ep = round(ev_best.get("ep", 0.0) + bonus_ep, 2)

    # ניחוש לדוח — מגוון וריאלי: ספירת שערים מעיגול ה-xG, אבל הכיוון (נצחון/תיקו)
    # לפי ההסתברות 1X2 המשוקללת. כך פחות 1-0, יותר שערים, ותיקו כשהוא הסביר ביותר.
    predicted = _realistic_scoreline(home_xg, away_xg, probs)

    return {
        "match_id": match.get("match_id"),
        "home_team": home_name,
        "away_team": away_name,
        "date": match.get("date"),
        "kickoff": match.get("kickoff"),
        "stage": match.get("stage"),
        "expected_goals": {"home": home_xg, "away": away_xg},
        "total_expected_goals": round(home_xg + away_xg, 2),   # תוחלת שערים סהכ
        "clean_sheet": clean_sheet_probabilities(matrix),      # שער נקי לכל צד
        "predicted_score": predicted,                # ניחוש הדוח — מגוון/ריאלי (xG+1X2)
        "recommended_score": recommended,            # ממוקסם לפי תוחלת נקודות (שמרני)
        "recommended_ep": recommended_ep,            # תוחלת הנקודות של ההמלצה
        "most_likely_score": best["score"],          # התוצאה הסבירה ביותר (לעיון)
        "alternatives": [s["score"] for s in ev_ranked[1:4]],
        "scoreline_probabilities": scorelines,
        "outcome_probabilities": probs,
        "model_probabilities": model_probs,
        "market_probabilities": market_probs,
        "market_sources": (market_probs or {}).get("sources") if market_probs else None,
        "market_agrees": market_agrees,
        "confidence": confidence,
        "explanation": _explain(home_name, away_name, home_xg, away_xg,
                                probs, recommended, best["score"], recommended_ep, context),
    }


def predict_all(db: dict) -> list[dict]:
    """מחזיר חיזוי לכל המשחקים ב-DB. מסנן לפי סף אמון אם הוגדר."""
    teams_by_name = {t.get("team_name"): t for t in db.get("teams", [])}
    predictions = []
    for match in db.get("matches", []):
        try:
            pred = predict_match(match, teams_by_name)
        except Exception as exc:  # noqa: BLE001
            log.error("חיזוי נכשל למשחק %s: %s", match.get("match_id"), exc)
            continue
        if pred["confidence"] >= config.MIN_CONFIDENCE:
            predictions.append(pred)
    log.info("הופקו %d חיזויים", len(predictions))
    return predictions


# --------------------------------------------------------------------------- #
# עזרים
# --------------------------------------------------------------------------- #
def _safe(value, fallback: float) -> float:
    try:
        v = float(value)
        return v if v > 0 else fallback
    except (TypeError, ValueError):
        return fallback


def _is_knockout(stage) -> bool:
    if not stage:
        return False
    s = str(stage).lower()
    return any(k in s for k in ("knockout", "round of", "quarter", "semi", "final", "16"))
