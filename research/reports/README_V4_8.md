# V4.8 Controlled Feature Research

Research and paper trading only. No broker connection, no orders, no profitability claim.

## What it does
Tests whether four **predeclared** feature models beat a training-rate prior on held-out dates, for CE and PE separately:

| id | model |
|----|-------|
| B0 | training-set event-rate prior (clipped to 0.01–0.99) |
| B1 | logistic (L2, λ=1, fixed) on `ln(settle[t-1]/settle[t-2])` |
| B2 | logistic on underlying return `u[t-1]/u[t-2]-1` |
| B3 | train-fitted rates per signed-moneyness bucket (CE: ln(S/K); PE: ln(K/S); >0 = in the money for both) |
| B4 | logistic on B1+B2+signed log-moneyness, only if minority-class events / variables ≥ 10 |

Decision time is the start of day *t*; every feature is an end-of-day value from *t-1* or earlier. The label is `settle[t+1] > settle[t]` for the same contract on the next consecutive source trading day (ties count as not-up). Because `settle[t]` is unknown at decision time, **the label is not a tradable return**.

Selection (validation only): a model is "selected" if the Bonferroni-adjusted (8 comparisons) train+eval **refit** 95% interval for its paired Brier difference vs B0 lies wholly below 0. Test results are reported, labelled exploratory, and never used to choose anything.

## Inputs (all in `/content` on Colab)
- the five `OPTIDX_NIFTY_*.csv` source files
- every available `BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv` (or `.csv.zip`)
- `v4_7_source_audit.py` and `v4_7_model_dataset.py` (unchanged V4.7 scripts; the notebook writes them)

## Run in Google Colab
1. Open `ML_Options_Paper_Trading_V4_8_Controlled_Feature_Research.ipynb` in Colab.
2. Run cell 1 and upload the source and bhavcopy files.
3. Run cells 2–5 (they write the scripts).
4. Run cell 6 **RUN V4.7 AUDIT**, cell 7 **RUN TESTS**, cell 8 **RUN V4.8 PIPELINE**. Cell 8 downloads `V4_8_Reports.zip`.

Any failed assertion, failed test, unresolved audit REVIEW check, changed/unknown input file or stale audit report stops the run with an error (fail closed).

Local: `python -m unittest test_v4_8_feature_research -v` then `python v4_8_feature_research.py DATA_DIR OUT_DIR` (DATA_DIR must hold the inputs and fresh V4.7 audit reports).

## Outputs (`V4_8_*`)
`Dataset`, `Dataset_Eligibility`, `Split_Integrity`, `Feature_Definitions`, `Baseline_Comparison`, `Uncertainty_Intervals`, `Calibration`, `Moneyness_Bucket_Rates`, `Validation_Checks`, and `Reports.zip` (all of the above).

## Limitations
- Intervals resample whole dates (and, for the `refit` method, training dates with refitting). They do **not** capture serial dependence between neighbouring dates, strike/contract selection, regime change, data errors, or earlier use of the same period.
- Test window (2026-05-07 onward) overlaps data used in earlier experiments; PE has no eligible test rows (PE source ends 2026-03-25) and none are fabricated.
- Only ~8% of source rows have a locally available official bhavcopy; the rest are unverified.
- Official volume/transactions are not evidence of executable prices; there is no bid/ask, fee or slippage model.
- Eight predeclared validation comparisons are Bonferroni-adjusted; the table of all results is still exploratory.
- Tests were run locally (Python 3.14, pandas 3.0). Not run on Colab's pandas/numpy versions by the author of this repo.
