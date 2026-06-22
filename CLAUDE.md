# CLAUDE.md — World Cup 2026 Predictor & Fantasy Bot

Operational guide for this project. Read this first; it explains the architecture,
how to run/deploy, and the non-obvious gotchas.

## ⚠️ Sync discipline — do this EVERY session (mobile, Cowork, local)
This repo is worked on from several places (local, `claude.ai/code` on mobile, Cowork) **and a cloud bot auto-commits `data/*.json` continuously**. To stay in sync:
1. **At the very start of every session, before any work:** `git pull --rebase origin main`. Never edit files (especially `data/db.json`, `data/my_predictions.json`) without pulling first — the cloud commits them often.
2. **After committing any change:** `git push` (and `git pull --rebase` first if the push is rejected). Do this as part of finishing the task, not later.
3. **One live session at a time** on the same files — concurrent edits in two places cause conflicts.
4. **On a rebase conflict in `data/*.json`:** these are generated/append-only — take the incoming version (`git checkout --theirs <file>`) or regenerate, then `git add` + continue. Never hand-merge them.
The user expects sync to "just work" across devices — owning the pull-first / push-last flow is part of every task here, not optional.

## What it does
A Hebrew (RTL) system for the 2026 FIFA World Cup that:
1. **Predicts match results** — 1X2 + exact score + confidence (Poisson model blended with market odds).
2. **Advises FIFA Fantasy** — legal 15-man squad, captain, per-position picks, transfers, relative to the user's real team.
3. **Telegram bot** — the user sends a screenshot (lineup or predictions); the bot reads it (Gemini Vision), replies with advice/comparison, then lets the user **chat freely with Gemini** about the lineup (no structured questions).
4. **Learns** — ingests real results to refine team strength, and tracks the user's prediction accuracy vs the model over the tournament.

Runs fully in the cloud (GitHub Actions) — no PC required.

## Processing the user's KICKOFF prediction screenshots (READ THIS when images are attached)
The user plays a Hebrew football-predictions league in the **KICKOFF** app (mobile-only). They upload screenshots of their predictions; you compare them to the model and track a cumulative "you vs system" score. **When the user attaches KICKOFF screenshots, do this — do NOT use Gemini, read them with your own vision** (Gemini Vision is only for the autonomous cloud bot, and it mis-reads RTL/orientation):
1. Each settled card shows: two team flags+names, the user's prediction badge **"ניחשת X-Y"**, the actual score, and a points badge — **בול = +3 (exact)**, **כיוון = +1 (correct direction)**, **פספוס = 0 (miss)**, **קנס חמור = −1 (reversed)**. The team shown **first / on the right** (RTL) is the home/first team. **Ignore input/edit screenshots** (editable score boxes, no points badge) — only take settled cards.
2. Extract per game: `home`, `away`, `user_home`, `user_away` (in the app's orientation). Names may be Hebrew or English — the helper maps them.
3. Run the helper, which computes the model's pick, records to `predictions_log` (`data/my_predictions.json`), settles against `data/db.json` results, and returns the "you vs system" summary:
   ```python
   import kickoff_predictions as kp
   print(kp.process([{"home": "Egypt", "away": "Belgium", "user_home": 0, "user_away": 2}, ...]))
   ```
   (or CLI: `python kickoff_predictions.py '<json-list>'`). Scoring is KICKOFF: exact +3, direction +1, reversed −1, miss 0 — **no independent goals bonus** (verified: a 4-goal game with wrong direction scored 0 in the app).
4. **Cross-check** your extracted `user_home/away` against the card's points badge — if `match_points(user, actual)` ≠ the app's badge, you read the orientation backwards; flip it.
5. For already-played games the model is **in-sample** (it learned from those results → inflated); the fair comparison is only for predictions saved **before** kickoff. Say so.
6. `git commit` `data/my_predictions.json` when done.

**If the user also uploads the daily reports they received** (Telegram/HTML, with the model's predictions): read them too, extract the **model's predicted score per game**, and pass it as `model_home`/`model_away` in the same `process(...)` call. This is the **fair** comparison — the model's pick as it was *before* the game. Without it, `process` recomputes the model, which for already-played games is **in-sample / inflated** (the model has since learned the result), so always prefer the report's prediction when available.

## Architecture (modules)
- `config.py` — settings; loads `.env`. Key knobs: `REPORT_UPCOMING_COUNT` (=5 matches in the report), `POSITION_PICKS_PER_POS`, `TRANSFER_CANDIDATES_PER_POS` (=2), `MARKET_BLEND_WEIGHT`, `ODDS_REVEAL_HOURS`.
  - `FANTASY_SOURCES` — site/feed names handed to Gemini as grounded-search hints for prices/form/xG (e.g. *Fantasy Football Scout, WhoScored, FBref, Flashscore, Reddit r/FantasyPL, FotMob, #FPL on Twitter/X*). Gemini does not scrape each site — it uses them to steer its Google-grounded search.
  - `ODDS_SOURCES` — bookmaker/model names for the consensus odds query (e.g. *Bet365, Pinnacle, Opta supercomputer, ...*).
- `utils.py` — logging (file is UTF-8), `load_json`/`save_json` (atomic), `_parse_dt`, `safe_get`.
- `scraper.py` — data collection. **Primary fantasy source is the official FIFA World Cup Fantasy feed** (`fetch_official_pool` → public no-auth JSON at `config.FIFA_FANTASY_PLAYERS_URL`/`_SQUADS_URL`, ~1488 players × 48 squads with real price/ownership/form/official points). It's the canonical roster — solves nation- and squad-level validity at the source (no Italy, etc.). `official_differentials` derives differentials from it — nailed starters under the ownership ceiling (`DIFFERENTIAL_MAX_OWNERSHIP`, raised to 15%), **ranked by scoring chance** (official avg points **multiplied by next-fixture ease** — a hard opponent discounts expected output — + form, with low ownership as a bonus via `DIFFERENTIAL_WEIGHTS`, not a hard low gate) so picks are high-upside players who happen to be under-owned, not obscure 1%-owned names. The availability filter excludes only *known* benched/unavailable players (`injury_status`/`suspension_status`/`expected_start is False`), so it still populates between rounds when no lineup is confirmed (`matchStatus` is null for everyone) — availability comes from `status=="playing"`, not lineup confirmation. It takes `fixture_difficulty`, so in `collect()` it runs after results + fixture-difficulty. Gemini grounded-search is now the **fallback/enrichment** layer (xG/xA, odds, results learning). `db['participants']` stores the 48 official squad names; `participating_nations` prefers it so `filter_to_participants` never false-drops a valid team. **Fixtures, results and fixture-difficulty also come from the official feed now** (`fetch_official_rounds` → `rounds.json`): `official_matches` (upcoming, carrying the real `stage` GROUP/R32/…), `official_results` (completed, with penalties + stage), `_record_results` (shared EWMA learner used by both official and the Gemini `ingest_results` fallback), `official_fixture_difficulty` (next-opponent strength: 65% **squad quality** from official player prices via `_team_quality` + 35% results-form, so strong squads like Netherlands read as hard even after one noisy round), and `seed_teams_from_squads` (all 48 teams, preserving learned strength, canonicalising to official names). In `collect()` the official feed is primary and **Gemini is bounded to near-window matches** (`_matches_within_days`) for what only it provides — injuries/expected-lineups (`fetch_match_context`), consensus odds, and `_enrich_fantasy_data` (xG/xA, penalty-takers). The whole pipeline runs to a complete db with Gemini fully disabled. Remaining Gemini-only: Vision (screenshot reading) + free chat (`telegram_intake`), odds, and xG/xA/injury enrichment. Fallbacks still via **Gemini grounded search**: `GeminiClient` (`ask_json`, `ask_json_image` for Vision). Fetches matches/teams/match-context/fantasy pool. `ingest_results()` learns from finished scores (EWMA into team goals → improves **predictions**). `ingest_player_results()` learns from real player performances. It prefers the **official FIFA Fantasy points** (the complex official scoring incl. bonuses, from play.fifa.com) per player; only when that's missing does it fall back to a computed estimate (`_fantasy_points_for`, the basic goals/assists/appearance/clean-sheet formula). The result is EWMA'd into each player's `recent_points` → improves **fantasy** picks; auto-adds standout players missing from the pool (`player_results` rows carry an `official` bool). **Both ingest functions now run inside `collect()` (both branches), after the scrape**, so the daily report keeps learning — they are not gated to the bot's ~5h refresh. `_enrich_fantasy_data()` pulls prices/form/xG from `FANTASY_SOURCES`. `filter_to_participants(db)` (called in both `collect()` branches) drops players whose nation isn't one of the 48 WC participants — `participating_nations(db)` derives the canonical set from `db['teams']` (+ fixtures/results), and `_clean_nation` normalises spelling variants (`_NATION_ALIASES`: e.g. Czech Republic→Czechia, Cape Verde→Cabo Verde) so valid players aren't dropped. This removes e.g. Italy/Ukraine players that Gemini surfaces from general football knowledge. Applies to both `players` and `differentials`; has a safety net (skips if <24 nations known). The pool/differentials prompts also demand official-26-squad membership.
- `fantasy.py` — `expected_points` blends **xG/xA** into the goals/assists per-match rates (50/50 via `_attacking_rates` — xG is a more stable predictor than raw goals, neutralising finishing luck), then blends each player's `recent_points` (actual recent FIFA-points signal from `ingest_player_results`) at 0.6 model / 0.4 actual·start-prob. **Captain/vice are chosen by `ceiling_points` (upside), not mean EP** — a high-variance goal threat is worth more when points are doubled; penalty takers (`penalty_taker` flag) get a goal-rate bump (`config.CAPTAIN_CEILING_WEIGHT`, `PENALTY_TAKER_GOAL_BONUS`).
- `backtest.py` — backtesting harness. Replays `db['results']` through `predictor.predict_match` and scores under KICKOFF (`match_points`) vs two naive baselines (always 1-0 home, always 1-1). `run_backtest(db, scoring)` → metrics (ppg/exact/direction) per variant; `format_report`. `tune(db, scoring, grid)` sweeps a config grid (default `MAX_XG`, `HOME_ADVANTAGE`) via a temporary `_override_config` and reports the best ppg **without mutating config**. Use for A/B before deploying a model change. In-sample (model learned from these results) — compare *relatively* between variants; tune is overfit-prone, apply only robust+sensible values. CLI: `python backtest.py` / `python backtest.py --tune`. (Tuning this way set `HOME_ADVANTAGE=0.10` for the neutral-venue WC → 1.421 ppg vs 1.316.)
- `predictor.py` — Poisson model; `predict_all(db)`; blends consensus odds. **Conditional home advantage** via `_home_advantage_for`: host nations (`config.HOST_NATIONS` = USA/Canada/Mexico) get `HOST_HOME_ADVANTAGE` (0.30) when home; everyone else the neutral `HOME_ADVANTAGE` (0.10). Three scorelines per prediction: **`predicted_score`** (what the daily report shows) = a varied/realistic line — goal count from **conservatively-rounded** xG (`_round_goals`, half-DOWN: rounds up only when the fraction is >0.5, so xG 4.5 → 4 not 5 — no goal-count exaggeration; matches the Poisson mode floor(μ)), direction from the blended 1X2, and a draw when `draw_prob ≥ config.DRAW_PREDICT_THRESHOLD` (=0.25, calibrated to the real ~31% draw rate; the EV pick almost never shows draws because draw is rarely the modal outcome). `recommended_score` **maximises expected points** under `config.PREDICTION_SCORING` (the conservative "safe for points" line, shown as a secondary tag). `most_likely_score` = single most-probable score. The model's total-goals average is well-calibrated (≈3.0/game = reality); `predicted_score` exists because the EV pick collapsed to 1-0/1-1 and the user wanted more varied, higher-scoring, draw-aware predictions. `_realistic_scoreline` builds it. Each prediction also carries `recommended_ep`.
- `odds.py` — consensus odds aggregation across sources.
- `fantasy.py` — squad rules (2 GK/5 DEF/5 MID/3 FWD, max 3/nation, 100M); `score_players`, `build_fantasy`, `estimate_price`, form/availability filtering, budget-reserve greedy pick.
- `advisor.py` — personal advice from `data/my_team.json`. Tolerant name matching (surname + accent-strip via `_make_resolver`/`_squad_identity`). Outputs starting XI, captain, `transfer_options` (per position: weakest out + 2 candidates), `suggest_transfers`, and `differentials` (low-ownership <5%, nailed-on starters, per position — counts `config.DIFFERENTIAL_COUNTS` = 3 GK / 5 DEF / 5 MID / 3 FWD — from `scraper.fetch_differentials` over the full 48-nation pool; `differentials_for_user` excludes squad by name+surname). The report's fantasy section is now **just** personal advice (pitch) + differentials pitch — the generic FIFA-Fantasy/plan blocks were removed.
- `planner.py` — fantasy plan; called with `num_matchdays=1` (upcoming matchday only — keeps the report short).
- `predictions_log.py` — saves the user's predicted scores, settles them against real results, computes hit-rate (outcome + exact) for user **and** model. File: `data/my_predictions.json`.
- `report.py` — jinja2 HTML report + Telegram message (`build_telegram_text`) + email fallback. `_within_days` shows all matches in the next `REPORT_UPCOMING_DAYS` (=5) days; `_pitch_rows` renders the lineup as a formation on a CSS pitch; `_differentials_split` shows differentials as 2-per-position on the pitch + 1-per-position on the bench.
- `state.py` — cadence / change-detection (decides whether to send).
- `telegram_intake.py` — the autonomous bot. `run_bot_once()` (cloud entry via `--bot`), `classify_image` (one Vision call: lineup vs fixtures), `_handle_lineup`, `_handle_fixtures`, `_handle_text` (free Gemini chat about the lineup via `GeminiClient.ask_text`), `_maybe_refresh_model` (gated ~every 5h: pulls **official** results + refreshes the official pool/differentials/fixture-difficulty — **Gemini-independent**, so results keep updating even with no quota; Gemini xG enrichment runs only if enabled; then settles the user's predictions). `_handle_fixtures` settles against the latest results before replying so the cumulative **"you vs system" score** in `format_summary_he` is current the moment a predictions screenshot is uploaded; it also reports how many predictions were read (`read`) and lists any bet games **not found in the system** so a missed/mis-read game is visible. **Durability:** if Gemini Vision is unavailable when an image arrives (quota 429), the bot no longer drops the upload — `_save_pending_image` stores it under `data/pending_images/` and `_process_pending_images` (run at the start of every `run_bot_once`) classifies + handles it once quota returns, so predictions/lineups are never lost across a quota outage. Also `process_incoming` (legacy photo-only).
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
_Auto-updated: 2026-06-22_

**Modules (15):**
- `advisor.py` — יועץ פנטזי אישי — מקבל את הקבוצה האמיתית שלך מ-data/my_team.json ומפיק
- `backtest.py` — Backtesting harness — מודד כמה נקודות KICKOFF המודל היה צובר על תוצאות שכבר
- `config.py` — מרכז ההגדרות של המערכת. טוען משתנים מקובץ .env.
- `fantasy.py` — מנוע FIFA Fantasy — חישוב Expected Points ובחירת הרכב אופטימלי.
- `kickoff_predictions.py` — עיבוד ניחושי KICKOFF — הסוכן (Claude) קורא את הצילומים בראייה שלו (לא Gemini),
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

**Data files:** `bot_state.json`, `db.json`, `db_2.json`, `my_predictions.json`, `my_team.json`, `state.json`, `telegram_offset.json`
**Workflows:** `bot-poll.yml` (`*/15 * * * *`); `daily-report.yml` (dispatch only)
**Tests:** 81
<!-- AUTO:END -->

