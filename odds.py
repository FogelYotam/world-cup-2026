"""
שקלול אודדס שוק — קונצנזוס 1X2 נטול-מרווח (vig), לשקלול לתוך הניבוי.

**שני מקורות, לפי סדר עדיפות (`fetch_market_odds`):**
1. 🥇 **the-odds-api.com** — אודדס אמיתיים מבוקמייקרים חיים, **קריאה אחת** לכל הטורניר.
   אמין ובלי תלות במכסת Gemini. דורש `ODDS_API_KEY` (מפתח חינמי, 500 קריאות/חודש).
2. 🥈 **Gemini** — קונצנזוס-אנליסט המשקף את השוק (`config.ODDS_SOURCES`), קריאה אחת
   לכל המשחקים שה-API לא כיסה. תלוי במכסה החינמית הקטנה (~20/יום).

בלי שני המקורות — מוחזר ריק והמערכת נשענת על מודל הפואסון בלבד. לעולם לא זורק חריגה.
"""
from __future__ import annotations

import unicodedata

import config
import utils

log = utils.get_logger("odds")


# --------------------------------------------------------------------------- #
# נרמול שמות נבחרות — להתאמת שמות ה-API לשמות הפיד הרשמי שלנו
# --------------------------------------------------------------------------- #
def _norm(s) -> str:
    """אותיות קטנות בלי דיאקריטיקה ורווחים עודפים — להתאמת זוגות-נבחרות."""
    decomp = unicodedata.normalize("NFKD", str(s or "").lower())
    return "".join(c for c in decomp if not unicodedata.combining(c)).strip()


# שמות the-odds-api (מנורמלים) -> שמות הפיד הרשמי שלנו (מנורמלים)
_ODDS_ALIASES = {
    "south korea": "korea republic",
    "ivory coast": "cote d'ivoire",
    "iran": "ir iran",
    "united states": "usa",
    "turkey": "turkiye",
    "dr congo": "congo dr",
    "cape verde": "cabo verde",
    "czech republic": "czechia",
}


def _alias(name) -> str:
    """ממיר שם נבחרת מ-the-odds-api למרחב השמות המנורמל של הפיד שלנו."""
    n = _norm(name)
    return _ODDS_ALIASES.get(n, n)


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
    מחזיר מיפוי match_id -> {home_win, draw, away_win, sources} לפי קונצנזוס השוק.

    **קריאת Gemini אחת לכל המשחקים** (לא אחת-לכל-משחק) — קריטי כי המכסה החינמית
    קטנה (~20/יום), וקריאה-לכל-משחק הייתה מפוצצת אותה לבדה. בכל כשל מחזיר ריק.
    """
    out: dict[str, dict] = {}
    if gemini is None or not getattr(gemini, "enabled", False):
        log.info("Gemini מושבת — מדלג על שליפת אודדס")
        return out
    valid = [m for m in matches
             if m.get("match_id") is not None and m.get("home_team") and m.get("away_team")]
    if not valid:
        return out

    by_id = {str(m["match_id"]): m["match_id"] for m in valid}
    lines = "\n".join(f'{m["match_id"]}: {m["home_team"]} vs {m["away_team"]}' for m in valid)
    source_list = ", ".join(config.ODDS_SOURCES)
    prompt = (
        f"אתה אנליסט הימורים. לכל משחק ברשימה הערך את הסתברויות התוצאה (1X2) "
        f"המשקפות את קונצנזוס השוק ({source_list}) וחוזק/כושר הנבחרות "
        f"ב-{config.COMPETITION}:\n{lines}\n"
        "החזר אך ורק JSON: {\"matches\": [{\"id\": <match_id מהרשימה>, "
        "\"home_win\": num, \"draw\": num, \"away_win\": num}]} — הסתברויות "
        "מנורמלות (סכום 1) לכל משחק."
    )
    raw = gemini.ask_json(prompt, default=None)
    rows = (raw or {}).get("matches") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        log.warning("שליפת אודדס: לא התקבל מידע שמיש (מכסה/פורמט)")
        return out
    n_src = len(config.ODDS_SOURCES)
    for r in rows:
        if not isinstance(r, dict):
            continue
        mid = by_id.get(str(r.get("id")))
        norm = remove_vig(r.get("home_win"), r.get("draw"), r.get("away_win"))
        if mid is not None and norm and _is_plausible(norm):
            norm["sources"] = n_src
            out[mid] = norm
    log.info("נאספו אודדס ל-%d משחקים (קריאה אחת)", len(out))
    return out


# --------------------------------------------------------------------------- #
# שליפה דרך the-odds-api.com (אודדס אמיתיים, חיים) — המקור הראשי
# --------------------------------------------------------------------------- #
def _probs_from_event(ev: dict, home_name, away_name) -> dict | None:
    """
    ממצע 1X2 נטול-מרווח על פני כל הבוקמייקרים באירוע the-odds-api יחיד.
    כל בוקמייקר מספק שוק 'h2h' עם שלוש תוצאות (בית/תיקו/חוץ) באודדס דצימליים.
    """
    books = ev.get("bookmakers")
    if not isinstance(books, list):
        return None
    triplets = []
    for b in books:
        markets = b.get("markets") if isinstance(b, dict) else None
        if not isinstance(markets, list):
            continue
        h2h = next((mk for mk in markets
                    if isinstance(mk, dict) and mk.get("key") == "h2h"), None)
        if not h2h:
            continue
        prices = {}
        for o in h2h.get("outcomes", []) or []:
            if not isinstance(o, dict):
                continue
            nm = o.get("name")
            if nm == home_name:
                prices["h"] = o.get("price")
            elif nm == away_name:
                prices["a"] = o.get("price")
            elif _norm(nm) in ("draw", "tie"):
                prices["d"] = o.get("price")
        h = implied_from_decimal(prices.get("h"))
        d = implied_from_decimal(prices.get("d"))
        a = implied_from_decimal(prices.get("a"))
        if None in (h, d, a):
            continue
        no_vig = remove_vig(h, d, a)
        if no_vig:
            triplets.append(no_vig)
    return consensus_probabilities(triplets)


def fetch_odds_api(matches: list[dict]) -> dict[str, dict]:
    """
    אודדס אמיתיים מ-the-odds-api.com — **קריאה אחת** לכל הטורניר.
    מחזיר match_id -> {home_win, draw, away_win, sources}. בכל כשל מחזיר {}.
    התאמת אירוע->משחק לפי זוג-נבחרות מנורמל (עם אליאסים לשמות שונים).
    """
    out: dict[str, dict] = {}
    if not getattr(config, "ODDS_API_KEY", None):
        return out
    valid = [m for m in matches if m.get("match_id") is not None
             and m.get("home_team") and m.get("away_team")]
    if not valid:
        return out

    url = f"{config.ODDS_API_BASE}/sports/{config.ODDS_API_SPORT}/odds"
    params = {"apiKey": config.ODDS_API_KEY, "regions": config.ODDS_API_REGIONS,
              "markets": "h2h", "oddsFormat": "decimal"}
    resp = utils.safe_get(url, params=params)
    if resp is None:
        log.warning("odds-api: אין תגובה (רשת/מפתח) — נופלים ל-Gemini")
        return out
    try:
        events = resp.json()
    except Exception:  # noqa: BLE001
        log.warning("odds-api: תגובה לא-JSON")
        return out
    if not isinstance(events, list):
        return out

    by_pair = {frozenset((_norm(m["home_team"]), _norm(m["away_team"]))): m
               for m in valid}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        m = by_pair.get(frozenset((_alias(ev.get("home_team")),
                                   _alias(ev.get("away_team")))))
        if not m:
            continue
        probs = _probs_from_event(ev, ev.get("home_team"), ev.get("away_team"))
        if probs and _is_plausible(probs):
            out[m["match_id"]] = probs
    log.info("odds-api: נאספו אודדס ל-%d/%d משחקים (קריאה אחת)", len(out), len(valid))
    return out


# --------------------------------------------------------------------------- #
# מקור משולב — API ראשי, Gemini גיבוי לפערים
# --------------------------------------------------------------------------- #
def fetch_market_odds(matches: list[dict], gemini=None) -> dict[str, dict]:
    """
    מקור האודדס המאוחד של המערכת, לפי עדיפות:
    1) the-odds-api (אמיתי, אמין) → 2) קונצנזוס Gemini למשחקים שנותרו → 3) ריק.
    """
    out = fetch_odds_api(matches)
    missing = [m for m in matches if m.get("match_id") not in out]
    if missing and gemini is not None and getattr(gemini, "enabled", False):
        for mid, p in fetch_consensus_odds(gemini, missing).items():
            out.setdefault(mid, p)
    return out


def attach_to_matches(matches: list[dict], odds_map: dict[str, dict]) -> None:
    """מצמיד market_probabilities לכל משחק לפי המיפוי (במקום, in-place)."""
    for match in matches:
        mid = match.get("match_id")
        if mid in odds_map:
            match["market_probabilities"] = odds_map[mid]
