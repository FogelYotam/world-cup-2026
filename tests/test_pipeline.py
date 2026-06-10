"""בדיקות בסיס: תקינות JSON, שדות סכמה, ערכים חסרים, ומבנה פלטים."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import advisor     # noqa: E402
import config       # noqa: E402
import fantasy      # noqa: E402
import odds         # noqa: E402
import planner     # noqa: E402
import predictor    # noqa: E402
import scraper      # noqa: E402
import state        # noqa: E402
import utils        # noqa: E402

from datetime import date, datetime, timedelta  # noqa: E402


# --------------------------------------------------------------------------- #
# JSON ו-DB
# --------------------------------------------------------------------------- #
def test_db_json_is_valid():
    data = json.loads(Path(config.DB_PATH).read_text(encoding="utf-8"))
    assert {"meta", "matches", "teams", "players"} <= set(data)


def test_load_json_missing_returns_default():
    assert utils.load_json(Path("does_not_exist.json"), default={"x": 1}) == {"x": 1}


def test_save_and_reload_roundtrip(tmp_path):
    p = tmp_path / "t.json"
    utils.save_json(p, {"שלום": "עולם", "n": 3})
    assert utils.load_json(p) == {"שלום": "עולם", "n": 3}


# --------------------------------------------------------------------------- #
# בנאי סכמה — כל השדות קיימים גם מקלט ריק
# --------------------------------------------------------------------------- #
def test_build_match_has_all_fields():
    keys = set(scraper.build_match({}))
    assert keys == {
        "match_id", "competition", "season", "date", "home_team",
        "away_team", "status", "venue", "stage", "score", "odds",
    }


def test_build_player_fills_defaults():
    p = scraper.build_player({"player_name": "X"})
    assert p["minutes"] == 0 and p["injury_status"] == "fit"


def test_build_team_uses_fallback_goals():
    t = scraper.build_team({"team_name": "Y"})
    assert t["goals_for"] == config.DEFAULT_GOALS_FOR


# --------------------------------------------------------------------------- #
# חיזוי
# --------------------------------------------------------------------------- #
@pytest.fixture
def sample_db():
    return {
        "teams": [
            {"team_name": "A", "goals_for": 2.0, "goals_against": 0.8},
            {"team_name": "B", "goals_for": 1.0, "goals_against": 1.5},
        ],
        "matches": [
            {"match_id": 1, "home_team": "A", "away_team": "B",
             "date": "2026-06-15", "stage": "Group",
             "context": {"home_advantage": 0.25, "injury_count": 0,
                         "lineup_confidence": "high"}},
        ],
        "players": [
            {"player_name": "Striker", "team": "A", "position": "Forward",
             "goals": 5, "assists": 2, "minutes": 900, "expected_start": True},
            {"player_name": "Keeper", "team": "A", "position": "Goalkeeper",
             "minutes": 900, "expected_start": True},
            {"player_name": "Hurt", "team": "B", "position": "Midfielder",
             "minutes": 100, "injury_status": "injured"},
        ],
    }


def test_poisson_pmf_sums_to_one():
    total = sum(predictor.poisson_pmf(k, 1.5) for k in range(20))
    assert abs(total - 1.0) < 1e-6


def test_prediction_structure(sample_db):
    preds = predictor.predict_all(sample_db)
    assert len(preds) == 1
    p = preds[0]
    for field in ("recommended_score", "alternatives", "outcome_probabilities",
                  "confidence", "explanation"):
        assert field in p
    assert len(p["alternatives"]) == 3
    o = p["outcome_probabilities"]
    assert abs(o["home_win"] + o["draw"] + o["away_win"] - 1.0) < 1e-3
    assert 0 <= p["confidence"] <= 100


def test_stronger_team_favored(sample_db):
    p = predictor.predict_all(sample_db)[0]
    assert p["outcome_probabilities"]["home_win"] > p["outcome_probabilities"]["away_win"]


# --------------------------------------------------------------------------- #
# פנטזי
# --------------------------------------------------------------------------- #
def test_fantasy_output_structure(sample_db):
    preds = predictor.predict_all(sample_db)
    fan = fantasy.build_fantasy(sample_db, preds)
    assert fan["available"] is True
    e = fan["starting_eleven"]
    assert e["captain"] is not None
    assert "avoid" in fan and "transfers" in fan


def test_injured_player_flagged_to_avoid(sample_db):
    preds = predictor.predict_all(sample_db)
    fan = fantasy.build_fantasy(sample_db, preds)
    avoid_names = {p["player_name"] for p in fan["avoid"]}
    assert "Hurt" in avoid_names


def test_max_three_per_nation_enforced():
    scored = [
        {"player_name": f"S{i}", "team": "Spain", "position": "MID", "price": 5.0,
         "expected_points": 100 - i, "minutes_risk": "low",
         "injury_status": "fit", "suspension_status": "available"}
        for i in range(6)
    ] + [
        {"player_name": f"X{i}", "team": f"T{i}", "position": pos, "price": 5.0,
         "expected_points": 50 - i, "minutes_risk": "low",
         "injury_status": "fit", "suspension_status": "available"}
        for i, pos in enumerate(["GK", "DEF", "DEF", "DEF", "MID", "MID",
                                 "FWD", "FWD", "DEF", "MID"])
    ]
    eleven = fantasy.pick_starting_eleven(scored)
    from collections import Counter
    by_nation = Counter(p["team"] for p in eleven["lineup"])
    assert all(c <= fantasy.MAX_PER_NATION for c in by_nation.values())
    assert by_nation["Spain"] <= 3


def _big_pool():
    """בריכת שחקנים גדולה ומגוונת ל-20 נבחרות לבדיקת בניית סגל מלא."""
    pool = []
    layout = [("GK", 2), ("DEF", 5), ("MID", 5), ("FWD", 3)]
    for n in range(20):
        for pos, cnt in layout:
            for j in range(cnt):
                pool.append({
                    "player_name": f"{pos}{n}_{j}", "team": f"Nation{n}",
                    "position": pos, "price": 5.0,
                    "expected_points": 50 - n - j * 0.1, "minutes_risk": "low",
                    "injury_status": "fit", "suspension_status": "available",
                })
    pool.sort(key=lambda x: x["expected_points"], reverse=True)
    return pool


def test_squad_is_fifteen_with_legal_composition():
    sq = fantasy.pick_squad(_big_pool())["squad"]
    from collections import Counter
    assert len(sq) == fantasy.SQUAD_SIZE
    pos = Counter(p["position"] for p in sq)
    assert dict(pos) == fantasy.SQUAD_COMPOSITION
    nation = Counter(p["team"] for p in sq)
    assert all(c <= fantasy.MAX_PER_NATION for c in nation.values())


def test_eleven_and_bench_split_from_squad():
    sq = fantasy.pick_squad(_big_pool())["squad"]
    res = fantasy.select_starting_eleven(sq)
    assert len(res["lineup"]) == fantasy.STARTING_SIZE
    assert len(res["bench"]) == fantasy.SQUAD_SIZE - fantasy.STARTING_SIZE
    from collections import Counter
    c = Counter(p["position"] for p in res["lineup"])
    assert c["GK"] == 1
    assert 3 <= c["DEF"] <= 5 and 2 <= c["MID"] <= 5 and 1 <= c["FWD"] <= 3
    # אין כפילות בין הרכב לספסל
    ids = {id(p) for p in res["lineup"]} | {id(p) for p in res["bench"]}
    assert len(ids) == fantasy.SQUAD_SIZE


def test_empty_players_does_not_crash():
    fan = fantasy.build_fantasy({"players": []}, [])
    assert fan["available"] is False


def test_position_normalization():
    assert fantasy.normalize_position("Goalkeeper") == "GK"
    assert fantasy.normalize_position("striker") == "FWD"
    assert fantasy.normalize_position(None) == "MID"


# --------------------------------------------------------------------------- #
# אודדס — מתמטיקה ושקלול
# --------------------------------------------------------------------------- #
def test_implied_from_decimal():
    assert abs(odds.implied_from_decimal(2.0) - 0.5) < 1e-9
    assert odds.implied_from_decimal(1.0) is None
    assert odds.implied_from_decimal("x") is None


def test_remove_vig_sums_to_one():
    p = odds.remove_vig(0.5, 0.3, 0.4)  # סכום גולמי 1.2 (מרווח)
    assert abs(p["home_win"] + p["draw"] + p["away_win"] - 1.0) < 1e-6


def test_consensus_averages_sources():
    triplets = [
        {"home_win": 0.6, "draw": 0.25, "away_win": 0.15},
        {"home_win": 0.5, "draw": 0.30, "away_win": 0.20},
    ]
    cons = odds.consensus_probabilities(triplets)
    assert cons["sources"] == 2
    assert abs(cons["home_win"] + cons["draw"] + cons["away_win"] - 1.0) < 1e-6
    assert 0.5 < cons["home_win"] < 0.6


def test_market_probs_from_decimal_sources():
    raw = {"sources": [
        {"bookmaker": "X", "home": 1.5, "draw": 4.0, "away": 7.0},
        {"bookmaker": "Y", "home": 1.6, "draw": 3.8, "away": 6.5},
    ]}
    p = odds._market_probs_from_raw(raw)
    assert p and p["sources"] == 2
    assert p["home_win"] > p["away_win"]


# --------------------------------------------------------------------------- #
# שקלול שוק במנוע החיזוי
# --------------------------------------------------------------------------- #
def test_blend_pulls_toward_market():
    model = {"home_win": 0.8, "draw": 0.15, "away_win": 0.05}
    market = {"home_win": 0.4, "draw": 0.3, "away_win": 0.3}
    blended = predictor.blend_probabilities(model, market, 0.5)
    assert 0.55 < blended["home_win"] < 0.65
    assert abs(sum(blended.values()) - 1.0) < 1e-3


def test_blend_without_market_returns_model():
    model = {"home_win": 0.5, "draw": 0.3, "away_win": 0.2}
    assert predictor.blend_probabilities(model, None, 0.5) == model


def test_market_disagreement_lowers_confidence():
    base = {"match_id": 1, "home_team": "A", "away_team": "B",
            "context": {"home_advantage": 0.25}}
    teams = {"A": {"goals_for": 2.0, "goals_against": 0.8},
             "B": {"goals_for": 1.0, "goals_against": 1.5}}
    agree = predictor.predict_match(dict(base, market_probabilities={
        "home_win": 0.7, "draw": 0.2, "away_win": 0.1}), teams)
    disagree = predictor.predict_match(dict(base, market_probabilities={
        "home_win": 0.1, "draw": 0.2, "away_win": 0.7}), teams)
    assert disagree["confidence"] < agree["confidence"]
    assert disagree["market_agrees"] is False


# --------------------------------------------------------------------------- #
# זיהוי שינויים וקצב
# --------------------------------------------------------------------------- #
def test_no_triggers_when_identical():
    snap = {"fixtures": ["1"], "injured": ["X|A"],
            "odds": {"1": {"favorite": "home_win", "prob": 0.7}}}
    assert state.detect_triggers(snap, snap) == []


def test_trigger_on_new_injury():
    prev = {"fixtures": ["1"], "injured": [], "odds": {}}
    curr = {"fixtures": ["1"], "injured": ["Messi|Argentina"], "odds": {}}
    trig = state.detect_triggers(prev, curr)
    assert any("Messi" in t for t in trig)


def test_trigger_on_odds_swing():
    prev = {"fixtures": ["1"], "injured": [],
            "odds": {"1": {"favorite": "home_win", "prob": 0.50}}}
    curr = {"fixtures": ["1"], "injured": [],
            "odds": {"1": {"favorite": "home_win", "prob": 0.70}}}
    assert state.detect_triggers(prev, curr)


def test_pre_tournament_sends_daily():
    snap = {"fixtures": [], "injured": [], "odds": {}}
    send, reasons = state.decide(snap, snap, today=date(2026, 6, 4))
    assert send is True


def test_first_run_always_sends():
    snap = {"fixtures": [], "injured": [], "odds": {}}
    send, _ = state.decide(None, snap, today=date(2026, 7, 1))
    assert send is True


def test_no_send_when_quiet_during_tournament():
    snap = {"fixtures": ["1"], "injured": [],
            "odds": {"1": {"favorite": "home_win", "prob": 0.7}}}
    send, reasons = state.decide(snap, snap, today=date(2026, 7, 1))
    assert send is False and reasons == []


# --------------------------------------------------------------------------- #
# חשיפת המלצת הימור (gating לפי שעת פתיחה)
# --------------------------------------------------------------------------- #
def test_odds_hidden_before_reveal_window():
    now = datetime(2026, 6, 15, 10, 0)
    kickoff = (now + timedelta(hours=5)).isoformat()
    revealed, label = utils.odds_revealed({"kickoff": kickoff}, 2, now)
    assert revealed is False and label  # יש תווית שעה, אך עוד לא נחשף


def test_odds_revealed_inside_window():
    now = datetime(2026, 6, 15, 10, 0)
    kickoff = (now + timedelta(hours=1)).isoformat()
    revealed, _ = utils.odds_revealed({"kickoff": kickoff}, 2, now)
    assert revealed is True


def test_odds_date_only_reveals_on_match_day():
    now = datetime(2026, 6, 15, 8, 0)
    before, _ = utils.odds_revealed({"date": "2026-06-16"}, 2, now)
    same, _ = utils.odds_revealed({"date": "2026-06-15"}, 2, now)
    assert before is False and same is True


def test_odds_no_date_stays_hidden():
    revealed, label = utils.odds_revealed({}, 2, datetime(2026, 6, 15))
    assert revealed is False and label == ""


# --------------------------------------------------------------------------- #
# מתכנן רב-מחזורי
# --------------------------------------------------------------------------- #
def _group_db():
    return {
        "teams": [
            {"team_name": t, "goals_for": gf, "goals_against": ga}
            for t, gf, ga in [
                ("A", 2.1, 0.8), ("B", 1.4, 1.2), ("C", 1.1, 1.4), ("D", 0.9, 1.7),
            ]
        ],
        "matches": [
            {"match_id": "A1", "home_team": "A", "away_team": "B",
             "date": "2026-06-11", "stage": "Group"},
            {"match_id": "A2", "home_team": "C", "away_team": "D",
             "date": "2026-06-11", "stage": "Group"},
        ],
        "players": _big_pool(),
    }


def test_derive_three_legal_matchdays():
    rounds = planner.derive_matchdays(_group_db(), num=3)
    assert [r["matchday"] for r in rounds] == [1, 2, 3]
    # כל מחזור: כל ארבע הנבחרות משחקות פעם אחת (2 משחקים)
    for r in rounds:
        teams = [t for m in r["matches"] for t in (m["home_team"], m["away_team"])]
        assert sorted(teams) == ["A", "B", "C", "D"]


def test_group_key_parsing():
    assert planner.group_key("A1") == "A"
    assert planner.group_key("H12") == "H"
    assert planner.group_key(None) is None


def test_build_plan_fixed_squad_rotating_eleven():
    plan = planner.build_plan(_group_db(), num_matchdays=3)
    assert plan["available"] is True
    assert len(plan["squad"]) == fantasy.SQUAD_SIZE
    assert len(plan["matchdays"]) == 3
    from collections import Counter
    for md in plan["matchdays"]:
        assert len(md["lineup"]) == fantasy.STARTING_SIZE
        c = Counter(p["position"] for p in md["lineup"])
        assert c["GK"] == 1
        assert 3 <= c["DEF"] <= 5 and 2 <= c["MID"] <= 5 and 1 <= c["FWD"] <= 3
        nation = Counter(p["team"] for p in md["lineup"])
        assert all(v <= fantasy.MAX_PER_NATION for v in nation.values())


def test_build_plan_no_players_unavailable():
    assert planner.build_plan({"matches": [], "players": []})["available"] is False


# --------------------------------------------------------------------------- #
# יועץ אישי (my_team)
# --------------------------------------------------------------------------- #
def _scored_pool():
    pool = []
    layout = [("GK", 2), ("DEF", 5), ("MID", 5), ("FWD", 3)]
    for n in range(6):
        for pos, cnt in layout:
            for j in range(cnt):
                pool.append({
                    "player_name": f"{pos}{n}_{j}", "team": f"Nation{n}",
                    "position": pos, "price": 5.0,
                    "expected_points": 30 - n * 2 - j, "minutes_risk": "low",
                    "injury_status": "fit", "suspension_status": "available",
                })
    pool.sort(key=lambda x: x["expected_points"], reverse=True)
    return pool


def _my_team_from(pool):
    # סגל חוקי: 2/5/5/3 ומקס' 3 לנבחרת — נבחר ידנית מהבריכה
    from collections import Counter
    want = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
    squad, counts, nation = [], Counter(), Counter()
    for p in pool:
        pos = p["position"]
        if counts[pos] >= want[pos] or nation[p["team"]] >= 3:
            continue
        squad.append({"player_name": p["player_name"], "team": p["team"],
                      "position": pos})
        counts[pos] += 1
        nation[p["team"]] += 1
    return {"squad": squad, "free_transfers": 1, "bank": 5.0,
            "captain": squad[0]["player_name"]}


def test_advice_unavailable_without_team(monkeypatch, tmp_path):
    # אין קובץ קבוצה אישית — אין המלצות אישיות
    monkeypatch.setattr(config, "MY_TEAM_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(advisor.config, "MY_TEAM_PATH", tmp_path / "missing.json")
    assert advisor.build_advice({}, _scored_pool(), my_team=None)["available"] is False


def test_advice_builds_legal_eleven():
    pool = _scored_pool()
    advice = advisor.build_advice({}, pool, my_team=_my_team_from(pool), matchday=1)
    assert advice["available"] is True
    assert len(advice["starting_eleven"]) == fantasy.STARTING_SIZE
    assert advice["recommended_captain"] is not None


def test_advice_flags_injured_squad_player():
    pool = _scored_pool()
    my = _my_team_from(pool)
    hurt = my["squad"][0]
    for s in pool:
        if s["player_name"] == hurt["player_name"] and s["team"] == hurt["team"]:
            s["injury_status"] = "injured"
    advice = advisor.build_advice({}, pool, my_team=my, matchday=1)
    flagged = {f["player_name"] for f in advice["flags"]}
    assert hurt["player_name"] in flagged


def test_suggest_transfers_respects_budget_and_position():
    pool = _scored_pool()
    my = _my_team_from(pool)
    my_scored = advisor._my_squad_scored(my, pool)
    sugg = advisor.suggest_transfers(my_scored, pool, free_transfers=1, bank=0.0)
    for s in sugg:
        assert s["in"]["position"] == s["out"]["position"]
        assert s["in"]["price"] - s["out"]["price"] <= 0.0 + 1e-9


# --------------------------------------------------------------------------- #
# התאמת שם סובלנית (שם משפחה / ניקוד) + המלצות לפי עמדה + חלון יומיים
# --------------------------------------------------------------------------- #
def test_resolver_matches_surname_and_accents():
    scored = [
        {"player_name": "Florian Wirtz", "team": "Germany", "position": "MID",
         "price": 9.0, "expected_points": 10.0, "minutes_risk": "low",
         "injury_status": "fit", "suspension_status": "available"},
        {"player_name": "Mikel Oyarzabal", "team": "Spain", "position": "FWD",
         "price": 8.0, "expected_points": 7.0, "minutes_risk": "low",
         "injury_status": "fit", "suspension_status": "available"},
    ]
    resolve = advisor._make_resolver(scored)
    # שם משפחה בלבד מול שם מלא
    assert resolve("Wirtz", "Germany")["player_name"] == "Florian Wirtz"
    # ניקוד שונה (Muñoz-style) — כאן בודקים נבחרת מנורמלת + שם משפחה
    assert resolve("Oyarzabal", "Spain")["player_name"] == "Mikel Oyarzabal"
    assert resolve("Nobody", "Germany") is None


def test_position_picks_marks_in_squad():
    pool = _scored_pool()
    my = _my_team_from(pool)
    # נשמור את הסגל עם שמות זהים לבריכה כדי לוודא סימון 'בסגל'
    advice = advisor.build_advice({}, pool, my_team=my, matchday=1)
    picks = advice["position_picks"]
    assert set(picks.keys()) == {"GK", "DEF", "MID", "FWD"}
    for pos in ("GK", "DEF", "MID", "FWD"):
        assert 1 <= len(picks[pos]) <= config.POSITION_PICKS_PER_POS
    # לפחות שחקן אחד מהסגל אמור להופיע כ-in_squad באחת העמדות
    any_in_squad = any(pk["in_squad"]
                       for pos in picks for pk in picks[pos])
    assert any_in_squad


def test_report_within_days_filters_window():
    import report
    now = datetime(2026, 6, 11, 9, 0)
    preds = [
        {"home_team": "A", "away_team": "B", "date": "2026-06-11"},   # today
        {"home_team": "C", "away_team": "D", "date": "2026-06-12"},   # tomorrow
        {"home_team": "E", "away_team": "F", "date": "2026-06-20"},   # out of window
    ]
    win = report._within_days(preds, 2, now=now)
    teams = {p["home_team"] for p in win}
    assert teams == {"A", "C"}


def test_report_within_days_fallback_when_undated():
    import report
    preds = [{"home_team": "A", "away_team": "B"},
             {"home_team": "C", "away_team": "D"}]
    # אין תאריכים כלל → מחזיר הכל (לא חוסם בגלל נתונים חסרים)
    assert len(report._within_days(preds, 2)) == 2


# --------------------------------------------------------------------------- #
# מעקב ניחושי המשתמש מול תוצאות אמת
# --------------------------------------------------------------------------- #
def test_predictions_log_record_settle_summary(monkeypatch, tmp_path):
    import predictions_log
    monkeypatch.setattr(predictions_log, "_PATH", tmp_path / "my_predictions.json")
    # שני ניחושים: אחד מנצח נכון לא-מדויק, אחד מדויק
    predictions_log.record_predictions([
        {"home": "Spain", "away": "Brazil", "date": "2026-06-12",
         "user_home": 2, "user_away": 1, "model_home": 1, "model_away": 1},
        {"home": "France", "away": "Japan", "date": "2026-06-12",
         "user_home": 3, "user_away": 0, "model_home": 2, "model_away": 0},
    ])
    # תוצאות אמת: Spain 3-0 (משתמש ניחש מנצח נכון, לא מדויק; מודל תיקו - שגוי)
    #            France 3-0 (משתמש מדויק; מודל מנצח נכון לא מדויק)
    n = predictions_log.settle_with_results([
        {"home": "Spain", "away": "Brazil", "home_goals": 3, "away_goals": 0},
        {"home": "France", "away": "Japan", "home_goals": 3, "away_goals": 0},
    ])
    assert n == 2
    s = predictions_log.summary()
    assert s["settled"] == 2
    assert s["user_outcome"] == (2, 2)   # שני ניצחונות בית — שניהם נכונים
    assert s["user_exact"] == (1, 2)     # רק France 3-0 מדויק
    assert s["model_outcome"] == (1, 2)  # Spain תיקו שגוי, France ניצחון נכון
    assert s["model_exact"] == (0, 2)


def test_predictions_log_handles_flipped_orientation(monkeypatch, tmp_path):
    import predictions_log
    monkeypatch.setattr(predictions_log, "_PATH", tmp_path / "p.json")
    predictions_log.record_predictions([
        {"home": "Italy", "away": "Germany", "user_home": 0, "user_away": 2,
         "model_home": 1, "model_away": 1},
    ])
    # התוצאה מגיעה בכיוון הפוך (Germany בבית) — חייב להתיישר נכון
    predictions_log.settle_with_results([
        {"home": "Germany", "away": "Italy", "home_goals": 2, "away_goals": 0},
    ])
    s = predictions_log.summary()
    assert s["settled"] == 1
    assert s["user_exact"] == (1, 1)     # 0-2 לטובת Germany = מדויק


# --------------------------------------------------------------------------- #
# מחולל התיעוד האוטומטי
# --------------------------------------------------------------------------- #
def test_docgen_block_has_inventory():
    import docgen
    block = docgen.build_block()
    assert docgen.BEGIN in block and docgen.END in block
    assert "**Modules (" in block
    assert "**Tests:**" in block
    # מודול ידוע אמור להופיע ברשימה
    assert "`predictions_log.py`" in block
