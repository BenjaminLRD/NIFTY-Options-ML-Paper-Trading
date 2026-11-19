# Results: what the project shows and does not show

## 1. Research (real NSE settlement data, frozen protocol, evaluated once)

Evaluation window 2026-06-30 to 2026-09-28 (64 dates). Brier difference = model minus training-rate prior (negative would be better). Source: `research/V5.1/V5_1_Eval_Brier_Results.csv`.

| Option | Feature / model | Eval rows | Brier prior | Brier model | Difference | 97.5% interval | Entirely below 0? |
|---|---|---|---|---|---|---|---|
| CE | `lr1` / B1 | 902 | 0.23987 | 0.24205 | +0.00218 | [-0.00042, +0.00441] | No |
| CE | `ur1` / B2 | 902 | 0.23987 | 0.24035 | +0.00048 | [-0.00096, +0.00150] | No |
| PE | `lr1` / B1 | 1,034 | 0.24891 | 0.24895 | +0.00004 | [-0.00169, +0.00144] | No |
| PE | `ur1` / B2 | 1,034 | 0.24891 | 0.24892 | +0.00001 | [-0.00078, +0.00059] | No |

**Reading:** no model improved on the prior (hypothesis not supported). The call-side `lr1` relationship was borderline non-homogeneous across eight chronological blocks (p = 0.0235 against a 0.025 threshold); the other three were not. The intervals are about +/-0.002 to +/-0.004 wide, so a small effect cannot be excluded: **no improvement was found, which is not proof that none exists.** Labels compare settlements, not executable prices, so none of this speaks to profitability.

## 2. Application demonstration (invented 2031 data)

A default run (`python run_ml_demo.py`, 40 invented trading days, demonstration model with hand-set parameters):

| Item | Value |
|---|---|
| Decisions | 12 BUY, 8 SELL, 20 NO TRADE |
| Simulated fills | 16 |
| Requests refused by the engine | 4, all `RISK_DAILY_LOSS` (the daily loss budget) |
| Audit of the replay | CONSISTENT, coverage FULL |
| Scoring on the invented series | Brier 0.25976 vs prior 0.29214 (difference -0.03238, 64 probabilities), direction hit rate 0.5469 |

**These numbers are not evidence of skill.** The data is invented (a random walk with option premiums as a fixed linear function of the index), the model's parameters are hand-set, and on 40 days a random walk can produce almost any score. The replay shows that the pipeline works: gating, decisions, fills with spread, slippage and fees, risk refusals, persistence and audit. The research models, by contrast, are barred from trading.

## 3. Engineering checks (automated; run on 2026-11-19)

| Check | Result |
|---|---|
| Application test suite | 904 tests OK, 5 skipped (JavaScript chart tests; Node.js not installed). Run inside a git repository |
| Safety scan (`application/`) | 146 files, 0 findings, 0 skipped (pattern-based; proves nothing about the absence of secrets) |
| Fixture replay | exit 0, 24 events, hash chain valid, accounting consistent (full coverage) |
| Research files | byte-identical to the originals; 14 of 82 protocol-recorded files present here and matching (see `research/reproducibility/VERIFICATION.md`) |

Not done: a manual browser click-through (sell, duplicate submit, restart); those flows are covered by automated tests only. The research tests and the one-shot evaluation were not re-run.

## 4. What is not claimed

Profitability; predictive accuracy or edge; live or real-time trading; broker integration; use of real market data by the application; that the demonstration model is a trained model.
