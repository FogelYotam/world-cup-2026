"""
מעקב ניקוד פנטזי **אמיתי** לפי מחזור — מבוסס על מי ש**פתח בפועל** (כולל חילופים
יומיים שתפסו יותר מ-11 שחקנים), עם הכפלת קפטן. הניקוד נלקח מהנקודות הרשמיות לכל
מחזור (`round_points` בבריכה), כך שהוא תואם את האפליקציה.

שימוש: בסוף כל מחזור, ספק את רשימת השחקנים ש**הנקודות שלהם נספרו לך** (אלה
שפתחו כשהמשחק שלהם שוחק) + הקפטן:
    import fantasy_score as fs
    print(fs.record_round("3", fielded=["Messi", "Salah", ...], captain="Messi"))
"""
from __future__ import annotations

import json
import unicodedata

import config
import utils

log = utils.get_logger("fantasy_score")

_PATH = config.DATA_DIR / "my_fantasy_score.json"


def _norm(s) -> str:
    d = unicodedata.normalize("NFKD", str(s or "").lower())
    return "".join(c for c in d if not unicodedata.combining(c)).strip()


def _load() -> dict:
    return utils.load_json(_PATH, default={"rounds": []}) or {"rounds": []}


def _save(d: dict) -> None:
    utils.save_json(_PATH, d)


def _round_pts_lookup(db: dict) -> dict:
    """שם-שחקן מנורמל → {round: points} מהבריכה הרשמית."""
    out = {}
    for p in (db.get("players") or []):
        out.setdefault(_norm(p.get("player_name")), p.get("round_points") or {})
    return out


def record_round(round_id, fielded: list[str], captain: str | None = None,
                 db: dict | None = None, transfer_hit: int = 0) -> str:
    """רושם את ניקוד המחזור: סכום נק' המחזור של מי שפתח + קפטן כפול − עונש טרנספרים.
    `fielded` = השחקנים שהנקודות שלהם נספרו לך (כולל חילופים יומיים). מחזיר סיכום."""
    db = db if db is not None else (utils.load_json(config.DB_PATH, default={}) or {})
    lut = _round_pts_lookup(db)
    rid = str(round_id)

    def pts(name):
        return float((lut.get(_norm(name)) or {}).get(rid, 0) or 0)

    base = sum(pts(n) for n in fielded)
    cap_bonus = pts(captain) if captain else 0.0          # הקפטן נספר פעם נוספת
    score = round(base + cap_bonus - (transfer_hit or 0))
    unknown = [n for n in fielded if _norm(n) not in lut]

    d = _load()
    d["rounds"] = [r for r in d["rounds"] if str(r.get("round")) != rid]   # החלפה אם קיים
    d["rounds"].append({"round": rid, "fielded": list(fielded), "captain": captain,
                        "transfer_hit": transfer_hit, "score": score})
    d["rounds"].sort(key=lambda r: int(r["round"]) if str(r["round"]).isdigit() else 999)
    _save(d)

    lines = [f"✅ נרשם מחזור {rid}: {score} נק' "
             f"({len(fielded)} שחקנים + קפטן {captain or '—'} כפול"
             + (f" − {transfer_hit} עונש" if transfer_hit else "") + ")"]
    if unknown:
        lines.append(f"⚠️ לא זוהו בבריכה (0 נק'): {', '.join(unknown)}")
    lines.append("")
    lines.append(format_he())
    return "\n".join(lines)


def summary() -> dict:
    d = _load()
    cum, rows = 0, []
    for r in d["rounds"]:
        cum += r.get("score", 0)
        rows.append({"round": r["round"], "score": r.get("score", 0), "cumulative": cum})
    return {"by_round": rows, "total": cum}


def format_he() -> str:
    s = summary()
    if not s["by_round"]:
        return "עדיין לא נרשמו מחזורים. שלח: fs.record_round('N', fielded=[...], captain='...')."
    parts = " · ".join(f"ס{r['round']} {r['score']}" for r in s["by_round"])
    return f"🏆 <b>הניקוד האמיתי שלך</b>: {parts} · <b>מצטבר {s['total']}</b>"
