# V5.1 Acquisition and Contamination Audit (evaluation NOT yet run)

Research and paper trading only. No broker, no orders, no profitability claim. Settlement-to-settlement direction is not an executable return. V4.4 to V5.0 artifacts are unchanged; this stage only adds `V5_1_*` files, `V5_1_Acquired_Raw/`, `v5_1_*.py`, `test_v5_1.py` and this file.

## Verdict
A qualifying window exists: **trade dates 2026-06-30 to 2026-09-29** (the remaining life of the NIFTY expiry 2026-09-29 that V5.0 studied). No new-window outcome (return, direction, label, prediction or score) has been computed or viewed. The evaluation has not been run and must not be run until the frozen protocol below is reviewed.

What "untouched" means here, and what it does not:
- Every local dataset, report, notebook and script was checked. The latest **trade** date found anywhere is 2026-06-29 (`V5_1_Contamination_Audit.csv`; 0 artifacts use a trade date on or after 2026-06-30). Strings such as 2026-09-29 in older files are contract expiry dates, not trade dates, and do not count.
- No file with a later date exists elsewhere on the machine (searched user profile, temp folders and recent-file links).
- This proves only that no local artifact used these dates. It cannot prove that nobody looked at NIFTY option prices for these dates elsewhere. If you did, say so and treat the window accordingly.
- The contracts are not new: the expiry-2026-09-29 contracts and their strikes were part of V5.0 training. The window is a later period for known contracts, so it tests temporal stability for them, not generalisation to unseen contracts.
- Label boundary: a training row dated 2026-06-29 has its label on 2026-06-30, so it is purged from training.
- Dates after 2026-09-29 carry other expiries and are out of scope.

## Data acquired
- Source: public official NSE archive `https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip`, one plain request per weekday from 2026-06-30 to 2026-09-29, 0.8 s pause, no login, cookies or header tricks.
- Result (`V5_1_Download_Log.csv`: URL, retrieval time in UTC, HTTP status, size, SHA-256): 65 files downloaded (HTTP 200); 2026-09-14 returned HTTP 404 (a weekday; probably an NSE holiday, which must be confirmed on NSE's holiday list; no value is invented for it).
- All 65 hashes and sizes on disk match the log. Raw files are never edited; the normalized dataset is `V5_1_Normalized_Bhavcopy_NIFTY_Options.csv` (108,536 NIFTY index-option rows, 65 dates).
- **Not acquired: contract-wise CE/PE exports.** They come from the interactive NSE page Historical Data > Derivatives. This pipeline does not automate that page (it needs a browser session, and automating it could breach the site's terms). Manual steps: open https://www.nseindia.com and go to Historical Data > Derivatives (menu names may change), choose Index Options, symbol NIFTY, expiry 29-Sep-2026, option type CE and then PE, date range 30-Jun-2026 to 29-Sep-2026 (the existing exports each cover about 3 months, so use the same window size if the page rejects a longer range), download the CSV, and save it as `OPTIDX_NIFTY_CE_30-Jun-2026_TO_29-Sep-2026.csv` and the PE equivalent. Check NSE's terms of use first. The daily bhavcopy contains the same fields, so the evaluation can proceed without them; the contract-wise files would be used only as a cross-check.
- The same fields are used for CE and PE, from one origin, so CE/PE comparisons are consistent.

## Structural validation (`V5_1_Raw_Integrity_Report.csv`; counts only, no outcomes)
0 REVIEW items. Checked: hash and size against the log, required columns, file name date equals the trade date in the file, one date per file, unparseable date, expiry or strike, option type, duplicate contract-date keys (date, expiry, option type, strike), missing settlement or close, negatives, high below low, dates outside the window, weekends, expiry before trade date, one underlying per date, traded rows with zero transactions.
NOTE items (kept unchanged, never repaired): 406 rows with settlement 0 or lower; 50,305 zero-volume rows of 108,536; 10,022 traded rows where close differs from settlement (the two stay separate columns); 514 settlements identical to the previous date on consecutive traded dates (possible stale values, informational); the one missing weekday; 12,626 study-expiry rows outside the frozen strike grid (ignored by the protocol).
Coverage of the frozen 18-strike grid, expiry 2026-09-29 (`V5_1_Study_Window_Coverage.csv`, `V5_1_Coverage_By_Date.csv`): CE 1,162 rows over 65 dates; PE 1,164 rows over 65 dates. 130 date/option-type pairs list fewer than all 18 strikes (not every grid strike trades or is listed on every date); this is reported, not filled.
Eligible rows and eligible dates are not reported yet, because eligibility rules use settlement prices and the count would be computed during evaluation.

## Frozen protocol (`V5_1_Frozen_Protocol.json`)
Frozen on 2026-10-26 before any evaluation-window outcome was computed. Combined SHA-256: `6c63883d1f9055a451d16b33eb1e02614b4c801267973027f22e233d60a4993d`. Keep this value outside the project: a verifier cannot protect itself, and anyone who edits `v5_1_freeze_protocol.py` and freezes again produces a new, self-consistent hash.
The record holds the protocol text and the SHA-256 of 82 files: the evaluation script `v5_1_evaluation.py`, every module it imports (`v4_7_model_dataset`, `v4_7_source_audit`, `v4_8_feature_research`, `v4_9_feature_diagnostics`, `v5_0_data_expansion`, `v5_0_stability_study`, `v5_1_acquisition_audit`), the verifier itself, `test_v5_0.py`, `test_v5_1.py`, the V5.0 hypothesis table, training data and strike grid, the download log, the integrity report, the normalized new-window file and all 65 raw ZIPs.
`verify(project, expected_combined)` recomputes every file hash and the combined hash itself, and raises on any changed, missing, added or removed manifest file, on a required file absent from the record, on protocol text that differs from the code or the record, on a stored hash that does not match the stored parts, on raw ZIPs that do not match the download log, and on an `expected_combined` mismatch. It only reads bytes and file names; it imports no data library and never parses the data.
Protocol summary: V5.0 features (lr1, ur1), label, eligibility rules and price floor unchanged; B1 (lr1) and B2 (ur1) logistic models fitted once on data whose labels end on or before 2026-06-29; pooled Brier difference against the training-rate prior; CE and PE separately; moving-block bootstrap over evaluation dates (block 5, 2,000 resamples, seed 20260630) with a Bonferroni 97.5% interval; supported only if the interval lies entirely below 0; V5.0 date-permutation heterogeneity test over training blocks 0 to 5 plus evaluation blocks of 30 dates (remainder merged into the last). Rows and distinct dates are reported in every table.
The evaluation script exists but **has not been run**. It refuses to start unless `verify()` passes against the hash you pass with `--expected-combined`, and it refuses a second run. It was exercised only on fabricated random-walk data in a temporary folder. A crash on the real data before any output would be fixed and re-frozen; once outputs exist the window cannot be reused as an independent test after any change.
2026-09-14 (no file, HTTP 404) is an NSE holiday according to several broker and finance sites; the NSE circular itself was not read. The protocol treats the dates present in the data as the trading calendar.

## Commands
```
python -m unittest test_v5_1 test_v5_0 test_v4_8_feature_research test_v4_9_feature_diagnostics
python v5_1_acquisition_audit.py PROJECT_DIR
python v5_1_freeze_protocol.py PROJECT_DIR --verify --expected-combined 6c63883d1f9055a451d16b33eb1e02614b4c801267973027f22e233d60a4993d
python v5_1_evaluation.py PROJECT_DIR --expected-combined 6c63883d1f9055a451d16b33eb1e02614b4c801267973027f22e233d60a4993d   # NOT run yet
```

## Status
| Item | Status |
|---|---|
| Local unit tests (V5.1 37, V5.0 20, V4.8 and V4.9 53; 110 in one run) | passed |
| Raw acquisition and hash check | done for the daily bhavcopy; contract-wise exports not acquired |
| Structural validation | done, 0 REVIEW |
| Protocol freeze and verifier | frozen, verifier passes, covers the evaluation implementation |
| Evaluation on the new window | **not run** |
| Google Colab | **not run** |

## Limitations
- Contamination is established from local files only.
- Contract-wise exports for the window are missing; the daily bhavcopy is the single origin.
- 2026-09-14 is an NSE holiday per secondary sources only.
- Settlement identical on consecutive dates is only counted, not judged.
- Blocks of about 30 dates detect only large changes; the bootstrap and permutation tests are approximations under serial dependence.
