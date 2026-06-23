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
_PENDING_DIR = config.DATA_DIR / "pending_images"   # תמונות שהמתינו (מכסה אזלה)
_EXT_BY_MIME = {"image/jpeg": "jpg", "image/jpg": "jpg",
                "image/png": "png", "image/webp": "webp"}
_VALID_POS = {"GK", "DEF", "MID", "FWD"}
_MAX_PER_NATION = 3
_SQUAD_SIZE = 15
_REFRESH_MIN_HOURS = 2          # רענון מודל לכל היותר כל שעתיים — פציעות/כושר/בעלות
                                # מהפיד הרשמי (זול, ללא Gemini), כדי שהנתונים ירגישו חיים


def _norm(s) -> str:
    """נרמול שם להשוואה (אותיות קטנות, ללא רווחים מובילים/נגררים)."""
    return str(s or "").strip().lower()


def _save_pending_image(img: bytes, mime: str) -> None:
    """שומר תמונה שלא ניתן היה לקרוא כרגע (מכסת Gemini אזלה) לעיבוד מאוחר —
    כך שניחוש/הרכב לא הולך לאיבוד, ואין צורך לשלוח שוב."""
    try:
        _PENDING_DIR.mkdir(parents=True, exist_ok=True)
        ext = _EXT_BY_MIME.get((mime or "").lower(), "jpg")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        (_PENDING_DIR / f"{ts}.{ext}").write_bytes(img)
        log.info("תמונה נשמרה לעיבוד מאוחר (pending)")
    except Exception as exc:  # noqa: BLE001
        log.error("שמירת תמונה ממתינה נכשלה: %s", exc)


def _process_pending_images(gemini) -> int:
    """מעבד תמונות שהמתינו (נשמרו כשהמכסה אזלה), כעת כשיש מכסה. מחזיר כמה עובדו."""
    if not getattr(gemini, "enabled", False) or not _PENDING_DIR.exists():
        return 0
    done = 0
    for path in sorted(_PENDING_DIR.glob("*.*")):
        try:
            mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            parsed = classify_image(gemini, path.read_bytes(), mime) or {}
            kind = parsed.get("kind")
            if kind == "lineup":
                _send_message("📦 מעבד צילום הרכב שהמתין (המכסה התחדשה)…")
                _handle_lineup(parsed)
            elif kind == "fixtures":
                _send_message("📦 מעבד ניחושים שהמתינו (המכסה התחדשה)…")
                _handle_fixtures(parsed)
            else:
                # עדיין לא ניתן לקרוא (מכסה לא חזרה) — משאירים לפעם הבאה
                if getattr(gemini, "_quota_exhausted", False):
                    break
                path.unlink(missing_ok=True)   # תמונה לא קריאה באמת — מסירים
                continue
            path.unlink(missing_ok=True)
            done += 1
        except Exception as exc:  # noqa: BLE001
            log.error("עיבוד תמונה ממתינה נכשל: %s", exc)
    if done:
        log.info("עובדו %d תמונות שהמתינו", done)
    return done


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
def _handle_lineup(parsed: dict) -> None:
    """מעבד תמונת הרכב: שמירה + המלצה אישית + הזמנה לשיח חופשי."""
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
        advice = advisor.build_advice(db, scored, my_team=my_team, matchday=1,
                                      predictions=preds)
        if advice.get("available"):
            lines: list[str] = []
            report._append_personal_advice(lines, advice)
            text = "\n".join(line for line in lines if line.strip())
            if text:
                _send_message(text)
    except Exception as exc:  # noqa: BLE001
        log.error("הפקת המלצה אישית בבוט נכשלה: %s", exc)

    # הזמנה לשיח חופשי עם Gemini על ההרכב (במקום שאלות מובנות)
    _send_message(
        "💬 עכשיו אפשר לשאול אותי בחופשיות כל דבר על ההרכב — "
        "מי לקפטן, איזה חילוף שווה, מי בסיכון, מערך מומלץ וכו'. פשוט כתוב."
    )
    state = _load_state()
    state["pending"] = "lineup_chat"
    state["squad"] = my_team["squad"]
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


def _parse_score(s):
    """ממיר מחרוזת תוצאה כמו '2-1' לזוג מספרים (2, 1). (None, None) בכשל."""
    try:
        parts = str(s).replace(":", "-").split("-")
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError, TypeError):
        return None, None


def _handle_fixtures(parsed: dict) -> None:
    """מעבד לוח משחקים/ניחושים: משווה למודל ומחזיר המלצה."""
    matches = parsed.get("matches") if isinstance(parsed, dict) else None
    if not isinstance(matches, list) or not matches:
        _send_message("⚠️ לא זיהיתי משחקים בתמונה. נסה צילום ברור של לוח המשחקים.")
        return
    import predictor
    import predictions_log
    db = utils.load_json(config.DB_PATH, default={}) or {}
    preds = predictor.predict_all(db)

    lines = ["<b>🎯 ניחושי המודל מול שלך</b>"]
    found = 0
    read = 0                    # כמה ניחושים נקראו מהתמונה (לאימות מול הצילום)
    missing: list[str] = []     # משחקים שהימרת עליהם אך אינם במערכת
    entries: list[dict] = []   # לשמירת הניחושים שלך למעקב לאורך זמן
    for m in matches:
        if not isinstance(m, dict):
            continue
        home, away = m.get("home"), m.get("away")
        if not home or not away:
            continue
        read += 1
        p = _find_prediction(preds, home, away)
        lines.append("")
        lines.append(f"<b>{home} מול {away}</b>")
        uh, ua = m.get("user_home_goals"), m.get("user_away_goals")
        if uh is not None and ua is not None:
            lines.append(f"• הניחוש שלך: {uh}-{ua}")
        # מודל מיושר לכיוון home/away של התמונה (לשמירה במעקב)
        mh = ma = None
        if p:
            ph, pa = _parse_score(p.get("recommended_score"))
            if _norm(p.get("home_team")) == _norm(home):
                mh, ma = ph, pa
            else:
                mh, ma = pa, ph
        entries.append({
            "home": home, "away": away, "date": m.get("date"),
            "user_home": uh, "user_away": ua,
            "model_home": mh, "model_away": ma,
        })
        if not p:
            lines.append("• ⚠️ המשחק הזה לא נמצא במערכת — לא יושווה/ייעקב.")
            missing.append(f"{home}–{away}")
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

    # סיכום קליטה — כדי שתוכל לוודא שכל הניחושים נקלטו, ולראות אילו חסרים במערכת
    lines.append("")
    lines.append(f"📥 נקלטו <b>{read}</b> ניחושים מהתמונה.")
    if missing:
        lines.append(f"⚠️ <b>{len(missing)} משחקים שהימרת עליהם לא נמצאו במערכת</b> "
                     f"(לא יושוו/ייעקבו): {', '.join(missing)}.")
        lines.append("אם המספר לא תואם למה ששלחת — ייתכן שצילום לא היה חד; שלח שוב.")

    # שמירת הניחושים שלך + הצגת הניקוד המצטבר מול המערכת (אם יש משחקים שהוכרעו)
    try:
        predictions_log.record_predictions(entries)
        # עדכן מול התוצאות הרשמיות העדכניות כדי שהניקוד יוצג מיד ומעודכן
        predictions_log.settle_with_results(db.get("results", []))
        summary_txt = predictions_log.format_summary_he()
        if summary_txt:
            lines.append("")
            lines.append(summary_txt)
    except Exception as exc:  # noqa: BLE001
        log.error("שמירת מעקב הניחושים נכשלה: %s", exc)

    _send_message("\n".join(lines))
    # אין שאלות המשך ללוח-משחקים
    state = _load_state()
    state.pop("pending", None)
    _save_state(state)


# --------------------------------------------------------------------------- #
# שיח חופשי על ההרכב מול Gemini (במקום שאלות מובנות)
# --------------------------------------------------------------------------- #
def _handle_forced_transfer(text: str) -> bool:
    """פקודת נעילת חילוף: 'תוציא X' / 'מכור X' / 'בטל נעילה'. True אם טופל."""
    t = text.strip()
    if t in ("בטל נעילה", "בטל", "נקה נעילה"):
        mt = utils.load_json(config.MY_TEAM_PATH, default={}) or {}
        if mt.pop("forced_out", None) is not None:
            utils.save_json(config.MY_TEAM_PATH, mt)
        _send_message("🔓 נעילת החילוף בוטלה. ההמלצות יחזרו לפי קושי המחזור.")
        return True
    for kw in ("תוציא", "מכור", "להוציא", "תמכור"):
        if t.startswith(kw):
            name = t[len(kw):].strip(" את:-")
            if not name:
                return False
            mt = utils.load_json(config.MY_TEAM_PATH, default={}) or {}
            mt["forced_out"] = name
            utils.save_json(config.MY_TEAM_PATH, mt)
            _send_message(f"🔒 ננעל: <b>{name}</b> ייצא בכל מקרה. "
                          "ההמלצות יבנו סביבו. (לביטול: 'בטל נעילה')")
            return True
    return False


def _handle_text(text: str) -> bool:
    """מנתב טקסט חופשי לשיחה עם Gemini בהקשר הקבוצה שלך. True אם טופל."""
    # פקודת נעילת חילוף קודמת לשיח החופשי
    if _handle_forced_transfer(text):
        return True
    state = _load_state()
    squad = state.get("squad") or []
    if state.get("pending") != "lineup_chat" and not squad:
        return False   # אין הקשר הרכב — לא מטפלים בטקסט

    gemini = scraper.GeminiClient()
    if not getattr(gemini, "enabled", False):
        _send_message("⚠️ השיח עם Gemini זמנית לא זמין (מכסת היום אזלה). נסה מאוחר יותר.")
        return True

    squad_str = ", ".join(
        f"{p.get('player_name')} ({p.get('team')}, {p.get('position')})"
        for p in squad
    ) or "לא ידועה"
    cap = state.get("captain") or "—"
    prompt = (
        "אתה יועץ FIFA Fantasy מומחה למונדיאל 2026. ענה בעברית, תכליתי ופרקטי "
        "(עד 6 שורות), בהתבסס על נתונים עדכניים ממקורות הפנטזי המובילים. "
        f"קבוצת המשתמש: {squad_str}. קפטן נוכחי: {cap}.\n"
        f"שאלת המשתמש: {text}"
    )
    answer = gemini.ask_text(prompt, default="")
    if answer:
        _send_message(answer[:3500])
    else:
        _send_message("🤔 לא הצלחתי לענות כרגע — נסה לנסח אחרת או מאוחר יותר.")
    return True


# --------------------------------------------------------------------------- #
# רענון מודל — כמה פעמים ביום, כולל למידה מתוצאות אמת
# --------------------------------------------------------------------------- #
def _maybe_refresh_model(gemini) -> None:
    """מרענן נתונים + לומד מתוצאות אמת, לכל היותר כל _REFRESH_MIN_HOURS שעות.

    מבוסס על ה-feed הרשמי של FIFA (תוצאות/בריכה) — **לא תלוי במכסת Gemini**, כך
    שהתוצאות מתעדכנות לאורך כל הטורניר גם אם Gemini אזל. העשרת xG (Gemini) רצה
    רק אם יש מכסה."""
    state = _load_state()
    last = utils._parse_dt(state.get("last_refresh"))
    if last and datetime.now() - last < timedelta(hours=_REFRESH_MIN_HOURS):
        return
    try:
        import predictions_log
        db = utils.load_json(config.DB_PATH, default={}) or {}

        # תוצאות אמת מהלוח הרשמי (מיידי, ללא Gemini)
        rounds = scraper.fetch_official_rounds()
        added = scraper._record_results(db, scraper.official_results(rounds)) if rounds else 0
        # רענון הבריכה הרשמית (מחיר/בעלות/נקודות/זמינות) + דיפרנציאלים/קושי
        pool = scraper.fetch_official_pool()
        if pool:
            db["participants"] = sorted({p["team"] for p in pool if p.get("team")})
            db["players"] = pool
            db["fixture_difficulty"] = (scraper.official_fixture_difficulty(rounds, db, pool)
                                        or db.get("fixture_difficulty", {}))
            db["differentials"] = (scraper.official_differentials(
                pool, fixture_difficulty=db["fixture_difficulty"])
                or db.get("differentials", {}))
            db["top_picks"] = (scraper.official_top_picks(
                pool, fixture_difficulty=db["fixture_difficulty"])
                or db.get("top_picks", {}))
        # העשרת xG/xA (Gemini) — רק אם יש מכסה
        if getattr(gemini, "enabled", False):
            scraper._enrich_fantasy_data(gemini, db)
        utils.save_json(config.DB_PATH, db)

        # סידור ניחושי המשתמש מול התוצאות האמיתיות שנכנסו
        predictions_log.settle_with_results(db.get("results", []))
        state["last_refresh"] = utils.now_iso()
        _save_state(state)
        log.info("רענון מודל בוצע (%d תוצאות חדשות מהמקור הרשמי)", added)
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
    if not getattr(config, "TELEGRAM_INTAKE_ENABLED", False):
        # קליטת הבוט כבויה — הניחושים נעשים מ-claude.ai/code. לא קוראים צילומים/צ'אט.
        return {"handled": 0, "disabled": True}

    gemini = scraper.GeminiClient()
    # קודם כל — לעבד תמונות שהמתינו (נשמרו כשהמכסה אזלה), כעת כשאולי יש מכסה
    _process_pending_images(gemini)
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
                try:
                    img, mime = _download_file(file_id)
                except Exception as exc:  # noqa: BLE001
                    log.error("הורדת תמונה נכשלה: %s", exc)
                    _send_message("⚠️ לא הצלחתי להוריד את התמונה. נסה לשלוח שוב.")
                    continue
                # מכסת Gemini אזלה — שומרים את התמונה לעיבוד אוטומטי מאוחר (לא מאבדים)
                if not getattr(gemini, "enabled", False):
                    _save_pending_image(img, mime)
                    _send_message("📥 קיבלתי את התמונה, אבל קריאת התמונות אזלה כרגע "
                                  "(מכסה יומית). שמרתי אותה — אעבד אותה אוטומטית כשהמכסה "
                                  "תתחדש, בלי צורך לשלוח שוב.")
                    handled += 1
                    continue
                parsed = classify_image(gemini, img, mime) or {}
                kind = parsed.get("kind")
                if kind == "lineup":
                    _handle_lineup(parsed)
                elif kind == "fixtures":
                    _handle_fixtures(parsed)
                elif getattr(gemini, "_quota_exhausted", False):
                    # המכסה אזלה תוך כדי — שומרים לעיבוד מאוחר
                    _save_pending_image(img, mime)
                    _send_message("📥 קיבלתי את התמונה אבל המכסה אזלה כרגע — שמרתי אותה "
                                  "ואעבד אותה אוטומטית כשהמכסה תתחדש (אין צורך לשלוח שוב).")
                else:
                    _send_message(
                        "🤔 לא הצלחתי לקרוא את התמונה — ייתכן שהיא לא חדה מספיק. "
                        "נסה לשלוח שוב צילום ברור של מסך ההרכב או לוח המשחקים."
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
