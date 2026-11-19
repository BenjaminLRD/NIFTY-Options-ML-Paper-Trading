# ML methodology

Source of truth: `research/V5.1/V5_1_Final_Research_Report.md` (and `V5_1_Frozen_Protocol.json`). This page summarises it; if they differ, the report wins.

## Research question
Is the relationship between a lagged option-settlement return (`lr1`) or a lagged index return (`ur1`) and the next settlement direction stable over time, and do models built on them beat a training-rate prior?

- **Label:** `target_next_up` = 1 if the next consecutive trading day's settlement is above the settlement on day t, for the same contract. It is a research target, not a tradable return (settlement is not an executable price).
- **Features:** `lr1` = ln(settle[t-1] / settle[t-2]); `ur1` = underlying[t-1] / underlying[t-2] - 1; computed on the full series before eligibility filtering; eligibility requires settlement >= 1.0 at t, t-1 and t-2.
- **Models:** B0 = training-rate prior (constant, CE 0.4421, PE 0.4795); B1 = single-feature logistic regression on `lr1`; B2 = single-feature logistic regression on `ur1` (winsorised at the training 1st/99th percentile, standardised, L2). CE and PE are modelled separately.
- **Scope:** NIFTY index options, expiry 2026-09-29, a fixed grid of 22 strikes (18 have rows in the evaluation window), observation period 2025-10-01 to 2026-09-29.

## Evaluation design (frozen before the evaluation window was examined)
- Fit once on data whose feature date and label date are both on or before 2026-06-29 (labels crossing the boundary purged); no refit, tuning or sign reversal.
- Evaluate once on 2026-06-30 to 2026-09-28 (64 dates; CE 902 rows, PE 1,034 rows).
- Metric: pooled Brier difference (model minus prior); moving-block bootstrap over dates (block of 5, 2,000 resamples, seed 20260630) with a Bonferroni 97.5% percentile interval; a model counts as better only if the interval lies entirely below 0.
- Integrity: SHA-256 hashes of 82 files recorded in the protocol (code, inputs, raw files); combined hash `6c63883d…a4993d` supplied externally and verified before and after the single run.

## How the application uses this
- `application/src/niftypaper/ml/features.py` re-implements `ur1` and `lr1` on point-in-time data (the two newest visible values; missing, stale, gapped or below-price-floor input gives no value and a reason code).
- `ml/models.py` registers four models, each with a **standing**: `b0_training_prior`, `b1_lr1_logistic`, `b2_ur1_logistic` have standing **NONE** (research models: display only, never tradable); `demo_synthetic_ur1` has **DEMONSTRATION_ONLY** (hand-set parameters, fitted to nothing, restricted to invented data).
- B1 and B2 produce no probability because their fitted parameters were never saved. A parameter file could be supplied by the owner, but producing it means running the research code on NSE data, whose permitted use is unresolved, so it was not done.
- `ml/strategy.py`: flat → BUY the higher-scored side if its prediction is eligible and at least 0.55; holding → SELL if the held contract's eligible probability is below 0.50; otherwise NO TRADE. Thresholds are placeholders, not tuned on any data. Every decision, including NO TRADE, is stored with its reason.
- The V5.1 result is shown on the dashboard as a copied record, not recomputed.

## What was not done
No retraining, tuning or re-evaluation anywhere in this repository; no use of the V5.1 window for any modelling; no use of NSE data by the application.
