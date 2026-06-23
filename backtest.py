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

import argparse
import contextlib
import itertools
import json

import config
import predictor
import utils

log = utils.get_logger("backtest")

# גריד ברירת-מחדל ל-auto-tune. אלה הקבועים שמשפיעים ישירות על חיזוי תוצאה
# (יתרון-שוק אינו בגריד כי לתוצאות השמורות אין אודדס מצורף — אין לו אפקט פה).
TUNE_GRID = {
    "MAX_XG": [3.0, 3.2, 3.4, 3.5],   # ≤3.5: מעל זה _round_goals מחזיר 4 → ניחושים מנופחים
    "HOME_ADVANTAGE": [0.10, 0.20, 0.25, 0.35],
    # משקל השוק — מכוונן רק כשלתוצאות יש אודדס מארכבים (אחרת inert, חסר השפעה)
    "MARKET_BLEND_WEIGHT": [0.4, 0.5, 0.6, 0.7, 0.8],
}


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


def _model_predict_fn(teams_by_name: dict, odds_by_pair: dict | None = None):
    """מחזיר predict_fn שמשתמש במודל האמיתי (recommended_score). אם יש אודדס
    מארכבים לזוג — מצורפים, כך ש-MARKET_BLEND_WEIGHT משפיע על ה-backtest."""
    odds_by_pair = odds_by_pair or {}

    def f(home: str, away: str):
        match = {"home_team": home, "away_team": away}
        mp = odds_by_pair.get(frozenset((str(home).lower(), str(away).lower())))
        if mp:
            match["market_probabilities"] = mp
        pred = predictor.predict_match(match, teams_by_name)
        # מנקדים את הניחוש שמוצג בדוח (predicted_score) — הוא מושפע משקלול השוק,
        # כך שכיוונון MARKET_BLEND_WEIGHT באמת משפיע, וה-tune מייעל את מה שאתה רואה.
        score = pred.get("predicted_score") or pred.get("recommended_score", "")
        try:
            ph, pa = (int(x) for x in score.split("-"))
            return ph, pa
        except (ValueError, AttributeError):
            return None
    return f


def run_backtest(db: dict | None = None, scoring: dict | None = None) -> dict:
    """מריץ backtest מלא: מודל מול שני בייסליינים נאיביים."""
    db = db if db is not None else (utils.load_json(config.DB_PATH, default={}) or {})
    scoring = scoring or config.PREDICTION_SCORING
    results = _clean_results(db)
    teams_by_name = {t.get("team_name"): t for t in db.get("teams", [])}
    odds_by_pair = {frozenset((str(r["home"]).lower(), str(r["away"]).lower())):
                    r["market_probabilities"]
                    for r in results if r.get("market_probabilities")}

    return {
        "n_results": len(results),
        "model": evaluate(results, _model_predict_fn(teams_by_name, odds_by_pair), scoring),
        "baseline_home_1_0": evaluate(results, lambda h, a: (1, 0), scoring),
        "baseline_draw_1_1": evaluate(results, lambda h, a: (1, 1), scoring),
        "calibration": model_calibration(db),
    }


def _result_outcome(r: dict) -> str | None:
    ah, aa = r.get("home_goals"), r.get("away_goals")
    if ah is None or aa is None:
        return None
    return "home_win" if ah > aa else "away_win" if aa > ah else "draw"


def model_calibration(db: dict | None = None) -> dict:
    """כיול ההסתברויות של המודל מול התוצאות (#6).

    - **Brier** (רב-מחלקתי): ממוצע על המשחקים של Σ_o (p_o − y_o)². נמוך=טוב.
      0=מושלם · 0.667=ניחוש אחיד (3 תוצאות) · מתחת ל-~0.6 = מודל שימושי.
    - **כיול הפייבוריט:** שיעור הפגיעה של הפייבוריט מול הביטחון הממוצע בו —
      קרובים זה לזה = מכויל היטב (לא בטוח-מדי / לא ביישן-מדי).
    בתוך-מדגם (המודל למד מהתוצאות) — אינדיקציה, לא ערובה.
    """
    db = db if db is not None else (utils.load_json(config.DB_PATH, default={}) or {})
    results = _clean_results(db)
    teams_by_name = {t.get("team_name"): t for t in db.get("teams", [])}
    odds_by_pair = {frozenset((str(r["home"]).lower(), str(r["away"]).lower())):
                    r["market_probabilities"]
                    for r in results if r.get("market_probabilities")}
    keys = ("home_win", "draw", "away_win")
    n = 0
    brier = 0.0
    fav_hit = 0
    fav_conf = 0.0
    for r in results:
        actual = _result_outcome(r)
        if actual is None:
            continue
        match = {"home_team": r["home"], "away_team": r["away"]}
        mp = odds_by_pair.get(frozenset((str(r["home"]).lower(), str(r["away"]).lower())))
        if mp:
            match["market_probabilities"] = mp
        probs = predictor.predict_match(match, teams_by_name).get("outcome_probabilities", {})
        if not probs:
            continue
        n += 1
        brier += sum((probs.get(k, 0.0) - (1.0 if k == actual else 0.0)) ** 2 for k in keys)
        fav = max(keys, key=lambda k: probs.get(k, 0.0))
        fav_conf += probs.get(fav, 0.0)
        if fav == actual:
            fav_hit += 1
    if not n:
        return {"n": 0}
    return {
        "n": n,
        "brier": round(brier / n, 4),
        "brier_uniform": 0.6667,
        "favorite_hit_rate": round(fav_hit / n, 3),
        "favorite_avg_confidence": round(fav_conf / n, 3),
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
    lines += ["", f"יתרון המודל מול הבייסליין הטוב: {edge:+.3f} נק/משחק — {verdict}"]

    cal = bt.get("calibration") or {}
    if cal.get("n"):
        b = cal["brier"]
        quality = ("מצוין" if b < 0.55 else "טוב" if b < 0.62 else "חלש")
        lines += [
            "",
            f"כיול הסתברויות (Brier): {b} מול {cal['brier_uniform']} (ניחוש אחיד) — {quality}",
            f"  פייבוריט: פוגע {cal['favorite_hit_rate']:.0%} · ביטחון ממוצע "
            f"{cal['favorite_avg_confidence']:.0%} "
            f"({'מכויל' if abs(cal['favorite_hit_rate'] - cal['favorite_avg_confidence']) < 0.06 else 'לא מכויל'})",
        ]
    lines += ["", "הערה: in-sample (המודל למד מהתוצאות) — השווה יחסית בין גרסאות."]
    return "\n".join(lines)


@contextlib.contextmanager
def _override_config(overrides: dict):
    """מחליף זמנית ערכי config (predictor קורא אותם בזמן ריצה) ומשחזר בסוף."""
    old = {k: getattr(config, k, None) for k in overrides}
    try:
        for k, v in overrides.items():
            setattr(config, k, v)
        yield
    finally:
        for k, v in old.items():
            setattr(config, k, v)


def tune(db: dict | None = None, scoring: dict | None = None,
         grid: dict | None = None) -> dict:
    """סורק גריד קונפיגורציות, מנקד כל אחת ב-backtest, ומחזיר את הטובה ביותר.

    אזהרה: in-sample על מעט תוצאות — נוטה ל-overfit. השתמש כרמז, לא כאמת מוחלטת;
    אל תחיל ערך קיצון בלי הבנה. הפונקציה *לא* משנה את config — רק מדווחת.
    """
    db = db if db is not None else (utils.load_json(config.DB_PATH, default={}) or {})
    scoring = scoring or config.PREDICTION_SCORING
    grid = grid or TUNE_GRID
    keys = list(grid)
    current = {k: getattr(config, k, None) for k in keys}
    current_ppg = run_backtest(db, scoring)["model"]["ppg"]

    rows = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        overrides = dict(zip(keys, combo))
        with _override_config(overrides):
            ppg = run_backtest(db, scoring)["model"]["ppg"]
        rows.append({"config": overrides, "ppg": ppg})
    rows.sort(key=lambda r: r["ppg"], reverse=True)
    best = rows[0] if rows else {"config": current, "ppg": current_ppg}
    return {"current": current, "current_ppg": current_ppg, "best": best, "all": rows}


def format_tune_report(t: dict) -> str:
    cur = ", ".join(f"{k}={v}" for k, v in t["current"].items())
    best = ", ".join(f"{k}={v}" for k, v in t["best"]["config"].items())
    delta = t["best"]["ppg"] - t["current_ppg"]
    lines = [
        f"Auto-tune — {len(t['all'])} קונפיגורציות (KICKOFF ppg)", "",
        f"נוכחי:  {cur}  →  {t['current_ppg']:.3f} ppg",
        f"הכי טוב: {best}  →  {t['best']['ppg']:.3f} ppg  ({delta:+.3f})", "",
        "טופ 5:",
    ]
    for r in t["all"][:5]:
        cfg = ", ".join(f"{k}={v}" for k, v in r["config"].items())
        lines.append(f"  {r['ppg']:.3f}   {cfg}")
    lines += ["",
              "⚠ in-sample על מעט תוצאות — נוטה ל-overfit. החל רק אם השיפור עקבי",
              "  והערך הגיוני; אמת מול הבייסליינים לפני deploy."]
    return "\n".join(lines)


def maybe_autotune(db: dict | None = None, scoring: dict | None = None,
                   min_gain: float = 0.10) -> dict:
    """כיוונון אוטומטי **פעם ביום** (נקרא מהדוח היומי, לפני הניחושים). מריץ tune,
    ומחיל את הקונפיג הטוב ביותר **רק אם** הוא משפר ב-≥min_gain ppg ובתחום שפוי.
    שומר ל-data/tuning.json (ש-config טוען בריצות הבאות) ומחיל בזיכרון לריצה הנוכחית.
    best-effort; לא זורק."""
    from datetime import date
    path = config.DATA_DIR / "tuning.json"
    today = date.today().isoformat()
    cur = {}
    if path.exists():
        try:
            cur = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cur = {}
    if cur.get("date") == today:            # כבר רץ היום — לא שוב
        return cur
    try:
        db = db if db is not None else (utils.load_json(config.DB_PATH, default={}) or {})
        if len(_clean_results(db)) < 10:    # מעט מדי תוצאות לכיול אמין
            log.info("auto-tune יומי דולג — פחות מ-10 תוצאות")
            return cur
        t = tune(db, scoring)
        best, gain = t["best"], t["best"]["ppg"] - t["current_ppg"]
        out = {"date": today, "current_ppg": t["current_ppg"],
               "best_ppg": best["ppg"], "gain": round(gain, 3)}
        if gain >= min_gain:
            out.update(best["config"])
            for k, v in best["config"].items():     # החלה מיידית לריצה הנוכחית
                setattr(config, k, v)
            log.info("auto-tune יומי: הוחל %s (שיפור %.3f ppg)", best["config"], gain)
        else:
            out.update({k: getattr(config, k, None) for k in TUNE_GRID})
            log.info("auto-tune יומי: אין שיפור משמעותי (%.3f) — נשמר הקיים", gain)
        json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        return out
    except Exception as exc:  # noqa: BLE001
        log.error("auto-tune יומי נכשל: %s", exc)
        return cur


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Backtest / auto-tune the predictor")
    ap.add_argument("--tune", action="store_true",
                    help="סרוק גריד קונפיגורציות ובחר את הטובה ביותר")
    args = ap.parse_args()
    print(format_tune_report(tune()) if args.tune else format_report(run_backtest()))
