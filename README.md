# Quantum Trader · v1

מערכת לתמיכה בהחלטות מסחר swing לפי ה-Playbook של הקורס (שיעורים 059–107). קוד מחשב את המספרים ואוכף את כללי הכסף והסיכון, Agents מפעילים שיקול דעת לפי כרטיסי החלטה, ואילי מאשר כל פעולה שמזיזה כסף.

מסמכי היסוד (בתיקיית הפרויקט): `architecture/v1-architecture.md`, `architecture/decision-cards.md`, `playbook/decisions.md`, `playbook/tiers/*.csv`, `playbook/acceptance-tests.md`.

## What is in v1 so far

| Module | What it does | Playbook |
|---|---|---|
| `qt/policy.yaml` | Every enforced number, with its source | decisions.md א, ד |
| `qt/playbook.py` | Rule tiers from `playbook_snapshot/` (copy of `playbook/tiers`) | tiers/README.md |
| `qt/calc/volatility.py` | ADR60, ATR 20-60 regime with ±0.325% hysteresis, VIX jump (flag only), σ conversions | S11-R24, S12-R24, S12-R25 |
| `qt/calc/trade.py` | k×ADR stop, reward:risk, vertical spreads, single options, sizing, horizon | S10, S11 |
| `qt/calc/portfolio.py` | Gross/net, options caps, sectors, latched 15% portfolio stop, 60/40, winner rebalance | S11, S12 |
| `qt/calc/traffic_light.py` | Seven-source aggregation, high-interval veto, stock/sector/market combine | S09, decision 16, fix 9 |
| `qt/rules/engine.py` | hard ⇒ block · default ⇒ needs override (J id + fact ids) · flags · Ilay approvals · exits never blocked | architecture 3.1–3.3 |
| `qt/rules/checks.py` | The entry/add checks and the daily position review | 41 hard rules + defaults |
| `qt/journal.py` | Append-only decision journal, Ilay reviews, outcomes, deviation hit-rate | architecture 7.1–7.3 |
| `qt/tradelog.py`, `qt/learning.py` | The learning loop. The trade journal: one row per trade in the columns of Ilay's paper sheet and the course (plan, execution, R, MFE/MAE, target reached in the trade or within 20 sessions after the exit, followed the plan?, idea and execution quality apart, loss or mistake, failure stage 1–13, what worked, the lesson). Daily step `learn`: the day's equity, and each closed trade measured and drafted by code, classified by agent L on DC-24 when agents are on, and sent to Ilay as a review item (approve = confirmed, reject with a reason = his correction, then his own lesson by reply). Weekly: every entry proposal measured (target or stop first, 60 days) against what Ilay answered, ideas that ran away (S08-R32, S13-J8), stopped trades whose target came later, re-entries after stops (S13-R6). Monthly: win rate and R vs S13-R18, Sharpe / Sortino / drawdown (S13-J17), agreement per card, deviations per rule, calibration, Critic returns, failure stages, streak warnings, change-proposal candidates (7.4, 10 cases). The journal page `/journal` (signed link from `/journal` in Telegram) with CSV per table | architecture 7, S13-R29, S08-R32, S08-J23, S13-J4 |
| `qt/facts.py` | Fact sheet with id/source/date; catches cited facts that don't exist | principle 4 |
| `qt/data/` | Yahoo (primary), FMP, FRED/ALFRED vintages, SEC EDGAR, Zacks CSV import, cache + point-in-time snapshots | architecture 9 |
| `qt/replay.py` | The lecturer's trades through the engine | research/audit-rerun.md |
| `qt/calc/indicators.py` | Resampling (week/month/quarter/year), mirrored bars for shorts, SMA/EMA, RSI, MACD, ATR, PSAR, Volatility Stop | S09, 080/082 |
| `qt/timing.py` | Six of the seven traffic-light sources from daily bars, high-interval veto, extended price, candidate levels (levels color comes from the agent, `levels_pending` until then) | S09, decision 16 |
| `qt/macro.py` | Stage 1: HY spread stress (MA250, distance, yearly change), 10Y−3M curve, commodities, real rate. Stage 2: 0–10 scoring per indicator, activity scale with zones and grey bands, ISM states, inflation vs 2–3%, labor, separate Fed scale, compass now, stance. Stage 3: bear line, breadth flags, quadrant ⇒ assets | S01–S03, decisions 14, 15, D-S02-9 |
| `qt/data/macro_feed.py` | FRED series with ALFRED vintages; manual monthly file for ISM and NFIB | architecture 9 |
| `qt/sectors.py` | Sector scorecard: RS vs SPY over 5 periods ⇒ rank, expected-growth rank, macro points 0/3/6/9 from the cycle table tilted by the stance, total ⇒ 3 long / 3 short; confirmations as flags (cycle pairs, three horizons, expectation change); industries vs sector, ISM streaks | S04–S05, D-S04-2/3/6 |
| `qt/deepdive.py` | Deep dive numbers: core YoY trend, operating leverage, profit source, acceleration, gap vs sector, health (rules out a long only), FCF from operations, dilution, net debt / EBITDA trend, liquidity, guidance-derived growth, premium needing a written justification, target price from the 3-year multiple (sector fallback, peer regression as info), surprises; the gate to the watchlist (questions 1–3, catalyst ⇒ cold, valuation is never a reason) | S07, D-S07-3/4/6/7/8 |
| `qt/watchlist.py` | Watchlist: main / guidance / event lists, computed fields, day-60 review flag, spread ratio, heat with a written reason and frozen triggers, no direction flip, composition warnings, tradability, catalyst classes A/B/C/D, guidance vs previous midpoint, the early and full idea-to-trade gate | S08, D-S08-1..8 |
| `qt/manage.py` | Daily review of open positions: stop exits, 60/40 trailing stop and option profit take (preapproved standing orders), growing winner trim to 24%, portfolio stop, tactical regime block plus the 084 cut order to below 50%, VIX flag, high net into a shock, adverse catalyst, beta disconnect, daily metrics | S12, D-S12-4/5/6 |
| `qt/db.py`, `qt/runs.py`, `qt/daily.py` | One database layer (Postgres via `QT_DATABASE_URL`, SQLite for tests and local); runs and steps with resume, a lock per run kind, events and agent-call tables; the daily run without agents: fetch (one snapshot per run, provider fallback, stale data blocks entries) → calc (ATR 20-60 regime, VIX) → rules (daily review) → journal → approval queue → notify | runtime plan 3.3, 5 |
| `qt/agents/` | The agents: 23 decision cards as closed-option JSON schemas, the five context parts (role, the stage's Playbook chapters + tiers + decisions, fact sheet, world state, the full card) with the fixed parts cached, models pinned per agent in `models.yaml`, code checks (facts exist, rule ids, deviations need a J principle, never on hard rules) with one retry, the Critic with escalation, refusal fallback, every call and cost in `agent_calls`, daily budget | architecture 5–6, runtime plan 8 |
| `qt/approvals.py`, `qt/telegram.py`, `qt/web.py` | The approval queue: every item valid until the next open, approve / reject (with an optional reason) / postpone one session, all journalled as Ilay's review; expired ⇒ "not reviewed"; approvals become paper orders filled at the next open; standing-order exits fill without waiting; the premarket recheck sends a moved item back as "changed". The Telegram bot (one message per item with buttons, signed details link, answers only Ilay's chat id, "resume run" button, portfolio and monthly macro files by upload) and the web service (webhook, item page, password dashboard with runs, queue, paper orders, cost) | runtime plan 6–7, 9 |
| `qt/schedule.py`, `qt/pipelines.py`, `qt/app.py`, `render.yaml` | New York time with the NYSE holidays; premarket (expire, quotes, recheck), weekly (runs, cost per agent, queue discipline) and monthly (waits for the manual ISM file, FRED, stance into the queue, then the preparation in `qt/monthly.py`) runs; the cron entry point (one firing of each DST pair runs, the other resumes a failure once, alert with a resume button, optional heartbeat); the Render Blueprint (web, four cron jobs, Postgres) | runtime plan 3–5 |
| `qt/watch.py` | The daily watchlist pass (code only): trigger hit, invalidation, time stop, stop and R:R from today's range, potential to target, day 60, earnings within a week, boiling; an opportunity becomes an `enter` item in the queue; the end-of-day summary in Telegram. The list comes from a research run (`watchlist.yaml`, sent to the bot or `QT_WATCHLIST`) | S08, S13-R22/R23 |
| `qt/monthly.py` | The monthly preparation after the stance: A1 on DC-01..04 with the Critic; the month's and quarter's candle of SPY and each sector (shapes and thresholds not from course); sector growth expectations vs a month before (from the weekly research's scorecard); the ISM industries and Short Supply tables from the manual file; the coming month's events (FOMC, FRED release schedule, ISM, earnings and time stops); 15-year seasonality of the month (method not from course); long / short / net exposure vs Ilay's caps (also flagged daily); sentiment (`qt/sentiment.py`): COT from the CFTC's TFF file (asset managers' share of open interest, Smart Money Spread) and NAAIM from the manual file, each smoothed over 4 periods | S13-R25, S04-R27/R29/R34, S05-R6/R9, D-S11-7 |
| `qt/agents/cli_client.py` | The agents on Ilay's Claude subscription: the same request answered by `claude -p` headless (no tools, no settings, the card's JSON schema, the system prompt by file), selected when `CLAUDE_CODE_OAUTH_TOKEN` is set and `ANTHROPIC_API_KEY` is not. Cost is the list price from the token counts, kept so the daily and weekly caps still bound the runs | runtime plan 8 |
| `qt/news.py` | The daily news pass (step `news`, after `watch`): Yahoo headlines from the last 3 days on the positions, then the watchlist; each new one classified once by A5 on DC-14 (class, for or against the idea, priced in; up to 12 a day, not from course: cost). A class A/B headline against an open position, not priced in, goes to A7 (DC-21) in the agents step; against a watchlist idea it is a thesis-breaker flag. The news diary (top 5) is in the end-of-day summary; without agents the headlines are listed raw | S13-R22/R27, S08-R30 |
| `qt/weekly.py` | The end-of-week review in the weekly run (code only): the sector arena (RS of the 11 sectors vs SPY over 1w/1m/3m/6m/1y, rank and its move since last week), old ideas revisited (invalidated / expired => propose archive; day 60, potential halved => review; never removed automatically), the week in the book (fills, realized P&L, positions with the week's move and distance to the stop, the ones that hurt), earnings and time stops in the next 14 days, the 2-new-ideas quota. News, the economic calendar and sentiment have no source yet | S04-R11/R29, S08-R29/R30, S13-R21/R25/R26 |
| `qt/book.py` | The paper book: each paper fill applied to the portfolio (cash, quantity, average entry, closed trades with P&L, every sell with its date as realized P&L), the day's standing stop moves (stops only tighten), the peak equity; saved as the newest portfolio, which the next run loads (a new portfolio file sent to the bot replaces it). With `qt/manage.py`: half at the target as a proposal (S12-R13) | S12-R2, R11, R13 |
| `qt/screening.py` | EG1/EG2 with the ±99 code, EPS 0 ⇒ −0.1, cleaned group averages + median, rounded-up SG1 gate, universe gates with rule ids, profiles L1–L8 + tags, ranking, short profiles, guidance ≥ 8%, loss-to-profit, R34/J6 conflict flag | S06, decisions 11–13 |

Run it: `qt run daily --as-of 2026-10-19 --portfolio examples/portfolio.yaml`, `qt run status`, `qt run resume <run-id>`.

Cloud: `qt run daily|premarket|weekly|monthly --scheduled` (cron), `gunicorn qt.web:application` (web), `qt set-webhook`
(once after the first deploy). See `docs/render-setup.md`.

Research by the agents: `qt research --agents` (instead of `--judgments`) asks A1 (DC-01..04), A3 (DC-09 above 8
candidates), A4 (DC-10..13, with the tell-a-friend lines on DC-13) and A6 (DC-17, 19, 20) with the Critic, and
writes the same judgments a person would (`qt/research/judge.py`); a run has its own budget (`weekly_research_usd`).

The scheduled weekly run ends with that research (`qt/weekly.py`, step `research`): the report is saved and linked in
Telegram (`/report/<id>`, signed), and its ideas are merged into the watchlist the daily pass checks.

Not yet: the course test set for the agents (Opus vs Sonnet), A2 / A8, L drafting the monthly change proposals (code lists the candidates), the shadow run of a new Playbook version (7.4 step 6), the case library (7.5), and writing the journal into the Drive sheet itself (today: the `/journal` page and its CSV files).

## Run

```bash
pip install -e '.[dev]'
pytest                 # acceptance tests + replays (offline, from fixtures)
pytest -m network      # live provider smoke tests (needs FRED_API_KEY, network)
qt check examples/acmr.yaml --journal journal.sqlite
qt macro examples/macro_2023_02.yaml
qt replay
qt rules
```

Updating the Playbook: copy `playbook/tiers/*.csv` into `playbook_snapshot/`, update `policy.yaml` if a number changed, run the tests. The playbook version in every journal entry is a hash of the snapshot.
