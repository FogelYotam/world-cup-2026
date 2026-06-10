"""
יועץ פנטזי אישי — מקבל את הקבוצה האמיתית שלך מ-data/my_team.json ומפיק
המלצות יומיות מותאמות: הרכב פותח חוקי מתוך הסגל שלך, קפטן/סגן, מועמדי
חילוף (transfer in/out) בכפוף לחוקי FIFA (15 שחקנים, 2/5/5/3, מקס' 3
לנבחרת, תקציב), והתרעות על שחקנים פצועים/מורחקים בסגל שלך.

לעולם לא זורק חריגה — אם אין קובץ קבוצה מוחזר {"available": False}.
"""
from __future__ import annotations

import unicodedata

import config
import fantasy
import utils

log = utils.get_logger("advisor")

_MIN_TRANSFER_GAIN = 0.8   # פער EP מינימלי שמצדיק הצעת חילוף
_TRANSFER_HIT = 4          # ניקוד שיורד על כל חילוף מעבר לחופשיים


def load_my_team() -> dict | None:
    """טוען את הקבוצה האישית. None אם הקובץ חסר או לא תקין."""
    if not config.MY_TEAM_PATH.exists():
        log.info("אין קובץ קבוצה אישית (%s) — מדלג על המלצות אישיות",
                 config.MY_TEAM_PATH.name)
        return None
    data = utils.load_json(config.MY_TEAM_PATH, default=None)
    if not isinstance(data, dict) or not isinstance(data.get("squad"), list):
        log.warning("קובץ הקבוצה האישית פגום — מדלג")
        return None
    return data


def _key(name, team) -> tuple:
    return (fantasy_norm(name), fantasy_norm(team))


def fantasy_norm(s) -> str:
    """מנרמל שם: אותיות קטנות + הסרת סימני ניקוד (Mbappé→mbappe, Muñoz→munoz)."""
    s = str(s or "").strip().lower()
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def _surname(name) -> str:
    """שם משפחה מנורמל (האסימון האחרון) — לזיהוי לפי שם קצר מול שם מלא."""
    toks = fantasy_norm(name).replace("-", " ").split()
    return toks[-1] if toks else ""


def _scored_index(scored: list[dict]) -> dict:
    return {_key(s["player_name"], s["team"]): s for s in scored}


def _make_resolver(scored: list[dict]):
    """מאתר שחקן מנוקד גם בשם מלא וגם בשם משפחה+נבחרת (סובלני להבדלי פורמט)."""
    by_full: dict[tuple, dict] = {}
    by_sur: dict[tuple, dict] = {}
    for s in scored:
        team = fantasy_norm(s.get("team"))
        by_full[(fantasy_norm(s.get("player_name")), team)] = s
        by_sur.setdefault((_surname(s.get("player_name")), team), s)

    def resolve(name, team):
        t = fantasy_norm(team)
        return by_full.get((fantasy_norm(name), t)) or by_sur.get((_surname(name), t))
    return resolve


def _squad_identity(players: list[dict]) -> set:
    """מזהה סגל לפי (שם-מלא, נבחרת) ו-(שם-משפחה, נבחרת) — לבדיקת 'בסגל'."""
    ident: set = set()
    for p in players:
        team = fantasy_norm(p.get("team"))
        ident.add((fantasy_norm(p.get("player_name")), team))
        ident.add((_surname(p.get("player_name")), team))
    return ident


def _in_identity(name, team, ident: set) -> bool:
    t = fantasy_norm(team)
    return (fantasy_norm(name), t) in ident or (_surname(name), t) in ident


def _my_squad_scored(my_team: dict, scored: list[dict]) -> list[dict]:
    """ממפה את שחקני הסגל האישי לניקוד EP של המחזור; שחקן חסר מקבל EP 0."""
    resolve = _make_resolver(scored)
    out = []
    for p in my_team.get("squad", []):
        s = resolve(p.get("player_name"), p.get("team"))
        if s:
            out.append(dict(s))
        else:
            out.append({
                "player_name": p.get("player_name"), "team": p.get("team"),
                "position": fantasy.normalize_position(p.get("position")),
                "price": fantasy.DEFAULT_PRICE, "expected_points": 0.0,
                "minutes_risk": "high", "injury_status": "unknown",
                "suspension_status": "available", "_missing": True,
            })
    return out


def _nation_counts(players: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for p in players:
        counts[p["team"]] = counts.get(p["team"], 0) + 1
    return counts


def suggest_transfers(my_scored: list[dict], scored: list[dict],
                      free_transfers: int, bank: float) -> list[dict]:
    """מציע חילופים 1-מול-1 לפי אותה עמדה, בכפוף לתקציב ולמכסת הנבחרת."""
    squad_ident = _squad_identity(my_scored)
    nation = _nation_counts(my_scored)

    pool_by_pos: dict[str, list[dict]] = {}
    for s in scored:
        if _in_identity(s["player_name"], s["team"], squad_ident):
            continue
        if s["suspension_status"] in ("suspended", "banned"):
            continue
        pool_by_pos.setdefault(s["position"], []).append(s)
    for lst in pool_by_pos.values():
        lst.sort(key=lambda x: x["expected_points"], reverse=True)

    suggestions = []
    used_in: set = set()
    # מחפשים שדרוג: מהשחקן החלש בסגל כלפי מעלה
    for out_p in sorted(my_scored, key=lambda x: x["expected_points"]):
        pos = out_p["position"]
        for cand in pool_by_pos.get(pos, []):
            ckey = _key(cand["player_name"], cand["team"])
            if ckey in used_in:
                continue
            # מכסת נבחרת: כמה יהיו אחרי החילוף
            after = nation.get(cand["team"], 0) - (1 if cand["team"] == out_p["team"] else 0)
            if after >= fantasy.MAX_PER_NATION:
                continue
            # תקציב: מותר אם יש כיסוי מהקופה + מכירת היוצא
            if cand["price"] - out_p["price"] > bank + 1e-9:
                continue
            gain = round(cand["expected_points"] - out_p["expected_points"], 2)
            if gain < _MIN_TRANSFER_GAIN:
                continue
            suggestions.append({
                "out": out_p, "in": cand, "gain": gain,
                "cost_delta": round(cand["price"] - out_p["price"], 1),
                "urgent": out_p.get("injury_status") in ("injured", "out", "doubtful")
                          or out_p.get("_missing", False),
            })
            used_in.add(ckey)
            break

    # דחופים (פצוע/חסר) קודם, אחר כך לפי רווח EP
    suggestions.sort(key=lambda x: (not x["urgent"], -x["gain"]))
    for i, s in enumerate(suggestions):
        s["is_free"] = i < max(0, free_transfers)
        s["point_hit"] = 0 if s["is_free"] else _TRANSFER_HIT
    return suggestions


def _position_picks(my_scored: list[dict], scored: list[dict],
                    per_pos: int | None = None) -> dict:
    """לכל עמדה (GK/DEF/MID/FWD) מחזיר 2-3 שחקנים מומלצים לפי EP, עם סימון
    אם הם כבר בסגל שלך ('בסגל') או מועמדים להבאה ('מחוץ לסגל')."""
    per_pos = per_pos or getattr(config, "POSITION_PICKS_PER_POS", 3)
    squad_ident = _squad_identity(my_scored)

    # מאחדים את הבריכה עם שחקני הסגל; שומרים את הניקוד הגבוה לכל שחקן
    merged: dict[tuple, dict] = {}
    for s in list(scored) + list(my_scored):
        k = _key(s.get("player_name"), s.get("team"))
        cur = merged.get(k)
        if cur is None or s.get("expected_points", 0) > cur.get("expected_points", 0):
            merged[k] = s

    by_pos: dict[str, list[dict]] = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for s in merged.values():
        pos = s.get("position")
        if pos in by_pos and s.get("suspension_status") not in ("suspended", "banned"):
            by_pos[pos].append(s)

    picks: dict[str, list[dict]] = {}
    for pos, lst in by_pos.items():
        lst.sort(key=lambda x: x.get("expected_points", 0), reverse=True)
        picks[pos] = [{
            "player_name": p.get("player_name"),
            "team": p.get("team"),
            "expected_points": round(float(p.get("expected_points", 0) or 0), 1),
            "price": p.get("price"),
            "in_squad": _in_identity(p.get("player_name"), p.get("team"), squad_ident),
            "risk": p.get("minutes_risk"),
        } for p in lst[:per_pos]]
    return picks


def transfer_options(my_scored: list[dict], scored: list[dict],
                     bank: float = 0.0, per_pos: int | None = None) -> list[dict]:
    """לכל עמדה (GK/DEF/MID/FWD): השחקן החלש בסגל (out) + עד N מועמדי החלפה
    הטובים ביותר מחוץ לסגל (in), בכפוף לתקציב ולמכסת הנבחרת."""
    per_pos = per_pos or getattr(config, "TRANSFER_CANDIDATES_PER_POS", 2)
    squad_ident = _squad_identity(my_scored)
    nation = _nation_counts(my_scored)

    pool_by_pos: dict[str, list[dict]] = {}
    for s in scored:
        if _in_identity(s["player_name"], s["team"], squad_ident):
            continue
        if s.get("suspension_status") in ("suspended", "banned"):
            continue
        pool_by_pos.setdefault(s["position"], []).append(s)
    for lst in pool_by_pos.values():
        lst.sort(key=lambda x: x.get("expected_points", 0), reverse=True)

    options: list[dict] = []
    for pos in ("GK", "DEF", "MID", "FWD"):
        in_pos = [p for p in my_scored if p.get("position") == pos]
        if not in_pos:
            continue
        out_p = min(in_pos, key=lambda x: x.get("expected_points", 0))
        cands = []
        for cand in pool_by_pos.get(pos, []):
            after = nation.get(cand["team"], 0) - (1 if cand["team"] == out_p["team"] else 0)
            if after >= fantasy.MAX_PER_NATION:
                continue
            if (cand.get("price") or 0) - (out_p.get("price") or 0) > bank + 1e-9:
                continue
            cands.append(cand)
            if len(cands) >= per_pos:
                break
        if not cands:
            continue
        options.append({
            "position": pos,
            "out": {
                "player_name": out_p["player_name"], "team": out_p["team"],
                "expected_points": round(out_p.get("expected_points", 0) or 0, 1),
            },
            "candidates": [{
                "player_name": c["player_name"], "team": c["team"],
                "expected_points": round(c.get("expected_points", 0) or 0, 1),
                "price": c.get("price"),
                "gain": round((c.get("expected_points", 0) or 0)
                              - (out_p.get("expected_points", 0) or 0), 1),
            } for c in cands],
        })
    return options


def build_advice(db: dict, scored: list[dict], my_team: dict | None = None,
                 matchday: int | None = None) -> dict:
    """מפיק חבילת המלצות אישית. scored = שחקנים מנוקדים למחזור הרלוונטי."""
    try:
        my_team = my_team or load_my_team()
        if not my_team:
            return {"available": False}

        my_scored = _my_squad_scored(my_team, scored)
        eleven = fantasy.select_starting_eleven(my_scored)

        free_t = int(my_team.get("free_transfers", 1) or 0)
        bank = float(my_team.get("bank", 0.0) or 0.0)
        transfers = suggest_transfers(my_scored, scored, free_t, bank)

        # התרעות על שחקנים בעייתיים בסגל שלך
        flags = [
            p for p in my_scored
            if p.get("_missing")
            or p.get("injury_status") in ("injured", "doubtful", "out")
            or p.get("suspension_status") in ("suspended", "banned")
        ]

        # קפטן: ההמלצה שלנו מול הבחירה שבקובץ
        rec_cap = eleven["captain"]
        owner_cap = my_team.get("captain")

        return {
            "available": True,
            "matchday": matchday,
            "free_transfers": free_t,
            "bank": bank,
            "starting_eleven": eleven["lineup"],
            "bench": eleven["bench"],
            "formation": eleven["formation"],
            "recommended_captain": rec_cap,
            "recommended_vice": eleven["vice_captain"],
            "owner_captain": owner_cap,
            "captain_change": bool(rec_cap and owner_cap
                                   and rec_cap["player_name"] != owner_cap),
            "total_expected_points": eleven["total_expected_points"],
            "transfers": transfers[:4],
            "flags": flags,
            "position_picks": _position_picks(my_scored, scored),
            "transfer_options": transfer_options(my_scored, scored, bank),
        }
    except Exception as exc:  # noqa: BLE001
        log.error("יועץ הפנטזי האישי נכשל: %s", exc)
        return {"available": False}
