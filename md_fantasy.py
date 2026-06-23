"""
מחולל הרכב פנטזי מלא למחזור הקרוב — סגל 15 חוקי + הרכב פותח + קפטן + ספסל + תקציב.

מתזמר את מנוע הפנטזי הקיים (`fantasy.build_fantasy`) ומוסיף שכבת הקשר למחזור:
יריבה לכל שחקן, סימון ⭐ scouting-bonus (בעלות < 5%), והדפסה ידידותית למשתמש.

שימוש:
    python md_fantasy.py            # מדפיס את ההרכב המומלץ למחזור הקרוב
"""
from __future__ import annotations

import config
import fantasy
import predictor
import utils

log = utils.get_logger("md_fantasy")

_POS_LABEL = {"GK": "שוער", "DEF": "הגנה", "MID": "קישור", "FWD": "חלוץ"}
_POS_ORDER = ("GK", "DEF", "MID", "FWD")


def _num(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def build_matchday_squad(db: dict | None = None,
                         predictions: list[dict] | None = None,
                         budget: float = fantasy.DEFAULT_BUDGET) -> dict:
    """בונה את הרכב הפנטזי למחזור הקרוב ומעשיר כל שחקן ביריבה + סימון scouting bonus.
    מחזיר את מבנה `build_fantasy` עם השדות הנוספים. לעולם לא זורק חריגה."""
    db = db if db is not None else (utils.load_json(config.DB_PATH, default={}) or {})
    if predictions is None:
        predictions = predictor.predict_all(db)
    result = fantasy.build_fantasy(db, predictions, budget)
    if not result.get("available"):
        return result

    fd = db.get("fixture_difficulty", {}) or {}
    sb_thr = getattr(config, "SCOUTING_BONUS_OWNERSHIP", 5.0)

    def _opp(team):
        d = fd.get(team)
        return d.get("opponent") if isinstance(d, dict) else None

    eleven = result["starting_eleven"]
    for p in eleven.get("squad", []):
        own = _num(p.get("ownership"))
        p["opponent"] = _opp(p.get("team"))
        p["scouting_bonus"] = own is not None and own < sb_thr
    return result


def _fmt_player(p: dict, *, captain=None, vice=None) -> str:
    name = p.get("player_name", "?")
    tag = ""
    if captain and (p.get("player_name"), p.get("team")) == captain:
        tag = " 👑"
    elif vice and (p.get("player_name"), p.get("team")) == vice:
        tag = " (ס)"
    star = " ⭐" if p.get("scouting_bonus") else ""
    opp = f" מול {p['opponent']}" if p.get("opponent") else ""
    own = p.get("ownership")
    own_s = f" · {own}%" if own is not None else ""
    price = p.get("price")
    price_s = f" · {price}M" if price is not None else ""
    return f"{name}{tag}{star} ({p.get('team', '?')}{opp}{price_s}{own_s})"


def format_squad(result: dict) -> str:
    """מעצב את ההרכב לטקסט קריא (טלגרם/קונסולה)."""
    if not result.get("available"):
        return "אין מספיק נתוני שחקנים לבניית סגל למחזור."
    e = result["starting_eleven"]
    cap = e.get("captain") or {}
    vice = e.get("vice_captain") or {}
    cap_key = (cap.get("player_name"), cap.get("team")) if cap else None
    vice_key = (vice.get("player_name"), vice.get("team")) if vice else None

    lines = ["🏆 <b>הרכב פנטזי — המחזור הקרוב</b>"]
    cost = e.get("total_cost")
    ep = e.get("total_expected_points")
    meta = f"מערך {e.get('formation', '?')}"
    if cost is not None:
        meta += f" · עלות {cost}/{int(fantasy.DEFAULT_BUDGET)}M"
    if ep is not None:
        meta += f" · תוחלת {ep} נק' (כולל קפטן ×2)"
    lines.append(meta)
    if cap:
        capt = f"👑 קפטן: <b>{cap.get('player_name')}</b>"
        if vice:
            capt += f" · סגן: {vice.get('player_name')}"
        lines.append(capt)

    lineup = e.get("lineup", [])
    lines.append("")
    lines.append("<b>הרכב פותח:</b>")
    for pos in _POS_ORDER:
        group = [p for p in lineup if p.get("position") == pos]
        if not group:
            continue
        items = " · ".join(_fmt_player(p, captain=cap_key, vice=vice_key) for p in group)
        lines.append(f"{_POS_LABEL[pos]}: {items}")

    bench = e.get("bench", [])
    if bench:
        names = " · ".join(_fmt_player(p, captain=cap_key, vice=vice_key) for p in bench)
        lines.append("")
        lines.append(f"🪑 ספסל: {names}")
    return "\n".join(lines)


def squad_context(db: dict | None = None) -> list[dict]:
    """מרכז הקשר לכל שחקן ב-data/my_team.json **מתוך db.json בלבד** (ללא רשת) —
    יריבה, קלות-משחק, שער-נקי%, נקודות, זמינות, ו-scouting bonus. נועד לחוות-דעת
    מהירה מהנייד (claude.ai/code) בלי pip install. לעולם לא זורק."""
    import lineup_alerts
    import predictor
    db = db if db is not None else (utils.load_json(config.DB_PATH, default={}) or {})
    squad = lineup_alerts.load_my_squad()
    pool = db.get("players", []) or []
    fd = db.get("fixture_difficulty", {}) or {}
    tbn = {t.get("team_name"): t for t in db.get("teams", [])}
    sb_thr = getattr(config, "SCOUTING_BONUS_OWNERSHIP", 5.0)
    by_norm = {}
    for p in pool:
        by_norm.setdefault(lineup_alerts._norm(p.get("player_name")), p)
    cs_cache: dict = {}

    def _cs(team):
        if team in cs_cache:
            return cs_cache[team]
        d = fd.get(team, {})
        opp = d.get("opponent") if isinstance(d, dict) else None
        val = None
        if opp and team in tbn:
            try:
                pr = predictor.predict_match(
                    {"home_team": team, "away_team": opp, "stage": "GROUP"}, tbn)
                val = round(pr["clean_sheet"]["home"] * 100)
            except Exception:  # noqa: BLE001
                val = None
        cs_cache[team] = (opp, val)
        return cs_cache[team]

    out = []
    for sp in squad:
        nm = sp.get("player_name")
        pl = lineup_alerts._match(nm, pool, by_norm) or {}
        team = pl.get("team") or sp.get("team")
        opp, cs = _cs(team) if team else (None, None)
        own = _num(pl.get("ownership"))
        ease = None
        d = fd.get(team, {})
        if isinstance(d, dict) and isinstance(d.get("difficulty"), (int, float)):
            ease = round(1 - d["difficulty"], 2)
        avail = lineup_alerts._bad_status(pl)
        out.append({
            "name": pl.get("player_name") or nm, "team": team,
            "position": pl.get("position") or sp.get("position"),
            "opponent": opp, "ease": ease, "clean_sheet_pct": cs,
            "recent_points": pl.get("recent_points"), "ownership": own,
            "scouting_bonus": own is not None and own < sb_thr,
            "availability": (avail[1] if avail else "✅ זמין/פותח"),
        })
    return out


def main() -> None:
    result = build_matchday_squad()
    text = format_squad(result)
    # קונסולה — בלי תגי HTML
    import re
    print(re.sub(r"</?b>", "", text))


if __name__ == "__main__":
    main()
