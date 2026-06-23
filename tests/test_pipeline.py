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


class _FakePlayerGemini:
    """Gemini מזויף שמחזיר ביצועי שחקנים בפועל — לבדיקת למידת הפנטזי."""
    enabled = True

    def ask_json(self, prompt, default=None):
        return {"players": [
            {"name": "Harry Kane", "team": "England", "position": "FWD",
             "goals": 2, "assists": 0, "minutes": 90, "clean_sheet": False,
             "date": "6/13/2026"},
            {"name": "New Star", "team": "Brazil", "position": "MID",
             "goals": 1, "assists": 1, "minutes": 90, "clean_sheet": False,
             "date": "6/13/2026"},
        ]}


def test_ingest_player_results_learns_and_dedupes():
    db = {"players": [{"player_name": "Harry Kane", "team": "England",
                       "position": "FWD", "goals": 0, "minutes": 0}]}
    added = scraper.ingest_player_results(_FakePlayerGemini(), db)
    assert added == 2
    kane = next(p for p in db["players"] if p["player_name"] == "Harry Kane")
    assert kane["recent_points"] == 10  # 2 שערים×4 + הופעה 2
    # שחקן בולט שחסר בבריכה — נוצר אוטומטית
    star = next(p for p in db["players"] if p["player_name"] == "New Star")
    assert star["recent_points"] == 10  # שער×5 + בישול×3 + הופעה 2
    # ריצה חוזרת לא סופרת שוב (dedup)
    assert scraper.ingest_player_results(_FakePlayerGemini(), db) == 0


def test_recent_points_boosts_expected_points():
    base = {"position": "MID", "team": "Brazil", "minutes": 90,
            "goals": 0, "assists": 0, "expected_start": True}
    hot = dict(base, recent_points=12)
    cold = dict(base, recent_points=0)
    ep_base = fantasy.expected_points(base, {}, {})
    assert fantasy.expected_points(hot, {}, {}) > ep_base
    assert fantasy.expected_points(cold, {}, {}) < ep_base


class _FakeOfficialGemini:
    enabled = True

    def ask_json(self, prompt, default=None):
        return {"players": [
            # ניקוד FIFA רשמי קיים — צריך לגבור על החישוב העצמאי (1*4+2=6)
            {"name": "Star A", "team": "Spain", "position": "FWD", "goals": 1,
             "assists": 0, "minutes": 90, "clean_sheet": False,
             "fantasy_points": 13, "date": "d1"},
            # אין ניקוד רשמי — נופלים לחישוב עצמאי
            {"name": "Star B", "team": "Brazil", "position": "FWD", "goals": 1,
             "assists": 0, "minutes": 90, "clean_sheet": False, "date": "d1"},
        ]}


def test_official_fifa_points_preferred_over_computed():
    db = {"players": []}
    scraper.ingest_player_results(_FakeOfficialGemini(), db)
    by_name = {r["name"]: r for r in db["player_results"]}
    assert by_name["Star A"]["official"] is True
    assert by_name["Star A"]["points"] == 13       # רשמי, לא 6 המחושב
    assert by_name["Star B"]["official"] is False
    assert by_name["Star B"]["points"] == 6         # fallback מחושב


def test_xg_boosts_expected_points():
    base = {"position": "FWD", "team": "X", "minutes": 90, "goals": 0,
            "assists": 0, "expected_start": True}
    assert (fantasy.expected_points(dict(base, xg=0.8), {}, {})
            > fantasy.expected_points(dict(base, xg=0.0), {}, {}))


def test_backtest_scores_and_baselines():
    import backtest
    scoring = config.PREDICTION_SCORING
    results = [
        {"home": "A", "away": "B", "home_goals": 1, "away_goals": 1, "date": "d1"},
        {"home": "C", "away": "D", "home_goals": 2, "away_goals": 0, "date": "d2"},
        {"home": "E", "away": "F", "home_goals": 0, "away_goals": 0, "date": "d3"},
    ]
    # בייסליין "תמיד 1-1" — מדויק על שני התיקו, כיוון נכון על שניהם, 0 על הניצחון
    draw = backtest.evaluate(results, lambda h, a: (1, 1), scoring)
    assert draw["n"] == 3
    assert draw["exact"] == 1                 # רק 1-1 מדויק (0-0 לא)
    assert draw["direction"] == 2             # שני התיקו בכיוון נכון
    # מנבא מושלם — כל המשחקים מדויקים
    perfect = backtest.evaluate(
        results, lambda h, a: {"A": (1, 1), "C": (2, 0), "E": (0, 0)}[h], scoring)
    assert perfect["exact"] == 3
    assert perfect["points"] == 3 * scoring["exact"]


def test_run_backtest_structure():
    import backtest
    db = {
        "teams": [{"team_name": "A", "goals_for": 2.5, "goals_against": 0.5},
                  {"team_name": "B", "goals_for": 0.6, "goals_against": 2.4}],
        "results": [{"home": "A", "away": "B", "home_goals": 3,
                     "away_goals": 0, "date": "d1"}],
    }
    bt = backtest.run_backtest(db)
    assert bt["n_results"] == 1
    for key in ("model", "baseline_home_1_0", "baseline_draw_1_1"):
        assert key in bt and bt[key]["n"] == 1
    assert "המודל" in backtest.format_report(bt)


def test_tune_finds_best_and_restores_config():
    import backtest
    db = {
        "teams": [{"team_name": "A", "goals_for": 2.5, "goals_against": 0.5},
                  {"team_name": "B", "goals_for": 0.6, "goals_against": 2.4}],
        "results": [{"home": "A", "away": "B", "home_goals": 3,
                     "away_goals": 0, "date": "d1"}],
    }
    before = config.HOME_ADVANTAGE
    t = backtest.tune(db, grid={"HOME_ADVANTAGE": [0.1, 0.25]})
    assert t["best"]["ppg"] >= max(r["ppg"] for r in t["all"]) - 1e-9
    assert config.HOME_ADVANTAGE == before     # tune לא משנה config


_FAKE_SQUADS = [{"id": 1, "name": "Argentina"}, {"id": 2, "name": "Sweden"}]
_FAKE_PLAYERS = [
    {"id": 10, "firstName": "Lionel", "lastName": "Messi", "knownName": None,
     "squadId": 1, "position": "FWD", "price": 10.0, "status": "playing",
     "matchStatus": "start", "percentSelected": 20.0,
     "stats": {"totalPoints": 30, "avgPoints": 15.0, "form": 8.0, "lastRoundPoints": 19}},
    {"id": 11, "firstName": "Yasin", "lastName": "Ayari", "knownName": None,
     "squadId": 2, "position": "MID", "price": 5.3, "status": "playing",
     "matchStatus": "start", "percentSelected": 1.0,
     "stats": {"totalPoints": 17, "avgPoints": 8.5, "form": 6.0, "lastRoundPoints": 17}},
    {"id": 12, "firstName": "Benched", "lastName": "Guy", "knownName": None,
     "squadId": 2, "position": "DEF", "price": 4.0, "status": "playing",
     "matchStatus": "not_in_squad", "percentSelected": 0.5, "stats": {"avgPoints": 0}},
]


def test_official_pool_parses(monkeypatch):
    def fake_get(url, timeout=20):
        return _FAKE_SQUADS if "squads" in url else _FAKE_PLAYERS
    monkeypatch.setattr(scraper, "_http_get_json", fake_get)
    pool = scraper.fetch_official_pool()
    by_name = {p["player_name"]: p for p in pool}
    assert by_name["Lionel Messi"]["team"] == "Argentina"
    assert by_name["Lionel Messi"]["price"] == 10.0
    assert by_name["Lionel Messi"]["ownership"] == 20.0
    assert by_name["Lionel Messi"]["recent_points"] == 15.0   # avgPoints הרשמי
    assert by_name["Lionel Messi"]["expected_start"] is True
    # not_in_squad → לא זמין לבחירה
    assert by_name["Benched Guy"]["injury_status"] == "out"
    assert by_name["Benched Guy"]["expected_start"] is False


def test_official_differentials_from_pool(monkeypatch):
    def fake_get(url, timeout=20):
        return _FAKE_SQUADS if "squads" in url else _FAKE_PLAYERS
    monkeypatch.setattr(scraper, "_http_get_json", fake_get)
    pool = scraper.fetch_official_pool()
    diffs = scraper.official_differentials(pool, counts={"MID": 3}, max_ownership=5.0)
    mids = diffs["MID"]
    assert any(d["player_name"] == "Yasin Ayari" for d in mids)  # 1% owned, nailed
    # מסי לא דיפרנציאל (20% בעלות) ולא MID
    assert all(d["player_name"] != "Lionel Messi" for d in mids)


_FAKE_ROUNDS = [
    {"id": 1, "stage": "GROUP", "status": "playing", "tournaments": [
        {"id": 101, "status": "complete", "homeSquadName": "Mexico",
         "awaySquadName": "South Africa", "homeScore": 2, "awayScore": 0,
         "homePenaltyScore": 0, "awayPenaltyScore": 0, "date": "2026-06-11T20:00:00+01:00",
         "homeGoalScorersAssists": [], "awayGoalScorersAssists": []},
        {"id": 102, "status": "scheduled", "homeSquadName": "Germany",
         "awaySquadName": "Brazil", "homeScore": None, "awayScore": None,
         "date": "2026-06-20T17:00:00+01:00", "venueCity": "Dallas"},
    ]},
    {"id": 5, "stage": "R16", "status": "scheduled", "tournaments": []},
]


def test_differentials_rank_by_scoring_chance_not_obscurity():
    pool = [
        # בעלות נמוכה מאוד אבל תוחלת נקודות נמוכה
        {"player_name": "Obscure", "team": "A", "position": "FWD",
         "expected_start": True, "ownership": 0.5, "recent_points": 2, "form": 2, "price": 5},
        # קצת יותר בעלות אבל ניקוד גבוה ומשחק קל — צריך לנצח
        {"player_name": "Star", "team": "B", "position": "FWD",
         "expected_start": True, "ownership": 12, "recent_points": 15, "form": 8, "price": 9},
        # לא בהרכב → מסונן
        {"player_name": "Bench", "team": "C", "position": "FWD",
         "expected_start": False, "ownership": 1, "recent_points": 20, "form": 9, "price": 8},
        # בעלות גבוהה מדי (מעל 15%) → מסונן
        {"player_name": "Popular", "team": "D", "position": "FWD",
         "expected_start": True, "ownership": 40, "recent_points": 18, "form": 9, "price": 10},
    ]
    diffs = scraper.official_differentials(
        pool, counts={"FWD": 3}, fixture_difficulty={"B": {"difficulty": 0.1}})
    fwds = [d["player_name"] for d in diffs["FWD"]]
    assert fwds[0] == "Star"          # סיכוי לנקד גבר על "אף אחד לא בחר"
    assert "Bench" not in fwds         # לא בהרכב הפותח
    assert "Popular" not in fwds       # מעל תקרת הבעלות


def test_fixture_difficulty_uses_squad_quality():
    # יריבה עם סגל יקר (חזק) צריכה לתת קושי גבוה, גם אם תוצאות עד כה צנועות
    rounds = [{"id": 1, "stage": "GROUP", "tournaments": [
        {"id": 1, "status": "scheduled", "homeSquadName": "Minnow",
         "awaySquadName": "Power", "date": "2026-06-20T18:00:00+01:00"}]}]
    db = {"teams": [{"team_name": "Power", "goals_for": 1.3, "goals_against": 1.3},
                    {"team_name": "Minnow", "goals_for": 1.3, "goals_against": 1.3}]}
    pool = ([{"team": "Power", "position": "MID", "price": 11.0}] * 15
            + [{"team": "Minnow", "position": "MID", "price": 4.5}] * 15)
    fd = scraper.official_fixture_difficulty(rounds, db, pool)
    assert fd["Minnow"]["difficulty"] > fd["Power"]["difficulty"]   # מול חזקה = קשה יותר
    assert fd["Minnow"]["difficulty"] >= 0.6


def test_differentials_populate_between_rounds_and_discount_hard_fixture():
    # מצב בין-מחזורים: אין הרכב מאומת (expected_start=None) — חייב עדיין להחזיר מועמדים,
    # ומשחק קשה צריך להוריד שחקן-על מתחת לשחקן עם משחק קל ותוחלת דומה.
    pool = [
        {"player_name": "HardStar", "team": "H", "position": "FWD",
         "expected_start": None, "injury_status": "fit", "suspension_status": "available",
         "ownership": 3, "recent_points": 16, "form": 8, "price": 9},
        {"player_name": "EasyGood", "team": "E", "position": "FWD",
         "expected_start": None, "injury_status": "fit", "suspension_status": "available",
         "ownership": 3, "recent_points": 13, "form": 7, "price": 8},
    ]
    fd = {"H": {"difficulty": 0.9}, "E": {"difficulty": 0.1}}   # H קשה מאוד, E קל
    diffs = scraper.official_differentials(pool, counts={"FWD": 2}, fixture_difficulty=fd)
    names = [d["player_name"] for d in diffs["FWD"]]
    assert len(names) == 2                       # אוכלס למרות expected_start=None
    assert names[0] == "EasyGood"                # משחק קל ניצח על שחקן-על במשחק קשה


def test_official_matches_and_results():
    matches = scraper.official_matches(_FAKE_ROUNDS)
    results = scraper.official_results(_FAKE_ROUNDS)
    assert [m["home_team"] for m in matches] == ["Germany"]   # רק שלא הסתיים
    assert matches[0]["stage"] == "GROUP" and matches[0]["date"] == "2026-06-20"
    assert len(results) == 1
    r = results[0]
    assert (r["home"], r["home_goals"], r["away_goals"]) == ("Mexico", 2, 0)
    assert r["stage"] == "GROUP" and r["home_pen"] == 0          # פנדלים נשמרים


def test_record_results_dedupes_and_learns():
    db = {"teams": [{"team_name": "Mexico", "goals_for": 1.3, "goals_against": 1.3},
                    {"team_name": "South Africa", "goals_for": 1.3, "goals_against": 1.3}],
          "results": []}
    rows = scraper.official_results(_FAKE_ROUNDS)
    assert scraper._record_results(db, rows) == 1
    assert scraper._record_results(db, rows) == 0       # dedup בריצה חוזרת
    mex = next(t for t in db["teams"] if t["team_name"] == "Mexico")
    assert mex["goals_for"] > 1.3                        # למד מ-2 השערים


def test_seed_teams_preserves_learned_strength():
    db = {"teams": [{"team_name": "Czech Republic", "goals_for": 2.7,
                     "goals_against": 0.6}]}
    squads = [{"id": 1, "name": "Czechia"}, {"id": 2, "name": "Brazil"}]
    teams = scraper.seed_teams_from_squads(db, squads)
    by = {t["team_name"]: t for t in teams}
    assert set(by) == {"Czechia", "Brazil"}             # קנוניזציה לשם הרשמי
    assert by["Czechia"]["goals_for"] == 2.7            # חוזק שנלמד נשמר


def test_filter_to_participants_drops_non_wc_nations():
    db = {
        "teams": [{"team_name": n} for n in (
            ["Czechia", "Netherlands", "Brazil", "USA"]
            + [f"Nat{i}" for i in range(40)])],  # >=24 כדי לעבור את רשת הביטחון
        "players": [
            {"player_name": "Di Lorenzo", "team": "Italy"},        # לא העפילה
            {"player_name": "Lunin", "team": "Ukraine"},           # לא העפילה
            {"player_name": "Soucek", "team": "Czech Republic"},   # וריאנט של Czechia — נשאר
            {"player_name": "Gakpo", "team": "Netherlands"},       # משתתפת — נשאר
        ],
        "differentials": {"GK": [{"player_name": "Lunin", "team": "Ukraine"}],
                          "MID": [{"player_name": "Gakpo", "team": "Netherlands"}]},
    }
    removed = scraper.filter_to_participants(db)
    names = {p["player_name"] for p in db["players"]}
    assert removed == 3      # 2 מהבריכה (Italy/Ukraine) + 1 מהדיפרנציאלים (Lunin)
    assert names == {"Soucek", "Gakpo"}          # וריאנט שם נשמר, נבחרות לא-משתתפות הוסרו
    assert db["differentials"]["GK"] == []         # Lunin הוסר גם מהדיפרנציאלים
    assert len(db["differentials"]["MID"]) == 1


def test_filter_to_participants_safety_net_when_no_teams():
    db = {"teams": [], "players": [{"player_name": "X", "team": "Italy"}]}
    assert scraper.filter_to_participants(db) == 0      # לא מסננים בלי רשימת נבחרות
    assert len(db["players"]) == 1


def test_squad_repaired_to_within_budget():
    # מאגר עם כוכבים יקרים + מספיק חלופות זולות בכל עמדה → חייב להיכנס ל-100M
    scored = []
    for i, pos in enumerate(["GK", "GK", "DEF", "DEF", "DEF", "DEF", "DEF",
                             "MID", "MID", "MID", "MID", "MID", "FWD", "FWD", "FWD"]):
        scored.append({"player_name": f"Star{i}", "team": f"N{i}", "position": pos,
                       "price": 13.0, "expected_points": 9.0 - i * 0.1,
                       "minutes_risk": "low", "injury_status": "fit",
                       "suspension_status": "available"})
    for i, pos in enumerate(["GK", "DEF", "DEF", "MID", "MID", "FWD"] * 3):
        scored.append({"player_name": f"Cheap{i}", "team": f"C{i}", "position": pos,
                       "price": 4.5, "expected_points": 1.0,
                       "minutes_risk": "low", "injury_status": "fit",
                       "suspension_status": "available"})
    res = fantasy.pick_squad(scored)
    assert len(res["squad"]) == fantasy.SQUAD_SIZE
    assert res["cost"] <= fantasy.DEFAULT_BUDGET
    # מכסת 3-לנבחרת נשמרה
    from collections import Counter
    nat = Counter(p["team"] for p in res["squad"])
    assert max(nat.values()) <= fantasy.MAX_PER_NATION


def test_conditional_host_home_advantage():
    host = config.HOME_ADVANTAGE + config.HOST_HOME_BONUS
    assert predictor._home_advantage_for("USA") == host
    assert predictor._home_advantage_for("Mexico") == host
    assert predictor._home_advantage_for("Brazil") == config.HOME_ADVANTAGE
    assert host > config.HOME_ADVANTAGE          # מארחת תמיד מעל הניטרלי
    # מארחת בבית מקבלת xG ביתי גבוה יותר מאותה נבחרת במגרש ניטרלי
    teams = {"USA": {"goals_for": 1.5, "goals_against": 1.2},
             "Brazil": {"goals_for": 1.5, "goals_against": 1.2},
             "Iran": {"goals_for": 1.0, "goals_against": 1.0}}
    host_xg = predictor.predict_match(
        {"home_team": "USA", "away_team": "Iran"}, teams)["expected_goals"]["home"]
    neutral_xg = predictor.predict_match(
        {"home_team": "Brazil", "away_team": "Iran"}, teams)["expected_goals"]["home"]
    assert host_xg > neutral_xg


def test_penalty_taker_raises_ceiling():
    p = {"position": "FWD", "team": "X", "minutes": 270, "goals": 2,
         "assists": 0, "xg": 2.0, "expected_start": True}
    base = fantasy.ceiling_points(p, {}, {})
    boosted = fantasy.ceiling_points(dict(p, penalty_taker=True), {}, {})
    assert boosted > base


def test_captain_chosen_by_ceiling_not_mean():
    # MID עם EP גבוה אך תקרה נמוכה מול FWD נפיץ — הקפטן צריך להיות ה-FWD
    squad = [
        {"player_name": "Keeper", "team": "G", "position": "GK", "price": 5.0,
         "expected_points": 3.0, "ceiling_points": 3.0, "minutes_risk": "low",
         "injury_status": "fit", "suspension_status": "available"},
        {"player_name": "SteadyMid", "team": "Y", "position": "MID", "price": 8.0,
         "expected_points": 6.0, "ceiling_points": 6.5, "minutes_risk": "low",
         "injury_status": "fit", "suspension_status": "available"},
        {"player_name": "ExplosiveFwd", "team": "X", "position": "FWD", "price": 9.0,
         "expected_points": 5.5, "ceiling_points": 9.0, "minutes_risk": "low",
         "injury_status": "fit", "suspension_status": "available"},
    ] + [
        {"player_name": f"F{i}", "team": f"T{i}", "position": pos, "price": 5.0,
         "expected_points": 2.0, "ceiling_points": 2.0, "minutes_risk": "low",
         "injury_status": "fit", "suspension_status": "available"}
        for i, pos in enumerate(["DEF", "DEF", "DEF", "MID", "FWD", "DEF", "MID", "FWD"])
    ]
    out = fantasy.select_starting_eleven(squad)
    assert out["captain"]["player_name"] == "ExplosiveFwd"


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


def test_differential_picks_low_ownership_only():
    pool = [
        {"player_name": "Star", "team": "A", "position": "MID",
         "expected_points": 9.0, "ownership": 40.0, "suspension_status": "available"},
        {"player_name": "Hidden1", "team": "B", "position": "MID",
         "expected_points": 7.0, "ownership": 3.0, "suspension_status": "available"},
        {"player_name": "Hidden2", "team": "C", "position": "FWD",
         "expected_points": 6.0, "ownership": 1.5, "suspension_status": "available"},
        {"player_name": "Hidden3", "team": "D", "position": "DEF",
         "expected_points": 5.0, "ownership": 4.9, "suspension_status": "available"},
        {"player_name": "NoOwn", "team": "E", "position": "FWD",
         "expected_points": 8.0, "ownership": None, "suspension_status": "available"},
    ]
    diffs = advisor.differential_picks([], pool, max_ownership=5.0)
    all_names = [d["player_name"] for pos in diffs for d in diffs[pos]]
    assert "Star" not in all_names       # בעלות גבוהה — לא דיפרנציאל
    assert "NoOwn" not in all_names       # אין נתון בעלות
    assert set(all_names) == {"Hidden1", "Hidden2", "Hidden3"}
    # כל אחד בעמדה שלו
    assert diffs["MID"][0]["player_name"] == "Hidden1"
    assert diffs["FWD"][0]["player_name"] == "Hidden2"
    assert diffs["DEF"][0]["player_name"] == "Hidden3"
    assert all(d["ownership"] < 5.0 for pos in diffs for d in diffs[pos])


def test_differential_picks_handles_none_expected_points():
    pool = [
        {"player_name": "X", "team": "A", "position": "MID",
         "expected_points": None, "ownership": 2.0, "suspension_status": "available"},
        {"player_name": "Y", "team": "B", "position": "FWD",
         "expected_points": None, "ownership": 1.0, "suspension_status": "available"},
    ]
    diffs = advisor.differential_picks([], pool, max_ownership=5.0)
    assert sum(len(v) for v in diffs.values()) == 2   # לא קורס; שניהם


def test_transfer_recommendations_budget_and_easy_in():
    my_team = {"bank": 0.5, "squad": [
        {"player_name": "OutMid", "team": "Scotland", "position": "MID", "price": 6.5},
        {"player_name": "OutDef", "team": "Colombia", "position": "DEF", "price": 4.6},
    ]}
    db = {
        "fixture_difficulty": {
            "Scotland": {"opponent": "Morocco", "difficulty": 0.7},
            "Colombia": {"opponent": "DRC", "difficulty": 0.3},
            "Brazil": {"opponent": "Haiti", "difficulty": 0.1},
            "Ecuador": {"opponent": "Curacao", "difficulty": 0.1},
        },
        "differentials": {
            "MID": [{"player_name": "InMid", "team": "Brazil", "ownership": 2.0, "price": 6.8}],
            "DEF": [{"player_name": "InDef", "team": "Ecuador", "ownership": 3.0, "price": 4.7}],
            "GK": [], "FWD": [],
        },
    }
    recs = advisor.transfer_recommendations(my_team, db, None)
    assert recs                                    # נוצרה לפחות אופציה אחת
    for o in recs:                                 # כל אופציה בתוך התקציב
        assert o["in_cost"] <= o["out_cost"] + my_team["bank"] + 1e-9


def test_differentials_for_user_prefers_db_and_excludes_squad():
    db_diffs = {
        "MID": [{"player_name": "GemA", "team": "X", "ownership": 2.0,
                 "expected_points": 6.0, "price": 7.0, "reason": "fixtures"},
                {"player_name": "Owned", "team": "Y", "ownership": 1.0,
                 "expected_points": 5.0}],
        "FWD": [], "DEF": [], "GK": [],
    }
    my_scored = [{"player_name": "Owned", "team": "Y", "position": "MID"}]
    out = advisor.differentials_for_user(db_diffs, my_scored, [])
    names = [d["player_name"] for pos in out for d in out[pos]]
    assert "GemA" in names          # מ-DB
    assert "Owned" not in names      # כבר בסגל → מסונן


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


def test_settle_backfills_missing_date_from_result(monkeypatch, tmp_path):
    """יישוב מול תוצאה רשמית ממלא תאריך חסר בניחוש (שיפור #3) —
    מונע backfill ידני של תאריכים בעתיד."""
    import predictions_log
    monkeypatch.setattr(predictions_log, "_PATH", tmp_path / "p.json")
    predictions_log.record_predictions([
        {"home": "Spain", "away": "Brazil",            # ללא date
         "user_home": 2, "user_away": 1, "model_home": 1, "model_away": 1}])
    n = predictions_log.settle_with_results([
        {"home": "Spain", "away": "Brazil", "home_goals": 3, "away_goals": 0,
         "date": "2026-06-20"}])
    assert n == 1
    rec = predictions_log._load()["predictions"][0]
    assert rec["date"] == "2026-06-20" and rec["settled"]


def test_clean_sheet_probabilities_match_matrix_margins():
    """שער נקי לבית = סכום עמודה 0 (האורחת לא כובשת); לאורחת = סכום שורה 0."""
    import predictor
    m = predictor.score_matrix(1.6, 0.9, 6)
    cs = predictor.clean_sheet_probabilities(m)
    total = sum(p for row in m for p in row)
    assert abs(cs["home"] - sum(row[0] for row in m) / total) < 1e-3   # מעוגל ל-4 ספרות
    assert abs(cs["away"] - sum(m[0]) / total) < 1e-3
    assert cs["home"] > cs["away"]          # הבית חזק יותר → סיכוי שער נקי גבוה יותר


def test_official_top_picks_are_premium_easy_fixture():
    """כוכבי המחזור: רק פרימיום (בעלות ≥ סף) עם משחק קל, ממוין לפי נקודות×קלות —
    נבדל מהדיפרנציאלים (נמוכי-בעלות)."""
    import scraper
    pool = [
        {"player_name": "Star", "team": "Easy", "position": "FWD", "ownership": 30.0,
         "price": 10.0, "recent_points": 9.0, "form": 6.0, "injury_status": "fit",
         "suspension_status": "available", "expected_start": None},
        {"player_name": "Diff", "team": "Easy", "position": "FWD", "ownership": 2.0,
         "price": 7.0, "recent_points": 12.0, "form": 6.0, "injury_status": "fit",
         "suspension_status": "available", "expected_start": None},
        {"player_name": "HardStar", "team": "Hard", "position": "FWD", "ownership": 30.0,
         "price": 10.0, "recent_points": 9.0, "form": 6.0, "injury_status": "fit",
         "suspension_status": "available", "expected_start": None},
    ]
    fd = {"Easy": {"difficulty": 0.1, "opponent": "Weak"},
          "Hard": {"difficulty": 0.9, "opponent": "Strong"}}
    tp = scraper.official_top_picks(pool, counts={"FWD": 5}, fixture_difficulty=fd)
    names = [d["player_name"] for d in tp["FWD"]]
    assert "Star" in names                 # פרימיום + משחק קל
    assert "Diff" not in names             # בעלות נמוכה — לא כוכב (זה דיפרנציאל)
    assert "HardStar" not in names         # משחק קשה — מסונן
    assert tp["FWD"][0]["opponent"] == "Weak"


def test_scouting_bonus_promotes_sub5_high_upside(monkeypatch):
    """מודעות scouting bonus (#4): שחקן מתחת ל-5% בעלות עם פוטנציאל ניקוד גבוה
    מתוגמל בדירוג ומסומן. בלי הבונוס הפופולרי (ניקוד בסיס גבוה) מוביל; עם הבונוס
    הזול עוקף."""
    import scraper, config
    pool = [
        {"player_name": "Popular", "team": "A", "position": "FWD", "ownership": 6.0,
         "price": 7.0, "recent_points": 8.0, "form": 5.0, "injury_status": "fit",
         "suspension_status": "available", "expected_start": None},
        {"player_name": "Cheap", "team": "A", "position": "FWD", "ownership": 4.0,
         "price": 7.0, "recent_points": 7.5, "form": 5.0, "injury_status": "fit",
         "suspension_status": "available", "expected_start": None},
    ]
    d = scraper.official_differentials(pool, counts={"FWD": 2})
    assert d["FWD"][0]["player_name"] == "Cheap"
    assert d["FWD"][0]["scouting_bonus"] is True and "scouting" in d["FWD"][0]["reason"]
    assert d["FWD"][1]["scouting_bonus"] is False
    monkeypatch.setattr(config, "SCOUTING_BONUS_POINTS", 0.0)
    d2 = scraper.official_differentials(pool, counts={"FWD": 2})
    assert d2["FWD"][0]["player_name"] == "Popular"   # בלי הבונוס — הפופולרי מוביל


def test_predicted_score_not_exaggerated():
    """ניחוש הדוח לא מגזים בשערים: MAX_XG≤3.5 (מעל זה _round_goals מחזיר 4),
    וגבול הכיוונון חוסם ניפוח חוזר. אפילו במפגש הכי לא-שוויוני — מקס 3 לקבוצה."""
    import predictor, config
    assert config.MAX_XG <= 3.5
    assert config._TUNING_BOUNDS["MAX_XG"][1] <= 3.5
    teams = {"S": {"team_name": "S", "goals_for": 3.6, "goals_against": 0.3},
             "W": {"team_name": "W", "goals_for": 0.3, "goals_against": 3.6}}
    pred = predictor.predict_match({"home_team": "S", "away_team": "W"}, teams)
    h, a = map(int, pred["predicted_score"].split("-"))
    assert max(h, a) <= 3
    assert pred["expected_goals"]["home"] <= 3.5    # התקרה אוכפת


def test_predict_match_exposes_goals_and_clean_sheet():
    """predict_match מחזיר total_expected_goals + clean_sheet לדוח (שיפור #2)."""
    import predictor
    teams = {"A": {"team_name": "A", "attack": 1.8, "defense": 1.0},
             "B": {"team_name": "B", "attack": 0.8, "defense": 1.4}}
    pred = predictor.predict_match({"home_team": "A", "away_team": "B"}, teams)
    assert "total_expected_goals" in pred and pred["total_expected_goals"] > 0
    cs = pred["clean_sheet"]
    assert 0.0 <= cs["home"] <= 1.0 and 0.0 <= cs["away"] <= 1.0


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


# --------------------------------------------------------------------------- #
# אופטימיזציה לפי שיטת הניקוד (תוחלת נקודות)
# --------------------------------------------------------------------------- #
def test_match_points_tiers():
    s = {"exact": 3, "direction": 1, "reversed": -1}
    assert predictor.match_points(2, 1, 2, 1, s) == 3    # מדויק
    assert predictor.match_points(2, 1, 1, 0, s) == 1    # כיוון נכון, לא מדויק
    assert predictor.match_points(2, 1, 0, 1, s) == -1   # תוצאה הפוכה
    assert predictor.match_points(1, 1, 2, 0, s) == 0    # ניחשת תיקו, נצחון בית
    assert predictor.match_points(2, 0, 1, 1, s) == 0    # ניחשת נצחון, תיקו


def test_ev_optimal_favors_favorite_winning():
    matrix = predictor.score_matrix(1.9, 0.6, config.MAX_GOALS_GRID)
    ev = predictor.ranked_by_expected_points(
        matrix, config.PREDICTION_SCORING, config.MAX_GOALS_GRID)
    top = ev[0]
    assert top["home"] > top["away"]   # ניצחון ביתי, לא תיקו
    # תוחלת הנקודות של הבחירה לא נמוכה מזו של הסביר-ביותר
    likely = predictor.ranked_scorelines(matrix)[0]
    ep_likely = predictor.expected_points(
        likely["home"], likely["away"], matrix, config.PREDICTION_SCORING)
    assert top["ep"] >= ep_likely - 1e-9


def test_predict_match_exposes_ev_fields():
    match = {"home_team": "A", "away_team": "B"}
    teams = {"A": {"goals_for": 2.2, "goals_against": 0.8},
             "B": {"goals_for": 0.9, "goals_against": 1.6}}
    pred = predictor.predict_match(match, teams)
    assert pred["recommended_score"]
    assert "recommended_ep" in pred
    assert "most_likely_score" in pred


def test_pending_image_saved_and_processed(monkeypatch, tmp_path):
    import telegram_intake as ti
    monkeypatch.setattr(ti, "_PENDING_DIR", tmp_path / "pending")
    ti._save_pending_image(b"fakebytes", "image/jpeg")
    assert len(list((tmp_path / "pending").glob("*.jpg"))) == 1
    sent = []
    monkeypatch.setattr(ti, "_send_message", lambda m: sent.append(m))
    monkeypatch.setattr(ti, "_handle_fixtures", lambda parsed: None)
    monkeypatch.setattr(ti, "classify_image",
                        lambda g, b, m: {"kind": "fixtures", "matches": []})

    class _G:
        enabled = True
        _quota_exhausted = False
    assert ti._process_pending_images(_G()) == 1
    assert list((tmp_path / "pending").glob("*.jpg")) == []   # נמחק אחרי עיבוד


def test_pending_image_kept_when_quota_still_out(monkeypatch, tmp_path):
    import telegram_intake as ti
    monkeypatch.setattr(ti, "_PENDING_DIR", tmp_path / "pending")
    ti._save_pending_image(b"x", "image/jpeg")
    monkeypatch.setattr(ti, "classify_image", lambda g, b, m: None)  # מכסה אזלה

    class _G:
        enabled = True
        _quota_exhausted = True
    assert ti._process_pending_images(_G()) == 0
    assert len(list((tmp_path / "pending").glob("*.jpg"))) == 1   # נשמר לפעם הבאה


def test_find_prediction_matches_either_orientation():
    import telegram_intake as ti
    preds = [{"home_team": "Egypt", "away_team": "Belgium", "recommended_score": "1-1"}]
    assert ti._find_prediction(preds, "Egypt", "Belgium") is not None
    assert ti._find_prediction(preds, "Belgium", "Egypt") is not None   # כיוון הפוך
    assert ti._find_prediction(preds, "Brazil", "Spain") is None        # לא קיים


def test_realistic_scoreline_varied_with_draws():
    import predictor as pr
    # פייבוריט ביתי ברור → מנצח, עם שערים מה-xG (לא 1-0)
    assert pr._realistic_scoreline(2.4, 0.6, {"home_win": 0.75, "draw": 0.16, "away_win": 0.09}) == "2-1"
    # תיקו סביר (≥ סף) → תוצאת תיקו, גם אם הבית הכי סביר
    assert pr._realistic_scoreline(1.4, 1.2, {"home_win": 0.45, "draw": 0.30, "away_win": 0.25}) == "1-1"
    # פייבוריט חוץ → חוץ מנצח
    assert pr._realistic_scoreline(0.7, 2.2, {"home_win": 0.12, "draw": 0.20, "away_win": 0.68}) == "1-2"
    # מבטיח שהפייבוריט מנצח גם כשעיגול ה-xG שווה
    out = pr._realistic_scoreline(1.6, 1.4, {"home_win": 0.55, "draw": 0.24, "away_win": 0.21})
    h, a = (int(x) for x in out.split("-"))
    assert h > a


def test_predicted_score_field_present():
    import config, utils, predictor
    db = utils.load_json(config.DB_PATH, default={}) or {}
    preds = predictor.predict_all(db)
    if preds:
        assert all("predicted_score" in p for p in preds)


def test_maybe_autotune_skips_with_few_results(monkeypatch, tmp_path):
    import backtest
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db = {"teams": [], "results": [{"home": "A", "away": "B", "home_goals": 1,
                                    "away_goals": 0, "date": "d"}]}
    backtest.maybe_autotune(db)
    assert not (tmp_path / "tuning.json").exists()   # <10 תוצאות → דולג


def test_config_tuning_respects_bounds(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    (tmp_path / "tuning.json").write_text(
        json.dumps({"MAX_XG": 99, "HOME_ADVANTAGE": 0.2}), encoding="utf-8")
    old_max, old_ha = config.MAX_XG, config.HOME_ADVANTAGE
    try:
        config._apply_saved_tuning()
        assert config.MAX_XG == old_max       # 99 מחוץ לתחום — לא הוחל
        assert config.HOME_ADVANTAGE == 0.2   # בתחום — הוחל
    finally:
        config.MAX_XG, config.HOME_ADVANTAGE = old_max, old_ha


def test_core_runs_without_optional_deps():
    """הליבה (kickoff_predictions) חייבת לייבא ולרוץ בלי python-dotenv/requests
    — כדי שעבודה מהנייד (claude.ai/code, סביבה טרייה) תעבוד בלי pip install."""
    import subprocess
    code = ("import sys; sys.modules['dotenv']=None; sys.modules['requests']=None; "
            "import kickoff_predictions as kp; "
            "kp.process([{'home':'Norway','away':'Iraq','user_home':1,'user_away':0,"
            "'model_home':1,'model_away':0}]); print('OK')")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=str(Path(__file__).resolve().parent.parent))
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr


def test_market_weight_tunable_via_archived_odds():
    """עם אודדס מארכבים בתוצאה, שינוי MARKET_BLEND_WEIGHT משנה את ניקוד ה-backtest
    — כלומר המשקל באמת מכוונן (לא inert)."""
    import backtest
    db = {
        "teams": [{"team_name": "A", "goals_for": 2.2, "goals_against": 0.7},
                  {"team_name": "B", "goals_for": 0.7, "goals_against": 2.2}],
        # A חזקה מאוד, אבל השוק נותן ל-B 85% (והיא ניצחה 0-2)
        "results": [{"home": "A", "away": "B", "home_goals": 0, "away_goals": 2,
                     "date": "d1", "market_probabilities":
                     {"home_win": 0.05, "draw": 0.10, "away_win": 0.85}}],
    }
    old = config.MARKET_BLEND_WEIGHT
    try:
        config.MARKET_BLEND_WEIGHT = 0.0
        ppg_model = backtest.run_backtest(db)["model"]["ppg"]
        config.MARKET_BLEND_WEIGHT = 0.9
        ppg_market = backtest.run_backtest(db)["model"]["ppg"]
    finally:
        config.MARKET_BLEND_WEIGHT = old
    assert ppg_market != ppg_model        # האודדס המארכבים אכן משפיעים


def test_consensus_odds_one_batched_call():
    """אודדס נמשכים בקריאת Gemini אחת לכל המשחקים (לא אחת-לכל-משחק) — קריטי
    למכסה החינמית הקטנה."""
    import odds
    calls = []

    class _G:
        enabled = True

        def ask_json(self, prompt, default=None):
            calls.append(prompt)
            return {"matches": [
                {"id": 1, "home_win": 0.60, "draw": 0.25, "away_win": 0.15},
                {"id": 2, "home_win": 0.20, "draw": 0.30, "away_win": 0.50}]}

    matches = [{"match_id": 1, "home_team": "A", "away_team": "B"},
               {"match_id": 2, "home_team": "C", "away_team": "D"}]
    o = odds.fetch_consensus_odds(_G(), matches)
    assert len(calls) == 1                       # קריאה אחת בלבד
    assert set(o) == {1, 2}
    assert abs(sum(o[1][k] for k in ("home_win", "draw", "away_win")) - 1.0) < 1e-3


class _OddsResp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def _odds_api_event(home, away, h, d, a):
    """אירוע the-odds-api מינימלי עם בוקמייקר יחיד ושוק h2h."""
    return {"home_team": home, "away_team": away,
            "bookmakers": [{"title": "Pinnacle", "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": home, "price": h},
                    {"name": "Draw", "price": d},
                    {"name": away, "price": a}]}]}]}


def test_odds_api_one_call_and_alias_mapping(monkeypatch):
    """the-odds-api נמשך ב*קריאה אחת* לכל הטורניר, וממופה למשחקים שלנו —
    כולל שמות-אליאס (South Korea -> Korea Republic)."""
    import odds
    calls = []
    payload = [
        _odds_api_event("Argentina", "Jordan", 1.3, 5.0, 9.0),
        _odds_api_event("South Korea", "Czechia", 2.2, 3.3, 3.3),
    ]

    def fake_get(url, params=None, **kw):
        calls.append((url, params))
        return _OddsResp(payload)

    monkeypatch.setattr(odds.config, "ODDS_API_KEY", "k")
    monkeypatch.setattr(odds.utils, "safe_get", fake_get)
    matches = [{"match_id": 10, "home_team": "Argentina", "away_team": "Jordan"},
               {"match_id": 11, "home_team": "Korea Republic", "away_team": "Czechia"}]
    out = odds.fetch_odds_api(matches)
    assert len(calls) == 1                          # קריאה אחת לכל הטורניר
    assert set(out) == {10, 11}                     # כולל התאמה דרך אליאס
    assert out[10]["home_win"] > out[10]["away_win"]  # ארגנטינה פייבוריט
    assert abs(sum(out[10][k] for k in ("home_win", "draw", "away_win")) - 1.0) < 1e-3


def test_fetch_market_odds_api_first_then_gemini_fills_gaps(monkeypatch):
    """מקור משולב: ה-API מכסה משחק אחד, ו-Gemini ממלא את החסר."""
    import odds
    monkeypatch.setattr(odds.config, "ODDS_API_KEY", "k")
    monkeypatch.setattr(odds.utils, "safe_get",
                        lambda url, params=None, **kw: _OddsResp(
                            [_odds_api_event("Argentina", "Jordan", 1.3, 5.0, 9.0)]))

    class _G:
        enabled = True

        def ask_json(self, prompt, default=None):
            return {"matches": [{"id": 11, "home_win": 0.5, "draw": 0.3, "away_win": 0.2}]}

    matches = [{"match_id": 10, "home_team": "Argentina", "away_team": "Jordan"},
               {"match_id": 11, "home_team": "Spain", "away_team": "Uruguay"}]
    out = odds.fetch_market_odds(matches, _G())
    assert set(out) == {10, 11}                     # 10 מה-API, 11 מ-Gemini


def test_fetch_odds_api_empty_without_key(monkeypatch):
    """בלי מפתח — לא נורית קריאת רשת, מוחזר ריק (נפילה ל-Gemini/מודל)."""
    import odds
    monkeypatch.setattr(odds.config, "ODDS_API_KEY", None)
    called = []
    monkeypatch.setattr(odds.utils, "safe_get",
                        lambda *a, **k: called.append(1) or None)
    out = odds.fetch_odds_api([{"match_id": 1, "home_team": "A", "away_team": "B"}])
    assert out == {} and not called
