"""
איסוף נתונים ממקורות חינמיים והרכבתם לסכמה אחידה ב-data/db.json.

מקור ראשי: Gemini עם עיגון לחיפוש Google (נתונים עדכניים).
מקור משלים: ה-JSON הציבורי של Sofascore.
בכל כשל — נרשם לוג, מוחזרים ערכי fallback, וההרצה ממשיכה.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
import urllib.request

import config
import odds as odds_mod
import utils

log = utils.get_logger("scraper")


def _is_rate_limit(exc) -> bool:
    """מזהה שגיאות quota/rate-limit (429) כדי להחליט על retry."""
    msg = str(exc).lower()
    return any(w in msg for w in ("429", "quota", "rate limit", "ratelimit",
                                  "resource exhausted", "exceeded"))

# --------------------------------------------------------------------------- #
# לקוח Gemini
# --------------------------------------------------------------------------- #
_GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-flash-latest"]


class GeminiClient:
    """עטיפה דקה סביב Gemini שמחזירה JSON מובנה, עם עיגון לחיפוש כשאפשר."""

    def __init__(self) -> None:
        self._model = None
        self._quota_exhausted = False   # ברגע שאזלה המכסה — לא מנסים שוב בריצה זו
        if not config.gemini_enabled():
            log.warning("Gemini מושבת (אין מפתח) — נשתמש רק ב-scraping ו-fallback")
            return
        try:
            import google.generativeai as genai

            genai.configure(api_key=config.GEMINI_API_KEY)
            self._genai = genai
            self._model = self._build_model()
        except Exception as exc:  # noqa: BLE001
            log.error("אתחול Gemini נכשל: %s", exc)
            self._model = None

    def _search_tool(self):
        """כלי עיגון לחיפוש Google (פורמט proto של Gemini 2.x)."""
        try:
            protos = self._genai.protos
            return [protos.Tool(google_search=protos.Tool.GoogleSearch())]
        except Exception:  # noqa: BLE001
            return None

    def _build_model(self):
        """בונה מודל, עם ניסיון להפעיל עיגון לחיפוש Google."""
        search_tool = self._search_tool()
        last_err = None
        for name in _GEMINI_MODELS:
            try:
                return self._genai.GenerativeModel(
                    name,
                    tools=search_tool,
                    generation_config={"temperature": 0.2},
                )
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                try:
                    return self._genai.GenerativeModel(
                        name, generation_config={"temperature": 0.2}
                    )
                except Exception as exc2:  # noqa: BLE001
                    last_err = exc2
        log.error("בניית מודל Gemini נכשלה: %s", last_err)
        return None

    @property
    def enabled(self) -> bool:
        return self._model is not None

    def ask_json(self, prompt: str, default=None, retries: int = 2):
        """שולח prompt ומצפה ל-JSON. מנסה שוב עם backoff על rate-limit."""
        if not self.enabled or self._quota_exhausted:
            return default
        full = (
            prompt
            + "\n\nהחזר אך ורק JSON תקין, ללא טקסט נוסף וללא הסברים."
        )
        for attempt in range(retries + 1):
            try:
                resp = self._model.generate_content(full)
                text = (resp.text or "").strip()
            except Exception as exc:  # noqa: BLE001
                if _is_rate_limit(exc):
                    if attempt < retries:
                        wait = 5 * (attempt + 1)
                        log.warning("Gemini rate-limit — ממתין %ds (ניסיון %d)", wait, attempt + 1)
                        time.sleep(wait)
                        continue
                    self._quota_exhausted = True
                    log.warning("מכסת Gemini אזלה — מדלג על שאר קריאות Gemini בריצה זו")
                    return default
                log.error("קריאת Gemini נכשלה: %s", str(exc).split(chr(10))[0])
                return default

            parsed = _extract_json(text)
            if parsed is None:
                log.warning("לא הצלחתי לפרסר JSON מתשובת Gemini")
                return default
            return parsed
        return default

    def ask_text(self, prompt: str, default: str = "", retries: int = 1) -> str:
        """שיחה חופשית — מחזיר טקסט חופשי (לא JSON). לשיח על ההרכב בבוט."""
        if not self.enabled or self._quota_exhausted:
            return default
        for attempt in range(retries + 1):
            try:
                resp = self._model.generate_content(prompt)
                return (resp.text or "").strip() or default
            except Exception as exc:  # noqa: BLE001
                if _is_rate_limit(exc):
                    if attempt < retries:
                        time.sleep(5 * (attempt + 1))
                        continue
                    self._quota_exhausted = True
                log.error("צ'אט Gemini נכשל: %s", str(exc).split(chr(10))[0])
                return default
        return default

    def ask_json_image(self, prompt: str, image_bytes: bytes,
                       mime_type: str = "image/jpeg", default=None,
                       retries: int = 2):
        """שולח prompt + תמונה (Gemini Vision) ומצפה ל-JSON. retry על rate-limit."""
        if not self.enabled or self._quota_exhausted:
            return default
        full = prompt + "\n\nהחזר אך ורק JSON תקין, ללא טקסט נוסף וללא הסברים."
        parts = [full, {"mime_type": mime_type, "data": image_bytes}]
        for attempt in range(retries + 1):
            try:
                resp = self._model.generate_content(parts)
                text = (resp.text or "").strip()
            except Exception as exc:  # noqa: BLE001
                if _is_rate_limit(exc):
                    if attempt < retries:
                        wait = 5 * (attempt + 1)
                        log.warning("Gemini rate-limit (תמונה) — ממתין %ds (ניסיון %d)",
                                    wait, attempt + 1)
                        time.sleep(wait)
                        continue
                    self._quota_exhausted = True
                log.error("קריאת Gemini (תמונה) נכשלה: %s", str(exc).split(chr(10))[0])
                return default
            parsed = _extract_json(text)
            if parsed is None:
                log.warning("לא הצלחתי לפרסר JSON מתמונת ההרכב")
                return default
            return parsed
        return default


def _extract_json(text: str):
    """מחלץ JSON מטקסט — כולל הסרת גדרות ```json ... ```."""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                return None
    return None


# --------------------------------------------------------------------------- #
# בנאי סכמה — מבטיחים שכל רשומה כוללת את כל השדות הנדרשים
# --------------------------------------------------------------------------- #
def build_match(raw: dict) -> dict:
    return {
        "match_id": utils.coalesce(raw.get("match_id"), raw.get("id")),
        "competition": raw.get("competition", config.COMPETITION),
        "season": raw.get("season", config.SEASON),
        "date": raw.get("date"),
        "home_team": raw.get("home_team"),
        "away_team": raw.get("away_team"),
        "status": raw.get("status", "scheduled"),
        "venue": raw.get("venue"),
        "stage": raw.get("stage"),
        "score": raw.get("score"),
        "odds": raw.get("odds"),
    }


def build_team(raw: dict) -> dict:
    return {
        "team_id": utils.coalesce(raw.get("team_id"), raw.get("id")),
        "team_name": raw.get("team_name") or raw.get("name"),
        "goals_for": _num(raw.get("goals_for"), config.DEFAULT_GOALS_FOR),
        "goals_against": _num(raw.get("goals_against"), config.DEFAULT_GOALS_AGAINST),
        "clean_sheets": _num(raw.get("clean_sheets"), 0),
        "home_form": raw.get("home_form"),
        "away_form": raw.get("away_form"),
        "ranking": raw.get("ranking"),
        "recent_matches": raw.get("recent_matches", []),
    }


# נרמול שמות נבחרות — מאחד וריאנטים נפוצים לצורה קנונית (ללא רווחים/סימנים)
_NATION_ALIASES = {
    "czechrepublic": "czechia",
    "capeverde": "caboverde",
    "ivorycoast": "cotedivoire",
    "turkey": "turkiye",
    "unitedstates": "usa", "unitedstatesofamerica": "usa", "us": "usa",
    "korearepublic": "southkorea", "republicofkorea": "southkorea",
    "southkorearepublic": "southkorea",
    "democraticrepublicofcongo": "drcongo", "congodr": "drcongo",
}


def _clean_nation(name) -> str:
    """מנרמל שם נבחרת: ללא ניקוד/רישיות/סימנים, עם מיפוי וריאנטים נפוצים."""
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]", "", s.lower())
    return _NATION_ALIASES.get(s, s)


def participating_nations(db: dict) -> set[str]:
    """קבוצת הנבחרות המשתתפות בפועל (מנורמלת). מעדיפה את db['participants'] —
    48 שמות הסגלים הרשמיים מ-FIFA (מקור אמת); אחרת נופלת ל-db['teams']+משחקים."""
    explicit = db.get("participants")
    if isinstance(explicit, list) and len(explicit) >= 24:
        nats = {_clean_nation(x) for x in explicit}
        nats.discard("")
        return nats
    nats = {_clean_nation(t.get("team_name"))
            for t in db.get("teams", []) if t.get("team_name")}
    for key, fields in (("matches", ("home_team", "away_team")),
                        ("results", ("home", "away"))):
        for r in db.get(key, []) or []:
            for f in fields:
                if r.get(f):
                    nats.add(_clean_nation(r.get(f)))
    nats.discard("")
    return nats


def filter_to_participants(db: dict) -> int:
    """מסיר מהבריכה ומהדיפרנציאלים שחקנים מנבחרות שאינן משתתפות במונדיאל
    (למשל איטליה — לא העפילה). מחזיר כמה שחקנים הוסרו. best-effort; לא זורק."""
    nats = participating_nations(db)
    if len(nats) < 24:  # רשת ביטחון: אם אין רשימת נבחרות שמישה — לא מסננים בכלל
        log.warning("סינון נבחרות דולג — רשימת נבחרות חסרה (%d)", len(nats))
        return 0
    removed = 0
    players = db.get("players")
    if isinstance(players, list):
        kept = [p for p in players if _clean_nation(p.get("team")) in nats]
        removed += len(players) - len(kept)
        db["players"] = kept
    diffs = db.get("differentials")
    if isinstance(diffs, dict):
        for pos, lst in diffs.items():
            if isinstance(lst, list):
                kept = [e for e in lst if _clean_nation(e.get("team")) in nats]
                removed += len(lst) - len(kept)
                diffs[pos] = kept
    if removed:
        log.info("סינון נבחרות לא-משתתפות: הוסרו %d שחקנים", removed)
    return removed


def build_player(raw: dict) -> dict:
    return {
        "player_id": utils.coalesce(raw.get("player_id"), raw.get("id")),
        "player_name": raw.get("player_name") or raw.get("name"),
        "team": raw.get("team"),
        "position": raw.get("position"),
        "minutes": _num(raw.get("minutes"), 0),
        "goals": _num(raw.get("goals"), 0),
        "assists": _num(raw.get("assists"), 0),
        "clean_sheet_contrib": _num(raw.get("clean_sheet_contrib"), 0),
        "injury_status": raw.get("injury_status", "fit"),
        "suspension_status": raw.get("suspension_status", "available"),
        "expected_start": raw.get("expected_start"),
        "expected_points": raw.get("expected_points"),
        # שדות פנטזי מהאתרים המובילים (אופציונליים)
        "price": _num(raw.get("price"), None),
        "ownership": _num(raw.get("ownership"), None),
        "form": _num(raw.get("form"), None),
        "xg": _num(raw.get("xg"), None),
        "xa": _num(raw.get("xa"), None),
        "penalty_taker": bool(raw.get("penalty_taker")),  # בועט פנדלים — תקרת נקודות גבוהה
    }


def _num(value, fallback):
    """המרה בטוחה למספר עם fallback."""
    try:
        if value is None:
            return fallback
        return float(value)
    except (TypeError, ValueError):
        return fallback


# --------------------------------------------------------------------------- #
# איסוף דרך Gemini
# --------------------------------------------------------------------------- #
def fetch_upcoming_matches(gemini: GeminiClient, days_ahead: int = 3) -> list[dict]:
    """משחקים קרובים במונדיאל ל-X הימים הקרובים."""
    prompt = (
        f"מהם משחקי {config.COMPETITION} המתוכננים ב-{days_ahead} הימים הקרובים? "
        "החזר מערך JSON, כל איבר עם המפתחות: "
        "match_id, date (ISO), home_team, away_team, venue, stage, status."
    )
    raw = gemini.ask_json(prompt, default=[])
    if not isinstance(raw, list):
        log.warning("פורמט משחקים לא צפוי מ-Gemini")
        return []
    return [build_match(m) for m in raw if isinstance(m, dict)]


def fetch_team_stats(gemini: GeminiClient, team_name: str) -> dict:
    """סטטיסטיקות וכושר של נבחרת."""
    prompt = (
        f"ספק נתוני נבחרת '{team_name}' לקראת {config.COMPETITION}. "
        "החזר אובייקט JSON עם המפתחות: team_name, goals_for (ממוצע למשחק), "
        "goals_against (ממוצע למשחק), clean_sheets, home_form, away_form, "
        "ranking (דירוג FIFA), recent_matches (מערך תוצאות אחרונות)."
    )
    raw = gemini.ask_json(prompt, default={})
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("team_name", team_name)
    return build_team(raw)


def fetch_match_context(gemini: GeminiClient, match: dict) -> dict:
    """פציעות, הרכב צפוי ושחקני מפתח לשתי הנבחרות במשחק."""
    home, away = match.get("home_team"), match.get("away_team")
    prompt = (
        f"עבור המשחק {home} מול {away} ב-{config.COMPETITION}, ספק: "
        "(1) injuries — שחקנים פצועים/מורחקים בכל נבחרת; "
        "(2) expected_lineup — הרכב פותח צפוי לכל נבחרת; "
        "(3) key_players — שחקני מפתח עם position. "
        "החזר JSON עם המפתחות home ו-away, כל אחד מכיל "
        "injuries[], expected_lineup[], key_players[] "
        "(לכל שחקן: name, position, injury_status)."
    )
    return gemini.ask_json(prompt, default={"home": {}, "away": {}}) or {}


def _http_get_json(url: str, timeout: int = 20):
    """GET פשוט שמחזיר JSON (stdlib, ללא תלות). זורק בכשל — הקוראים עוטפים."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def fetch_official_pool() -> list[dict]:
    """מושך את בריכת השחקנים הרשמית של FIFA World Cup Fantasy (players.json +
    squads.json) — מקור האמת: רק שחקנים בסגלים הרשמיים, עם מחיר/בעלות/כושר/
    נקודות רשמיות. מחזיר רשימת build_player (עם recent_points מהממוצע הרשמי).
    best-effort; בכל כשל מחזיר [] כדי שניפול חזרה לבריכת Gemini."""
    try:
        squads = _http_get_json(config.FIFA_FANTASY_SQUADS_URL)
        players = _http_get_json(config.FIFA_FANTASY_PLAYERS_URL)
    except Exception as exc:  # noqa: BLE001
        log.warning("בריכה רשמית מ-FIFA נכשלה: %s — נופלים ל-Gemini", exc)
        return []
    if not isinstance(players, list) or not isinstance(squads, list):
        log.warning("בריכה רשמית מ-FIFA: מבנה לא צפוי")
        return []

    squad_name = {s.get("id"): s.get("name") for s in squads
                  if isinstance(s, dict) and s.get("name")}
    pool = []
    for p in players:
        if not isinstance(p, dict):
            continue
        name = p.get("knownName") or " ".join(
            x for x in (p.get("firstName"), p.get("lastName")) if x).strip()
        team = squad_name.get(p.get("squadId"))
        if not name or not team:
            continue
        stats = p.get("stats") or {}
        status = str(p.get("status") or "").lower()
        mstat = str(p.get("matchStatus") or "").lower()
        rec = build_player({
            "player_id": p.get("id"),
            "player_name": name,
            "team": team,
            "position": p.get("position"),
            "price": p.get("price"),
            "ownership": p.get("percentSelected"),
            "form": stats.get("form"),
            "expected_start": True if mstat == "start"
            else (False if mstat in ("sub", "not_in_squad") else None),
            # לא בסגל המחזור → לא זמין לבחירה; מורחק → suspended
            "injury_status": "out" if mstat == "not_in_squad" else "fit",
            "suspension_status": "suspended" if status == "suspended" else "available",
        })
        # נקודות פנטזי רשמיות (ממוצע למחזור) — אות 'recent_points' שמנוע הפנטזי משלב
        rec["recent_points"] = _num(stats.get("avgPoints"), None)
        rec["fifa_total_points"] = _num(stats.get("totalPoints"), None)
        pool.append(rec)
    log.info("בריכה רשמית מ-FIFA: %d שחקנים (%d נבחרות)", len(pool), len(squad_name))
    return pool


def official_differentials(pool: list[dict], counts: dict | None = None,
                           max_ownership: float | None = None) -> dict:
    """גוזר דיפרנציאלים מהבריכה הרשמית: בעלות נמוכה + מקום מובטח בהרכב,
    מדורגים לפי נקודות הפנטזי הרשמיות. מחזיר {GK:[...],DEF:[...],MID:[...],FWD:[...]}."""
    counts = counts or getattr(config, "DIFFERENTIAL_COUNTS",
                               {"GK": 3, "DEF": 5, "MID": 5, "FWD": 3})
    thr = max_ownership if max_ownership is not None else getattr(
        config, "DIFFERENTIAL_MAX_OWNERSHIP", 5.0)
    out: dict[str, list] = {}
    for pos, n in counts.items():
        cands = [p for p in pool
                 if p.get("position") == pos
                 and p.get("expected_start") is True
                 and _num(p.get("ownership"), 999) <= thr]
        cands.sort(key=lambda p: (_num(p.get("recent_points"), 0),
                                  _num(p.get("form"), 0)), reverse=True)
        out[pos] = [{
            "player_name": p["player_name"], "team": p["team"], "position": pos,
            "ownership": p.get("ownership"), "price": p.get("price"),
            "expected_points": p.get("recent_points"), "expected_start": True,
            "reason": f"בעלות {p.get('ownership')}% · ממוצע רשמי {p.get('recent_points')} נק'",
        } for p in cands[:n]]
    return out


def fetch_fantasy_player_pool(gemini: GeminiClient, limit: int = 120) -> list[dict]:
    """אוסף בריכת שחקני פנטזי רחבה מהאתרים המובילים בעולם (config.FANTASY_SOURCES),
    עם מחיר/בעלות/כושר/xG — כדי שמגבלת התקציב והפיזור בין נבחרות יהיו אמיתיים.
    best-effort; בכשל מחזיר []."""
    if not getattr(gemini, "enabled", False):
        return []
    sources = ", ".join(config.FANTASY_SOURCES)
    prompt = (
        f"בהתבסס על המקורות המובילים בעולם לפנטזי כדורגל ({sources}), החזר את "
        f"{limit} השחקנים הרלוונטיים ביותר ל-FIFA Fantasy ב-{config.COMPETITION} "
        "(מגוון נבחרות ועמדות). חובה: רק שחקנים מנבחרות שהעפילו למונדיאל 2026 "
        "ושנמצאים בסגל ה-26 הרשמי שלהן (איטליה, למשל, לא העפילה — לא לכלול). "
        "לכל שחקן ספק נתונים עדכניים. החזר JSON: "
        "{\"players\": [{\"name\": str, \"team\": str, "
        "\"position\": \"GK\"|\"DEF\"|\"MID\"|\"FWD\", "
        "\"price\": number (מחיר FIFA Fantasy במיליון), "
        "\"ownership\": number (אחוז בעלות), \"form\": number, "
        "\"xg\": number, \"xa\": number, \"goals\": number, \"assists\": number, "
        "\"minutes\": number, \"injury_status\": \"fit\"|\"doubtful\"|\"injured\"|\"out\", "
        "\"expected_start\": boolean}]}"
    )
    raw = gemini.ask_json(prompt, default=None)
    rows = (raw or {}).get("players") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        log.warning("בריכת פנטזי: לא התקבל מידע שמיש מהמקורות")
        return []
    pool = []
    for r in rows:
        if isinstance(r, dict) and (r.get("name") or r.get("player_name")) and r.get("team"):
            pool.append(build_player(r))
    log.info("נאספו %d שחקני פנטזי מהאתרים המובילים", len(pool))
    return pool


def fetch_differentials(gemini: GeminiClient, counts: dict | None = None) -> dict:
    """מאתר את שחקני ה-DIFFERENTIAL הטובים ביותר לכל עמדה — בעלות נמוכה (<5%)
    ומקום מובטח בהרכב — מתוך כל מאגר ה-FIFA Fantasy (48 נבחרות, 1000+ שחקנים).
    מחזיר {GK:[...], DEF:[...], MID:[...], FWD:[...]}. best-effort."""
    if not getattr(gemini, "enabled", False):
        return {}
    counts = counts or getattr(config, "DIFFERENTIAL_COUNTS",
                               {"GK": 3, "DEF": 5, "MID": 5, "FWD": 3})
    sources = ", ".join(config.FANTASY_SOURCES)
    thr = getattr(config, "DIFFERENTIAL_MAX_OWNERSHIP", 5.0)
    prompt = (
        f"סרוק את כל מאגר השחקנים של {config.COMPETITION} Fantasy — 48 נבחרות, "
        f"1000+ שחקנים — בהתבסס על המקורות: {sources}. "
        f"מצא את שחקני ה-DIFFERENTIAL הטובים ביותר לכל עמדה: בעלות (ownership) "
        f"מתחת ל-{thr}%, **ועם מקום מובטח בהרכב הפותח** (לא ספסלנים/סיכון רוטציה), "
        "וערך גבוה (כושר, פיקסצ'ר קל, בעיטות עונשין/קרן). "
        "חובה: רק שחקנים מנבחרות שהעפילו למונדיאל 2026 ובסגל ה-26 הרשמי "
        "(איטליה לא העפילה — לא לכלול). "
        f"החזר בדיוק: {counts.get('GK',3)} שוערים, {counts.get('DEF',5)} מגנים, "
        f"{counts.get('MID',5)} קשרים, {counts.get('FWD',3)} חלוצים. "
        "JSON בלבד: {\"GK\":[{\"name\":str,\"team\":str,\"ownership\":number,"
        "\"price\":number,\"expected_points\":number,"
        "\"expected_start\":boolean,\"reason\":str}],"
        "\"DEF\":[...],\"MID\":[...],\"FWD\":[...]}"
    )
    raw = gemini.ask_json(prompt, default=None)
    if not isinstance(raw, dict):
        log.warning("שליפת דיפרנציאלים: לא התקבל מידע שמיש")
        return {}
    out: dict[str, list] = {}
    for pos in ("GK", "DEF", "MID", "FWD"):
        items = raw.get(pos) or []
        rows = []
        for it in items:
            if not isinstance(it, dict):
                continue
            name = it.get("name") or it.get("player_name")
            if not name:
                continue
            rows.append({
                "player_name": name, "team": it.get("team"), "position": pos,
                "ownership": _num(it.get("ownership"), None),
                "price": _num(it.get("price"), None),
                "expected_points": _num(it.get("expected_points"), None),
                "expected_start": it.get("expected_start"),
                "reason": it.get("reason"),
            })
        out[pos] = rows
    log.info("שליפת דיפרנציאלים: %s", {k: len(v) for k, v in out.items()})
    return out


def fetch_fixture_difficulty(gemini: GeminiClient) -> dict:
    """לכל נבחרת: יריב המשחק הקרוב + דרגת קושי (0=קל, 1=קשה) לפי חוזק היריב.
    מחזיר {team: {opponent, difficulty}}. משמש את המלצות החילוף. best-effort."""
    if not getattr(gemini, "enabled", False):
        return {}
    prompt = (
        f"עבור המחזור הקרוב ב-{config.COMPETITION} (סבב משחקי שלב הבתים הבא), "
        "לכל נבחרת ציין את יריב המשחק הקרוב ואת דרגת הקושי: מספר בין 0.0 (קל מאוד) "
        "ל-1.0 (קשה מאוד), לפי חוזק היריב והסיכוי של הנבחרת לנצח. "
        "החזר JSON: {\"fixtures\": [{\"team\": str, \"opponent\": str, "
        "\"difficulty\": number}]} — שמות נבחרות באנגלית."
    )
    raw = gemini.ask_json(prompt, default=None)
    rows = (raw or {}).get("fixtures") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        log.warning("קושי מחזור: לא התקבל מידע שמיש")
        return {}
    out = {}
    for r in rows:
        if isinstance(r, dict) and r.get("team"):
            out[r["team"]] = {"opponent": r.get("opponent"),
                              "difficulty": _num(r.get("difficulty"), None)}
    log.info("קושי מחזור: נטענו %d נבחרות", len(out))
    return out


# --------------------------------------------------------------------------- #
# ריענון פציעות לנתונים קיימים (לפני תחילת המונדיאל / כשאין משחקים חדשים)
# --------------------------------------------------------------------------- #
_OUT_WORDS = ("out", "ruled out", "injured", "torn", "surgery", "season")
_DOUBT_WORDS = ("doubt", "doubtful", "uncertain", "race", "fitness", "minor")


def _refresh_injuries(gemini: GeminiClient, db: dict) -> None:
    """שאילתת grounding אחת לעדכון סטטוס פציעה/הרחקה לשחקנים הקיימים ב-DB."""
    players = db.get("players", [])
    if not players or not getattr(gemini, "enabled", False):
        return
    prompt = (
        f"לקראת {config.COMPETITION} (יוני 2026), מי מהשחקנים הבולטים פצוע, "
        "מוטל בספק, או מורחק כרגע? החזר JSON: "
        "{\"players\": [{\"name\": str, \"team\": str, \"status\": "
        "\"out\"|\"doubtful\"|\"fit\"}]}"
    )
    raw = gemini.ask_json(prompt, default=None)
    updates = (raw or {}).get("players") if isinstance(raw, dict) else None
    if not isinstance(updates, list):
        log.warning("ריענון פציעות: לא התקבל מידע שמיש")
        return

    status_by_name = {}
    for u in updates:
        if isinstance(u, dict) and u.get("name"):
            status_by_name[_norm(u["name"])] = _classify_status(u.get("status"))

    changed = 0
    for p in players:
        st = status_by_name.get(_norm(p.get("player_name")))
        if st is None:
            continue
        if st == "fit":
            if p.get("injury_status") != "fit":
                p["injury_status"] = "fit"
                changed += 1
        elif p.get("injury_status") != st:
            p["injury_status"] = st
            p["expected_start"] = False
            changed += 1
    log.info("ריענון פציעות: עודכנו %d שחקנים", changed)


def _enrich_fantasy_data(gemini: GeminiClient, db: dict) -> None:
    """מעשיר את שחקני ה-DB בנתוני פנטזי (מחיר, בעלות, כושר, xG/xA) שנאספים
    מהאתרים המומלצים בעולם (config.FANTASY_SOURCES). שאילתה אחת, best-effort.
    כל כשל נרשם ולא מפיל את הצינור."""
    players = db.get("players", [])
    if not players or not getattr(gemini, "enabled", False):
        return
    sources = ", ".join(config.FANTASY_SOURCES)
    names = ", ".join(
        sorted({str(p.get("player_name")) for p in players if p.get("player_name")})
    )[:1500]
    prompt = (
        f"בהתבסס על המקורות המובילים לפנטזי כדורגל ({sources}), ספק נתוני פנטזי "
        f"עדכניים ל-{config.COMPETITION} עבור השחקנים הבאים: {names}. "
        "החזר JSON: {\"players\": [{\"name\": str, \"team\": str, "
        "\"price\": number (מיליון), \"ownership\": number (אחוז), "
        "\"form\": number, \"xg\": number, \"xa\": number, "
        "\"goals\": number, \"assists\": number, \"minutes\": number, "
        "\"penalty_taker\": boolean (האם בועט הפנדלים הראשי של הנבחרת), "
        "\"expected_points\": number}]}"
    )
    raw = gemini.ask_json(prompt, default=None)
    updates = (raw or {}).get("players") if isinstance(raw, dict) else None
    if not isinstance(updates, list):
        log.warning("העשרת פנטזי: לא התקבל מידע שמיש")
        return

    by_name: dict[str, dict] = {}
    for u in updates:
        if isinstance(u, dict) and u.get("name"):
            by_name[_norm(u["name"])] = u

    _NUM_FIELDS = ("price", "ownership", "form", "xg", "xa",
                   "goals", "assists", "minutes", "expected_points")
    changed = 0
    for p in players:
        u = by_name.get(_norm(p.get("player_name")))
        if not u:
            continue
        touched = False
        for f in _NUM_FIELDS:
            val = _num(u.get(f), None)
            if val is not None:
                p[f] = val
                touched = True
        if u.get("penalty_taker") is not None:
            p["penalty_taker"] = bool(u.get("penalty_taker"))
            touched = True
        changed += int(touched)
    log.info("העשרת פנטזי: עודכנו %d שחקנים ממקורות הפנטזי", changed)


def ingest_results(gemini: GeminiClient, db: dict) -> int:
    """מושך תוצאות אמת אחרונות מהמונדיאל ומלמד מהן את המודל.

    שומר את התוצאות ב-db['results'] (ללא כפילויות) ומשקלל אותן לתוך ממוצעי
    השערים של הנבחרות (EWMA) כך שהחיזויים משתפרים מתוצאות אמיתיות.
    מחזיר כמה תוצאות חדשות נקלטו. best-effort; לא זורק.
    """
    if not getattr(gemini, "enabled", False):
        return 0
    prompt = (
        f"מהן תוצאות הסיום של משחקי {config.COMPETITION} שכבר הסתיימו "
        "(עד 12 המשחקים האחרונים)? כלול רק משחקים שנגמרו עם תוצאה סופית. "
        "החזר JSON: {\"results\": [{\"home\": str, \"away\": str, "
        "\"home_goals\": int, \"away_goals\": int, \"date\": str}]} "
        "עם שמות נבחרות באנגלית."
    )
    raw = gemini.ask_json(prompt, default=None)
    rows = (raw or {}).get("results") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        log.warning("קליטת תוצאות: לא התקבל מידע שמיש")
        return 0

    teams = {t.get("team_name"): t for t in db.get("teams", [])}
    db.setdefault("results", [])
    seen = {
        (_norm(r.get("home")), _norm(r.get("away")), str(r.get("date")))
        for r in db["results"]
    }
    added = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        home, away = r.get("home"), r.get("away")
        hg, ag = _num(r.get("home_goals"), None), _num(r.get("away_goals"), None)
        if not home or not away or hg is None or ag is None:
            continue
        key = (_norm(home), _norm(away), str(r.get("date")))
        if key in seen:
            continue
        db["results"].append({
            "home": home, "away": away,
            "home_goals": int(hg), "away_goals": int(ag),
            "date": r.get("date"),
        })
        seen.add(key)
        added += 1
        # למידה: שקלול התוצאה לתוך ממוצעי השערים של הנבחרות
        for side, gf, ga in ((home, hg, ag), (away, ag, hg)):
            t = teams.get(side)
            if not t:
                continue
            t["goals_for"] = round(
                0.7 * _num(t.get("goals_for"), config.DEFAULT_GOALS_FOR) + 0.3 * gf, 2)
            t["goals_against"] = round(
                0.7 * _num(t.get("goals_against"), config.DEFAULT_GOALS_AGAINST) + 0.3 * ga, 2)
    if added:
        db.setdefault("meta", {})["results_updated"] = utils.now_iso()
    log.info("קליטת תוצאות: %d תוצאות חדשות נלמדו", added)
    return added


def _fantasy_points_for(pos, goals, assists, minutes, clean_sheet) -> float:
    """מחשב נקודות פנטזי בפועל מביצוע יחיד, באותם קבועים של מנוע הפנטזי."""
    import fantasy
    p = fantasy.normalize_position(pos)
    pts = fantasy.APPEARANCE_POINTS if minutes >= 1 else 0.0
    pts += goals * fantasy.GOAL_POINTS[p]
    pts += assists * fantasy.ASSIST_POINTS
    if clean_sheet and minutes >= 60:
        pts += fantasy.CLEAN_SHEET_POINTS[p]
    return round(pts, 2)


def ingest_player_results(gemini: GeminiClient, db: dict) -> int:
    """מושך ביצועי שחקנים בפועל מהמשחקים שהסתיימו ומלמד מהם את ציוני הפנטזי.

    לכל שחקן מתעדכן שדה ``recent_points`` (EWMA של נקודות הפנטזי בפועל) — כך
    ההמלצות (הרכב/קפטן/דיפרנציאלים/חילופים) זזות לכיוון מי שבאמת הופיע והבקיע,
    בדיוק כפי ש-``ingest_results`` מעדכן את חוזק הנבחרות לניחושים.
    שומר ב-db['player_results'] (ללא כפילויות). מחזיר כמה ביצועים חדשים נקלטו.
    best-effort; לא זורק.
    """
    if not getattr(gemini, "enabled", False):
        return 0
    prompt = (
        f"מהם ביצועי השחקנים הבולטים במשחקי {config.COMPETITION} שכבר הסתיימו "
        "(עד 60 השחקנים המובילים מהמחזור האחרון, מכל הקווים — "
        "שוערים/בלמים/קשרים/חלוצים)? כלול רק משחקים שנגמרו. "
        "לכל שחקן ציין את מספר נקודות הפנטזי הרשמיות שצבר באותו מחזור במשחק "
        "FIFA World Cup Fantasy הרשמי (play.fifa.com/fantasy) בשדה fantasy_points — "
        "זהו הניקוד המורכב הרשמי (כולל בונוסים), עדיף על חישוב עצמאי. "
        "החזר JSON: {\"players\": [{\"name\": str, \"team\": str, "
        "\"position\": \"GK\"|\"DEF\"|\"MID\"|\"FWD\", \"goals\": int, "
        "\"assists\": int, \"minutes\": int, \"clean_sheet\": bool, "
        "\"fantasy_points\": number, \"date\": str}]} עם שמות באנגלית."
    )
    raw = gemini.ask_json(prompt, default=None)
    rows = (raw or {}).get("players") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        log.warning("קליטת ביצועי שחקנים: לא התקבל מידע שמיש")
        return 0

    players = db.setdefault("players", [])

    def _surname(n):
        parts = _norm(n).split()
        return parts[-1] if parts else ""

    index: dict[tuple, dict] = {}
    for p in players:
        nm, tm = p.get("player_name"), p.get("team")
        index[(_norm(nm), _norm(tm))] = p
        index.setdefault((_surname(nm), _norm(tm)), p)

    db.setdefault("player_results", [])
    seen = {
        (_norm(r.get("name")), _norm(r.get("team")), str(r.get("date")))
        for r in db["player_results"]
    }
    added = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        name, team = r.get("name"), r.get("team")
        if not name or not team:
            continue
        key = (_norm(name), _norm(team), str(r.get("date")))
        if key in seen:
            continue
        goals = int(_num(r.get("goals"), 0))
        assists = int(_num(r.get("assists"), 0))
        minutes = int(_num(r.get("minutes"), 0))
        cs = bool(r.get("clean_sheet"))
        pos = r.get("position")
        # מעדיפים את הניקוד הרשמי המורכב של FIFA; נופלים לחישוב עצמאי רק אם חסר
        official = _num(r.get("fantasy_points"), None)
        actual = round(official, 2) if official is not None \
            else _fantasy_points_for(pos, goals, assists, minutes, cs)
        db["player_results"].append({
            "name": name, "team": team, "date": r.get("date"),
            "goals": goals, "assists": assists, "minutes": minutes,
            "clean_sheet": cs, "points": actual,
            "official": official is not None,
        })
        seen.add(key)
        added += 1
        # מאתר את השחקן בבריכה (שם מלא או שם משפחה) — או יוצר חדש אם הוא בלט אך חסר
        p = index.get((_norm(name), _norm(team))) or index.get((_surname(name), _norm(team)))
        if not p:
            p = build_player({"player_name": name, "team": team, "position": pos})
            players.append(p)
            index[(_norm(name), _norm(team))] = p
        old = p.get("recent_points")
        p["recent_points"] = (round(actual, 2) if old is None
                              else round(0.6 * _num(old, 0.0) + 0.4 * actual, 2))
    if added:
        db.setdefault("meta", {})["player_results_updated"] = utils.now_iso()
    log.info("קליטת ביצועי שחקנים: %d ביצועים חדשים נלמדו", added)
    return added


def _classify_status(raw_status) -> str:
    s = str(raw_status or "").strip().lower()
    if any(w in s for w in _OUT_WORDS):
        return "out"
    if any(w in s for w in _DOUBT_WORDS):
        return "doubtful"
    return "fit"


def _norm(name) -> str:
    return str(name or "").strip().lower()


# --------------------------------------------------------------------------- #
# תזמור
# --------------------------------------------------------------------------- #
def collect(days_ahead: int = 3) -> dict:
    """אוסף נתונים ובונה את מבנה ה-DB המלא. לעולם לא זורק חריגה."""
    log.info("מתחיל איסוף נתונים (%d ימים קדימה)", days_ahead)
    gemini = GeminiClient()

    db = utils.load_json(config.DB_PATH, default={}) or {}
    db.setdefault("meta", {})
    db["meta"].update(
        {
            "competition": config.COMPETITION,
            "season": config.SEASON,
            "last_updated": utils.now_iso(),
        }
    )

    matches = fetch_upcoming_matches(gemini, days_ahead)
    log.info("נמצאו %d משחקים קרובים", len(matches))

    # הגנה: אם Gemini לא החזיר משחקים (למשל לפני תחילת המונדיאל), לא מוחקים
    # את נתוני המחזור הקיימים — מרעננים להם אודדס ופציעות בלבד.
    if not matches:
        log.warning("לא נמצאו משחקים חדשים — משמרים DB קיים ומרעננים אודדס/פציעות")
        existing = db.get("matches", [])
        _refresh_injuries(gemini, db)
        # מקור אמת: הבריכה הרשמית של FIFA; נופלים ל-Gemini רק אם נכשלה
        official = fetch_official_pool()
        pool = official or fetch_fantasy_player_pool(gemini)
        if official:
            db["participants"] = sorted({p["team"] for p in official if p.get("team")})
            db["players"] = pool                       # הרשמית מחליפה לגמרי
        elif pool:
            db["players"] = _dedupe_players(list(db.get("players", [])) + pool)
        _enrich_fantasy_data(gemini, db)
        db["differentials"] = (official_differentials(official) if official
                               else fetch_differentials(gemini)) or db.get("differentials", {})
        db["fixture_difficulty"] = (fetch_fixture_difficulty(gemini)
                                    or db.get("fixture_difficulty", {}))
        odds_map = odds_mod.fetch_consensus_odds(gemini, existing)
        odds_mod.attach_to_matches(existing, odds_map)
        ingest_results(gemini, db)  # למידה מתוצאות אמת — מעדכן חוזק נבחרות לניחושים הבאים
        ingest_player_results(gemini, db)  # למידה מביצועי שחקנים — מעדכן ציוני פנטזי
        filter_to_participants(db)  # מסנן שחקנים מנבחרות שלא במונדיאל
        utils.save_json(config.DB_PATH, db)
        return db

    teams: dict[str, dict] = {}
    players: list[dict] = []

    for match in matches:
        for side in ("home_team", "away_team"):
            name = match.get(side)
            if name and name not in teams:
                teams[name] = fetch_team_stats(gemini, name)

        context = fetch_match_context(gemini, match)
        match["context"] = _summarize_context(context)
        players.extend(_extract_players(context, match))

    # אודדס קונצנזוס מ-10 מקורות נפוצים — מצורף לכל משחק
    odds_map = odds_mod.fetch_consensus_odds(gemini, matches)
    odds_mod.attach_to_matches(matches, odds_map)

    # מקור אמת לבריכה: FIFA הרשמי (סגלים, מחיר, בעלות, נקודות); נופלים ל-Gemini
    # רק אם נכשל. שחקני המשחקים (key players מה-context) מצורפים בכל מקרה.
    official = fetch_official_pool()
    pool = official or fetch_fantasy_player_pool(gemini)
    if official:
        db["participants"] = sorted({p["team"] for p in official if p.get("team")})
    db["matches"] = matches
    db["teams"] = list(teams.values())
    db["players"] = _dedupe_players(players + pool)

    # העשרה בנתוני פנטזי מהאתרים המומלצים (xG/xA/פנדלים — משלים את הרשמי)
    _enrich_fantasy_data(gemini, db)

    # דיפרנציאלים — מהבריכה הרשמית (בעלות+הרכב אמיתיים); נפילה ל-Gemini
    db["differentials"] = (official_differentials(official) if official
                           else fetch_differentials(gemini)) or db.get("differentials", {})
    # קושי המחזור הקרוב לכל נבחרת — להמלצות חילוף
    db["fixture_difficulty"] = (fetch_fixture_difficulty(gemini)
                                or db.get("fixture_difficulty", {}))

    # למידה מתוצאות אמת אחרי הסקרייפ — כך הניחושים העתידיים נשארים ריאליסטיים
    # (חוזק הנבחרות לא נשאר על הערכת Gemini בלבד אלא משוקלל מול מה שקרה בפועל)
    ingest_results(gemini, db)
    # למידה מביצועי שחקנים בפועל — מעדכן את ציוני הפנטזי לפי מי שהופיע/הבקיע
    ingest_player_results(gemini, db)
    # סינון שחקנים מנבחרות שאינן משתתפות במונדיאל (איטליה וכו') — בריכה ודיפרנציאלים
    filter_to_participants(db)

    utils.save_json(config.DB_PATH, db)
    log.info(
        "איסוף הושלם: %d משחקים, %d נבחרות, %d שחקנים",
        len(db["matches"]), len(db["teams"]), len(db["players"]),
    )
    return db


def _summarize_context(context: dict) -> dict:
    """גוזר מדדי Context ברמת המשחק מתוך נתוני הפציעות/הרכב."""
    home = context.get("home", {}) if isinstance(context, dict) else {}
    away = context.get("away", {}) if isinstance(context, dict) else {}
    injury_count = len(home.get("injuries", []) or []) + len(
        away.get("injuries", []) or []
    )
    has_lineups = bool(home.get("expected_lineup")) and bool(
        away.get("expected_lineup")
    )
    return {
        "home_advantage": config.HOME_ADVANTAGE,
        "injury_count": injury_count,
        "lineup_confidence": "high" if has_lineups else "low",
    }


def _extract_players(context: dict, match: dict) -> list[dict]:
    """ממיר את שחקני ה-context לרשומות Player בסכמה."""
    out: list[dict] = []
    if not isinstance(context, dict):
        return out
    for side, team_name in (
        ("home", match.get("home_team")),
        ("away", match.get("away_team")),
    ):
        block = context.get(side, {}) or {}
        injured = {
            (p.get("name") if isinstance(p, dict) else p)
            for p in (block.get("injuries") or [])
        }
        starters = {
            (p.get("name") if isinstance(p, dict) else p)
            for p in (block.get("expected_lineup") or [])
        }
        for p in block.get("key_players", []) or []:
            if not isinstance(p, dict):
                continue
            name = p.get("name")
            out.append(
                build_player(
                    {
                        "player_name": name,
                        "team": team_name,
                        "position": p.get("position"),
                        "injury_status": "injured"
                        if name in injured
                        else p.get("injury_status", "fit"),
                        "expected_start": name in starters,
                    }
                )
            )
    return out


def _dedupe_players(players: list[dict]) -> list[dict]:
    """מאחד שחקנים כפולים לפי (שם, נבחרת). שדות חסרים ברשומה הראשונה מושלמים
    מרשומות מאוחרות (כך מידע פנטזי כמו price/form מהבריכה לא הולך לאיבוד)."""
    seen: dict[tuple, dict] = {}
    for p in players:
        key = (p.get("player_name"), p.get("team"))
        if key not in seen:
            seen[key] = dict(p)
            continue
        base = seen[key]
        for field, val in p.items():
            if val in (None, "", 0) and field in base and base[field] not in (None, "", 0):
                continue  # לא לדרוס ערך קיים בערך ריק
            if base.get(field) in (None, "", 0) and val not in (None, "", 0):
                base[field] = val
    return list(seen.values())


if __name__ == "__main__":
    collect()
