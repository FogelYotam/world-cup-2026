"""
Backtesting harness — מודד כמה נקודות KICKOFF המודל היה צובר על תוצאות שכבר
ידועות (db['results']), מול בייסליינים נאיביים. מאפשר לאמת ששינוי במודל באמת
משפר *לפני* שמעלים אותו לענן.

שימוש:
    python backtest.py                 # מריץ על db['results'] ומדפיס דוח

זרימת A/B מומלצת:
    1. הרץ backtest ושמור את ה-ppg של המודל.
    2. שנה קוד/קונפיג (xG cap, משקל שוק, נוסחת ניקוד...).
    3. הרץ שוב — אם ה-ppg עלה והמודל עדיין מכה את הבייסליינים, השינוי משפר.

הערה חשובה (in-sample): המודל כבר למד מהתוצאות האלה דרך ingest_results, לכן
הציון המוחלט אופטימי במקצת. הערך הוא ההשוואה *היחסית* — בין גרסאות מודל ומול
הבייסליינים — לא המספר המוחלט.
"""
from __future__ import annotations

import config
import predictor
import utils

log = utils.get_logger("backtest")


def _clean_results(db: dict) -> list[dict]:
    """מסנן תוצאות שמישות מתוך ה-DB."""
    out = []
    for r in db.get("results", []) or []:
        if not isinstance(r, dict):
            continue
        if not r.get("home") or not r.get("away"):
            continue
        if r.get("home_goals") is None or r.get("away_goals") is None:
            continue
        out.append(r)
    return out


def evaluate(results: list[dict], predict_fn, scoring: dict) -> dict:
    """מריץ predict_fn(home, away) -> (ph, pa)|None על כל תוצאה ומנקד תחת KICKOFF.

    מחזיר: n, points, ppg (נק' למשחק), exact, direction, detail.
    """
    n = exact = direction = 0
    pts = 0.0
    detail = []
    for r in results:
        ah, aa = int(r["home_goals"]), int(r["away_goals"])
        pred = predict_fn(r["home"], r["away"])
        if pred is None:
            continue
        ph, pa = pred
        sp = predictor.match_points(ph, pa, ah, aa, scoring)
        n += 1
        pts += sp
        if ph == ah and pa == aa:
            exact += 1
        if predictor._sign(ph, pa) == predictor._sign(ah, aa):
            direction += 1
        detail.append({"home": r["home"], "away": r["away"],
                       "pred": f"{ph}-{pa}", "actual": f"{ah}-{aa}", "points": sp})
    return {
        "n": n,
        "points": round(pts, 1),
        "ppg": round(pts / n, 3) if n else 0.0,
        "exact": exact,
        "direction": direction,
        "detail": detail,
    }


def _model_predict_fn(teams_by_name: dict):
    """מחזיר predict_fn שמשתמש במודל האמיתי (recommended_score)."""
    def f(home: str, away: str):
        pred = predictor.predict_match(
            {"home_team": home, "away_team": away}, teams_by_name)
        try:
            ph, pa = (int(x) for x in pred["recommended_score"].split("-"))
            return ph, pa
        except (KeyError, ValueError, AttributeError):
            return None
    return f


def run_backtest(db: dict | None = None, scoring: dict | None = None) -> dict:
    """מריץ backtest מלא: מודל מול שני בייסליינים נאיביים."""
    db = db if db is not None else (utils.load_json(config.DB_PATH, default={}) or {})
    scoring = scoring or config.PREDICTION_SCORING
    results = _clean_results(db)
    teams_by_name = {t.get("team_name"): t for t in db.get("teams", [])}

    return {
        "n_results": len(results),
        "model": evaluate(results, _model_predict_fn(teams_by_name), scoring),
        "baseline_home_1_0": evaluate(results, lambda h, a: (1, 0), scoring),
        "baseline_draw_1_1": evaluate(results, lambda h, a: (1, 1), scoring),
    }


def format_report(bt: dict) -> str:
    """דוח טקסט קריא להשוואה."""
    lines = [f"Backtest על {bt['n_results']} תוצאות ידועות (KICKOFF scoring)", ""]
    rows = [
        ("המודל", bt["model"]),
        ("בייסליין 1-0 ביתי", bt["baseline_home_1_0"]),
        ("בייסליין 1-1 תיקו", bt["baseline_draw_1_1"]),
    ]
    lines.append(f"{'גרסה':<22}{'נק/משחק':>9}{'סהכ':>7}{'מדויק':>8}{'כיוון':>8}")
    for name, m in rows:
        lines.append(f"{name:<22}{m['ppg']:>9}{m['points']:>7}"
                     f"{m['exact']:>5}/{m['n']:<2}{m['direction']:>4}/{m['n']:<2}")
    edge = bt["model"]["ppg"] - max(bt["baseline_home_1_0"]["ppg"],
                                    bt["baseline_draw_1_1"]["ppg"])
    verdict = "✓ המודל מכה את הבייסליין" if edge > 0 else (
        "= שווה לבייסליין" if edge == 0 else "✗ המודל מתחת לבייסליין")
    lines += ["", f"יתרון המודל מול הבייסליין הטוב: {edge:+.3f} נק/משחק — {verdict}",
              "", "הערה: in-sample (המודל למד מהתוצאות) — השווה יחסית בין גרסאות."]
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_report(run_backtest()))
