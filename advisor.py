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
        if cur is None or (s.get("expected_points") or 0) > (cur.get("expected_points") or 0):
            merged[k] = s

    by_pos: dict[str, list[dict]] = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for s in merged.values():
        pos = s.get("position")
        if pos in by_pos and s.get("suspension_status") not in ("suspended", "banned"):
            by_pos[pos].append(s)

    picks: dict[str, list[dict]] = {}
    for pos, lst in by_pos.items():
        lst.sort(key=lambda x: x.get("expected_points") or 0, reverse=True)
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
        lst.sort(key=lambda x: x.get("expected_points") or 0, reverse=True)

    options: list[dict] = []
    for pos in ("GK", "DEF", "MID", "FWD"):
        in_pos = [p for p in my_scored if p.get("position") == pos]
        if not in_pos:
            continue
        out_p = min(in_pos, key=lambda x: x.get("expected_points") or 0)
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


_POSITIONS = ("GK", "DEF", "MID", "FWD")
_DEFAULT_DIFF_COUNTS = {"GK": 3, "DEF": 5, "MID": 5, "FWD": 3}


def _nailed(s) -> int:
    """מובטח-דקות? 1 אם כן, 0 אם סיכון מינוטים גבוה / לא צפוי בהרכב."""
    if s.get("expected_start") is False:
        return 0
    if s.get("minutes_risk") == "high":
        return 0
    return 1


def differential_picks(my_scored: list[dict], scored: list[dict],
                       max_ownership: float | None = None,
                       counts: dict | None = None) -> dict:
    """שחקני דיפרנציאל **לכל עמדה** — בעלות נמוכה (< הסף %), מובטחי-דקות קודם.
    כמות לכל עמדה לפי config.DIFFERENTIAL_COUNTS (3 GK / 5 DEF / 5 MID / 3 FWD)."""
    max_own = (max_ownership if max_ownership is not None
               else getattr(config, "DIFFERENTIAL_MAX_OWNERSHIP", 5.0))
    counts = counts or getattr(config, "DIFFERENTIAL_COUNTS", _DEFAULT_DIFF_COUNTS)
    squad_ident = _squad_identity(my_scored)
    squad_surnames = {_surname(p.get("player_name"))
                      for p in my_scored if p.get("player_name")}

    by_pos: dict[str, list] = {p: [] for p in _POSITIONS}
    for s in scored:
        pos = s.get("position")
        if pos not in by_pos:
            continue
        if _in_identity(s["player_name"], s["team"], squad_ident):
            continue
        if _surname(s.get("player_name")) in squad_surnames:
            continue
        if s.get("suspension_status") in ("suspended", "banned"):
            continue
        try:
            own = float(s.get("ownership"))
        except (TypeError, ValueError):
            continue
        if own >= max_own:
            continue
        by_pos[pos].append((own, s))

    out: dict[str, list] = {}
    for pos, cands in by_pos.items():
        # מובטחי-דקות קודם, ואז לפי תוחלת נקודות
        cands.sort(key=lambda t: (_nailed(t[1]), t[1].get("expected_points") or 0),
                   reverse=True)
        out[pos] = [{
            "player_name": s["player_name"], "team": s["team"], "position": pos,
            "expected_points": round(s.get("expected_points") or 0, 1),
            "ownership": round(own, 1), "price": s.get("price"),
        } for own, s in cands[:counts.get(pos, 3)]]
    return out


def differentials_for_user(db_diffs: dict | None, my_scored: list[dict],
                           scored: list[dict]) -> dict:
    """דיפרנציאלים לכל עמדה — מעדיף את השליפה הממוקדת מה-DB (כל המאגר);
    מסנן שחקנים שכבר בסגל; נופל לבריכת השחקנים אם אין שליפה ייעודית."""
    counts = getattr(config, "DIFFERENTIAL_COUNTS", _DEFAULT_DIFF_COUNTS)
    max_own = getattr(config, "DIFFERENTIAL_MAX_OWNERSHIP", 5.0)
    if isinstance(db_diffs, dict) and any(db_diffs.get(p) for p in _POSITIONS):
        squad_ident = _squad_identity(my_scored)
        # סינון נוסף לפי שם משפחה בלבד — עמיד לנבחרת שגויה ב-my_team.json
        squad_surnames = {_surname(p.get("player_name"))
                          for p in my_scored if p.get("player_name")}
        # אל תציע שחקנים מנבחרת שכבר במכסה (3) אצל המשתמש
        nation_counts = _nation_counts(my_scored)
        capped = {n for n, c in nation_counts.items() if c >= fantasy.MAX_PER_NATION}
        out: dict[str, list] = {}
        for pos in _POSITIONS:
            cands = []
            for it in (db_diffs.get(pos) or []):
                if not isinstance(it, dict) or not it.get("player_name"):
                    continue
                if _in_identity(it.get("player_name"), it.get("team"), squad_ident):
                    continue
                if _surname(it.get("player_name")) in squad_surnames:
                    continue
                if it.get("team") in capped:
                    continue
                own = it.get("ownership")
                if own is not None and own >= max_own:
                    continue
                cands.append(it)
            # מובטחי-דקות קודם (שמירה על סדר המקור בתוך אותה רמה)
            cands.sort(key=_nailed, reverse=True)
            out[pos] = [{
                "player_name": it.get("player_name"), "team": it.get("team"),
                "position": pos,
                "expected_points": round(it.get("expected_points") or 0, 1),
                "ownership": round(it["ownership"], 1) if it.get("ownership") is not None else None,
                "price": it.get("price"), "reason": it.get("reason"),
            } for it in cands[:counts.get(pos, 3)]]
        return out
    return differential_picks(my_scored, scored)


def fixture_difficulty(predictions: list[dict] | None) -> dict:
    """לכל נבחרת: קושי המשחק הקרוב לפי הסתברות הניצחון מהמודל (0=קל, 1=קשה)."""
    out: dict[str, dict] = {}
    for p in predictions or []:
        o = p.get("outcome_probabilities") or {}
        for team, wp, opp in (
            (p.get("home_team"), o.get("home_win", 0), p.get("away_team")),
            (p.get("away_team"), o.get("away_win", 0), p.get("home_team")),
        ):
            if not team or team in out:      # שומרים את המשחק הקרוב ביותר
                continue
            out[team] = {"opponent": opp, "win_prob": round(wp, 3),
                         "difficulty": round(1 - wp, 3)}
    return out


def _brief_player(p: dict) -> dict:
    return {"player_name": p.get("player_name"), "team": p.get("team"),
            "position": p.get("position"), "price": p.get("price"),
            "ownership": p.get("ownership"), "opponent": p.get("opponent"),
            "difficulty": p.get("difficulty")}


def _pick_two_in(in_by_pos: dict, need: list, budget: float,
                 nation_counts: dict) -> list | None:
    """בוחר 2 נכנסים בעמדות הנדרשות, בתוך התקציב, עם ניצול תקציבי מקסימלי."""
    best = None
    for a in in_by_pos.get(need[0], []):
        for b in in_by_pos.get(need[1], []):
            if a["player_name"] == b["player_name"]:
                continue
            nc = dict(nation_counts)
            nc[a["team"]] = nc.get(a["team"], 0) + 1
            nc[b["team"]] = nc.get(b["team"], 0) + 1
            if any(v > fantasy.MAX_PER_NATION for v in nc.values()):
                continue
            cost = (a["price"] or 0) + (b["price"] or 0)
            if cost > budget + 1e-9:
                continue
            da = a["difficulty"] if a["difficulty"] is not None else 0.5
            db_ = b["difficulty"] if b["difficulty"] is not None else 0.5
            n_easy = (1 if da <= 0.45 else 0) + (1 if db_ <= 0.45 else 0)
            # קודם כמה נכנסים בפיקסצ'ר קל, ואז ניצול תקציב מקסימלי
            score = (n_easy, round(cost, 1), -(da + db_))
            if best is None or score > best[0]:
                best = (score, [a, b])
    return best[1] if best else None


def transfer_recommendations(my_team: dict | None, db: dict,
                             predictions: list[dict] | None,
                             forced_out: str | None = None,
                             max_options: int = 8) -> list[dict]:
    """המלצות חילוף 2-מול-2: קשה-מחזור יוצא, דיפרנציאל בטוח-בקל נכנס,
    בתוך התקציב (in ≤ out + bank), עם ניצול תקציבי מקסימלי."""
    my_team = my_team or {}
    squad = [dict(p) for p in my_team.get("squad", []) if p.get("player_name")]
    if not squad:
        return []
    # מעדיפים מפת-קושי מדויקת שנשמרה ב-DB; אחרת גוזרים מהחיזויים
    diff = (db.get("fixture_difficulty") if isinstance(db, dict) else None) \
        or fixture_difficulty(predictions)
    bank = float(my_team.get("bank") or 0)
    for p in squad:
        d = diff.get(p.get("team")) or {}
        p["difficulty"] = d.get("difficulty")
        p["opponent"] = d.get("opponent")
        p["price"] = p.get("price") or 0

    squad_surnames = {_surname(p["player_name"]) for p in squad}
    nation_counts: dict = {}
    for p in squad:
        nation_counts[p.get("team")] = nation_counts.get(p.get("team"), 0) + 1

    in_by_pos = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for pos, items in (db.get("differentials") or {}).items():
        if pos not in in_by_pos:
            continue
        for it in (items or []):
            if _surname(it.get("player_name")) in squad_surnames:
                continue
            row = dict(it)
            d = diff.get(it.get("team")) or {}
            row["difficulty"] = d.get("difficulty")
            row["opponent"] = d.get("opponent")
            row["price"] = it.get("price") or 0
            in_by_pos[pos].append(row)
        in_by_pos[pos].sort(key=lambda x: (
            x["difficulty"] if x["difficulty"] is not None else 0.5,
            x.get("ownership") if x.get("ownership") is not None else 99))

    def hardness(p):
        return p["difficulty"] if p["difficulty"] is not None else 0.0

    forced = None
    if forced_out:
        forced = next((p for p in squad
                       if _surname(p["player_name"]) == _surname(forced_out)), None)
    out1_list = [forced] if forced else sorted(squad, key=hardness, reverse=True)[:3]

    options, seen = [], set()
    for out1 in out1_list:
        if not out1:
            continue
        for out2 in sorted(squad, key=lambda p: (-hardness(p), p["price"])):
            if out2["player_name"] == out1["player_name"]:
                continue
            need = [out1["position"], out2["position"]]
            budget = out1["price"] + out2["price"] + bank
            nc = dict(nation_counts)
            nc[out1["team"]] = nc.get(out1["team"], 0) - 1
            nc[out2["team"]] = nc.get(out2["team"], 0) - 1
            picks = _pick_two_in(in_by_pos, need, budget, nc)
            if not picks:
                continue
            key = tuple(sorted([out1["player_name"], out2["player_name"]])
                        + sorted([c["player_name"] for c in picks]))
            if key in seen:
                continue
            seen.add(key)
            in_cost = round(sum(c["price"] for c in picks), 1)
            options.append({
                "out": [_brief_player(out1), _brief_player(out2)],
                "in": [_brief_player(c) for c in picks],
                "out_cost": round(out1["price"] + out2["price"], 1),
                "in_cost": in_cost, "bank_after": round(budget - in_cost, 1),
            })
            if len(options) >= max_options:
                return options
    return options


def _norm_name(s) -> str:
    import unicodedata
    d = unicodedata.normalize("NFKD", str(s or "").lower())
    return "".join(c for c in d if not unicodedata.combining(c)).strip()


def _owned_pool(my_team: dict, db: dict) -> dict:
    """ממפה שם-שחקן מנורמל → רשומת הבריכה הרשמית, עבור שחקני הסגל שלך."""
    by_name = {}
    for p in (db.get("players") or []):
        by_name.setdefault(_norm_name(p.get("player_name")), p)
    out = {}
    for s in my_team.get("squad", []):
        p = by_name.get(_norm_name(s.get("player_name")))
        if p:
            out[_norm_name(s.get("player_name"))] = p
    return out


def squad_total_points(my_team: dict, db: dict) -> dict:
    """סך נקודות הפנטזי שצברו שחקני הסגל שלך עד כה (סכום fifa_total_points).
    אומדן — לא הניקוד ההיסטורי המדויק של הקבוצה (שתלוי במי פתח/קפטן בכל מחזור)."""
    owned = _owned_pool(my_team, db)
    total = sum(p.get("fifa_total_points") or 0 for p in owned.values())
    return {"total": round(total), "counted": len(owned), "squad": len(my_team.get("squad", []))}


def daily_substitutions(lineup: list[dict], bench: list[dict], db: dict) -> list[dict]:
    """הצעות חילוף מהספסל **לפי יום-משחק** (שעון מקומי): בכל יום, הכנס שחקן ספסל
    שמשחק באותו יום במקום מתחיל מאותה עמדה שלא משחק באותו יום."""
    import utils
    fd = db.get("fixture_difficulty", {}) or {}

    def matchday(p):
        d = fd.get(p.get("team"))
        dt = utils._parse_dt(d.get("date")) if isinstance(d, dict) else None
        return dt.date() if dt else None

    days: dict = {}
    for p in lineup:
        md = matchday(p)
        if md:
            days.setdefault(md, {"xi": [], "bench": []})["xi"].append(p)
    for p in bench:
        md = matchday(p)
        if md:
            days.setdefault(md, {"xi": [], "bench": []})["bench"].append(p)

    out = []
    for day in sorted(days):
        grp = days[day]
        xi_out = [p for p in lineup if matchday(p) != day]   # מתחילים שלא משחקים היום
        used, swaps = set(), []
        for bp in grp["bench"]:
            cand = next((x for x in xi_out
                         if x.get("position") == bp.get("position") and id(x) not in used), None)
            if cand:
                used.add(id(cand))
                swaps.append({"out": cand.get("player_name"), "in": bp.get("player_name"),
                              "position": bp.get("position")})
        out.append({
            "date": day.isoformat(),
            "playing": [p.get("player_name") for p in grp["xi"]],
            "bench_playing": [p.get("player_name") for p in grp["bench"]],
            "swaps": swaps,
        })
    return out


def build_advice(db: dict, scored: list[dict], my_team: dict | None = None,
                 matchday: int | None = None,
                 predictions: list[dict] | None = None) -> dict:
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

        # קפטן: ההמלצה שלנו מול הבחירה שבקובץ — אך אם הקפטן הנוכחי "בּוֹעֵר"
        # (עשה ≥ CAPTAIN_KEEP_THRESHOLD במחזור האחרון) — לא ממליצים להחליפו.
        rec_cap = eleven["captain"]
        owner_cap = my_team.get("captain")
        owned = _owned_pool(my_team, db)
        cap_last = (owned.get(_norm_name(owner_cap)) or {}).get("last_round_points") if owner_cap else None
        keep_cap = (cap_last is not None
                    and cap_last >= getattr(config, "CAPTAIN_KEEP_THRESHOLD", 9.0))
        captain_change = bool(rec_cap and owner_cap
                              and rec_cap["player_name"] != owner_cap and not keep_cap)

        return {
            "available": True,
            "matchday": matchday,
            "free_transfers": free_t,
            "bank": bank,
            "squad_total_points": squad_total_points(my_team, db),
            "starting_eleven": eleven["lineup"],
            "bench": eleven["bench"],
            "formation": eleven["formation"],
            "recommended_captain": rec_cap,
            "recommended_vice": eleven["vice_captain"],
            "owner_captain": owner_cap,
            "captain_change": captain_change,
            "captain_keep": keep_cap,
            "captain_last_points": cap_last,
            "daily_subs": daily_substitutions(eleven["lineup"], eleven["bench"], db),
            "total_expected_points": eleven["total_expected_points"],
            "transfers": transfers[:4],
            "flags": flags,
            "position_picks": _position_picks(my_scored, scored),
            "transfer_options": transfer_options(my_scored, scored, bank),
            "transfer_recs": transfer_recommendations(
                my_team, db, predictions, forced_out=my_team.get("forced_out")),
            "forced_out": my_team.get("forced_out"),
            "differentials": differentials_for_user(
                db.get("differentials") if isinstance(db, dict) else None,
                my_scored, scored),
            "top_picks": db.get("top_picks") if isinstance(db, dict) else None,
        }
    except Exception as exc:  # noqa: BLE001
        log.error("יועץ הפנטזי האישי נכשל: %s", exc)
        return {"available": False}
