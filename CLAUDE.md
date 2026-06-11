# CLAUDE.md — World Cup 2026 Predictor & Fantasy Bot

Operational guide for this project. Read this first; it explains the architecture,
how to run/deploy, and the non-obvious gotchas.

## What it does
A Hebrew (RTL) system for the 2026 FIFA World Cup that:
1. **Predicts match results** — 1X2 + exact score + confidence (Poisson model blended with market odds).
2. **Advises FIFA Fantasy** — legal 15-man squad, captain, per-position picks, transfers, relative to the user's real team.
3. **Telegram bot** — the user sends a screenshot (lineup or predictions); the bot reads it (Gemini Vision), replies with advice/comparison, then lets the user **chat freely with Gemini** about the lineup (no structured questions).
4. **Learns** — ingests real results to refine team strength, and tracks the user's prediction accuracy vs the model over the tournament.

Runs fully in the cloud (GitHub Actions) — no PC required.

## Architecture (modules)
- `config.py` — settings; loads `.env`. Key knobs: `REPORT_UPCOMING_COUNT` (=5 matches in the report), `POSITION_PICKS_PER_POS`, `TRANSFER_CANDIDATES_PER_POS` (=2), `MARKET_BLEND_WEIGHT`, `ODDS_REVEAL_HOURS`.
  - `FANTASY_SOURCES` — site/feed names handed to Gemini as grounded-search hints for prices/form/xG (e.g. *Fantasy Football Scout, WhoScored, FBref, Flashscore, Reddit r/FantasyPL, FotMob, #FPL on Twitter/X*). Gemini does not scrape each site — it uses them to steer its Google-grounded search.
  - `ODDS_SOURCES` — bookmaker/model names for the consensus odds query (e.g. *Bet365, Pinnacle, Opta supercomputer, ...*).
- `utils.py` — logging (file is UTF-8), `load_json`/`save_json` (atomic), `_parse_dt`, `safe_get`.
- `scraper.py` — data collection via **Gemini grounded search**. `GeminiClient` (`ask_json`, `ask_json_image` for Vision). Fetches matches/teams/match-context/fantasy pool. `ingest_results()` learns from finished scores (EWMA into team goals). `_enrich_fantasy_data()` pulls prices/form/xG from `FANTASY_SOURCES`.
- `predictor.py` — Poisson model; `predict_all(db)`; blends consensus odds.
- `odds.py` — consensus odds aggregation across sources.
- `fantasy.py` — squad rules (2 GK/5 DEF/5 MID/3 FWD, max 3/nation, 100M); `score_players`, `build_fantasy`, `estimate_price`, form/availability filtering, budget-reserve greedy pick.
- `advisor.py` — personal advice from `data/my_team.json`. Tolerant name matching (surname + accent-strip via `_make_resolver`/`_squad_identity`). Outputs starting XI, captain, `position_picks`, `transfer_options` (per position: weakest out + 2 candidates), `suggest_transfers`.
- `planner.py` — fantasy plan; called with `num_matchdays=1` (upcoming matchday only — keeps the report short).
- `predictions_log.py` — saves the user's predicted scores, settles them against real results, computes hit-rate (outcome + exact) for user **and** model. File: `data/my_predictions.json`.
- `report.py` — jinja2 HTML report + Telegram message (`build_telegram_text`) + email fallback. `_upcoming` shows the next `REPORT_UPCOMING_COUNT` matches; `_pitch_rows` renders the lineup as a formation on a CSS pitch.
- `state.py` — cadence / change-detection (decides whether to send).
- `telegram_intake.py` — the autonomous bot. `run_bot_once()` (cloud entry via `--bot`), `classify_image` (one Vision call: lineup vs fixtures), `_handle_lineup`, `_handle_fixtures`, `_handle_text` (free Gemini chat about the lineup via `GeminiClient.ask_text`), `_maybe_refresh_model` (gated ~every 5h: ingest results + enrich + settle predictions). Also `process_incoming` (legacy photo-only).
- `main.py` — pipeline entry: `run()` → intake → scrape → predict → fantasy → plan → advice → report → telegram.

## Data files (`data/`, committed & cloud-synced)
- `db.json` — matches, teams, players, results.
- `my_team.json` — user's fantasy squad (from screenshots).
- `my_predictions.json` — prediction-accuracy tracking.
- `bot_state.json` — conversation state + `last_refresh`.
- `telegram_offset.json` — Telegram `getUpdates` offset.
- `state.json` — cadence snapshot.

## Deployment (cloud, no PC)
- GitHub repo: **`FogelYotam/world-cup-2026`** (public → unlimited Actions minutes).
- `.github/workflows/daily-report.yml` — `08:00 UTC` (= 11:00 Israel summer), runs `python main.py --days 3`.
- `.github/workflows/bot-poll.yml` — `workflow_dispatch` + a `*/15` schedule backup; runs `python telegram_intake.py --bot`.
- **Reliable trigger:** a **Google Apps Script** (account `yotamfogel@gmail.com`) calls the `bot-poll.yml` dispatch endpoint every ~10 min using a fine-grained PAT (Actions: read/write, **expires ~Sept 2026**). This is the real driver — GitHub's own cron is throttled and unreliable.
- **Secrets** (GitHub repo → Settings → Secrets → Actions): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GEMINI_API_KEY`. Same values live locally in `.env` (git-ignored).
- Each workflow commits `data/*.json` back to the repo (`[skip ci]`) so the model "remembers" between ephemeral runs.

## Setup (first time)
- **Python 3.11** with a virtual env: `python -m venv .venv`.
- Activate it (`.venv\Scripts\activate` on Windows) then `pip install -r requirements.txt`.
- Copy `.env.example` to `.env` and fill `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GEMINI_API_KEY` (never commit `.env`).

## Common operations
- **Tests:** with the venv active, `python -m pytest -q` (47 tests). On Windows without activating: `.venv/Scripts/python.exe -m pytest -q`.
- **Run locally:** `python main.py [--days N] [--no-send] [--no-scrape] [--force]` (or `run.bat`).
- **Run bot once:** `python telegram_intake.py --bot`.
- **Windows env (Hebrew):** prefix `PYTHONUTF8=1 PYTHONIOENCODING=utf-8`.
- **Deploy:** commit + push to `main`; pull `--rebase` first (cloud auto-commits data). Cloud picks up automatically.
- **Add a fantasy source:** edit `FANTASY_SOURCES` in `config.py`.
- **Change schedule:** edit the cron in the workflow and/or the Apps Script trigger interval.
- **Check cloud runs:** `https://github.com/FogelYotam/world-cup-2026/actions` or the REST API `/actions/runs`.

## Gotchas (important)
- **Gemini free-tier daily quota (429):** image reading and refresh need quota; refresh is gated to ~every 5h. Quota resets daily. Code degrades gracefully (never throws).
- **GitHub schedule cron is unreliable** for frequent intervals — that's why the external Apps Script trigger exists.
- **Name matching:** screenshots give short surnames; `db` has full names. `advisor` normalizes surname + strips accents — preserve this when touching matching.
- **UTF-8 on Windows PowerShell:** always set `PYTHONUTF8=1`; write files with `encoding="utf-8"`.
- **NEVER commit `.env`** (it holds the secrets). It is git-ignored — keep it that way. Do not echo secret values.
- **`data/*.json` is public** (squad visible) but contains **no secrets** — tokens/keys are only in `.env` + encrypted GitHub Secrets + the user's Apps Script.
- **PAT expiry (~Sept 2026):** when the Apps Script trigger stops, regenerate the GitHub fine-grained token and update it in the script.
- **Local Windows scheduled tasks are disabled** — the cloud is the single source of truth (avoid double-processing Telegram updates).

## Conventions
- Comments and logs are in Hebrew; the report is Hebrew RTL.
- Bot/scraper code paths must never raise — log and continue.
- Before publishing anything outward, run the test suite and confirm `.env` is not staged.

## Keeping this file current
- **When you change modules, deployment, or behaviour, update the relevant prose above** — this is part of the change, not optional.
- The factual inventory below is **auto-generated** from the code by `python docgen.py` (it runs on every cloud "Daily Report"). Do not hand-edit between the `AUTO:BEGIN`/`AUTO:END` markers.

## Project inventory (auto-generated)
<!-- AUTO:BEGIN — generated by `python docgen.py`; do not edit by hand -->
_Auto-updated: 2026-06-11_

**Modules (13):**
- `advisor.py` — יועץ פנטזי אישי — מקבל את הקבוצה האמיתית שלך מ-data/my_team.json ומפיק
- `config.py` — מרכז ההגדרות של המערכת. טוען משתנים מקובץ .env.
- `fantasy.py` — מנוע FIFA Fantasy — חישוב Expected Points ובחירת הרכב אופטימלי.
- `main.py` — נקודת הכניסה — מריץ את כל הצינור: איסוף → חיזוי → פנטזי → דוח → מייל.
- `odds.py` — שקלול אודדס מאתרי הימורים — ממיר אודדס דצימליים מ-10 מקורות נפוצים
- `planner.py` — מתכנן פנטזי רב-מחזורי. גוזר את מחזורי שלב הבתים (מחזור 1 מהנתונים,
- `predictions_log.py` — מעקב ניחושי המשתמש לאורך הטורניר: שומר את הניחושים שנשלחו בצילום,
- `predictor.py` — מנוע חיזוי לניחושי 365 — מבוסס מודל פואסון.
- `report.py` — הפקת דוח HTML בעברית (RTL) ושליחתו במייל.
- `scraper.py` — איסוף נתונים ממקורות חינמיים והרכבתם לסכמה אחידה ב-data/db.json.
- `state.py` — זיהוי שינויים וקצב עדכונים.
- `telegram_intake.py` — קליטת צילומי הרכב FIFA Fantasy דרך בוט הטלגרם הקיים.
- `utils.py` — כלי עזר משותפים: לוגים, קריאה/כתיבה של JSON, ובקשות HTTP בטוחות.

**Data files:** `bot_state.json`, `db.json`, `my_team.json`, `state.json`, `telegram_offset.json`
**Workflows:** `bot-poll.yml` (`*/15 * * * *`); `daily-report.yml` (dispatch only)
**Tests:** 48
<!-- AUTO:END -->

