# V5.0 Historical Data Expansion and Temporal Stability Study

Research and paper trading only. No broker, no orders, no profitability claim. "Direction" here is settlement-to-settlement and uses settle(t), which is unknown at the decision time, so it is **not** an executable return. Settlement prices are not assumed to be executable entry or exit prices.

V4.4 to V4.9 notebooks, scripts, reports and raw files are untouched. V4.9 is the diagnostic baseline (its alignment and leakage checks are reused through the imported V4.7 to V4.9 modules). Only `V5_0_*` files and the V4.7 audit reports that gate the run are written.

## Question (predeclared in `v5_0_stability_study.py`, written to `V5_0_Hypotheses.csv`)
Primary: is the relationship between lagged option settlement returns and the next observed settlement direction stable across chronological periods? CE and PE are analysed separately.
- H1/H2 (main, CE): block-wise rank correlation of `lr1` (lagged option settlement return) and `ur1` (lagged underlying return, secondary hypothesis) with next-day direction is the same in every block of about 30 trading dates (date-permutation heterogeneity test, reject at 0.05/2).
- H3/H4 (secondary, CE): expanding-window walk-forward single-feature logistic against the training-rate prior; pooled paired Brier difference with a date-clustered Bonferroni interval; the last day before each test block is purged so no label crosses a split boundary.
- H5 (exploratory): the same questions for PE. Blocks with fewer than 15 eligible dates are excluded and listed.
Lags and the target are computed on the full series before any eligibility filter. No sign is reversed, no threshold tuned, no feature selected from results. Every period was seen in earlier V4.x experiments, so nothing here is an untouched holdout and every result is exploratory.

## Data: what was retrieved and what was not
- Missing before this run: the PE contract-wise export `OPTIDX_NIFTY_PE_30-Mar-2026_TO_29-Jun-2026.csv` (60 trading dates, 2026-03-30 to 2026-06-29). The NSE Historical Data screen that produced the other `OPTIDX_NIFTY_*` files is not reachable by this pipeline, so that export is still **not acquired**.
- Retrieved on 2026-10-26: 55 daily official F&O bhavcopy (UDiFF) zip files for the missing PE dates, from the public archive `https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip`, one plain request per file with a one-second pause, no login, no cookies, no header tricks. File names, URLs, sizes and SHA-256 are in `V5_0_Download_Log.csv`; the files are in `V5_0_Acquired_Raw/` and are never edited. The other 5 dates were already local files. Check NSE's terms of use before redistributing the files.
- Official listing page: https://www.nseindia.com/all-reports-derivatives (Reports > Derivatives, and Historical Data > Derivatives).
- Still not acquired: contract-wise exports for 2026-06-30 onward (PE and CE) and the 29-Sep-2026 window. Those dates are the only data no earlier experiment has used; `V5_0_Source_Acquisition_Manifest.csv` lists them by priority.
- Coverage gained (`V5_0_Coverage_Change.csv`): PE raw rows 1,631 to 2,651 (120 to 180 dates); PE eligible rows 1,443 to 2,361 (117 to 177 dates). CE is unchanged (2,253 eligible rows, 177 dates). `V5_0_Coverage_Gaps.csv` is empty: no PE trading date inside the CE calendar is missing now.
- Sources differ in origin: PE rows from 2026-03-30 onward come from official bhavcopy, earlier PE rows and all CE rows from contract-wise exports. Where both exist the contract-wise row is used and disagreements are flagged, never overwritten.

## Integrity checks (`V5_0_Schema_Integrity_Checks.csv`, 169 checks: 168 PASS, 1 NOTE, 0 REVIEW)
Required columns, file name vs trade date vs declared window, duplicate contract keys (trade date, symbol, expiry, option type, strike), non-positive settlement, cross-origin agreement for settle, close, volume, open interest and underlying, close kept distinct from settlement, row conservation, raw-file hashes before and after.
The single NOTE: 28 official bhavcopy rows of the study expiry (all PE strike 15000, zero volume, close 0.10) carry settlement 0.00. They are kept unchanged and excluded by the price-floor rule (settlement below 1.0); a study check asserts no eligible row is below the floor. In this run that check was changed from REVIEW to NOTE after the 55 new files exposed it, and the change is limited to that one row.

## Results (exploratory; `V5_0_Heterogeneity_Tests.csv`, `V5_0_Walk_Forward_Pooled.csv`)
- CE lr1 heterogeneity p = 0.0235 (borderline against 0.025); CE ur1 p = 0.028 (not rejected). Block correlations for CE lr1 range from -0.37 to +0.26 over 6 blocks.
- PE (6 blocks, 177 dates, 2,361 rows): lr1 p = 0.306, ur1 p = 0.222; homogeneity not rejected, low power.
- Walk-forward pooled Brier differences against the training-rate prior are positive for every CE and PE feature (no improvement); all Bonferroni intervals include 0.
- Block sizes are about 30 dates, so only large changes are detectable; the permutation test is anti-conservative under serial dependence.
None of this shows an executable strategy. No spreads, fees or slippage are modelled.

## Run in Google Colab (clean runtime)
Colab has **not** been run for V5.0 by the author of this repository; only a local simulation exists (see Tests).
1. Open `ML_Options_Paper_Trading_V5_0_Historical_Data_and_Stability.ipynb` in Colab (Runtime > Run all is fine after step 2).
2. Run cell 1 and upload: the 5 `OPTIDX_NIFTY_*.csv` files, all local `BhavCopy_NSE_FO_0_0_0_*.csv` files, and the 55 `.csv.zip` files from `V5_0_Acquired_Raw` (select all, upload together; Colab keeps them in `/content`).
3. Cells 2 to 9 write the scripts and tests to `/content`. Cell 10 runs the V4.7 audit (gate; stops on unresolved items). Cell 11 runs the V5.0 unit tests (stops on failure). Cell 12 runs the pipeline and downloads `V5_0_Reports.zip`.
4. If a new file is added later, rerun cells 10 to 12; the gate refuses to run on a stale audit.
Local equivalent: `python -m unittest test_v5_0 -v`, then `python v4_7_source_audit.py DATA_DIR DATA_DIR`, then `python v5_0_stability_study.py DATA_DIR OUT_DIR`.

## Tests
- Local, actually run (Python 3.14, pandas 3.0): `test_v5_0` 20 tests OK; `test_v4_8_feature_research` plus `test_v4_9_feature_diagnostics` 53 tests OK.
- Local simulation of the notebook: all code cells executed in an empty folder with the 5 contract-wise files, 13 bhavcopy csv files and 55 bhavcopy zips (audit gate, tests, pipeline) completed without error.
- Colab: not run.

## Status (as of 2026-10-26)
| Item | Status |
|---|---|
| Local unit tests (V5.0 20, V4.8 and V4.9 53; 73 in one run) | passed |
| Local clean-environment reproduction (two independent runs) | passed, reports byte-identical |
| Notebook code cells run in an empty local folder (not Colab) | passed |
| Google Colab run | **not run** |

Clean-environment reproduction: a new virtual environment holding only `numpy==2.5.1` and `pandas==3.0.5` (Python 3.14), and two separate empty folders (runA, runB), each given only the 5 `OPTIDX_NIFTY_*.csv`, the 13 local `BhavCopy_NSE_FO_*.csv`, the 55 acquired zips and the nine `.py` files (`v4_7_*`, `v4_8_*`, `v4_9_*`, `v5_0_*`, three `test_*`). No generated report was copied in. Commands, run in each folder:
```
python -m unittest test_v5_0 test_v4_8_feature_research test_v4_9_feature_diagnostics
python v4_7_source_audit.py . .
python v5_0_stability_study.py . out
python compare_colab_v5_0.py runA/out/V5_0_Reports.zip runB/out/V5_0_Reports.zip
```
Results: 73 tests OK in both runs; the audit reported no items needing review; the two `V5_0_Reports.zip` files and the shipped `V5_0_Reports.zip` have the same SHA-256 (all 18 reports identical, no differences within tolerance).
Provenance check: all 55 files in `V5_0_Acquired_Raw` match the SHA-256 and byte size in `V5_0_Download_Log.csv`, and all 73 rows of `V5_0_Raw_File_Hashes.csv` match the files on disk (the 55 log hashes equal the corresponding rows). Limits: the identical zips show the pipeline is deterministic on one machine and Python version; they do not show it behaves the same on Colab's Python or library versions. The download hashes show the files have not changed since download, not that NSE's copy is unchanged.

## Files
`ML_Options_Paper_Trading_V5_0_Historical_Data_and_Stability.ipynb`, `v5_0_data_expansion.py`, `v5_0_stability_study.py`, `test_v5_0.py`, `README_V5_0.md`, `V5_0_Download_Log.csv`, `V5_0_Acquired_Raw/`, `V5_0_Reports.zip` (18 reports) and the unzipped `V5_0_*` csv files. `V5_0_Reports_prelim_before_download.zip` is the earlier run without the downloaded files, kept for comparison. The earlier notebook `ML_Options_Paper_Trading_V5_0_Data_Expansion_Stability_Study.ipynb` is kept unchanged.
