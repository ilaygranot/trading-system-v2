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
| `qt/screening.py` | EG1/EG2 with the ±99 code, EPS 0 ⇒ −0.1, cleaned group averages + median, rounded-up SG1 gate, universe gates with rule ids, profiles L1–L8 + tags, ranking, short profiles, guidance ≥ 8%, loss-to-profit, R34/J6 conflict flag | S06, decisions 11–13 |

Not yet: the agents and Critic, approval queue, scheduler.

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
