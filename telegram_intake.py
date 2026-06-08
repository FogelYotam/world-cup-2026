"""
קליטת צילומי הרכב FIFA Fantasy דרך בוט הטלגרם הקיים.

זרימה: אתה שולח צילום מסך של ההרכב לבוט → המודול מושך את התמונה (getUpdates
+ getFile), קורא ממנה את הסגל דרך Gemini Vision, מעדכן data/my_team.json,
ושולח לך אישור חזרה בטלגרם. לעולם לא זורק חריגה — כל כשל נרשם ביומן.

הרצה ידנית/מתוזמנת:  python telegram_intake.py
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import sys

import requests

import config
import scraper
import utils

log = utils.get_logger("intake")

_OFFSET_PATH = config.DATA_DIR / "telegram_offset.json"
_STATE_PATH = config.DATA_DIR / "bot_state.json"
_VALID_POS = {"GK", "DEF", "MID", "FWD"}
_MAX_PER_NATION = 3
_SQUAD_SIZE = 15
_REFRESH_MIN_HOURS = 5          # רענון מודל לכל היותר כל 5 שעות (~כמה פעמים ביום)


# --------------------------------------------------------------------------- #
# עזרי Telegram API
# --------------------------------------------------------------------------- #
def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/{method}"


def _load_offset() -> int:
    data = utils.load_json(_OFFSET_PATH, default={}) or {}
    try:
        return int(data.get("offset", 0))
    except (TypeError, ValueError):
        return 0


def _save_offset(offset: int) -> None:
    utils.save_json(_OFFSET_PATH, {"offset": offset})


def _get_updates(offset: int) -> list[dict]:
    resp = requests.get(
        _api("getUpdates"),
        params={
            "offset": offset,
            "timeout": 0,
            "allowed_updates": json.dumps(["message"]),
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("result", []) or []


def _mime_for(path: str) -> str:
    p = (path or "").lower()
    if p.endswith(".png"):
        return "image/png"
    if p.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"


def _image_file_id(message: dict) -> str | None:
    """מאתר file_id של תמונה — בין אם נשלחה כ-photo או כמסמך-תמונה."""
    photos = message.get("photo") or []
    if photos:  # טלגרם שולח כמה רזולוציות; ניקח את הגדולה ביותר
        return max(photos, key=lambda p: p.get("file_size", 0)).get("file_id")
    doc = message.get("document") or {}
    if str(doc.get("mime_type", "")).startswith("image/"):
        return doc.get("file_id")
    return None


def _download_file(file_id: str) -> tuple[bytes, str]:
    """מוריד קובץ מטלגרם לפי file_id. מחזיר (bytes, mime_type)."""
    r = requests.get(_api("getFile"), params={"file_id": file_id}, timeout=30)
    r.raise_for_status()
    file_path = r.json()["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{config.TELEGRAM_BOT_TOKEN}/{file_path}"
    img = requests.get(url, timeout=60)
    img.raise_for_status()
    return img.content, _mime_for(file_path)


def _send_message(text: str) -> None:
    try:
        requests.post(
            _api("sendMessage"),
            data={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("שליחת הודעת אישור לטלגרם נכשלה: %s", exc)


# --------------------------------------------------------------------------- #
# פענוח תמונת ההרכב דרך Gemini Vision
# --------------------------------------------------------------------------- #
def parse_squad_image(gemini, image_bytes: bytes, mime: str) -> dict | None:
    """מבקש מ-Gemini לקרוא את ההרכב מתוך צילום המסך. מחזיר dict או None."""
    prompt = (
        "זוהי תמונת מסך של קבוצת FIFA Fantasy למונדיאל 2026. "
        "חלץ את כל השחקנים שמופיעים בקבוצה — גם את 11 הפותחים וגם את 4 שחקני הספסל. "
        "לכל שחקן זהה את שמו המלא, את העמדה (GK=שוער, DEF=מגן, MID=קישור, FWD=חלוץ), "
        "ואת הנבחרת. אם מופיע סימן קפטן (C) סמן is_captain=true, "
        "ואם מופיע סימן סגן (V) סמן is_vice=true. "
        "אם מוצגים יתרת תקציב (bank) או מספר חילופים חופשיים (free transfers) — כלול אותם. "
        'החזר JSON במבנה: {"squad": [{"player_name": str, "team": str, '
        '"position": "GK"|"DEF"|"MID"|"FWD", "is_captain": bool, "is_vice": bool}], '
        '"bank": number, "free_transfers": number}'
    )
    return gemini.ask_json_image(prompt, image_bytes, mime_type=mime, default=None)


def _to_my_team(parsed: dict, prev: dict) -> tuple[dict | None, str | None]:
    """ממיר את פלט ה-Vision למבנה my_team.json. מחזיר (my_team, שגיאה)."""
    rows = parsed.get("squad") if isinstance(parsed, dict) else None
    if not isinstance(rows, list) or not rows:
        return None, "לא זוהו שחקנים בתמונה. נסה צילום ברור יותר של מסך ההרכב."

    squad: list[dict] = []
    captain = None
    vice = None
    for r in rows:
        if not isinstance(r, dict):
            continue
        name = str(r.get("player_name") or r.get("name") or "").strip()
        pos = str(r.get("position") or "").strip().upper()
        if not name or pos not in _VALID_POS:
            continue
        squad.append({
            "player_name": name,
            "team": str(r.get("team") or "").strip(),
            "position": pos,
        })
        if r.get("is_captain"):
            captain = name
        if r.get("is_vice"):
            vice = name

    if len(squad) < 11:
        return None, (
            f"זוהו רק {len(squad)} שחקנים בתמונה — צריך לפחות 11. "
            "שלח צילום ברור יותר, או צרף גם צילום של הספסל."
        )

    my_team = {
        "note": prev.get(
            "note",
            "מתעדכן אוטומטית מצילומי הרכב שנשלחים לבוט הטלגרם. "
            "position: GK/DEF/MID/FWD.",
        ),
        "budget": prev.get("budget", 100.0),
        "bank": scraper._num(parsed.get("bank"), prev.get("bank", 0.0)),
        "free_transfers": int(
            scraper._num(parsed.get("free_transfers"), prev.get("free_transfers", 1))
        ),
        "captain": captain or prev.get("captain"),
        "vice_captain": vice or prev.get("vice_captain"),
        "squad": squad,
    }
    return my_team, None


def _confirm_text(my_team: dict) -> str:
    """בונה הודעת אישור עברית עם פירוט הסגל, קפטן, ואזהרות לפי חוקי FIFA."""
    squad = my_team["squad"]
    by_pos: dict[str, list[str]] = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    nations: dict[str, int] = {}
    for p in squad:
        by_pos.setdefault(p["position"], []).append(p["player_name"])
        team = p.get("team") or "—"
        nations[team] = nations.get(team, 0) + 1

    lines = ["<b>✅ הקבוצה שלך עודכנה מצילום המסך</b>"]
    lines.append(
        f"זוהו <b>{len(squad)}</b> שחקנים · "
        f"קפטן: <b>{my_team.get('captain') or '—'}</b> · "
        f"סגן: {my_team.get('vice_captain') or '—'}"
    )
    for pos, label in (("GK", "שוערים"), ("DEF", "הגנה"),
                       ("MID", "קישור"), ("FWD", "חלוץ")):
        if by_pos[pos]:
            lines.append(f"<b>{label}:</b> {', '.join(by_pos[pos])}")

    warnings = []
    if len(squad) != _SQUAD_SIZE:
        warnings.append(
            f"זוהו {len(squad)} שחקנים ולא {_SQUAD_SIZE} — "
            "אם חסר הספסל, שלח גם צילום שלו."
        )
    over = [f"{n} ({c})" for n, c in nations.items() if c > _MAX_PER_NATION]
    if over:
        warnings.append("חריגה ממקס' 3 לנבחרת: " + ", ".join(over))
    if warnings:
        lines.append("")
        lines.append("⚠️ " + " · ".join(warnings))

    lines.append("")
    lines.append("<i>ההמלצות האישיות יתעדכנו לפי הקבוצה הזו בריצה הקרובה.</i>")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# תזמור — מעבד את כל הצילומים שהתקבלו מאז הריצה האחרונה
# --------------------------------------------------------------------------- #
def process_incoming(gemini=None) -> dict:
    """מעבד צילומי הרכב חדשים שנשלחו לבוט. מחזיר {'processed': N}. לא זורק."""
    if not config.telegram_enabled():
        log.warning("טלגרם לא מוגדר — אין מאיפה לקלוט צילומי הרכב")
        return {"processed": 0}

    gemini = gemini or scraper.GeminiClient()
    offset = _load_offset()
    try:
        updates = _get_updates(offset)
    except Exception as exc:  # noqa: BLE001
        log.error("getUpdates נכשל: %s", exc)
        return {"processed": 0}

    new_offset = offset
    processed = 0
    saw_photo = False
    for upd in updates:
        new_offset = max(new_offset, int(upd.get("update_id", 0)) + 1)
        msg = upd.get("message") or upd.get("channel_post") or {}

        # מקבלים רק מהצ'אט המורשה (המשתמש עצמו)
        chat_id = str((msg.get("chat") or {}).get("id"))
        if config.TELEGRAM_CHAT_ID and chat_id != str(config.TELEGRAM_CHAT_ID):
            continue

        file_id = _image_file_id(msg)
        if not file_id:
            continue
        saw_photo = True

        try:
            img, mime = _download_file(file_id)
        except Exception as exc:  # noqa: BLE001
            log.error("הורדת התמונה מטלגרם נכשלה: %s", exc)
            _send_message("⚠️ לא הצלחתי להוריד את התמונה. נסה לשלוח שוב.")
            continue

        if not getattr(gemini, "enabled", False):
            _send_message(
                "⚠️ קריאת תמונות זמנית לא זמינה (מכסת Gemini היומית מוצתה). "
                "נסה שוב מאוחר יותר."
            )
            continue

        parsed = parse_squad_image(gemini, img, mime)
        prev = utils.load_json(config.MY_TEAM_PATH, default={}) or {}
        my_team, err = _to_my_team(parsed or {}, prev)
        if err:
            _send_message(f"⚠️ {err}")
            continue

        utils.save_json(config.MY_TEAM_PATH, my_team)
        processed += 1
        log.info("my_team.json עודכן מצילום (%d שחקנים)", len(my_team["squad"]))
        _send_message(_confirm_text(my_team))

    if new_offset != offset:
        _save_offset(new_offset)
    if saw_photo:
        log.info("עיבוד צילומי הרכב הסתיים: %d עודכנו", processed)
    return {"processed": processed}


# =========================================================================== #
# מצב הבוט (שיחה קצרה + תזמון רענון) — data/bot_state.json
# =========================================================================== #
def _load_state() -> dict:
    return utils.load_json(_STATE_PATH, default={}) or {}


def _save_state(state: dict) -> None:
    utils.save_json(_STATE_PATH, state)


# --------------------------------------------------------------------------- #
# סיווג + חילוץ תמונה בשיחה אחת (חיסכון במכסת Gemini)
# --------------------------------------------------------------------------- #
def classify_image(gemini, image_bytes: bytes, mime: str) -> dict | None:
    """קריאת Vision אחת: מסווגת לתמונת הרכב או לוח-משחקים ומחלצת את הנתונים."""
    prompt = (
        "זוהי תמונת מסך מאפליקציית כדורגל. סווג אותה והחזר JSON לפי הכלל:\n"
        "• אם זו קבוצת FIFA Fantasy (רשת שחקנים/הרכב): \n"
        '  {"kind":"lineup","squad":[{"player_name":str,"team":str,'
        '"position":"GK"|"DEF"|"MID"|"FWD","is_captain":bool,"is_vice":bool}],'
        '"bank":number,"free_transfers":number}\n'
        "• אם זה לוח משחקים / ניחושי תוצאה: \n"
        '  {"kind":"fixtures","matches":[{"home":str,"away":str,"date":str,'
        '"user_home_goals":int|null,"user_away_goals":int|null}]}\n'
        "שמות נבחרות/שחקנים באנגלית. כלול את כל הפריטים שמופיעים בתמונה."
    )
    return gemini.ask_json_image(prompt, image_bytes, mime_type=mime, default=None)


# --------------------------------------------------------------------------- #
# מטפל בתמונת הרכב — שומר, מפיק המלצה אישית, ושואל שאלות מנחות
# --------------------------------------------------------------------------- #
_GUIDING_QUESTIONS = (
    "<b>❓ שאלות מנחות (ענה קצר, שורה לכל שאלה):</b>\n"
    "1️⃣ סגנון: (א) סולידי · (ב) אגרסיבי\n"
    "2️⃣ קפטן: (א) להשאיר את הבחירה · (ב) לשקול חלופה\n"
    "3️⃣ כמה חילופים חופשיים יש לך? (0/1/2 או 'חופשי')\n"
    "4️⃣ יש שחקן שלא תחליף? (שם / 'אין')"
)


def _handle_lineup(parsed: dict) -> None:
    """מעבד תמונת הרכב: שמירה + המלצה אישית + שאלות מנחות."""
    prev = utils.load_json(config.MY_TEAM_PATH, default={}) or {}
    my_team, err = _to_my_team(parsed, prev)
    if err:
        _send_message(f"⚠️ {err}")
        return
    utils.save_json(config.MY_TEAM_PATH, my_team)
    log.info("my_team.json עודכן מצילום (%d שחקנים)", len(my_team["squad"]))

    # אישור + בדיקת חוקיות
    _send_message(_confirm_text(my_team))

    # המלצה אישית מהמנוע (אם השחקנים מוכרים ב-DB)
    try:
        import predictor
        import fantasy
        import advisor
        import report
        db = utils.load_json(config.DB_PATH, default={}) or {}
        preds = predictor.predict_all(db)
        scored = fantasy.score_players(db, preds)
        advice = advisor.build_advice(db, scored, my_team=my_team, matchday=1)
        if advice.get("available"):
            lines: list[str] = []
            report._append_personal_advice(lines, advice)
            text = "\n".join(line for line in lines if line.strip())
            if text:
                _send_message(text)
    except Exception as exc:  # noqa: BLE001
        log.error("הפקת המלצה אישית בבוט נכשלה: %s", exc)

    # שאלות מנחות + שמירת מצב שיחה
    _send_message(_GUIDING_QUESTIONS)
    state = _load_state()
    state["pending"] = "lineup_questions"
    state["squad_snapshot"] = my_team["squad"]
    state["captain"] = my_team.get("captain")
    _save_state(state)


# --------------------------------------------------------------------------- #
# מטפל בלוח-משחקים — משווה ניחושי המשתמש לניחושי המודל
# --------------------------------------------------------------------------- #
def _find_prediction(preds: list[dict], home: str, away: str) -> dict | None:
    target = {_norm(home), _norm(away)}
    for p in preds:
        if {_norm(p.get("home_team")), _norm(p.get("away_team"))} == target:
            return p
    # fallback: התאמה חלקית לפי הכלה
    for p in preds:
        names = _norm(p.get("home_team")) + "|" + _norm(p.get("away_team"))
        if _norm(home) in names and _norm(away) in names:
            return p
    return None


def _handle_fixtures(parsed: dict) -> None:
    """מעבד לוח משחקים/ניחושים: משווה למודל ומחזיר המלצה."""
    matches = parsed.get("matches") if isinstance(parsed, dict) else None
    if not isinstance(matches, list) or not matches:
        _send_message("⚠️ לא זיהיתי משחקים בתמונה. נסה צילום ברור של לוח המשחקים.")
        return
    import predictor
    db = utils.load_json(config.DB_PATH, default={}) or {}
    preds = predictor.predict_all(db)

    lines = ["<b>🎯 ניחושי המודל מול שלך</b>"]
    found = 0
    for m in matches:
        if not isinstance(m, dict):
            continue
        home, away = m.get("home"), m.get("away")
        if not home or not away:
            continue
        p = _find_prediction(preds, home, away)
        lines.append("")
        lines.append(f"<b>{home} מול {away}</b>")
        uh, ua = m.get("user_home_goals"), m.get("user_away_goals")
        if uh is not None and ua is not None:
            lines.append(f"• הניחוש שלך: {uh}-{ua}")
        if not p:
            lines.append("• אין למודל ניחוש למשחק הזה עדיין.")
            continue
        found += 1
        o = p.get("outcome_probabilities", {})
        lines.append(
            f"• המודל: <b>{p.get('recommended_score')}</b> "
            f"(אמון {p.get('confidence')}%) — "
            f"{p.get('home_team')} {round(o.get('home_win',0)*100)}% · "
            f"תיקו {round(o.get('draw',0)*100)}% · "
            f"{p.get('away_team')} {round(o.get('away_win',0)*100)}%"
        )
        # השוואת מנצח (יישור לכיוון המודל)
        if uh is not None and ua is not None:
            if _norm(p.get("home_team")) == _norm(home):
                mu_h, mu_a = uh, ua
            else:
                mu_h, mu_a = ua, uh
            user_w = "home" if mu_h > mu_a else "away" if mu_a > mu_h else "draw"
            probs = {"home": o.get("home_win", 0), "draw": o.get("draw", 0),
                     "away": o.get("away_win", 0)}
            model_w = max(probs, key=probs.get)
            if user_w == model_w:
                lines.append("✔️ תואם את נטיית המודל.")
            else:
                lines.append("⚠️ המודל נוטה לכיוון אחר — שקול לעדכן.")
    if found:
        lines.append("")
        lines.append("<i>המודל רץ על נתונים מתעדכנים; ככל שנכנסות תוצאות אמת — מדויק יותר.</i>")
    _send_message("\n".join(lines))
    # אין שאלות המשך ללוח-משחקים
    state = _load_state()
    state.pop("pending", None)
    _save_state(state)


# --------------------------------------------------------------------------- #
# מטפל בתשובות טקסט קצרות לשאלות המנחות
# --------------------------------------------------------------------------- #
def _interpret_answers(text: str) -> dict:
    """מפרש תשובה קצרה (רב-שורתית) לשאלות המנחות."""
    parts = [p.strip() for p in text.replace(",", "\n").splitlines() if p.strip()]
    ans: dict = {}
    if len(parts) >= 1:
        ans["style"] = "aggressive" if parts[0].startswith(("ב", "b", "B")) else "solid"
    if len(parts) >= 2:
        ans["captain_alt"] = parts[1].startswith(("ב", "b", "B"))
    if len(parts) >= 3:
        low = parts[2]
        if "חופ" in low or "free" in low.lower():
            ans["free_transfers"] = "unlimited"
        else:
            digits = "".join(c for c in low if c.isdigit())
            ans["free_transfers"] = digits or low
    if len(parts) >= 4:
        ans["untouchable"] = None if parts[3] in ("אין", "-", "none") else parts[3]
    return ans


def _handle_text(text: str) -> bool:
    """מטפל בתשובת טקסט אם יש שאלות פתוחות. מחזיר True אם טופל."""
    state = _load_state()
    if state.get("pending") != "lineup_questions":
        return False
    a = _interpret_answers(text)

    lines = ["<b>🔧 חידוד לפי התשובות שלך</b>"]
    if a.get("style") == "aggressive":
        lines.append("• סגנון אגרסיבי: שחק את השחקנים מול היריבות החלשות, שקול מערך התקפי "
                     "(3-4-3) וסטאק של 2-3 שחקנים מאותו משחק קל.")
    else:
        lines.append("• סגנון סולידי: העדף שחקנים מובטחי-דקות עם פיקסצ'ר נוח; הימנע מהימורי רוטציה.")
    if a.get("captain_alt"):
        lines.append("• קפטן: שקול דיפרנציאל מהמשחק הקל ביותר שלך (תקרה גבוהה) במקום הבחירה הנוכחית.")
    else:
        cap = state.get("captain") or "הבחירה הנוכחית"
        lines.append(f"• קפטן: נשארים עם <b>{cap}</b> — בחירה בטוחה.")
    ft = a.get("free_transfers")
    if ft == "unlimited":
        lines.append("• חילופים חופשיים עד הדדליין → כל שינוי בחינם, נצל לסדר את כל ההרכב.")
    elif ft:
        lines.append(f"• עם {ft} חילופים חופשיים — מקד את השינוי בחוליה החלשה ביותר בלבד.")
    if a.get("untouchable"):
        lines.append(f"• {a['untouchable']} נשאר בסגל בכל מקרה.")

    _send_message("\n".join(lines))
    state.pop("pending", None)
    _save_state(state)
    return True


# --------------------------------------------------------------------------- #
# רענון מודל — כמה פעמים ביום, כולל למידה מתוצאות אמת
# --------------------------------------------------------------------------- #
def _maybe_refresh_model(gemini) -> None:
    """מרענן נתונים + לומד מתוצאות אמת, לכל היותר כל _REFRESH_MIN_HOURS שעות."""
    state = _load_state()
    last = utils._parse_dt(state.get("last_refresh"))
    if last and datetime.now() - last < timedelta(hours=_REFRESH_MIN_HOURS):
        return
    if not getattr(gemini, "enabled", False):
        return
    try:
        db = utils.load_json(config.DB_PATH, default={}) or {}
        added = scraper.ingest_results(gemini, db)
        scraper._enrich_fantasy_data(gemini, db)
        utils.save_json(config.DB_PATH, db)
        state["last_refresh"] = utils.now_iso()
        _save_state(state)
        log.info("רענון מודל בוצע (%d תוצאות חדשות)", added)
    except Exception as exc:  # noqa: BLE001
        log.error("רענון המודל נכשל: %s", exc)


# --------------------------------------------------------------------------- #
# לולאת הבוט האוטונומי — נקראת מהמשימה המתוזמנת כל כמה דקות
# --------------------------------------------------------------------------- #
def run_bot_once(poll_timeout: int = 15) -> dict:
    """בודק הודעות חדשות, מגיב אוטומטית, ומרענן מודל מדי פעם. לא זורק."""
    if not config.telegram_enabled():
        log.warning("טלגרם לא מוגדר — הבוט לא יכול לרוץ")
        return {"handled": 0}

    gemini = scraper.GeminiClient()
    offset = _load_offset()
    try:
        resp = requests.get(
            _api("getUpdates"),
            params={"offset": offset, "timeout": poll_timeout,
                    "allowed_updates": json.dumps(["message"])},
            timeout=poll_timeout + 15,
        )
        resp.raise_for_status()
        updates = resp.json().get("result", []) or []
    except Exception as exc:  # noqa: BLE001
        log.error("getUpdates נכשל: %s", exc)
        return {"handled": 0}

    new_offset = offset
    handled = 0
    for upd in updates:
        new_offset = max(new_offset, int(upd.get("update_id", 0)) + 1)
        # הודעה בודדת בעייתית לעולם לא מפילה את כל הריצה
        try:
            msg = upd.get("message") or {}
            chat_id = str((msg.get("chat") or {}).get("id"))
            if config.TELEGRAM_CHAT_ID and chat_id != str(config.TELEGRAM_CHAT_ID):
                continue

            file_id = _image_file_id(msg)
            if file_id:
                if not getattr(gemini, "enabled", False):
                    _send_message("⚠️ קריאת תמונות זמנית לא זמינה (מכסת Gemini היומית "
                                  "אזלה). נסה שוב מאוחר יותר.")
                    continue
                try:
                    img, mime = _download_file(file_id)
                except Exception as exc:  # noqa: BLE001
                    log.error("הורדת תמונה נכשלה: %s", exc)
                    _send_message("⚠️ לא הצלחתי להוריד את התמונה. נסה לשלוח שוב.")
                    continue
                parsed = classify_image(gemini, img, mime) or {}
                kind = parsed.get("kind")
                if kind == "lineup":
                    _handle_lineup(parsed)
                elif kind == "fixtures":
                    _handle_fixtures(parsed)
                else:
                    _send_message(
                        "🤔 לא הצלחתי לקרוא את התמונה — ייתכן שהיא לא חדה מספיק, "
                        "או שמכסת Gemini אזלה לרגע. נסה לשלוח שוב צילום ברור של "
                        "מסך ההרכב או לוח המשחקים."
                    )
                handled += 1
                continue

            text = (msg.get("text") or "").strip()
            if text and _handle_text(text):
                handled += 1
        except Exception as exc:  # noqa: BLE001
            log.error("טיפול בהודעה (update %s) נכשל: %s",
                      upd.get("update_id"), exc)
            continue

    if new_offset != offset:
        _save_offset(new_offset)
        # אישור מול טלגרם שהעדכונים טופלו — חשוב בהרצה בענן (שרת זמני)
        # כדי שלא יעובדו שוב גם אם קובץ ה-offset לא נשמר בין הרצות.
        try:
            requests.get(_api("getUpdates"),
                         params={"offset": new_offset, "timeout": 0}, timeout=20)
        except Exception:  # noqa: BLE001
            pass
    _maybe_refresh_model(gemini)
    if handled:
        log.info("הבוט טיפל ב-%d הודעות", handled)
    return {"handled": handled}


if __name__ == "__main__":
    if "--bot" in sys.argv:
        run_bot_once()
    else:
        result = process_incoming()
        print(f"processed: {result['processed']}")
