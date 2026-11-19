# NIFTY Options ML Paper-Trading Platform

A local, **simulation-only** platform that connects a prediction layer to a risk-controlled paper-trading engine and a browser dashboard, built around the discipline of *not fooling yourself*: strict point-in-time data access, fail-closed data checks, hash-chained audit logs, and an honest record of what the underlying research did and did not show.

> **No real orders are placed. No broker is connected. No real market data or credentials are used. No profitability is claimed.**
> The demo runs on **invented 2031 data**. The one model that can trade in the demo has **hand-set parameters and is not a validated ML model**. The earlier research (V4–V5.1) found **no model better than the baseline**, and the platform says so on screen.

**Development note.** This repository was developed with substantial assistance from an AI coding assistant (Claude Code). The owner's own contribution statement is not recorded here.

**Screenshots** of the running application (main dashboard, ML replay page, error state) are in `../screenshots/` (project root, next to this `application/` folder). They sit outside `application/` on purpose: the application's safety scan fails closed on binary files, so images are kept out of the scanned tree.

## Problem and purpose

Retail option-trading ML projects usually fail quietly: look-ahead leakage, fills at prices nobody could get, untracked risk, and results that cannot be reproduced. This project is the *infrastructure* for testing an options-prediction idea honestly before any money is involved:

1. a research record on real NSE index-option settlement data (kept in the project's `../research/` folder, frozen) that asks whether a simple lagged-return signal is stable, and reports the answer, which was **no measurable edge**;
2. a paper-trading engine with spreads, slippage, fees, lot sizing and a daily loss budget, which only ever sees verified prices;
3. an ML integration layer (features → prediction → decision → paper execution) that is chronologically correct and leak-tested, demonstrated on invented data because permitted real data is not available (NSE's terms bar simulation use without written permission; see `docs/PROVIDER_FEASIBILITY.md`).

## What it demonstrates

| Area | What is implemented (and tested) |
|---|---|
| ML integration | Prediction interface (`predict()` → probability, provenance, eligibility), point-in-time features (`ur1`, `lr1`), model registry with *standing* (research models can be shown but never trade), decision layer (BUY / SELL / NO TRADE with recorded explanation) |
| Paper trading | Fills at the touch plus slippage, illustrative costs, whole-lot sizing, 2-lot / 3-position / 10 % exposure limits, 2 % worst-case daily loss budget, append-only saved account that survives restart, unpriced positions never given invented prices |
| Data discipline | Replay runner accepts only labelled synthetic fixtures; `available_at` gate; manifest hashes; exclusion ledger; hash-chained event log with independent audit (CONSISTENT / FULL) |
| Dashboard | Main page (market status, chart marked SIMULATED HISTORY, order form, cash, positions, risk budget, activity) and an ML page (data-kind status, predictions with explanations, risk limits and refusals, performance, portfolio history, trades, assumptions, limitations) |
| Safety | Standard library only, loopback-only server, no order or broker code (checked by tests and a scanner), provider data can never exceed `UNVERIFIED` without evidence, duplicate-submit protection |
| Reliability | 904 automated tests; reproducible replays (same inputs → identical hashes); interrupted runs leave no half result; saved runs are never overwritten |

## Quick start

Requirements: **Python 3.12+**. Nothing to install (no third-party packages).

```powershell
git clone <this repository>      # or copy the folder
cd NIFTY_PaperTrading_V6
python run_ml_demo.py
```

Open **http://127.0.0.1:8765/ml** (main dashboard: **http://127.0.0.1:8765/**). Stop with Ctrl+C. The first run replays 40 invented trading days and saves them to `data/ml_replay/run1`; later runs show the newest replay unchanged (`--new` makes another, `--list` lists them). Full walkthrough: [`docs/ML_DEMO_GUIDE.md`](docs/ML_DEMO_GUIDE.md).

Other ways to start:

```powershell
python run_dashboard.py                       # the normal dashboard only (Synthetic Mode, no ML page data needed): http://127.0.0.1:8765/
python tools/replay_fixture_demo.py           # fixture replay (see below)
```

**Fixture-replay tool.** `python tools/replay_fixture_demo.py` runs a fixed, scripted replay on the **invented, labelled fixture data** in `tests/fixtures/historical_synthetic/` (no real market data, no network, no credentials). It publishes a hash-chained event log under `data/replay_demo/run1` (never overwriting) and audits it; only if every check passes does it print a success summary. Details and limits: `docs/HISTORICAL_REPLAY_AUDIT.md`.

Run the tests:

```powershell
python -B -W error::ResourceWarning -m unittest discover -s tests -t .
```

Safety scan (pattern check for secrets and order-placement code):

```powershell
$env:PYTHONPATH = "src"; python -B -m niftypaper.safety.scan --root . --include-untracked
```

## Architecture (summary)

```
invented fixtures ─► ingest + point-in-time gate ─► features ─► predict() ─► decision layer
                                                                               │ BUY / SELL / NO TRADE + explanation
              dashboard (/ , /ml, /api/*) ◄─ summary.json, decisions.jsonl     ▼
                          ▲                                    intent ─► bridge ─► paper engine (risk limits, costs)
                          └──────────── saved account / ledger ◄───────────────┘   (append-only, hash-chained log)
```

Diagrams and data flow for each path (ML prediction, decision and execution, persistence, dashboard): [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

Stack: Python 3.12+ standard library only (`decimal`, `dataclasses`, `http.server`, `hashlib`, `json`, `unittest`); a small local JavaScript file draws the chart; no framework, no database server (persistence is append-only JSON Lines).

## ML methodology and honest results

* **Research question (V4–V5.1, frozen, in `../research/`):** is the relationship between a lagged option-settlement return (`lr1`) or lagged index return (`ur1`) and the next settlement direction stable over time? Two single-feature logistic models (B1, B2) were compared with a training-rate prior (B0) by Brier score, with chronological splits, purged labels and a frozen, hash-verified protocol for the final one-shot evaluation.
* **Result:** on the untouched 2026-06-30 → 2026-09-28 window every Brier difference against the prior was positive or about zero and every interval included 0 (**not supported**). No predictive edge was found and none is claimed. The numbers are shown in the ML page's research record.
* **In this platform:** B0 is registered as a baseline; B1/B2 have no saved fitted parameters and cannot trade; the demo model (`demo_synthetic_ur1`) is a hand-set rule for exercising the pipeline on invented data. Replay "performance" on invented data (shown as such) demonstrates that scoring works, not that any model has skill.
* **Why synthetic data:** NSE's terms of use bar using its data for virtual trading or simulation unless NSE agrees in writing, and no data provider is approved. Details: [`docs/PROVIDER_FEASIBILITY.md`](docs/PROVIDER_FEASIBILITY.md), [`docs/ML_INTEGRATION.md`](docs/ML_INTEGRATION.md).

## Simulator assumptions

Fills at the touch plus one tick; illustrative fees (₹20 + 0.05 % of turnover); whole lots of 65 units (an invented size, not a verified exchange lot); no partial fills or shorting; quotes at most 5 s old; limits of 2 lots per trade, 3 positions, 10 % exposure and a 2 % worst-case daily loss budget. Full list: [`docs/PAPER_ACCOUNTING.md`](docs/PAPER_ACCOUNTING.md).

## Limitations

* Invented data only; nothing here is evidence about any market or any model's profitability.
* Research models have no demonstrated edge; B1/B2 parameters were never saved.
* One decision a day, one expiry, one strike per side; no stop rule other than the model's exit.
* Lot size and tick size are not verified against an exchange circular.
* Real data would need written permission from the data source, a documented quote timestamp and a new data-origin class in the replay runner (the runner is deliberately fixture-only).
* Five JavaScript chart tests are skipped when Node.js is not installed.

## Future work (not started)

Written permission and an approved data source; a real-data origin class with its own ingest tests; exporting B1/B2 parameters under that permission; a pre-registered forward evaluation before any model may trade; intraday features.

## Repository map

| Path | Contents |
|---|---|
| `run_ml_demo.py`, `run_dashboard.py` | Entry points (ML demo; main dashboard) |
| `src/niftypaper/ml/` | Features, models, `predict()`, decision layer, fixtures, replay session, run listing |
| `src/niftypaper/paper/` | Paper engine, risk limits, append-only store |
| `src/niftypaper/historical/`, `strategy/`, `evaluation/` | Point-in-time gate, replay runner, audit, baseline strategy, evaluator |
| `src/niftypaper/dashboard/` | Pages, local server, chart |
| `src/niftypaper/providers/`, `eligibility.py` | Fail-closed quote eligibility and a read-only provider adapter (never contacted; no approved provider) |
| `tests/` | 904 `unittest` tests |
| `docs/` | Architecture, demo guide, accounting, safety, provider feasibility, milestone records, final checklist |

The earlier, long-form README (providers, replay audit tooling, operational detail) is preserved in [`docs/REFERENCE_DETAILED.md`](docs/REFERENCE_DETAILED.md). The submission checklist is [`docs/FINAL_SUBMISSION_CHECKLIST.md`](docs/FINAL_SUBMISSION_CHECKLIST.md); resume and interview notes are in [`docs/RESUME_AND_INTERVIEW.md`](docs/RESUME_AND_INTERVIEW.md).

## License

No license has been chosen yet (all rights reserved by default). Add one before publishing publicly.
