"""
נקודת הכניסה — מריץ את כל הצינור: איסוף → חיזוי → פנטזי → דוח → מייל.

הרצה:  python main.py [--days N] [--no-email]
"""
from __future__ import annotations

import argparse
import sys

import advisor
import config
import fantasy
import planner
import predictor
import publish
import report
import scraper
import state
import telegram_intake
import utils

log = utils.get_logger("main")


def _force_utf8_console() -> None:
    """מוודא שהמסוף ב-Windows מדפיס עברית בלי לקרוס."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def print_console_summary(predictions: list[dict], fantasy_result: dict) -> None:
    """סיכום קצר וקריא למסוף."""
    print("\n" + "=" * 60)
    print(f"  מונדיאל 2026 — סיכום ({len(predictions)} משחקים)")
    print("=" * 60)
    for p in predictions:
        print(
            f"  {p['home_team']} מול {p['away_team']}: "
            f"{p['recommended_score']}  (אמון {p['confidence']}%)"
        )
    if fantasy_result.get("available"):
        e = fantasy_result["starting_eleven"]
        cap = e["captain"]["player_name"] if e.get("captain") else "—"
        print("-" * 60)
        print(f"  פנטזי: מערך {e['formation']} · קפטן {cap} · "
              f"צפי {e['total_expected_points']} נק'")
    print("=" * 60 + "\n")


def run(days_ahead: int = 3, send_mail: bool = True, scrape: bool = True,
        force: bool = False) -> int:
    """מריץ את הצינור המלא. מחזיר קוד יציאה (0 = הצלחה)."""
    log.info("=== התחלת ריצה ===")

    # קליטת הודעות מהבוט (צילומי הרכב/לוח-משחקים + תשובות) לפני ההמלצות
    try:
        telegram_intake.run_bot_once(poll_timeout=2)
    except Exception as exc:  # noqa: BLE001
        log.error("קליטת הודעות מהבוט נכשלה: %s", exc)

    if scrape:
        try:
            db = scraper.collect(days_ahead=days_ahead)
        except Exception as exc:  # noqa: BLE001
            log.error("שלב האיסוף נכשל לחלוטין: %s — ממשיך עם DB קיים", exc)
            db = utils.load_json(config.DB_PATH, default={}) or {}
    else:
        log.info("מצב --no-scrape: משתמש בנתונים הקיימים ב-db.json")
        db = utils.load_json(config.DB_PATH, default={}) or {}

    predictions = predictor.predict_all(db)
    fantasy_result = fantasy.build_fantasy(db, predictions)

    # תכנון פנטזי כמה מחזורים קדימה (סגל קבוע + הרכב/קפטן מתחלפים)
    try:
        plan = planner.build_plan(db, num_matchdays=days_ahead)
    except Exception as exc:  # noqa: BLE001
        log.error("בניית תוכנית המחזורים נכשלה: %s", exc)
        plan = {"available": False}

    # המלצות אישיות לפי הקבוצה האמיתית שלך (data/my_team.json) למחזור הקרוב
    try:
        scored_md1 = fantasy.score_players(db, predictions)
        advice = advisor.build_advice(db, scored_md1, matchday=1)
    except Exception as exc:  # noqa: BLE001
        log.error("בניית ההמלצות האישיות נכשלה: %s", exc)
        advice = {"available": False}

    # החלטת קצב: יומי עד תחילת המונדיאל, אחריו רק כשיש טריגר
    prev_state = state.load_state()
    snapshot = state.build_snapshot(db, predictions)
    should_send, reasons = state.decide(prev_state, snapshot, force=force)

    try:
        html = report.render_html(predictions, fantasy_result, plan=plan, advice=advice)
    except Exception as exc:  # noqa: BLE001
        log.error("הפקת הדוח נכשלה: %s", exc)
        html = ""

    print_console_summary(predictions, fantasy_result)

    if not should_send:
        log.info("אין שינוי מהותי — לא נשלח עדכון היום")
        print("ℹ️  אין שינוי מהותי מאז העדכון האחרון — לא נשלח דוח.\n")
        log.info("=== סיום ריצה ===")
        return 0

    log.info("נשלח עדכון. סיבות: %s", " · ".join(reasons))
    if send_mail:
        html_path = config.OUTPUT_DIR / "report.html"
        sent_telegram = report.send_telegram(
            predictions, fantasy_result, html_path, reasons=reasons,
            plan=plan, advice=advice
        )
        # מייל נשלח רק אם טלגרם לא מוגדר, וכגיבוי
        if not sent_telegram and html and config.mail_enabled():
            report.send_email(html)

    # פרסום אוטומטי לכתובת הציבורית (GitHub Pages) אם הופעל ב-.env
    if config.GIT_AUTO_PUBLISH:
        publish.publish_docs(message=f"Daily report {utils.now_iso()[:10]}")

    state.save_state(snapshot)
    log.info("=== סיום ריצה ===")
    return 0


def main() -> int:
    _force_utf8_console()
    parser = argparse.ArgumentParser(description="מערכת ניחושי מונדיאל ופנטזי")
    parser.add_argument("--days", type=int, default=3, help="כמה ימים קדימה לאסוף")
    parser.add_argument("--no-send", action="store_true",
                        help="אל תשלח (טלגרם/מייל) — הפק דוח מקומי בלבד")
    parser.add_argument("--no-scrape", action="store_true",
                        help="דלג על איסוף — השתמש בנתונים הקיימים ב-db.json")
    parser.add_argument("--force", action="store_true",
                        help="שלח עדכון גם אם אין שינוי מהותי (עוקף את לוגיקת הקצב)")
    args = parser.parse_args()
    return run(days_ahead=args.days, send_mail=not args.no_send,
               scrape=not args.no_scrape, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
