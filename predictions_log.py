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


def _points(ph, pa, ah, aa) -> int | None:
    """ניקוד KICKOFF: מדויק=3, כיוון נכון=1, הפוך=-1."""
    if None in (ph, pa, ah, aa):
        return None
    if ph == ah and pa == aa:
        return 3
    dp, da = _outcome(ph, pa), _outcome(ah, aa)
    if dp == da:
        return 1
    if dp != "D" and da != "D" and dp != da:
        return -1
    return 0


def summary() -> dict:
    d = _load()
    settled = [p for p in d["predictions"] if p.get("settled")]

    def rate(key):
        vals = [p.get(key) for p in settled if p.get(key) is not None]
        return (sum(1 for v in vals if v), len(vals))

    user_pts = model_pts = 0
    for p in settled:
        up = _points(p.get("user_home"), p.get("user_away"),
                     p.get("actual_home"), p.get("actual_away"))
        mp = _points(p.get("model_home"), p.get("model_away"),
                     p.get("actual_home"), p.get("actual_away"))
        user_pts += up or 0
        model_pts += mp or 0

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
    lead = "🤝 תיקו" if up == mp else ("🟢 אתה מוביל" if up > mp else "🔴 המודל מוביל")
    return (
        f"<b>📈 ניחושים — אתה מול המערכת</b> ({s['settled']} משחקים)\n"
        f"🏆 <b>ניקוד: אתה {up} · המערכת {mp}</b> ({lead})\n"
        f"אתה: מנצח {uo_h}/{uo_n} · מדויק {ue_h}/{ue_n}\n"
        f"המערכת: מנצח {mo_h}/{mo_n} · מדויק {me_h}/{me_n}"
    )
