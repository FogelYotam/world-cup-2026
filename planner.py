"""
מתכנן פנטזי רב-מחזורי. גוזר את מחזורי שלב הבתים (מחזור 1 מהנתונים,
מחזורים 2-3 בליגת-סבב מתוך ארבע נבחרות הבית), מחשב לכל מחזור את היריב
וצפי השערים, ובוחר 11 פותחים + ספסל מתוך אותו סגל 15 לפי קושי המשחק.

הסגל נבנה פעם אחת (כמו draft אמיתי) ונשמר; רק ההרכב הפותח והקפטן
מתחלפים ממחזור למחזור.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

import fantasy
import predictor
import utils

log = utils.get_logger("planner")

_GROUP_RE = re.compile(r"^([A-Za-z]+)")


def group_key(match_id) -> str | None:
    """אות הבית ממזהה המשחק (A1 -> 'A'). None אם לא בפורמט הזה."""
    if not match_id:
        return None
    m = _GROUP_RE.match(str(match_id))
    return m.group(1).upper() if m else None


def _shift_date(date_str: str | None, days: int) -> str | None:
    if not date_str:
        return None
    try:
        d = date.fromisoformat(str(date_str)[:10])
    except ValueError:
        return None
    return (d + timedelta(days=days)).isoformat()


def _synth_match(group: str, idx: int, home: str, away: str,
                 date_str: str | None, md: int) -> dict:
    """בונה רשומת משחק גזורה למחזור 2/3 (ללא אודדס — חיזוי מודל בלבד)."""
    return {
        "match_id": f"{group}{idx}-md{md}",
        "home_team": home,
        "away_team": away,
        "date": date_str,
        "stage": "Group",
        "status": "scheduled",
        "context": {},
    }


def derive_matchdays(db: dict, num: int = 3) -> list[dict]:
    """
    מחזיר רשימת מחזורים: [{"matchday": n, "matches": [...]}].
    מחזור 1 — מהנתונים הקיימים. מחזורים 2-3 — ליגת-סבב גזורה לכל בית
    (כל נבחרת פוגשת כל אחת מהשלוש פעם אחת).
    """
    by_group: dict[str, list[dict]] = {}
    for m in db.get("matches", []):
        g = group_key(m.get("match_id"))
        if g:
            by_group.setdefault(g, []).append(m)

    md1, md2, md3 = [], [], []
    for g, matches in by_group.items():
        if len(matches) < 2:
            md1.extend(matches)
            continue
        m1, m2 = matches[0], matches[1]
        t0, t1 = m1.get("home_team"), m1.get("away_team")
        t2, t3 = m2.get("home_team"), m2.get("away_team")
        md1.extend([m1, m2])
        d2 = _shift_date(m1.get("date"), 7)
        d3 = _shift_date(m1.get("date"), 13)
        # סבב חוקי: כל נבחרת משחקת פעם אחת בכל מחזור
        md2.append(_synth_match(g, 1, t0, t2, d2, 2))
        md2.append(_synth_match(g, 2, t1, t3, d2, 2))
        md3.append(_synth_match(g, 1, t0, t3, d3, 3))
        md3.append(_synth_match(g, 2, t1, t2, d3, 3))

    rounds = [
        {"matchday": 1, "matches": md1},
        {"matchday": 2, "matches": md2},
        {"matchday": 3, "matches": md3},
    ]
    return rounds[:max(1, num)]


def _predict_matches(db: dict, matches: list[dict]) -> list[dict]:
    """מריץ את מנוע החיזוי על קבוצת משחקים ספציפית (בלי סינון אמון)."""
    teams_by_name = {t.get("team_name"): t for t in db.get("teams", [])}
    preds = []
    for m in matches:
        try:
            preds.append(predictor.predict_match(m, teams_by_name))
        except Exception as exc:  # noqa: BLE001
            log.error("חיזוי נכשל למשחק %s: %s", m.get("match_id"), exc)
    return preds


def _date_range(matches: list[dict]) -> str:
    dates = sorted(d[:10] for d in (m.get("date") for m in matches) if d)
    if not dates:
        return ""
    fmt = lambda s: f"{s[8:10]}/{s[5:7]}"  # noqa: E731
    return fmt(dates[0]) if dates[0] == dates[-1] else f"{fmt(dates[0])}–{fmt(dates[-1])}"


def build_plan(db: dict, num_matchdays: int = 3,
               budget: float = fantasy.DEFAULT_BUDGET) -> dict:
    """
    בונה תוכנית פנטזי לכמה מחזורים קדימה. הסגל (15) נקבע פעם אחת לפי הביצוע
    הממוצע על פני המחזורים המתוכננים; לכל מחזור נבחרים 11 + ספסל + קפטן.
    לעולם לא זורק חריגה — בכשל מחזיר {"available": False}.
    """
    try:
        rounds = derive_matchdays(db, num_matchdays)
        if not rounds or not db.get("players"):
            return {"available": False}

        # ניקוד לכל מחזור בנפרד (רגיש ליריב)
        scored_by_md, fixtures_by_md = [], []
        for r in rounds:
            preds = _predict_matches(db, r["matches"])
            scored_by_md.append(fantasy.score_players(db, preds))
            fixtures_by_md.append(preds)

        # ניקוד ממוצע על פני המחזורים — בסיס לבחירת הסגל הקבוע
        avg = _average_scored(scored_by_md)
        squad_info = fantasy.pick_squad(avg, budget)
        squad = squad_info["squad"]
        if not squad:
            return {"available": False}

        matchdays = []
        for r, scored_md, preds in zip(rounds, scored_by_md, fixtures_by_md):
            squad_md = fantasy.squad_for_matchday(squad, scored_md)
            eleven = fantasy.select_starting_eleven(squad_md)
            matchdays.append({
                "matchday": r["matchday"],
                "date_range": _date_range(r["matches"]),
                "formation": eleven["formation"],
                "captain": eleven["captain"],
                "vice_captain": eleven["vice_captain"],
                "total_expected_points": eleven["total_expected_points"],
                "lineup": eleven["lineup"],
                "bench": eleven["bench"],
                "fixtures": _fixtures_view(preds),
            })

        return {
            "available": True,
            "squad": squad,
            "squad_cost": squad_info["cost"],
            "matchdays": matchdays,
        }
    except Exception as exc:  # noqa: BLE001
        log.error("בניית תוכנית הפנטזי נכשלה: %s", exc)
        return {"available": False}


def _average_scored(scored_by_md: list[list[dict]]) -> list[dict]:
    """ממצע EP של כל שחקן על פני המחזורים; שאר השדות מהמחזור הראשון."""
    if not scored_by_md:
        return []
    acc: dict[tuple, dict] = {}
    for scored in scored_by_md:
        for s in scored:
            key = (s["player_name"], s["team"])
            if key not in acc:
                acc[key] = dict(s)
                acc[key]["_sum"] = 0.0
                acc[key]["_n"] = 0
            acc[key]["_sum"] += s["expected_points"]
            acc[key]["_n"] += 1
    out = []
    for s in acc.values():
        s["expected_points"] = round(s["_sum"] / s["_n"], 2)
        del s["_sum"], s["_n"]
        out.append(s)
    out.sort(key=lambda x: x["expected_points"], reverse=True)
    return out


def _fixtures_view(preds: list[dict]) -> list[dict]:
    """רשימת משחקים מסודרת ומקוצרת לתצוגה."""
    rows = [
        {
            "home_team": p.get("home_team"),
            "away_team": p.get("away_team"),
            "recommended_score": p.get("recommended_score"),
            "confidence": p.get("confidence"),
            "date": p.get("date"),
        }
        for p in preds
    ]
    rows.sort(key=lambda r: (str(r.get("date") or ""), str(r.get("home_team") or "")))
    return rows
