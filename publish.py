"""
פרסום הדוח היומי לכתובת ציבורית דרך GitHub Pages.

בכל ריצה מוצלחת, אם GIT_AUTO_PUBLISH פעיל, מבוצע git add/commit/push
לקובץ docs/index.html — וכך הכתובת הציבורית מתעדכנת אוטומטית כל בוקר.
לעולם לא זורק חריגה: כל כשל נרשם ביומן וההרצה ממשיכה.
"""
from __future__ import annotations

import subprocess

import config
import utils

log = utils.get_logger("publish")

_DOCS_REL = "docs/index.html"


def _git(*args, timeout: int = 60) -> subprocess.CompletedProcess:
    """מריץ פקודת git בתיקיית הפרויקט ומחזיר את התוצאה."""
    return subprocess.run(
        ["git", *args],
        cwd=str(config.BASE_DIR),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def publish_docs(message: str | None = None) -> bool:
    """מפרסם את docs/index.html ל-GitHub (add → commit → push).

    מחזיר True אם נדחף בהצלחה, False אם דולג/נכשל. אינו זורק לעולם.
    """
    try:
        if not (config.BASE_DIR / _DOCS_REL).exists():
            log.info("אין docs/index.html לפרסום — מדלג")
            return False

        inside = _git("rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0:
            log.info("התיקייה אינה ריפו git — מדלג על פרסום אוטומטי")
            return False

        _git("add", _DOCS_REL)

        # יש בכלל שינוי לפרסם?
        staged = _git("diff", "--cached", "--quiet")
        if staged.returncode == 0:
            log.info("אין שינוי בדוח מאז הפרסום האחרון — אין מה לדחוף")
            return False

        msg = message or f"Daily report {utils.now_iso()[:10]}"
        commit = _git("commit", "-m", msg)
        if commit.returncode != 0:
            log.warning("git commit נכשל: %s", (commit.stderr or "").strip()[:200])
            return False

        push = _git("push", timeout=120)
        if push.returncode != 0:
            log.warning("git push נכשל: %s — בדוק התחברות/credential helper",
                        (push.stderr or "").strip()[:200])
            return False

        log.info("הדוח פורסם לכתובת הציבורית בהצלחה")
        return True
    except FileNotFoundError:
        log.warning("git אינו מותקן/בנתיב — מדלג על פרסום אוטומטי")
        return False
    except Exception as exc:  # noqa: BLE001
        log.error("פרסום הדוח נכשל: %s", exc)
        return False
