"""
עיבוד ניחושי KICKOFF — הסוכן (Claude) קורא את הצילומים בראייה שלו (לא Gemini),
מחלץ לכל משחק את הניחוש, ומעביר לכאן רשימה. המודול:
  1. מחשב את ניחוש המודל לכל משחק (predictor).
  2. שומר את הניחושים ב-predictions_log (data/my_predictions.json).
  3. מיישב מול התוצאות הרשמיות (data/db.json).
  4. מדפיס/מחזיר "אתה מול המערכת" עם ניקוד KICKOFF: מדויק +3, כיוון +1, הפוך -1, פספוס 0.

שימוש מתוך סוכן שקרא צילומים:
    import kickoff_predictions as kp
    print(kp.process([
        {"home": "Egypt", "away": "Belgium", "user_home": 0, "user_away": 2},
        {"home": "Norway", "away": "Iraq", "user_home": 3, "user_away": 0},
    ]))

או CLI (JSON ברשימת משחקים):
    python kickoff_predictions.py '[{"home":"Egypt","away":"Belgium","user_home":0,"user_away":2}]'

הערות:
  • שמות נבחרות יכולים להיות באנגלית או עברית — יש מפת כינויים (_ALIASES).
  • orientation: home = הקבוצה שמופיעה ראשונה אצלך באפליקציה. היישוב מול התוצאות
    הרשמיות מטפל בהיפוך בית/חוץ אוטומטית.
  • לעבר (סבב 1) המודל in-sample (למד מהתוצאות) — צוין בפלט. ההשוואה ההוגנת היא
    על משחקים שטרם שוחקו בזמן השמירה.
"""
from __future__ import annotations

import json
import sys
import unicodedata

import config
import predictor
import predictions_log
import utils

log = utils.get_logger("kickoff")

# כינויי נבחרות נפוצים (KICKOFF/עברית/וריאנטים) → שם רשמי ב-db
_ALIASES = {
    "cape verde": "Cabo Verde", "קייפ ורדה": "Cabo Verde", "כף ורדה": "Cabo Verde",
    "iran": "IR Iran", "איראן": "IR Iran",
    "turkey": "Turkiye", "טורקיה": "Turkiye",
    "south korea": "Korea Republic", "דרום קוריאה": "Korea Republic",
    "usa": "USA", "ארצות הברית": "USA", "united states": "USA",
    "ivory coast": "Cote d'Ivoire", "חוף השנהב": "Cote d'Ivoire",
    "congo dr": "Congo DR", "dr congo": "Congo DR", "קונגו": "Congo DR",
    "bosnia": "Bosnia and Herzegovina", "בוסניה": "Bosnia and Herzegovina",
    "uzbekistan": "Uzbekistan", "אוזבקיסטן": "Uzbekistan",
    "czechia": "Czechia", "czech republic": "Czechia", "צ'כיה": "Czechia",
    "curacao": "Curacao", "קוראסאו": "Curacao", "קוראסao": "Curacao",
}


def _norm(s) -> str:
    s = str(s or "").lower().strip()
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def _resolve_team(name, teams_by_name) -> str | None:
    """מאתר שם נבחרת רשמי ב-db מתוך שם חופשי (אנגלית/עברית/וריאנט)."""
    if not name:
        return None
    if _ALIASES.get(str(name).strip().lower()):
        name = _ALIASES[str(name).strip().lower()]
    idx = {_norm(k): k for k in teams_by_name}
    n = _norm(name)
    if n in idx:
        return idx[n]
    for k in idx:
        if n and (n in k or k in n):
            return idx[k]
    return None


def _report_model(th, ta):
    """ניחוש המודל מהדוח השמור (`data/report_predictions.json`) — **טרום-משחק,
    הוגן** — מיושר ל-(th=בית, ta=חוץ). מחזיר (None,None) אם אין. מ-`report.py`
    מתועד שם בכל הרצה; כאן רק קוראים. כך השוואת אתה-מול-מודל הוגנת אוטומטית."""
    store = utils.load_json(config.DATA_DIR / "report_predictions.json", default={}) or {}
    direct = store.get(f"{_norm(th)}|{_norm(ta)}")
    if direct:
        return direct.get("model_home"), direct.get("model_away")
    rev = store.get(f"{_norm(ta)}|{_norm(th)}")     # הדוח רשם בכיוון ההפוך
    if rev:
        return rev.get("model_away"), rev.get("model_home")
    return None, None


def process(games: list[dict], db: dict | None = None) -> str:
    """מקבל רשימת ניחושים שחולצו מצילומים, שומר, מיישב, ומחזיר טקסט סיכום.

    כל game: {home, away, user_home, user_away, [model_home, model_away], [date]}.
    אם המשתמש מעלה את **הדוחות** שקיבל — חלץ מהם את ניחוש המודל והעבר אותו
    כ-model_home/model_away (זו ההשוואה ההוגנת: ניחוש המודל *לפני* המשחק). בלי זה
    המודל מחושב מחדש — וזה in-sample/מנופח לעבר. החזרה: סיכום "אתה מול המערכת".
    """
    db = db if db is not None else (utils.load_json(config.DB_PATH, default={}) or {})
    teams_by_name = {t.get("team_name"): t for t in db.get("teams", [])}

    entries, unresolved = [], []
    for g in games:
        if not isinstance(g, dict):
            continue
        home, away = g.get("home"), g.get("away")
        th, ta = _resolve_team(home, teams_by_name), _resolve_team(away, teams_by_name)
        mh, ma = g.get("model_home"), g.get("model_away")
        # 1) אם ניחוש המודל סופק במפורש (מהדוח) — משתמשים בו.
        # 2) אחרת — מנסים מהדוח **השמור** (`report_predictions.json`, הוגן/טרום-משחק).
        # 3) רק אם אין בכלל — מחשבים (לעבר זה in-sample/מנופח).
        if (mh is None or ma is None) and th and ta:
            mh, ma = _report_model(th, ta)
        if (mh is None or ma is None) and th and ta:
            pred = predictor.predict_match({"home_team": th, "away_team": ta}, teams_by_name)
            try:
                ph, pa = (int(x) for x in pred["recommended_score"].split("-"))
                mh, ma = (ph, pa) if _norm(pred["home_team"]) == _norm(th) else (pa, ph)
            except (KeyError, ValueError, AttributeError):
                pass
        if not (th and ta):
            unresolved.append(f"{home}-{away}")
        entries.append({
            "home": th or home, "away": ta or away, "date": g.get("date"),
            "user_home": g.get("user_home"), "user_away": g.get("user_away"),
            "model_home": mh, "model_away": ma,
        })

    predictions_log.record_predictions(entries)
    predictions_log.settle_with_results(db.get("results", []))
    s = predictions_log.summary()

    lines = [f"נקלטו {len(entries)} ניחושים · הוכרעו {s['settled']}."]
    if unresolved:
        lines.append(f"⚠️ משחקים שלא זוהו במערכת (לא יושוו): {', '.join(unresolved)}")
    lines.append("")
    lines.append(predictions_log.format_summary_he() or "אין עדיין משחקים מוכרעים להשוואה.")
    lines.append("")
    lines.append("ℹ️ לעבר המודל in-sample (למד מהתוצאות) — מנופח. ההשוואה ההוגנת היא "
                 "על ניחושים שנשמרו לפני המשחק.")
    return "\n".join(lines)


def _main(argv):
    if len(argv) < 2:
        # אין ארגומנט — מציג את המצב הנוכחי בלבד
        print(predictions_log.format_summary_he() or "אין ניחושים שמורים עדיין.")
        return 0
    try:
        games = json.loads(argv[1])
    except json.JSONDecodeError as exc:
        print(f"JSON לא תקין: {exc}")
        return 1
    print(process(games))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
