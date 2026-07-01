"""
מעקב ניחושי המשתמש לאורך הטורניר: שומר את הניחושים שנשלחו בצילום,
מסדר אותם מול תוצאות אמת כשהן נכנסות, ומחשב אחוזי פגיעה — שלך מול המודל.

הקובץ data/my_predictions.json נשמר בריפו ומסונכרן בין הרצות הענן,
כך שהמעקב נצבר לאורך זמן. לעולם לא זורק חריגה.
"""
from __future__ import annotations

import unicodedata

import config
import utils

log = utils.get_logger("predlog")

_PATH = config.DATA_DIR / "my_predictions.json"


def _norm(s) -> str:
    s = str(s or "").strip().lower()
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def _key(home, away):
    return frozenset((_norm(home), _norm(away)))


def _outcome(h, a):
    if h is None or a is None:
        return None
    return "H" if h > a else "A" if a > h else "D"


def _load() -> dict:
    d = utils.load_json(_PATH, default={"predictions": []}) or {"predictions": []}
    if not isinstance(d.get("predictions"), list):
        d = {"predictions": []}
    return d


def _save(d: dict) -> None:
    utils.save_json(_PATH, d)


def record_predictions(entries: list[dict]) -> int:
    """שומר/מעדכן ניחושי משתמש. entry: home, away, date, user_home, user_away,
    model_home, model_away. מחזיר כמה נשמרו/עודכנו."""
    entries = [e for e in (entries or []) if e.get("home") and e.get("away")]
    if not entries:
        return 0
    d = _load()
    index = {_key(p["home"], p["away"]): p for p in d["predictions"]}
    saved = 0
    for e in entries:
        k = _key(e["home"], e["away"])
        rec = index.get(k)
        new = rec is None
        rec = rec or {"settled": False}
        rec.update({
            "home": e["home"], "away": e["away"], "date": e.get("date"),
            "user_home": e.get("user_home"), "user_away": e.get("user_away"),
            "model_home": e.get("model_home"), "model_away": e.get("model_away"),
            "saved_at": utils.now_iso(),
        })
        if new:
            rec["settled"] = False
            d["predictions"].append(rec)
            index[k] = rec
        saved += 1
    _save(d)
    log.info("נשמרו %d ניחושי משתמש", saved)
    return saved


def settle_with_results(results: list[dict]) -> int:
    """מסדר ניחושים פתוחים מול תוצאות אמת. מחזיר כמה הוכרעו עכשיו."""
    if not results:
        return 0
    d = _load()
    by_key = {_key(r.get("home"), r.get("away")): r for r in results}
    changed = 0
    for rec in d["predictions"]:
        if rec.get("settled"):
            continue
        r = by_key.get(_key(rec["home"], rec["away"]))
        if not r:
            continue
        ah, aa = r.get("home_goals"), r.get("away_goals")
        if _norm(r.get("home")) != _norm(rec["home"]):  # יישור לכיוון הרשומה
            ah, aa = aa, ah
        if ah is None or aa is None:
            continue
        ah, aa = int(ah), int(aa)
        rec["actual_home"], rec["actual_away"] = ah, aa
        # תאריך אוטומטי מהתוצאה הרשמית — מונע backfill ידני בעתיד (שיפור #3)
        if not rec.get("date") and r.get("date"):
            rec["date"] = r["date"]
        if r.get("stage"):                  # שלב — למכפיל הניקוד (×2/×3)
            rec["stage"] = r["stage"]
        act = _outcome(ah, aa)
        uh, ua = rec.get("user_home"), rec.get("user_away")
        mh, ma = rec.get("model_home"), rec.get("model_away")
        rec["user_outcome_ok"] = (_outcome(uh, ua) == act) if uh is not None else None
        rec["user_exact_ok"] = (uh == ah and ua == aa) if uh is not None else None
        rec["model_outcome_ok"] = (_outcome(mh, ma) == act) if mh is not None else None
        rec["model_exact_ok"] = (mh == ah and ma == aa) if mh is not None else None
        rec["settled"] = True
        changed += 1
    if changed:
        _save(d)
        log.info("הוכרעו %d ניחושים מול תוצאות אמת", changed)
    return changed


# מכפיל הניקוד לפי שלב: בתים ×1 · R32/R16 ×2 · רבע-גמר עד הגמר ×3
_STAGE_MULT = {
    "GROUP": 1, "R32": 2, "R16": 2, "QF": 3, "SF": 3, "FINAL": 3, "F": 3,
    "ROUND-OF-32": 2, "ROUND-OF-16": 2, "QUARTER-FINAL": 3, "SEMI-FINAL": 3,
}


def _stage_mult(stage) -> int:
    s = str(stage or "").upper().replace("_", "-").strip()
    if s in _STAGE_MULT:
        return _STAGE_MULT[s]
    if any(k in s for k in ("FINAL", "SEMI", "QUARTER", "QF", "SF")):
        return 3
    if any(k in s for k in ("R32", "R16", "OF-32", "OF-16")):
        return 2
    return 1


def _points(ph, pa, ah, aa) -> int | None:
    """ניקוד KICKOFF (לפני מכפיל-שלב): מדויק=3, כיוון נכון=1, **הפך-מדויק (מראה)=−1**,
    אחרת=0. קנס (−1) חל רק כשהניחוש הוא ההפך המדויק של התוצאה (ניחשת 2-1, יצא 1-2);
    כיוון שגוי שאינו מראה = פספוס (0). המכפיל (×2/×3) מוחל ב-summary לפי השלב."""
    if None in (ph, pa, ah, aa):
        return None
    if ph == ah and pa == aa:
        return 3
    dp, da = _outcome(ph, pa), _outcome(ah, aa)
    if dp == da:
        return 1
    if ph == aa and pa == ah:          # ההפך המדויק בלבד
        return -1
    return 0


def summary() -> dict:
    d = _load()
    settled = [p for p in d["predictions"] if p.get("settled")]

    def rate(key):
        vals = [p.get(key) for p in settled if p.get(key) is not None]
        return (sum(1 for v in vals if v), len(vals))

    # בסיס שלב-הבתים מעוגן למספרים הרשמיים מהאפליקציה — המעקב כיסה רק 60/72 משחקי
    # בתים, אז סכימה ישירה מחטיאה. משתמש 62 (רשמי מהאפליקציה); מודל 58 (אומדן הוגן,
    # קצב ניחוש-מראש 0.81/משחק × 72). משחקי נוקאאוט נספרים ישירות מעל הבסיס.
    base = getattr(config, "PREDICTION_GROUP_BASELINE", {"user": 62, "model": 58})
    user_pts, model_pts = base.get("user", 0), base.get("model", 0)
    for p in settled:
        mult = _stage_mult(p.get("stage"))           # ×1 בתים · ×2 R32/R16 · ×3 רבע+
        if mult == 1:                                # בתים כבר בבסיס — לא לספור שוב
            continue
        up = _points(p.get("user_home"), p.get("user_away"),
                     p.get("actual_home"), p.get("actual_away"))
        mp = _points(p.get("model_home"), p.get("model_away"),
                     p.get("actual_home"), p.get("actual_away"))
        user_pts += (up or 0) * mult
        model_pts += (mp or 0) * mult

    return {
        "total": len(d["predictions"]),
        "settled": len(settled),
        "user_outcome": rate("user_outcome_ok"),
        "user_exact": rate("user_exact_ok"),
        "model_outcome": rate("model_outcome_ok"),
        "model_exact": rate("model_exact_ok"),
        "user_points": user_pts,
        "model_points": model_pts,
    }


def format_summary_he(s: dict | None = None) -> str:
    """טקסט עברי לטלגרם. ריק אם אין עדיין משחקים שהוכרעו."""
    s = s or summary()
    if s["settled"] == 0:
        return ""
    uo_h, uo_n = s["user_outcome"]
    ue_h, ue_n = s["user_exact"]
    mo_h, mo_n = s["model_outcome"]
    me_h, me_n = s["model_exact"]
    up, mp = s.get("user_points", 0), s.get("model_points", 0)
    if up == mp:
        lead = "🤝 תיקו"
    else:
        who = "🟢 אתה מוביל" if up > mp else "🔴 המודל מוביל"
        lead = f"{who} ב-{abs(up - mp)}"
    return (
        f"<b>📈 ניחושים — אתה מול המערכת</b> ({s['settled']} משחקים · "
        f"בתים ×1 · R32/R16 ×2 · רבע→גמר ×3)\n"
        f"🏆 <b>ניקוד: אתה {up} · המערכת {mp}</b> ({lead})\n"
        f"אתה: מנצח {uo_h}/{uo_n} · מדויק {ue_h}/{ue_n}\n"
        f"המערכת: מנצח {mo_h}/{mo_n} · מדויק {me_h}/{me_n}"
    )
