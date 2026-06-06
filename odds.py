"""
שקלול אודדס מאתרי הימורים — ממיר אודדס דצימליים מ-10 מקורות נפוצים
להסתברויות 1X2 נטולות מרווח (vig), וממצע ביניהם לקונצנזוס שוק.

מקור הנתונים: Gemini עם עיגון לחיפוש Google (כשמוגדר מפתח). בלי מפתח —
מוחזר מה שכבר שמור ב-db.json, ואם אין כלום מוחזר מילון ריק והמערכת
נשענת על מודל הפואסון בלבד. לעולם לא זורק חריגה.
"""
from __future__ import annotations

import config
import utils

log = utils.get_logger("odds")


# --------------------------------------------------------------------------- #
# מתמטיקה של אודדס
# --------------------------------------------------------------------------- #
def implied_from_decimal(decimal_odds) -> float | None:
    """הסתברות גולמית מאודד דצימלי (1/odds). מחזיר None אם לא תקין."""
    try:
        d = float(decimal_odds)
    except (TypeError, ValueError):
        return None
    if d <= 1.0:
        return None
    return 1.0 / d


def remove_vig(home: float, draw: float, away: float) -> dict | None:
    """מנרמל שלוש הסתברויות גולמיות כך שיסתכמו ל-1 (הסרת מרווח הסוכן)."""
    total = (home or 0) + (draw or 0) + (away or 0)
    if total <= 0:
        return None
    return {
        "home_win": round(home / total, 4),
        "draw": round(draw / total, 4),
        "away_win": round(away / total, 4),
    }


def consensus_probabilities(source_probs: list[dict]) -> dict | None:
    """ממצע הסתברויות 1X2 נטולות מרווח על פני מספר מקורות."""
    valid = [p for p in source_probs if _is_prob_triplet(p)]
    if not valid:
        return None
    n = len(valid)
    avg = {
        "home_win": round(sum(p["home_win"] for p in valid) / n, 4),
        "draw": round(sum(p["draw"] for p in valid) / n, 4),
        "away_win": round(sum(p["away_win"] for p in valid) / n, 4),
    }
    # נרמול אחרון ליתר ביטחון
    norm = remove_vig(avg["home_win"], avg["draw"], avg["away_win"]) or avg
    norm["sources"] = n
    return norm


def _is_prob_triplet(p) -> bool:
    return (
        isinstance(p, dict)
        and all(k in p for k in ("home_win", "draw", "away_win"))
        and all(isinstance(p.get(k), (int, float)) for k in ("home_win", "draw", "away_win"))
    )


def _is_plausible(p: dict) -> bool:
    """
    מסנן הסתברויות לא-סבירות שעלולות להגיע ממקור שהמציא נתונים:
    הסכום חייב להיות קרוב ל-1, כל ערך בטווח [0,1], ובמשחק כדורגל
    התיקו כמעט אף פעם לא מתחת ל-5% (מנבא טריפלט מנוון/מומצא).
    """
    if not _is_prob_triplet(p):
        return False
    vals = [p["home_win"], p["draw"], p["away_win"]]
    if any(v < 0.0 or v > 1.0 for v in vals):
        return False
    if abs(sum(vals) - 1.0) > 0.05:
        return False
    if p["draw"] < 0.05:
        return False
    return True


# --------------------------------------------------------------------------- #
# המרת רשומת אודדס גולמית מ-Gemini -> הסתברויות קונצנזוס
# --------------------------------------------------------------------------- #
def _market_probs_from_raw(raw: dict) -> dict | None:
    """
    מקבל רשומת אודדס למשחק בודד ומחזיר הסתברויות קונצנזוס.

    תומך בשני פורמטים שמגיעים מ-Gemini:
    1) sources: [{home, draw, away}, ...]  — אודדס דצימליים לכל מקור
    2) probabilities: {home_win, draw, away_win}  — הסתברויות מוכנות
    """
    if not isinstance(raw, dict):
        return None

    sources = raw.get("sources")
    if isinstance(sources, list) and sources:
        triplets = []
        for s in sources:
            if not isinstance(s, dict):
                continue
            h = implied_from_decimal(s.get("home") or s.get("home_win"))
            d = implied_from_decimal(s.get("draw"))
            a = implied_from_decimal(s.get("away") or s.get("away_win"))
            if None in (h, d, a):
                continue
            no_vig = remove_vig(h, d, a)
            if no_vig:
                triplets.append(no_vig)
        cons = consensus_probabilities(triplets)
        if cons and _is_plausible(cons):
            return cons

    probs = raw.get("probabilities")
    if _is_prob_triplet(probs):
        norm = remove_vig(probs["home_win"], probs["draw"], probs["away_win"])
        if norm and _is_plausible(norm):
            norm["sources"] = int(raw.get("source_count", 1) or 1)
            return norm

    return None


# --------------------------------------------------------------------------- #
# שליפה דרך Gemini
# --------------------------------------------------------------------------- #
def fetch_consensus_odds(gemini, matches: list[dict]) -> dict[str, dict]:
    """
    מחזיר מיפוי match_id -> {home_win, draw, away_win, sources} לפי קונצנזוס
    של מקורות ההימורים. בכל כשל מחזיר מה שאפשר (אולי ריק).
    """
    out: dict[str, dict] = {}
    if gemini is None or not getattr(gemini, "enabled", False):
        log.info("Gemini מושבת — מדלג על שליפת אודדס חיה")
        return out

    source_list = ", ".join(config.ODDS_SOURCES)
    for match in matches:
        mid = match.get("match_id")
        home, away = match.get("home_team"), match.get("away_team")
        if not (mid and home and away):
            continue
        prompt = (
            f"חפש את הסיכויים האמיתיים לתוצאת 1X2 במשחק {home} נגד {away} "
            f"ב-{config.COMPETITION}, לפי קונצנזוס המקורות: {source_list}. "
            "העדף הסתברויות מאגרגטורים (Oddschecker, Opta) על פני ניחוש. "
            "החזר JSON עם הסתברויות מנורמלות (סכום 1): "
            "{\"probabilities\": {\"home_win\": num, \"draw\": num, "
            "\"away_win\": num}, \"source_count\": int}. "
            "אם יש לך אודדס דצימליים אמיתיים פר-סוכן, החזר במקום: "
            "{\"sources\": [{\"bookmaker\": str, \"home\": num, \"draw\": num, "
            "\"away\": num}]}. אל תמציא — אם אין נתון, החזר source_count נמוך."
        )
        raw = gemini.ask_json(prompt, default=None)
        probs = _market_probs_from_raw(raw) if raw else None
        if probs:
            out[mid] = probs
            log.info("אודדס למשחק %s: %s מקורות", mid, probs.get("sources"))
    log.info("נאספו אודדס ל-%d משחקים", len(out))
    return out


def attach_to_matches(matches: list[dict], odds_map: dict[str, dict]) -> None:
    """מצמיד market_probabilities לכל משחק לפי המיפוי (במקום, in-place)."""
    for match in matches:
        mid = match.get("match_id")
        if mid in odds_map:
            match["market_probabilities"] = odds_map[mid]
