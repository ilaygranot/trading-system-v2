# Quantum Trader · v1

מערכת לתמיכה בהחלטות מסחר swing לפי ה-Playbook של הקורס (שיעורים 059–107). קוד מחשב את המספרים ואוכף את כללי הכסף והסיכון, Agents מפעילים שיקול דעת לפי כרטיסי החלטה, ואילי מאשר כל פעולה שמזיזה כסף.

מסמכי היסוד (בתיקיית הפרויקט): `architecture/v1-architecture.md`, `architecture/decision-cards.md`, `playbook/decisions.md`, `playbook/tiers/*.csv`, `playbook/acceptance-tests.md`.

## What is in this first slice

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

Not yet: indicators behind the traffic light (PSAR, Volatility Stop, ...), screening and growth profiles (S06), macro scoring (S01–S03), the agents and Critic, approval queue, scheduler.

## Run

```bash
pip install -e '.[dev]'
pytest                 # acceptance tests + replays (offline, from fixtures)
pytest -m network      # live provider smoke tests (needs FRED_API_KEY, network)
qt check examples/acmr.yaml --journal journal.sqlite
qt replay
qt rules
```

Updating the Playbook: copy `playbook/tiers/*.csv` into `playbook_snapshot/`, update `policy.yaml` if a number changed, run the tests. The playbook version in every journal entry is a hash of the snapshot.
