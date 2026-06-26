"""
הפקת דוח HTML בעברית (RTL) ושליחתו במייל.

הדוח נשמר ל-output/report.html ול-output/index.html (לפרסום ב-GitHub Pages).
המייל נשלח דרך Gmail SMTP: אם הוגדר REPORT_PUBLIC_URL — נשלח קישור;
אחרת ה-HTML מוטמע ישירות בגוף המייל.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
import smtplib

import requests
from jinja2 import Template

import config
import utils

log = utils.get_logger("report")

_TEMPLATE = Template(
    """<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>מונדיאל 2026 — דוח יומי</title>
<style>
  body { font-family: "Segoe UI", Arial, sans-serif; background:#0f172a; color:#e2e8f0;
         margin:0; padding:24px; line-height:1.5; }
  .wrap { max-width:920px; margin:0 auto; }
  h1 { color:#38bdf8; margin:0 0 4px; }
  .date { color:#94a3b8; margin-bottom:24px; }
  h2 { color:#fbbf24; border-bottom:2px solid #334155; padding-bottom:6px; margin-top:32px; }
  .card { background:#1e293b; border-radius:12px; padding:16px 20px; margin:14px 0;
          box-shadow:0 2px 8px rgba(0,0,0,.3); }
  .match-head { font-size:1.15em; font-weight:bold; color:#f1f5f9; }
  .score { color:#38bdf8; font-weight:bold; }
  .alts { color:#94a3b8; font-size:.9em; }
  .conf { display:inline-block; padding:2px 10px; border-radius:999px; font-size:.85em; font-weight:bold; }
  .conf-high { background:#166534; color:#dcfce7; }
  .conf-mid  { background:#854d0e; color:#fef9c3; }
  .conf-low  { background:#7f1d1d; color:#fee2e2; }
  .probs { color:#cbd5e1; font-size:.9em; margin-top:6px; }
  .explain { color:#94a3b8; font-size:.9em; margin-top:6px; font-style:italic; }
  table { width:100%; border-collapse:collapse; margin-top:8px; }
  th, td { text-align:right; padding:8px 10px; border-bottom:1px solid #334155; }
  th { color:#94a3b8; font-weight:600; }
  .cap { color:#fbbf24; font-weight:bold; }
  .vice { color:#a3e635; }
  .pill { background:#0f172a; border-radius:6px; padding:1px 8px; font-size:.85em; }
  .risk-high { color:#f87171; }
  .risk-medium { color:#fbbf24; }
  .risk-low { color:#4ade80; }
  .muted { color:#64748b; }
  .pitch { background:linear-gradient(160deg,#15803d,#166534); border-radius:14px;
           padding:18px 8px; margin:14px 0; box-shadow:inset 0 0 0 3px rgba(255,255,255,.15); }
  .pitch-row { display:flex; justify-content:space-around; flex-wrap:wrap; gap:8px; margin:12px 0; }
  .player { background:#0f172a; border:2px solid #e2e8f0; border-radius:10px;
            padding:6px 8px; text-align:center; min-width:74px; }
  .player .pname { font-weight:bold; color:#f1f5f9; font-size:.85em; }
  .player .pmeta { color:#94a3b8; font-size:.75em; margin-top:2px; }
  .player.cap { border-color:#fbbf24; box-shadow:0 0 8px rgba(251,191,36,.5); }
  footer { margin-top:40px; color:#64748b; font-size:.85em; text-align:center; }
</style>
</head>
<body>
<div class="wrap">
  <h1>⚽ מונדיאל 2026 — דוח יומי</h1>
  <div class="date">{{ date_str }}</div>

  <h2>ניחושי משחקים — {% if round_no %}סיבוב {{ round_no }}{% else %}הסיבוב הקרוב{% endif %}</h2>
  <div class="muted" style="margin-bottom:8px">מוצגים כל משחקי הסיבוב הקרוב. המלצת ההימור נחשפת כ-{{ reveal_hours }} שעות לפני פתיחת כל משחק.</div>
  {% if predictions %}
    {% for p in predictions %}
    <div class="card">
      <div class="match-head">{{ loop.index }}. {{ p.home_team }} מול {{ p.away_team }}
        <span class="muted">· {{ p.stage or '' }}{% if p.date %} · {{ p.date }}{% endif %}</span></div>
      {% if p.odds_locked %}
      <div>🔒 <span class="muted">ההמלצה תיחשף {{ p.reveal_label }}</span></div>
      {% else %}
      <div>הניחוש: <span class="score">{{ p.predicted_score if p.predicted_score is defined else p.recommended_score }}</span>
        {% if p.recommended_score and p.recommended_score != p.predicted_score %}<span class="muted">· בטוח-לניקוד {{ p.recommended_score }}</span>{% endif %}
        <span class="conf conf-{{ p.conf_class }}">אמון {{ p.confidence }}%</span></div>
      <div class="alts">חלופות (לפי תוחלת): {{ p.alternatives | join(', ') }}</div>
      <div class="probs">סיכויים — {{ p.home_team }}: {{ (p.outcome_probabilities.home_win*100)|round|int }}%
        · תיקו: {{ (p.outcome_probabilities.draw*100)|round|int }}%
        · {{ p.away_team }}: {{ (p.outcome_probabilities.away_win*100)|round|int }}%</div>
      {% if p.total_expected_goals is defined %}
      <div class="probs">⚽ שערים צפויים: <b>{{ '%.1f'|format(p.expected_goals.home) }}–{{ '%.1f'|format(p.expected_goals.away) }}</b>
        (סה״כ {{ '%.1f'|format(p.total_expected_goals) }})
        · 🧤 שער נקי — {{ p.home_team }}: {{ (p.clean_sheet.home*100)|round|int }}%
        · {{ p.away_team }}: {{ (p.clean_sheet.away*100)|round|int }}%</div>
      {% endif %}
      <div class="explain">{{ p.explanation }}</div>
      {% endif %}
    </div>
    {% endfor %}
  {% else %}
    <div class="card muted">אין משחקים בסיבוב הקרוב.</div>
  {% endif %}

  {% if advice.available %}
  <h2>👤 הקבוצה שלך — המלצות אישיות</h2>
  <div class="card">
    <div>מערך: <span class="pill">{{ advice.formation }}</span>
      · 👑 קפטן מומלץ: <span class="cap">{{ advice.recommended_captain.player_name if advice.recommended_captain else '—' }}</span>
      · סגן: <span class="vice">{{ advice.recommended_vice.player_name if advice.recommended_vice else '—' }}</span>
      {% if advice.captain_change %}<span class="muted">(שינוי מ-{{ advice.owner_captain }})</span>{% endif %}</div>
    {% if advice_pitch %}
    <div class="pitch">
      {% for pos, players in advice_pitch %}
      <div class="pitch-row">
        {% for pl in players %}
        {% set is_cap = advice.recommended_captain and pl.player_name == advice.recommended_captain.player_name %}
        <div class="player{% if is_cap %} cap{% endif %}">
          <div class="pname">{{ pl.player_name }}{% if is_cap %} 👑{% endif %}</div>
          <div class="pmeta">{{ pl.team }} · EP {{ pl.expected_points }}</div>
        </div>
        {% endfor %}
      </div>
      {% endfor %}
    </div>
    {% endif %}
    <div class="muted" style="margin-top:8px">🪑 ספסל: {% for b in advice.bench %}{{ b.position }} {{ b.player_name }}{% if not loop.last %} · {% endif %}{% endfor %}</div>
  </div>
  {% if advice.transfer_recs %}
  <div class="card">
    <strong>🔁 המלצות חילוף לפי קושי המחזור הבא</strong>
    <span class="muted">(קשה יוצא · דיפרנציאל בקל נכנס · בתוך התקציב{% if advice.forced_out %} · 🔒 נעול: {{ advice.forced_out }}{% endif %})</span>
    <table>
      <tr><th>#</th><th>החוצה</th><th>פנימה</th><th>תקציב</th></tr>
      {% for o in advice.transfer_recs %}
      <tr>
        <td>{{ loop.index }}</td>
        <td class="risk-high">{% for p in o.out %}{{ p.player_name }} <span class="muted">({{ p.price }}{% if p.opponent %} · v{{ p.opponent }}{% endif %})</span>{% if not loop.last %}<br>{% endif %}{% endfor %}</td>
        <td class="cap">{% for p in o['in'] %}{{ p.player_name }} <span class="muted">({{ p.price }}{% if p.ownership is not none %} · {{ p.ownership }}%{% endif %}{% if p.opponent %} · v{{ p.opponent }}{% endif %})</span>{% if not loop.last %}<br>{% endif %}{% endfor %}</td>
        <td class="muted">{{ o.in_cost }} ≤ {{ (o.out_cost + advice.bank)|round(1) }}</td>
      </tr>
      {% endfor %}
    </table>
    <div class="muted" style="margin-top:6px;font-size:.85em">לנעילת חילוף: שלח בבוט "תוציא [שם]".</div>
  </div>
  {% endif %}
  {% if star_rows %}
  <div class="card">
    <strong>⭐ כוכבי המחזור</strong>
    <span class="muted">(פרימיום · משחק קל · "קח אותם")</span>
    <div class="pitch">
      {% for pos, players in star_rows %}
      <div class="pitch-row">
        {% for d in players %}
        <div class="player">
          <div class="pname">{{ d.player_name }}</div>
          <div class="pmeta">{{ d.team }}{% if d.opponent %} · v{{ d.opponent }}{% endif %}{% if d.ownership is not none %} · {{ d.ownership }}%{% endif %}</div>
        </div>
        {% endfor %}
      </div>
      {% endfor %}
    </div>
  </div>
  {% endif %}
  {% if diff_pitch.rows %}
  <div class="card">
    <strong>🎯 דיפרנציאלים מומלצים</strong>
    <span class="muted">(בעלות &lt; {{ diff_threshold }}% · מובטחי-דקות · 3 שוערים / 5 הגנה / 5 קישור / 3 חלוץ)</span>
    <div class="pitch">
      {% for pos, players in diff_pitch.rows %}
      <div class="pitch-row">
        {% for d in players %}
        <div class="player">
          <div class="pname">{{ d.player_name }}{% if d.scouting_bonus %} ⭐{% endif %}</div>
          <div class="pmeta">{{ d.team }}{% if d.ownership is not none %} · {{ d.ownership }}%{% endif %}</div>
        </div>
        {% endfor %}
      </div>
      {% endfor %}
    </div>
    {% if diff_pitch.bench %}
    <div class="muted" style="margin-top:8px">🪑 ספסל: {% for d in diff_pitch.bench %}{{ d.player_name }} ({{ d.position }}{% if d.ownership is not none %}, {{ d.ownership }}%{% endif %}){% if not loop.last %} · {% endif %}{% endfor %}</div>
    {% endif %}
  </div>
  {% else %}
  <div class="card muted">🎯 דיפרנציאלים (בעלות &lt; {{ diff_threshold }}%) יופיעו כשייכנסו נתוני בעלות מהאתרים.</div>
  {% endif %}
  {% endif %}

  <footer>הופק אוטומטית · {{ generated_at }}</footer>
</div>
</body>
</html>"""
)


def _conf_class(score: int) -> str:
    if score >= 70:
        return "high"
    if score >= 45:
        return "mid"
    return "low"


def _norm_team(s) -> str:
    import unicodedata
    d = unicodedata.normalize("NFKD", str(s or "").lower())
    return "".join(c for c in d if not unicodedata.combining(c)).strip()


def _archive_report_predictions(predictions: list[dict], now=None) -> None:
    """שומר את ניחוש המודל (predicted_score) של **משחקים עתידיים** ל-
    `data/report_predictions.json` — כדי שבעתיד, כשהמשתמש יעלה את ניחושיו, ההשוואה
    תשתמש בניחוש ה**טרום-משחק** (הוגן) ולא תחשב מחדש in-sample. לא דורס משחקי עבר
    (משמר את הניחוש כפי שהיה לפני המשחק). לעולם לא זורק."""
    try:
        now = now or utils.now_local()
        today = now.date()
        path = config.DATA_DIR / "report_predictions.json"
        store = utils.load_json(path, default={}) or {}
        changed = False
        for p in predictions:
            d = utils._parse_dt(p.get("kickoff")) or utils._parse_dt(p.get("date"))
            if not (d and d.date() >= today):       # רק עתידיים — לא לדרוס עבר
                continue
            score = p.get("predicted_score") or p.get("recommended_score")
            if not score or "-" not in str(score):
                continue
            try:
                mh, ma = (int(x) for x in str(score).split("-"))
            except ValueError:
                continue
            key = f"{_norm_team(p.get('home_team'))}|{_norm_team(p.get('away_team'))}"
            store[key] = {"home": p.get("home_team"), "away": p.get("away_team"),
                          "model_home": mh, "model_away": ma,
                          "date": p.get("date"), "saved_at": utils.now_iso()}
            changed = True
        if changed:
            utils.save_json(path, store)
    except Exception as exc:  # noqa: BLE001
        log.error("ארכוב ניחושי הדוח נכשל: %s", exc)


def _next_round(predictions: list[dict], now=None) -> list[dict]:
    """מחזיר את משחקי **הסיבוב הקרוב** (matchday) — לא חלון-ימים שמערבב סיבובים.
    הסיבוב נקבע לפי המשחק העתידי המוקדם ביותר (date ≥ היום), ומוצגים כל משחקי
    אותו `round`. נדרש שהניחושים תויגו ב-`round` (ב-main.py). נופל ל-5 ימים אם לא."""
    now = now or utils.now_local()
    today = now.date()
    dated = []
    for p in predictions:
        if p.get("round") is None:
            continue
        d = utils._parse_dt(p.get("kickoff")) or utils._parse_dt(p.get("date"))
        if d and d.date() >= today:
            dated.append((p, d))
    if not dated:
        return _within_days(predictions, config.REPORT_UPCOMING_DAYS, now)
    next_rd = min(dated, key=lambda x: x[1])[0]["round"]      # סיבוב המשחק המוקדם
    out = [p for p, _ in dated if p["round"] == next_rd]
    out.sort(key=lambda p: utils._parse_dt(p.get("kickoff")) or utils._parse_dt(p.get("date")) or now)
    return out


def _within_days(predictions: list[dict], days: int, now=None) -> list[dict]:
    """מסנן משחקים לחלון של N הימים הקרובים (מהיום ועד היום+N-1).
    אם לאף משחק אין תאריך תקין — מחזיר הכל (לא חוסם בגלל נתונים חסרים)."""
    now = now or utils.now_local()
    today = now.date()
    upper = today + timedelta(days=max(0, days - 1))
    sel, any_dated = [], False
    for p in predictions:
        d = utils._parse_dt(p.get("kickoff")) or utils._parse_dt(p.get("date"))
        if d:
            any_dated = True
            if today <= d.date() <= upper:
                sel.append(p)
    return sel if any_dated else list(predictions)


def _upcoming(predictions: list[dict], n: int, now=None) -> list[dict]:
    """N המשחקים הקרובים מהיום והלאה, ממוינים לפי שעת פתיחה/תאריך.
    אם אין משחקים עתידיים — מחזיר את האחרונים; אם אין תאריכים — N הראשונים."""
    now = now or utils.now_local()
    today = now.date()
    dated = []
    for p in predictions:
        d = utils._parse_dt(p.get("kickoff")) or utils._parse_dt(p.get("date"))
        if d:
            dated.append((d, p))
    if dated:
        future = sorted((dp for dp in dated if dp[0].date() >= today),
                        key=lambda x: x[0])
        sel = [p for _, p in future][:n]
        if sel:
            return sel
        return [p for _, p in sorted(dated, key=lambda x: x[0])][:n]
    return list(predictions or [])[:n]


_PITCH_ORDER = ("FWD", "MID", "DEF", "GK")   # מלמעלה (התקפה) למטה (שוער)


def _pitch_rows(lineup: list[dict]) -> list[tuple]:
    """מקבץ הרכב לשורות מערך על המגרש: חלוץ למעלה, שוער למטה."""
    by = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for p in lineup or []:
        by.setdefault(p.get("position"), []).append(p)
    return [(pos, by[pos]) for pos in _PITCH_ORDER if by[pos]]


def _differentials_split(diffs: dict | None) -> dict:
    """כל הדיפרנציאלים לכל עמדה על המגרש (3 שוערים / 5 הגנה / 5 קישור / 3 חלוץ)."""
    diffs = diffs or {}
    rows = []
    for pos in _PITCH_ORDER:                 # FWD מלמעלה, GK למטה
        items = diffs.get(pos) or []
        if items:
            rows.append((pos, items))
    return {"rows": rows, "bench": []}


def render_html(predictions: list[dict], fantasy_result: dict,
                plan: dict | None = None, advice: dict | None = None) -> str:
    """מרנדר את דוח ה-HTML ושומר אותו ל-output/. מחזיר את ה-HTML."""
    now = utils.now_local()
    today = now.date()
    for p in predictions:
        p["conf_class"] = _conf_class(p.get("confidence", 0))
        # בדוח המלא מציגים את כל הניחושים (ללא חסימה); מסמנים מה משוחק היום
        p["odds_locked"] = False
        _dt = utils._parse_dt(p.get("kickoff")) or utils._parse_dt(p.get("date"))
        p["is_today"] = bool(_dt and _dt.date() == today)
        p["reveal_label"] = _kickoff_label(p) or (p.get("date") or "")

    # ארכוב ניחושי המודל (טרום-משחק) להשוואה הוגנת עתידית — לפני כל סינון
    _archive_report_predictions(predictions, now)

    # מציגים בדוח את משחקי **הסיבוב הקרוב** (לא חלון-ימים שמערבב סיבובים)
    window_days = config.REPORT_UPCOMING_DAYS
    window = _next_round(predictions, now)
    round_no = window[0].get("round") if window else None

    # ההרכב האישי כמערך על המגרש + דיפרנציאלים (2 מגרש + 1 ספסל לעמדה)
    advice_pitch = None
    diff_pitch = {"rows": [], "bench": []}
    star_rows = []
    if advice and advice.get("available"):
        advice_pitch = _pitch_rows(advice.get("starting_eleven"))
        diff_pitch = _differentials_split(advice.get("differentials"))
        _stars = advice.get("top_picks") or {}
        star_rows = [(pos, _stars[pos]) for pos in ("GK", "DEF", "MID", "FWD")
                     if _stars.get(pos)]

    html = _TEMPLATE.render(
        predictions=window,
        fantasy=fantasy_result,
        plan=plan or {},
        advice=advice or {},
        advice_pitch=advice_pitch,
        diff_pitch=diff_pitch,
        star_rows=star_rows,
        diff_threshold=config.DIFFERENTIAL_MAX_OWNERSHIP,
        reveal_hours=config.ODDS_REVEAL_HOURS,
        window_days=window_days,
        round_no=round_no,
        date_str=now.strftime("%d/%m/%Y"),
        generated_at=now.strftime("%d/%m/%Y %H:%M"),
    )

    (config.OUTPUT_DIR / "report.html").write_text(html, encoding="utf-8")
    (config.OUTPUT_DIR / "index.html").write_text(html, encoding="utf-8")
    log.info("דוח HTML נשמר ב-output/")
    return html


def send_email(html: str) -> bool:
    """שולח את הדוח במייל. מחזיר True בהצלחה, False אחרת."""
    if not config.mail_enabled():
        log.warning("מייל לא מוגדר (חסר GMAIL_APP_PASSWORD) — מדלג על שליחה")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"⚽ מונדיאל 2026 — דוח {utils.now_local():%d/%m/%Y}"
    msg["From"] = config.GMAIL_ADDRESS
    msg["To"] = config.MAIL_TO

    if config.REPORT_PUBLIC_URL:
        body = (
            f"<div dir='rtl' style='font-family:Arial'>"
            f"<h2>הדוח היומי מוכן</h2>"
            f"<p><a href='{config.REPORT_PUBLIC_URL}'>צפייה בדוח המלא</a></p></div>"
        )
        msg.attach(MIMEText(body, "html", "utf-8"))
    else:
        msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
            server.send_message(msg)
        log.info("המייל נשלח ל-%s", config.MAIL_TO)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("שליחת המייל נכשלה: %s", exc)
        return False


# --------------------------------------------------------------------------- #
# טלגרם — ערוץ ההפצה המועדף
# --------------------------------------------------------------------------- #
_TG_LIMIT = 4096  # תקרת תווים להודעת טלגרם


_POS_EMOJI = {"GK": "🧤 שוער", "DEF": "🛡️ הגנה", "MID": "⚙️ קישור", "FWD": "⚔️ חלוץ"}


def _he_day(iso_date) -> str:
    """תאריך ISO → 'DD/MM' לתצוגה."""
    try:
        from datetime import date
        return date.fromisoformat(str(iso_date)[:10]).strftime("%d/%m")
    except Exception:  # noqa: BLE001
        return str(iso_date)


def _group_lineup(players: list[dict]) -> str:
    """שורה אחת מקובצת לפי עמדה (לספסל): GK | DEF | MID | FWD."""
    by = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for p in players:
        by.setdefault(p.get("position"), []).append(escape(str(p.get("player_name"))))
    parts = [", ".join(by[pos]) for pos in ("GK", "DEF", "MID", "FWD") if by[pos]]
    return " | ".join(parts)


def _lineup_block(players: list[dict], cap_name=None, vice_name=None) -> list[str]:
    """הרכב רב-שורתי, שורה לכל קו עם אמוג'י עמדה; קפטן/סגן מסומנים בשם."""
    by = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for p in players:
        nm = escape(str(p.get("player_name")))
        if cap_name and p.get("player_name") == cap_name:
            nm += " 👑"
        elif vice_name and p.get("player_name") == vice_name:
            nm += " 🅥"
        by.setdefault(p.get("position"), []).append(nm)
    return [f"{_POS_EMOJI[pos]}: " + " · ".join(by[pos])
            for pos in ("GK", "DEF", "MID", "FWD") if by[pos]]


def _append_personal_advice(lines: list[str], advice: dict | None) -> None:
    """מוסיף את בלוק ההמלצות האישיות (לפי my_team.json) לטלגרם."""
    if not (advice and advice.get("available")):
        return
    pos_labels = {"GK": "🧤 שוער", "DEF": "🛡️ הגנה", "MID": "⚙️ קישור", "FWD": "⚔️ חלוץ"}
    lines.append("➖➖➖➖➖➖➖➖➖➖")
    lines.append("<b>💎 פנטזי — המחזור הקרוב</b>")
    stp = advice.get("squad_total_points")
    if stp and stp.get("by_round"):
        br = " · ".join(f"ס{r['round']} {r['points']}" for r in stp["by_round"])
        lines.append(f"📊 תרומת 15 השחקנים שלך לפי מחזור: {br}")
        lines.append(f"<i>(מצטבר {stp['total']} — לא הניקוד שלך באפליקציה: סופר 15 ולא 11, "
                     f"בלי קפטן כפול)</i>")
    lines.append("")

    # 1) ההרכב שלך — רב-שורתי, עם קפטן/סגן מסומנים
    cap = (advice.get("recommended_captain") or {}).get("player_name")
    vice = (advice.get("recommended_vice") or {}).get("player_name")
    head = f"<b>👤 ההרכב שלך</b> · מערך {advice.get('formation', '?')}"
    lines.append(head)
    # קפטן — אם הנוכחי "בוער" (9+ במחזור האחרון), שומרים ולא ממליצים להחליף
    if advice.get("captain_keep") and advice.get("owner_captain"):
        lines.append(f"👑 קפטן: <b>{escape(str(advice['owner_captain']))}</b> — "
                     f"🔥 שמור! (עשה {advice.get('captain_last_points')} נק' במחזור האחרון)")
    else:
        cap_line = f"👑 קפטן: <b>{escape(str(cap or '—'))}</b> · 🅥 סגן: {escape(str(vice or '—'))}"
        if advice.get("captain_change") and advice.get("owner_captain"):
            cap_line += f" <i>(שינוי מ-{escape(str(advice['owner_captain']))})</i>"
        lines.append(cap_line)
    lines.extend(_lineup_block(advice.get("starting_eleven") or [], cap, vice))
    if advice.get("bench"):
        lines.append(f"🪑 ספסל: {_group_lineup(advice['bench'])}")

    # 1b) חילופי ספסל לפי יום-משחק (שעון מקומי) — מה להכניס בכל יום
    dsubs = [d for d in (advice.get("daily_subs") or []) if d.get("swaps")]
    if dsubs:
        lines.append("")
        lines.append("<b>🔄 חילופי ספסל לפי יום-משחק</b> <i>(שעון ישראל)</i>:")
        for d in dsubs:
            day = _he_day(d["date"])
            pairs = " · ".join(f"{escape(s['in'])} ⬅️ {escape(s['out'])}" for s in d["swaps"])
            lines.append(f"📅 {day}: {pairs}")

    # 2) חילופים מומלצים
    recs = advice.get("transfer_recs") or []
    if recs:
        lines.append("")
        hdr = "<b>🔁 חילופים מומלצים</b> <i>(קשה יוצא · קל נכנס)</i>:"
        if advice.get("forced_out"):
            hdr = hdr[:-1] + f" 🔒 {escape(str(advice['forced_out']))}:"
        lines.append(hdr)
        for i, o in enumerate(recs[:3], 1):
            outs = " + ".join(escape(str(p["player_name"])) for p in o["out"])
            ins = " + ".join(
                f"<b>{escape(str(p['player_name']))}</b>"
                f"{(' v'+escape(str(p['opponent']))) if p.get('opponent') else ''}"
                for p in o["in"])
            lines.append(f"  {i}. {outs} ⬅️ {ins}")
        lines.append("<i>לנעילת חילוף: כתוב 'תוציא [שם]'.</i>")
    flags = advice.get("flags") or []
    if flags:
        names = ", ".join(escape(str(f["player_name"])) for f in flags[:4])
        lines.append(f"⚠️ בעייתי בסגל: {names}")

    # 3) כוכבי המחזור — פרימיום בטוח
    stars = advice.get("top_picks") or {}
    if any(stars.get(p) for p in ("GK", "DEF", "MID", "FWD")):
        lines.append("")
        lines.append("<b>⭐ כוכבי המחזור</b> <i>(פרימיום · משחק קל · \"קח אותם\")</i>:")
        for pos in ("GK", "DEF", "MID", "FWD"):
            items = (stars.get(pos) or [])[:2]
            if not items:
                continue
            tags = []
            for d in items:
                opp = d.get("opponent")
                vs = f" <i>מול {escape(str(opp))}</i>" if opp else ""
                own = d.get("ownership")
                ow = f" ({own}%)" if own is not None else ""
                tags.append(f"{escape(str(d['player_name']))}{vs}{ow}")
            lines.append(f"{pos_labels[pos]}: " + " · ".join(tags))

    # 4) דיפרנציאלים — פוטנציאל גבוה, בעלות נמוכה
    diffs = advice.get("differentials") or {}
    thr = getattr(config, "DIFFERENTIAL_MAX_OWNERSHIP", 5.0)
    if any(diffs.get(p) for p in ("GK", "DEF", "MID", "FWD")):
        lines.append("")
        lines.append("<b>🎯 דיפרנציאלים</b> <i>(פוטנציאל גבוה · ⭐ = בעלות &lt;5% → "
                     "scouting bonus +2)</i>:")

        def _diff_tag(d):
            own = d.get("ownership")
            suffix = f" ({own}%)" if own is not None else ""
            star = " ⭐" if d.get("scouting_bonus") else ""
            return f"{escape(str(d['player_name']))}{suffix}{star}"

        for pos in ("GK", "DEF", "MID", "FWD"):
            items = (diffs.get(pos) or [])[:3]
            if not items:
                continue
            lines.append(f"{pos_labels[pos]}: " + " · ".join(_diff_tag(d) for d in items))
    else:
        lines.append(f"<i>🎯 דיפרנציאלים יופיעו כשייכנסו נתוני בעלות.</i>")
    lines.append("")


def _kickoff_label(match: dict) -> str:
    """תווית שעת פתיחה (HH:MM) אם קיימת, אחרת ריק."""
    dt = utils._parse_dt(match.get("kickoff"))
    return dt.strftime("%H:%M") if dt else ""


def _todays_matches(predictions: list[dict], now=None) -> list[dict]:
    """מסנן את המשחקים שמתקיימים היום (לפי kickoff או date), ממוין לפי שעה."""
    now = now or utils.now_local()
    today = now.date()
    out = []
    for p in predictions:
        dt = utils._parse_dt(p.get("kickoff")) or utils._parse_dt(p.get("date"))
        if dt and dt.date() == today:
            out.append(p)
    out.sort(key=lambda m: (utils._parse_dt(m.get("kickoff")) or now))
    return out


def build_telegram_text(predictions: list[dict], fantasy_result: dict,
                        reasons: list[str] | None = None,
                        plan: dict | None = None,
                        advice: dict | None = None) -> str:
    """בונה סיכום עברי קצר בפורמט HTML של טלגרם (תגיות מוגבלות)."""
    today = utils.now_local().strftime("%d/%m/%Y")
    lines = [f"<b>⚽ מונדיאל 2026 — דוח {today}</b>"]
    if reasons:
        lines.append("<i>סיבת עדכון: " + escape(" · ".join(reasons)) + "</i>")
    lines.append("")

    # תזכורת הימורים: בצ'אט מציגים רק את משחקי היום (לא את כל המחזור).
    # הרשימה המלאה נמצאת בדוח ה-HTML המצורף.
    todays = _todays_matches(predictions)
    lines.append("<b>🎯 תזכורת הימורים — משחקי היום</b>")
    if todays:
        for i, p in enumerate(todays, 1):
            home = escape(str(p.get("home_team")))
            away = escape(str(p.get("away_team")))
            o = p.get("outcome_probabilities", {})
            src = p.get("market_sources")
            market_tag = ""
            if src:
                flag = "" if p.get("market_agrees") is not False else " ⚠️שוק חולק"
                market_tag = f" <i>[{src} מקורות{flag}]</i>"
            ko = _kickoff_label(p)
            ko_tag = f" ({ko})" if ko else ""
            pscore = p.get("predicted_score") or p.get("recommended_score", "")
            safe = p.get("recommended_score", "")
            safe_tag = f" <i>(בטוח {escape(safe)})</i>" if safe and safe != pscore else ""
            lines.append(
                f"{i}. <b>{home}</b> מול <b>{away}</b>{ko_tag} → "
                f"<b>{escape(pscore)}</b>{safe_tag} "
                f"({home} {round(o.get('home_win',0)*100)}% · "
                f"ת {round(o.get('draw',0)*100)}% · "
                f"{away} {round(o.get('away_win',0)*100)}% · אמון {p.get('confidence',0)}%)"
                f"{market_tag}"
            )
            cs = p.get("clean_sheet")
            if cs and p.get("expected_goals"):
                eg = p["expected_goals"]
                lines.append(
                    f"   ⚽ שערים צפ' {eg.get('home',0):.1f}–{eg.get('away',0):.1f} "
                    f"(סה״כ {p.get('total_expected_goals',0):.1f}) · "
                    f"🧤 שער נקי {home} {round(cs.get('home',0)*100)}% · "
                    f"{away} {round(cs.get('away',0)*100)}%"
                )
    else:
        lines.append("<i>אין משחקים היום. כל הניחושים המלאים בדוח המצורף 📄</i>")

    lines.append("")
    _append_personal_advice(lines, advice)

    # מעקב דיוק הניחושים שלך מול המודל (אם יש משחקים שהוכרעו)
    try:
        import predictions_log
        acc = predictions_log.format_summary_he()
        if acc:
            lines.append("")
            lines.append(acc)
    except Exception:  # noqa: BLE001
        pass

    text = "\n".join(lines)
    return text[: _TG_LIMIT - 1]


def send_telegram(predictions: list[dict], fantasy_result: dict, html_path=None,
                  reasons: list[str] | None = None,
                  plan: dict | None = None, advice: dict | None = None) -> bool:
    """שולח את הסיכום לטלגרם, ומצרף את הדוח המלא כקובץ HTML."""
    if not config.telegram_enabled():
        log.warning("טלגרם לא מוגדר (חסר token/chat_id) — מדלג")
        return False

    base = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
    text = build_telegram_text(predictions, fantasy_result, reasons, plan, advice)

    ok = True
    try:
        resp = requests.post(
            f"{base}/sendMessage",
            data={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        resp.raise_for_status()
        log.info("הודעת טלגרם נשלחה")
    except Exception as exc:  # noqa: BLE001
        log.error("שליחת הודעת טלגרם נכשלה: %s", exc)
        ok = False

    if html_path:
        try:
            with open(html_path, "rb") as f:
                requests.post(
                    f"{base}/sendDocument",
                    data={"chat_id": config.TELEGRAM_CHAT_ID,
                          "caption": "הדוח המלא (פתח בדפדפן)"},
                    files={"document": ("report.html", f, "text/html")},
                    timeout=60,
                ).raise_for_status()
            log.info("קובץ הדוח נשלח לטלגרם")
        except Exception as exc:  # noqa: BLE001
            log.error("שליחת קובץ הדוח לטלגרם נכשלה: %s", exc)

    return ok
