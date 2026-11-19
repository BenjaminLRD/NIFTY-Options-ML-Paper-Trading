# V4.9 Feature Diagnostics

Diagnostic only (paper research). It explains *why* the V4.8 lagged-return models failed on CE validation. It changes no target, timing or feature convention, tunes nothing, never uses the exploratory test split for a performance statistic, and makes no skill or profitability claim.

## What it does
1. **Temporal-alignment audit.** Rebuilds `lr1`, `ur1`, signed moneyness, the next-day target, splits and purging from the raw `OPTIDX_NIFTY_*.csv` files with an *independent* dictionary-lookup implementation and compares every row with `V4_8_Dataset.csv`. Also: hand-worked example rows, leakage perturbations (future prices; same-day price), a lead/lag positive control (date-level option up-rate vs underlying return over [t+k, t+k+1], must peak at k=0), shuffled-input check, preprocessing parity, reproduction of the V4.8 validation Brier scores.
2. **Feature diagnostics (train + validation, CE and PE separately).** Distributions and outliers, rank correlations with date-clustered intervals, event rates in fixed pre-declared bins, calibration (in-sample vs validation) and calibration slopes, date-level performance, loss concentration, a train-vs-validation sign-reversal test (diagnostic only).
3. **Moneyness.** Hand-worked CE/PE formulas, bucket counts and rates by type and split, train-only refit check, and how much of the CE B3 improvement depends on single dates, strikes or buckets.
4. **Ranked conclusions** (`V4_9_Diagnostic_Conclusions.csv`) separating implementation bugs from statistical findings and absence of evidence.

## Inputs (all in `/content`)
- the five `OPTIDX_NIFTY_*.csv` source files
- the V4.8 reports (`V4_8_Dataset.csv`, `V4_8_Baseline_Comparison.csv`, `V4_8_Moneyness_Bucket_Rates.csv`; the notebook unzips `V4_8_Reports*.zip`)

## Run in Google Colab
1. Open `ML_Options_Paper_Trading_V4_9_Feature_Diagnostics.ipynb`.
2. Cell 1: upload the 5 source CSVs and the V4.8 reports ZIP. Cell 2: unzip.
3. Cells 3–8: write the scripts. Cell 9 **RUN TESTS** (stops on failure). Cell 10 **RUN V4.9 DIAGNOSTICS** (downloads `V4_9_Reports.zip`).

Local: `python -m unittest test_v4_9_feature_diagnostics -v` then `python v4_9_feature_diagnostics.py DATA_DIR OUT_DIR`.

## Outputs (`V4_9_*`)
`Temporal_Alignment_Checks`, `Hand_Worked_Examples`, `Alignment_Lead_Lag_Control`, `Preprocessing_Parity`, `Feature_Distributions`, `Feature_Target_Relationships`, `Sign_Reversal_Test`, `Binned_Event_Rates`, `Model_Metrics`, `Calibration`, `Calibration_Slopes`, `Date_Level_Performance`, `Loss_Concentration`, `Underlying_Autocorrelation`, `Moneyness_Examples`, `Moneyness_Bucket_Counts`, `Moneyness_Train_Only_Check`, `B3_Driver_Analysis`, `V48_Reproduction`, `Eligibility_Correction_Effect`, `Diagnostic_Conclusions`, and `Reports.zip`.

## Limitations
- Intervals resample whole dates (rank correlations use full-sample ranks inside the bootstrap). They ignore serial dependence between neighbouring dates, contract selection, regime change and data errors. The effective sample is the number of dates (CE: 105 train, 35 validation; PE validation: 11).
- Findings are descriptive; multiple diagnostics are not multiplicity-adjusted. The sign-reversal result is borderline.
- The label compares settle(t+1) with settle(t); settle(t) is unknown at the decision time, so it is not a tradable return.
- Only ~8% of source rows have a local official bhavcopy; no bid/ask, fees or slippage.
- Local tests ran on Python 3.14 / pandas 3.0; Colab was not used by the author of these scripts.
