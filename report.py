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

  <h2>ניחושי משחקים — {{ window_days }} הימים הקרובים</h2>
  <div class="muted" style="margin-bottom:8px">מוצגים כל המשחקים ב-{{ window_days }} הימים הקרובים. המלצת ההימור נחשפת כ-{{ reveal_hours }} שעות לפני פתיחת כל משחק.</div>
  {% if predictions %}
    {% for p in predictions %}
    <div class="card">
      <div class="match-head">{{ loop.index }}. {{ p.home_team }} מול {{ p.away_team }}
        <span class="muted">· {{ p.stage or '' }}{% if p.date %} · {{ p.date }}{% endif %}</span></div>
      {% if p.odds_locked %}
      <div>🔒 <span class="muted">ההמלצה תיחשף {{ p.reveal_label }}</span></div>
      {% else %}
      <div>המלצה לניקוד: <span class="score">{{ p.recommended_score }}</span>
        {% if p.recommended_ep is defined %}<span class="alts">תוחלת {{ p.recommended_ep }} נק'</span>{% endif %}
        {% if p.most_likely_score and p.most_likely_score != p.recommended_score %}<span class="muted">· הכי סביר {{ p.most_likely_score }}</span>{% endif %}
        <span class="conf conf-{{ p.conf_class }}">אמון {{ p.confidence }}%</span></div>
      <div class="alts">חלופות (לפי תוחלת): {{ p.alternatives | join(', ') }}</div>
      <div class="probs">סיכויים — {{ p.home_team }}: {{ (p.outcome_probabilities.home_win*100)|round|int }}%
        · תיקו: {{ (p.outcome_probabilities.draw*100)|round|int }}%
        · {{ p.away_team }}: {{ (p.outcome_probabilities.away_win*100)|round|int }}%</div>
      <div class="explain">{{ p.explanation }}</div>
      {% endif %}
    </div>
    {% endfor %}
  {% else %}
    <div class="card muted">אין משחקים ב-{{ window_days }} הימים הקרובים.</div>
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
  {% if advice.transfer_options %}
  <div class="card">
    <strong>🔁 מועמדי חילוף לפי עמדה</strong>
    <span class="muted">(לכל עמדה — החלש בסגל מול 2 המועמדים הטובים)</span>
    <table>
      <tr><th>עמדה</th><th>החוצה</th><th>מועמדים להחלפה (EP · רווח)</th></tr>
      {% for opt in advice.transfer_options %}
      <tr>
        <td>{{ opt.position }}</td>
        <td class="risk-high">{{ opt.out.player_name }} <span class="muted">({{ opt.out.expected_points }})</span></td>
        <td>{% for c in opt.candidates %}<span class="cap">{{ c.player_name }}</span> <span class="muted">({{ c.team }}, EP {{ c.expected_points }}, +{{ c.gain }})</span>{% if not loop.last %} · {% endif %}{% endfor %}</td>
      </tr>
      {% endfor %}
    </table>
  </div>
  {% endif %}
  {% set diffs = advice.differentials %}
  {% if diffs and (diffs.GK or diffs.DEF or diffs.MID or diffs.FWD) %}
  <div class="card">
    <strong>🎯 דיפרנציאלים מומלצים לפי עמדה</strong>
    <span class="muted">(בעלות &lt; {{ diff_threshold }}% · מתוך כל מאגר ה-48 נבחרות · שווה לשקול לצרף)</span>
    <table>
      <tr><th>עמדה</th><th>שחקן</th><th>נבחרת</th><th>בעלות</th><th>EP</th></tr>
      {% for pos in ['GK','DEF','MID','FWD'] %}
      {% for d in diffs[pos] %}
      <tr><td>{{ pos }}</td><td>➕ {{ d.player_name }}</td><td>{{ d.team }}</td>
        <td>{% if d.ownership is not none %}{{ d.ownership }}%{% else %}—{% endif %}</td><td>{{ d.expected_points }}</td></tr>
      {% endfor %}
      {% endfor %}
    </table>
  </div>
  {% else %}
  <div class="card muted">🎯 דיפרנציאלים (בעלות &lt; {{ diff_threshold }}%) יופיעו כשייכנסו נתוני בעלות מהאתרים.</div>
  {% endif %}
  {% endif %}

  {% if plan.available %}
  <h2>🏆 FIFA Fantasy — המחזור הקרוב</h2>
  <div class="card">
    <div>סגל קבוע של 15 · <span class="muted">2 שוערים · 5 הגנה · 5 קישור · 3 חלוץ · מקס' 3 לנבחרת</span>
      · עלות: <span class="pill">{{ plan.squad_cost }}M</span></div>
  </div>
  {% for md in plan.matchdays %}
  <div class="card">
    <div class="match-head">📅 מחזור {{ md.matchday }} <span class="muted">· {{ md.date_range }}</span>
      · <span class="pill">{{ md.formation }}</span>
      · 👑 <span class="cap">{{ md.captain.player_name if md.captain else '—' }}</span>
      <span class="muted">(סגן {{ md.vice_captain.player_name if md.vice_captain else '—' }})</span></div>
    <table>
      <tr><th>עמדה</th><th>שחקן</th><th>נבחרת</th><th>EP</th></tr>
      {% for pl in md.lineup %}
      <tr><td>{{ pl.position }}</td><td>{{ pl.player_name }}</td><td>{{ pl.team }}</td><td>{{ pl.expected_points }}</td></tr>
      {% endfor %}
    </table>
    <div class="muted" style="margin-top:8px">🪑 ספסל: {% for b in md.bench %}{{ b.position }} {{ b.player_name }}{% if not loop.last %} · {% endif %}{% endfor %}</div>
  </div>
  {% endfor %}
  {% endif %}

  <h2>FIFA Fantasy — הרכב מומלץ</h2>
  {% if fantasy.available %}
    {% set e = fantasy.starting_eleven %}
    <div class="card">
      <div>מערך: <span class="pill">{{ e.formation }}</span>
        · צפי נקודות (כולל קפטן): <span class="score">{{ e.total_expected_points }}</span>
        · עלות: <span class="pill">{{ e.total_cost }}M</span></div>
      <table>
        <tr><th>עמדה</th><th>שחקן</th><th>נבחרת</th><th>EP</th><th>סיכון</th><th>חלופה</th></tr>
        {% for pl in e.lineup %}
        <tr>
          <td>{{ pl.position }}</td>
          <td>{{ pl.player_name }}
            {% if pl.player_name == e.captain.player_name %}<span class="cap">(C)</span>{% endif %}
            {% if e.vice_captain and pl.player_name == e.vice_captain.player_name %}<span class="vice">(V)</span>{% endif %}
          </td>
          <td>{{ pl.team }}</td>
          <td>{{ pl.expected_points }}</td>
          <td class="risk-{{ pl.minutes_risk }}">{{ pl.minutes_risk }}</td>
          <td class="muted">{% if pl.alternative %}{{ pl.alternative.player_name }}
            <span class="pill">{{ pl.alternative.expected_points }}</span>{% else %}—{% endif %}</td>
        </tr>
        {% endfor %}
      </table>
    </div>

    {% if e.bench %}
    <div class="card">
      <strong>ספסל ({{ e.bench | length }}):</strong>
      <div class="probs">
        {% for b in e.bench %}{{ b.position }} {{ b.player_name }} ({{ b.team }}, EP {{ b.expected_points }}){% if not loop.last %} · {% endif %}{% endfor %}
      </div>
      <div class="muted" style="margin-top:6px;font-size:.85em">סגל מלא: 15 שחקנים · 2 שוערים, 5 הגנה, 5 קישור, 3 חלוץ · מקס' 3 לנבחרת</div>
    </div>
    {% endif %}

    <div class="card">
      <strong>מועמדים להבאה (מחוץ לסגל):</strong>
      <div class="probs">
        {% for t in fantasy.transfers %}{{ t.player_name }} ({{ t.team }}, EP {{ t.expected_points }}){% if not loop.last %} · {% endif %}{% endfor %}
      </div>
    </div>

    <div class="card">
      <strong class="risk-high">שחקנים להימנע מהם:</strong>
      <div class="probs">
        {% for a in fantasy.avoid %}{{ a.player_name }} ({{ a.team }}, {{ a.injury_status }}){% if not loop.last %} · {% endif %}{% endfor %}
      </div>
    </div>
  {% else %}
    <div class="card muted">אין מספיק נתוני שחקנים להמלצת הרכב.</div>
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


def _within_days(predictions: list[dict], days: int, now=None) -> list[dict]:
    """מסנן משחקים לחלון של N הימים הקרובים (מהיום ועד היום+N-1).
    אם לאף משחק אין תאריך תקין — מחזיר הכל (לא חוסם בגלל נתונים חסרים)."""
    now = now or datetime.now()
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
    now = now or datetime.now()
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


def render_html(predictions: list[dict], fantasy_result: dict,
                plan: dict | None = None, advice: dict | None = None) -> str:
    """מרנדר את דוח ה-HTML ושומר אותו ל-output/. מחזיר את ה-HTML."""
    now = datetime.now()
    today = now.date()
    for p in predictions:
        p["conf_class"] = _conf_class(p.get("confidence", 0))
        # בדוח המלא מציגים את כל הניחושים (ללא חסימה); מסמנים מה משוחק היום
        p["odds_locked"] = False
        _dt = utils._parse_dt(p.get("kickoff")) or utils._parse_dt(p.get("date"))
        p["is_today"] = bool(_dt and _dt.date() == today)
        p["reveal_label"] = _kickoff_label(p) or (p.get("date") or "")

    # מציגים בדוח את כל המשחקים ב-N הימים הקרובים
    window_days = config.REPORT_UPCOMING_DAYS
    window = _within_days(predictions, window_days, now)

    # ההרכב האישי כמערך על המגרש
    advice_pitch = None
    if advice and advice.get("available"):
        advice_pitch = _pitch_rows(advice.get("starting_eleven"))

    html = _TEMPLATE.render(
        predictions=window,
        fantasy=fantasy_result,
        plan=plan or {},
        advice=advice or {},
        advice_pitch=advice_pitch,
        diff_threshold=config.DIFFERENTIAL_MAX_OWNERSHIP,
        reveal_hours=config.ODDS_REVEAL_HOURS,
        window_days=window_days,
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
    msg["Subject"] = f"⚽ מונדיאל 2026 — דוח {datetime.now():%d/%m/%Y}"
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


def _group_lineup(players: list[dict]) -> str:
    """שורה אחת מקובצת לפי עמדה: GK | DEF | MID | FWD."""
    by = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for p in players:
        by.setdefault(p.get("position"), []).append(escape(str(p.get("player_name"))))
    parts = [", ".join(by[pos]) for pos in ("GK", "DEF", "MID", "FWD") if by[pos]]
    return " | ".join(parts)


def _append_personal_advice(lines: list[str], advice: dict | None) -> None:
    """מוסיף את בלוק ההמלצות האישיות (לפי my_team.json) לטלגרם."""
    if not (advice and advice.get("available")):
        return
    lines.append("<b>👤 הקבוצה שלך — המלצות אישיות</b>")
    cap = (advice.get("recommended_captain") or {}).get("player_name", "—")
    vice = (advice.get("recommended_vice") or {}).get("player_name", "—")
    line = f"מערך {advice['formation']} · 👑 קפטן מומלץ: <b>{escape(str(cap))}</b> · סגן: {escape(str(vice))}"
    if advice.get("captain_change") and advice.get("owner_captain"):
        line += f" <i>(שינוי מ-{escape(str(advice['owner_captain']))})</i>"
    lines.append(line)
    lines.append(f"הרכב: {_group_lineup(advice['starting_eleven'])}")
    if advice.get("bench"):
        lines.append(f"🪑 ספסל: {_group_lineup(advice['bench'])}")

    options = advice.get("transfer_options") or []
    if options:
        lines.append("<b>🔁 מועמדי חילוף לפי עמדה</b> (החלש בסגל ← 2 מועמדים):")
        labels = {"GK": "שוער", "DEF": "הגנה", "MID": "קישור", "FWD": "חלוץ"}
        for opt in options:
            outp = escape(str(opt["out"]["player_name"]))
            cands = " · ".join(
                f"<b>{escape(str(c['player_name']))}</b> (+{c['gain']})"
                for c in opt["candidates"]
            )
            lines.append(f"{labels.get(opt['position'], opt['position'])}: "
                         f"החוצה {outp} ← {cands}")
    flags = advice.get("flags") or []
    if flags:
        names = ", ".join(escape(str(f["player_name"])) for f in flags[:4])
        lines.append(f"⚠️ בעייתי בסגל שלך: {names}")

    diffs = advice.get("differentials") or {}
    thr = getattr(config, "DIFFERENTIAL_MAX_OWNERSHIP", 5.0)
    pos_labels = {"GK": "שוער", "DEF": "הגנה", "MID": "קישור", "FWD": "חלוץ"}
    if any(diffs.get(p) for p in ("GK", "DEF", "MID", "FWD")):
        lines.append(f"<b>🎯 דיפרנציאלים לפי עמדה</b> (בעלות &lt; {thr}% — שווה לצרף):")
        for pos in ("GK", "DEF", "MID", "FWD"):
            items = diffs.get(pos) or []
            if not items:
                continue
            parts = []
            for d in items:
                own = d.get("ownership")
                own_tag = f" {own}%" if own is not None else ""
                parts.append(f"{escape(str(d['player_name']))}{own_tag}")
            lines.append(f"{pos_labels[pos]}: " + " · ".join(parts))
    else:
        lines.append(f"<i>🎯 דיפרנציאלים (בעלות &lt; {thr}%) יופיעו כשייכנסו נתוני בעלות מהאתרים.</i>")
    lines.append("")


def _kickoff_label(match: dict) -> str:
    """תווית שעת פתיחה (HH:MM) אם קיימת, אחרת ריק."""
    dt = utils._parse_dt(match.get("kickoff"))
    return dt.strftime("%H:%M") if dt else ""


def _todays_matches(predictions: list[dict], now=None) -> list[dict]:
    """מסנן את המשחקים שמתקיימים היום (לפי kickoff או date), ממוין לפי שעה."""
    now = now or datetime.now()
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
    today = datetime.now().strftime("%d/%m/%Y")
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
            ep = p.get("recommended_ep")
            ep_tag = f" <i>(תוחלת {ep} נק')</i>" if ep is not None else ""
            lines.append(
                f"{i}. <b>{home}</b> מול <b>{away}</b>{ko_tag} → "
                f"<b>{escape(p.get('recommended_score',''))}</b>{ep_tag} "
                f"({home} {round(o.get('home_win',0)*100)}% · "
                f"ת {round(o.get('draw',0)*100)}% · "
                f"{away} {round(o.get('away_win',0)*100)}% · אמון {p.get('confidence',0)}%)"
                f"{market_tag}"
            )
    else:
        lines.append("<i>אין משחקים היום. כל הניחושים המלאים בדוח המצורף 📄</i>")

    lines.append("")
    _append_personal_advice(lines, advice)
    if plan and plan.get("available") and plan.get("matchdays"):
        # רק המחזור הקרוב (לא תוכנית רב-מחזורית) — דוח קצר
        md = plan["matchdays"][0]
        cap = (md.get("captain") or {}).get("player_name", "—")
        vice = (md.get("vice_captain") or {}).get("player_name", "—")
        rng = md.get("date_range") or ""
        lines.append(f"<b>🏆 FIFA Fantasy — המחזור הקרוב</b> "
                     f"<i>(15 שחקנים · {plan.get('squad_cost')}M)</i>")
        lines.append(
            f"📅 {escape(rng)} · מערך {md['formation']} · "
            f"👑 <b>{escape(str(cap))}</b> (סגן {escape(str(vice))})"
        )
        lines.append(f"הרכב: {_group_lineup(md['lineup'])}")
        avoid = ", ".join(escape(str(a["player_name"]))
                          for a in fantasy_result.get("avoid", [])[:3])
        if avoid:
            lines.append(f"⚠️ להימנע: {avoid}")
    elif fantasy_result.get("available"):
        e = fantasy_result["starting_eleven"]
        cap = e.get("captain", {}).get("player_name", "—")
        vice = (e.get("vice_captain") or {}).get("player_name", "—")
        lines.append("<b>🏆 FIFA Fantasy</b>")
        lines.append(f"מערך {e['formation']} · צפי {e['total_expected_points']} נק'")
        lines.append(f"👑 קפטן: <b>{escape(str(cap))}</b> · סגן: {escape(str(vice))}")
        lines.append(f"הרכב: {_group_lineup(e['lineup'])}")
        if e.get("bench"):
            lines.append(f"🪑 ספסל: {_group_lineup(e['bench'])}")
    else:
        lines.append("<b>🏆 FIFA Fantasy</b>")
        lines.append("אין מספיק נתונים להמלצת הרכב.")

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
