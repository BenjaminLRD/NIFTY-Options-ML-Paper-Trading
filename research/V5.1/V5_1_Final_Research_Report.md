# NIFTY Options Paper-Trading Research: Final Report (V4.9, V5.0 and the V5.1 one-shot evaluation)

Research and paper trading only. No broker connection, no orders, no live trading. All outcomes are settlement-to-settlement direction labels. Settlement is not an executable price, and no spread, fee, slippage or fill model is included. **Nothing in this report is evidence of profitability.**

Conventions used below: **Observed** = a number or fact read from a report file. **Interpretation** = a reading of those numbers. **Limitation** = what the observation cannot support.

---

## 1. Research objective and scope

**Question.** Is the relationship between lagged option settlement return (`lr1` = ln(settle[t-1]/settle[t-2])) and the next observed settlement direction stable across chronological periods? A secondary question asks the same of the lagged underlying return (`ur1` = underlying[t-1]/underlying[t-2] - 1). Two single-feature logistic models (B1 on `lr1`, B2 on `ur1`) were compared with a training-rate prior (B0) using the Brier score.

**Label.** `target_next_up` = 1 if the next consecutive trading day's settlement is above the settlement on day t, for the same contract. Features use information up to the close of t-1. Settlement(t) is not known at the decision time, so the label is a research target, not a tradable return.

**Scope.** NIFTY index options, expiry 2026-09-29, a fixed grid of 22 strikes per option type (fixed in the V5.0 universe; only 18 of them have rows in the V5.1 window), CE and PE reported separately. Observation period 2025-10-01 to 2026-09-29. The study concerns one expiry and its strikes. It says nothing about other expiries, other underlyings or unseen contracts.

**Version map.**

| Stage | What it did |
|---|---|
| V4.4 to V4.6 | Earlier modelling and a leakage audit (not re-reported here; left unchanged). |
| V4.7 | Source-data audit against official NSE bhavcopy; extreme-movement and liquidity classification; first modelling dataset. |
| V4.8 | Controlled feature research (B0 to B4); five V4.7 defects fixed in V4.8 only. |
| V4.9 | Diagnosis of why `lr1`/`ur1` failed on CE validation. |
| V5.0 | Data expansion (PE coverage gap) and a temporal-stability study. |
| V5.1 | Acquisition and contamination audit of a new window, protocol freeze, one-shot evaluation. |

---

## 2. Data sources, coverage expansion and unresolved provenance gaps

### 2.1 Sources
1. **Contract-wise exports** (`OPTIDX_NIFTY_*.csv`), five files, 4,042 rows, 180 dates: CE and PE from 2025-10-01 to 2025-12-26 and 2025-12-30 to 2026-03-27, plus CE from 2026-03-30 to 2026-06-29. These came from NSE's interactive Historical Data screen (by filename pattern).
2. **Official NSE daily F&O bhavcopy (UDiFF)**, `BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv[.zip]`:
   - 13 files already local (2026-02-23, 02-24, 03-12, 03-17, 03-20, 03-23, 03-25, 03-27, 06-01 to 06-05).
   - 55 files downloaded in V5.0 for the remaining PE dates between 2026-03-30 and 2026-06-29 (`V5_0_Acquired_Raw/`, `V5_0_Download_Log.csv`).
   - 65 files downloaded in V5.1 for 2026-06-30 to 2026-09-29 (`V5_1_Acquired_Raw/`, `V5_1_Download_Log.csv`; HTTP 200 for 65, HTTP 404 for 2026-09-14).
   - Downloads used plain requests to `https://nsearchives.nseindia.com/content/fo/...`, one request per file with a pause, no login, cookies or header tricks. Check NSE's terms of use before redistributing these files.

### 2.2 Coverage expansion (observed)
- **PE contract-wise coverage ended 2026-03-27.** The whole `OPTIDX_NIFTY_PE_30-Mar-2026_TO_29-Jun-2026.csv` window (60 trading dates) was missing and was never acquired as a contract-wise export.
- **V5.0** filled those 60 PE dates from official bhavcopy (`V5_0_Coverage_Change.csv`): PE raw rows 1,631 to 2,651 (120 to 180 dates); PE eligible rows 1,443 to 2,361 (117 to 177 dates). CE is unchanged: 2,411 raw rows (180 dates), 2,253 eligible rows (177 dates). `V5_0_Coverage_Gaps.csv` is empty.
- **V5.1** added a window containing no earlier-used trade date: 65 trading dates, 108,536 NIFTY index-option rows in the normalized file, of which the frozen strike grid gives CE 1,162 rows and PE 1,164 rows over 65 dates (`V5_1_Study_Window_Coverage.csv`).

### 2.3 Unresolved provenance gaps
1. **No contract-wise PE export for 2026-03-30 to 2026-06-29, and none for CE or PE for 2026-06-30 onward.** Those periods rest on a single origin (daily bhavcopy). Cross-origin agreement was checked only where both origins overlap (321 contract-dates in the first 13 days: zero disagreements on settlement, close, volume, open interest and underlying). No independent second source exists for the PE April to June period or for the V5.1 window.
2. **2026-09-14 returned HTTP 404.** It is assumed to be an NSE holiday on the strength of secondary (broker and finance) sources only. The NSE circular was not read. The calendar is "the dates present in the data".
3. **Hashes prove local integrity, not agreement with NSE.** SHA-256 values in the download logs show the files have not changed since download. They do not show NSE's archive is unchanged, and NSE publishes no checksum that was used.
4. **The V5.1 freeze does not cover every upstream raw file.** The 82-file frozen manifest hashes the V5.0 study dataset, universe and hypothesis table (derived files) and the V5.1 raw ZIPs, but not the V4 contract-wise CSVs, the 13 local bhavcopy files or the 55 V5.0 ZIPs. Their hashes are in `V5_0_Raw_File_Hashes.csv` and `V5_0_Download_Log.csv`; on the day of this report all 73 rows of the former matched the files on disk. That is a read-only check, not part of the freeze.
5. **The original "64 extreme movements" list from V4.7 was never recovered.** The pipeline used a documented threshold (more than 100% change between consecutive observations: 39 movements) instead.
6. **Local-only contamination evidence** (see section 5).
7. **Post-hoc classification change in V5.0.** One integrity check (non-positive settlement in the study expiry) was changed from REVIEW to NOTE after the downloaded files exposed 28 such rows. Those rows are kept unchanged and are excluded by the price floor in any case.

---

## 3. V4.9 diagnostic findings (baseline for later stages)

V4.9 diagnosed why the V4.8 `lr1` (B1) and `ur1` (B2) models were worse than the prior on CE validation. It changed no target, timing or feature convention, and it used only train and validation data. The user reported that V4.9 was run in Google Colab with 26 tests passed and zero issues in the alignment and leakage checks. I did not witness that run. The 21 report members in `V4_9_Reports.zip` and `V4_9_Reports (1).zip` are identical in content (the two ZIP files differ in bytes).

**Observed**
- **Alignment: no defect.** An independent dictionary-lookup rebuild from the raw files matched every row of the V4.8 dataset for `lr1`, `ur1`, signed moneyness, target, target date and split labels. Perturbing future prices, or the decision day's price, moved no past feature. A positive control found the CE date-level up-rate correlated +0.73 with the underlying move from t to t+1 (PE: -0.78); every other lag had absolute correlation at most 0.26.
- **One minor V4.8 defect (fixed in V4.9 only).** V4.8 eligibility required the lag-2 return, which no model used, so 40 rows were wrongly excluded. After the fix, validation Brier scores moved by at most 0.0024.
- **Relationship change between periods (CE).** Spearman correlation of `ur1` with next-day direction: -0.132 in training (105 dates) and +0.175 in validation (35 dates); change +0.307, date-clustered 95% interval +0.00 to +0.61 (lower bound within 0.02 of 0). For `lr1`: -0.101 to +0.182, change +0.283, interval +0.01 to +0.54. Each individual 95% interval included 0.
- **Probability scale.** The calibration slope of B2 on CE validation was -0.80 (interval -2.01 to +0.26). The standard deviation of `ur1` roughly doubled from training to validation (0.0069 to 0.0142), so predictions spread wider (0.074 to 0.123).
- **Not supported:** extreme observations (13 B1 rows outside training bounds; excluding them left the excess loss at +0.0125 against +0.0144), loss concentrated in a few dates (the 5 worst carry 37% of the loss on worse dates), and the pattern being plain NIFTY return autocorrelation (the signs disagree and the intervals include 0).
- **Moneyness (B3) on CE validation:** Brier difference -0.0026, better on 22 of 35 dates, surviving removal of any one date or strike; V4.8 had not selected it (its Bonferroni refit interval included 0).

**Interpretation.** The best-supported reading is a weak training relationship that did not hold in validation, amplified by a volatility regime shift. The data also cannot exclude "no measurable signal in either period".

**Limitations.** The reversal result is borderline. The intervals resample whole dates and ignore serial dependence. The V4.9 diagnostics were not corrected for multiple testing.

---

## 4. V5.0 temporal-stability findings

Predeclared in `v5_0_stability_study.py` and written to `V5_0_Hypotheses.csv`: blocks of 30 trading dates; blocks with fewer than 15 eligible dates excluded; main tests are date-permutation heterogeneity tests of the block-wise rank correlation (reject at 0.05/2 = 0.025); secondary tests are expanding-window walk-forward logistic models against the prior, with the last day before each test block purged. Lags and the target were computed on the full series before any eligibility filter. No sign was reversed and no feature selected from results. Every period had been seen in earlier experiments, so V5.0 results are exploratory.

**Observed (`V5_0_Heterogeneity_Tests.csv`, `V5_0_Walk_Forward_Pooled.csv`)**

| | feature | blocks | rows | dates | block rho range | p |
|---|---|---|---|---|---|---|
| CE | lr1 | 6 | 2,253 | 177 | -0.373 to +0.259 | 0.0235 |
| CE | ur1 | 6 | 2,253 | 177 | -0.411 to +0.305 | 0.0280 |
| PE | lr1 | 6 | 2,361 | 177 | -0.165 to +0.226 | 0.3058 |
| PE | ur1 | 6 | 2,361 | 177 | -0.263 to +0.263 | 0.2219 |

- Walk-forward pooled Brier difference against the prior (4 test blocks, 119 dates each type): CE lr1 +0.00218, CE ur1 +0.00792, PE lr1 +0.00108, PE ur1 +0.00422. All are positive (no improvement) and all Bonferroni 97.5% intervals include 0.
- Integrity checks: 169, with 168 PASS and 1 NOTE (28 study-expiry rows with settlement 0.00, positive close, kept unchanged and excluded by the price floor).
- Tests: the V5.0 suite passed locally. Colab was not run for V5.0.

**Interpretation.** CE `lr1` heterogeneity is just below the 0.025 threshold; CE `ur1` is just above it. The two features are strongly related, so the different verdicts reflect threshold fragility more than a real difference. PE shows no evidence of instability, but it has less power in its blocks.

**Limitations.** A block of about 30 dates detects only large changes. The permutation test assumes dates are exchangeable across blocks and is anti-conservative under serial dependence.

---

## 5. V5.1 frozen protocol, hash verification, contamination audit and execution record

### 5.1 Contamination audit (before freezing)
`V5_1_Contamination_Audit.csv` lists 90 local artifacts (data, reports, notebooks and scripts). **Observed:** none uses a trade date on or after 2026-06-30; the latest trade date found anywhere is 2026-06-29. Strings such as 2026-09-29 elsewhere are expiry dates. **Limitation:** this shows only that no local artifact used these dates. It cannot show that nobody viewed NIFTY option prices for this period elsewhere, and the contracts themselves are not new (the same expiry and strikes were in V5.0 training). So the window tests temporal stability for known contracts, not generalisation to unseen contracts.

### 5.2 Frozen protocol
`V5_1_Frozen_Protocol.json` holds the protocol text and SHA-256 hashes of 82 files: the evaluation script, every module it imports, the verifier, two test files, the V5.0 hypothesis table, training data and strike grid, the download log, the integrity report, the normalized new-window file and all 65 raw ZIPs.

Protocol summary: B1 and B2 fitted once on training data whose feature date and label date are both on or before 2026-06-29 (labels crossing the boundary purged); no refit, tuning or sign reversal; training-rate prior as baseline; pooled Brier difference (model minus prior); CE and PE evaluated separately; moving-block bootstrap over evaluation dates (block of 5 dates, 2,000 resamples, seed 20260630) with a Bonferroni 97.5% percentile interval; supported only if the interval lies entirely below 0; the V5.0 date-permutation heterogeneity test (2,000 permutations, seed 500) over training blocks 0 to 5 plus evaluation blocks of 30 dates, remainder merged into the last; reject at 0.025.

| Item | Value |
|---|---|
| Combined SHA-256 (supplied externally and verified) | `6c63883d1f9055a451d16b33eb1e02614b4c801267973027f22e233d60a4993d` |
| Protocol text SHA-256 | `03c278c2576ebfb672881dacc39f45e325e7c21bdbf5c4f6118af019b07e86e4` |
| `V5_1_Frozen_Protocol.json` SHA-256 | `8cb02956ecb78a3ab0f500a73577a165913b86414ded0ff56532110c51c02607` |
| `v5_1_evaluation.py` SHA-256 | `6cdafc8dfea5c90bdad29076517e900bdcb2057fdba595842239563b845bf340` |
| `v5_1_freeze_protocol.py` SHA-256 | `52cfdb243717bed34a46ce99caace52ddfad4f2fd3b4af0175160a386cfa1540` |

### 5.3 Execution record (observed)
- Before the run the verifier printed `VERIFIED 6c63883d...a4993d` (exit 0) and no `V5_1_Eval_*` file existed. The same verifier gave the same result after the run, and a before/after SHA-256 snapshot of every top-level file and every raw ZIP showed only the nine new output files.
- Command run once: `python v5_1_evaluation.py . --expected-combined 6c63883d1f9055a451d16b33eb1e02614b4c801267973027f22e233d60a4993d`. Exit status 0, empty stderr. `V5_1_Eval_Run_Record.csv`: UTC time 2026-10-25T22:57:59Z; Python 3.14.6; numpy 2.5.1; pandas 3.0.5.
- Timestamps: downloads 2026-10-25T21:35Z to 21:38Z; freeze record written about 22:21Z; evaluation 22:57:59Z. "Frozen before any outcome was examined" is a statement in the protocol and is consistent with these timestamps; it is not independently attested.
- The evaluation was run once and is blocked from re-running by design (it refuses when `V5_1_Eval_Run_Record.csv` exists). It was not run again for this report.

---

## 6. V5.1 results: sample sizes, Brier scores, intervals and stability tests

### 6.1 Sample sizes (`V5_1_Eval_Sample_Sizes.csv`)

| Option | Set | Eligible rows | Distinct dates | First date | Last date |
|---|---|---|---|---|---|
| CE | training | 2,253 | 177 | 2025-10-06 | 2026-06-25 |
| CE | evaluation | 902 | 64 | 2026-06-30 | 2026-09-28 |
| PE | training | 2,361 | 177 | 2025-10-06 | 2026-06-25 |
| PE | evaluation | 1,034 | 64 | 2026-06-30 | 2026-09-28 |

Split integrity: latest training label date 2026-06-29; earliest evaluation feature date 2026-06-30; latest evaluation label date 2026-09-29; 30 training rows purged because their label fell in the window; all nine validation checks passed.

### 6.2 Brier scores and paired intervals (`V5_1_Eval_Brier_Results.csv`)
Brier difference is model minus prior; negative would be better. Interval: moving-block bootstrap over evaluation dates, Bonferroni 97.5%, fitted parameters held fixed.

| Option | Model | Prior Brier | Model Brier | Difference | 97.5% interval | Interval below 0? |
|---|---|---|---|---|---|---|
| CE | B1 (lr1) | 0.23987 | 0.24205 | +0.00218 | -0.00042 to +0.00441 | No |
| CE | B2 (ur1) | 0.23987 | 0.24035 | +0.00048 | -0.00096 to +0.00150 | No |
| PE | B1 (lr1) | 0.24891 | 0.24895 | +0.00004 | -0.00169 to +0.00144 | No |
| PE | B2 (ur1) | 0.24891 | 0.24892 | +0.00001 | -0.00078 to +0.00059 | No |

Log loss (prior / model): CE B1 0.67281 / 0.67719; CE B2 0.67281 / 0.67379; PE B1 0.69097 / 0.69105; PE B2 0.69097 / 0.69098. Up-rate: CE training 0.4421, evaluation 0.3836; PE training 0.4795, evaluation 0.4632.

### 6.3 Stability tests (`V5_1_Eval_Heterogeneity_Tests.csv`; 8 blocks, 241 dates; CE 3,155 rows, PE 3,395 rows)

| Option | Feature | Observed H | Block rho range | p | Verdict (reject if p < 0.025) |
|---|---|---|---|---|---|
| CE | lr1 | 8.364 | -0.376 to +0.259 | 0.0235 | Instability supported (borderline) |
| CE | ur1 | 9.101 | -0.403 to +0.310 | 0.0660 | Homogeneity not rejected |
| PE | lr1 | 4.460 | -0.173 to +0.230 | 0.3753 | Homogeneity not rejected |
| PE | ur1 | 6.096 | -0.266 to +0.260 | 0.3868 | Homogeneity not rejected |

The two evaluation-window blocks (30 and 34 dates) have these rank correlations with date-clustered 95% intervals: CE lr1 +0.016 (-0.204 to +0.230) and +0.177 (-0.001 to +0.360); CE ur1 +0.072 and +0.001; PE lr1 -0.097 and +0.060; PE ur1 +0.035 and +0.054. Every interval for PE and for CE `ur1` includes 0.

All eight pre-registered Brier and stability verdict rows are in `V5_1_Eval_Hypotheses.csv`: the four Brier criteria are NOT SUPPORTED.

---

## 7. Data-quality issues and their implications

| Issue (observed) | Count | Implication |
|---|---|---|
| Settlement <= 0 in the new window (all with a positive close) | 406 rows | Kept unchanged; the frozen price floor (settlement >= 1.0 at t, t-1, t-2) excludes them. Close and settlement are different fields, so a row can have a traded close and a zero settlement. |
| Settlement identical to the previous date on two consecutive traded dates | 514 | Possible stale values; counted, not judged. If real, they would bias labels toward "not up" through ties. |
| Zero-volume rows | 50,305 of 108,536 | Prices on those rows are carried or derived, not trade prices. |
| Close differs from settlement on traded rows | 10,022 of 58,231 | Settlement is not a traded price; any executable-return claim would need traded or quoted prices. |
| Missing weekday file | 2026-09-14 (HTTP 404) | Treated as a holiday on secondary evidence; one evaluation date is absent. |
| Date/option-type pairs missing part of the strike grid | 130 | Reported, not filled; no value is interpolated. |
| Grid strikes with no rows in the new window | 4 of 22 per type (18 present) | The evaluation contract set is a subset of the training set. `README_V5_1.md` calls the grid an "18-strike grid" while the integrity report says grid size 22; that wording discrepancy is unresolved, and I used the counts from the data. |
| Study-expiry rows outside the frozen strike grid | 12,626 | Ignored by the protocol; the study covers a fixed set of far and near strikes. |
| Ties counted as "not up" | 12 eligible rows in the V4.8 dataset | Small, but ties exist. |
| Eligibility drop from raw rows | training CE 2,253 of 2,411 raw | Early-series rows lack lags, and the price floor removes the cheapest contracts. |

**Implications.** The inputs are internally consistent (hash checks, schema and duplicate checks, zero cross-origin disagreements where checked). They are not execution-grade: settlement-based labels on illiquid, far-from-the-money contracts say nothing about obtainable prices. The evaluation window covers the final three months of life of one expiry, so time-to-expiry, moneyness and base rate shift together (CE up-rate 0.442 in training against 0.384 in evaluation). The paired Brier difference is robust to a shifted base rate only to the extent that both models face the same shift.

---

## 8. Conclusions

### 8.1 Established (observed, within the limits stated)
1. The frozen protocol, its dependencies and the raw inputs were verified by hash before and after a single evaluation run, with no outputs present beforehand and no file changed afterwards.
2. On the new window, neither lagged-return model improved on the training-rate prior for CE or PE under the pre-registered criterion. All four Brier differences are slightly positive or about zero, and all four 97.5% intervals include 0.
3. The CE `lr1` relationship was not homogeneous across eight chronological blocks under the pre-registered test (p = 0.0235 against 0.025), which is borderline. CE `ur1` and both PE features did not reject homogeneity.
4. Earlier diagnostics found no alignment or leakage defect in the target, lag and split construction (V4.9), apart from one minor eligibility defect that did not change any conclusion.

### 8.2 Not established
- **No predictive improvement was found; that is not proof that none exists.** The intervals are about +/-0.002 to +/-0.004 wide in Brier units; a small effect in either direction cannot be excluded. Blocks of about 30 dates have limited power. The test covers two simple single-feature models only.
- **No profitability.** Labels compare settlements, and no spread, fee, slippage or fill is modelled.
- **No generalisation to unseen contracts.** The window uses the same expiry and strikes as training; it tests temporal stability for those contracts. It does not test other expiries, strikes or instruments.
- **Whether the CE `lr1` instability is real.** The p-value sits just under the threshold, and the permutation test is anti-conservative under serial dependence. The related `ur1` test did not reject.
- **Untested explanations.** The shift in CE base rate and the terminal-life timing of the window may explain part of the difference between periods. This was not decomposed.

### 8.3 Interpretation
The evidence is consistent with weak, unstable lag relationships that do not translate into better out-of-sample probabilities than a simple prior. It is also consistent with a small effect that this design cannot detect. The conservative reading is that the project has not found a reliable predictive feature among these two, and no stronger claim is warranted.

---

## 9. Reproducibility

### 9.1 Environment (from `V5_1_Eval_Run_Record.csv` and the local machine)
Windows 11 (10.0.26200), Python 3.14.6, numpy 2.5.1, pandas 3.0.5. No other third-party library is used by the evaluation. Results were produced by pure-Python/numpy/pandas code; the same environment was used for the local tests below.

### 9.2 Commands (run from the project folder)
```
python -m unittest test_v5_1 test_v5_0 test_v4_8_feature_research test_v4_9_feature_diagnostics
python v5_1_acquisition_audit.py .
python v5_1_freeze_protocol.py . --verify --expected-combined 6c63883d1f9055a451d16b33eb1e02614b4c801267973027f22e233d60a4993d
python v5_1_evaluation.py . --expected-combined 6c63883d1f9055a451d16b33eb1e02614b4c801267973027f22e233d60a4993d   # ALREADY RUN ONCE; refuses to run again
```
A rerun of the evaluation in the project folder is blocked by design. Running it in a copy would reproduce the numbers but would not be an independent test of the window; this was not done.

### 9.3 Test status
- **Local tests passed.** For this report I re-ran `python -B -m unittest test_v5_1 test_v5_0 test_v4_8_feature_research test_v4_9_feature_diagnostics`: 110 tests, OK (exit 0), 19.7 seconds. A before/after SHA-256 snapshot confirmed the test run changed no project file.
- **Google Colab was not run** for V5.0, for the V5.1 acquisition and audit, for the V5.1 evaluation, or for this report. The user reported a Colab run of V4.9 (26 tests passed); I could not verify it, but its 21 report members match the local ones in content.
- Reproduction on Colab's Python and library versions has not been checked.

### 9.4 Report files and SHA-256 hashes (V5.1 evaluation outputs, unchanged since the run)

| File | SHA-256 |
|---|---|
| `V5_1_Eval_Block_Relationships.csv` | `3859d1f3a6299e39ed8b726a795668ea95e535d8aa877c9b7a3b6bde28ef9a52` |
| `V5_1_Eval_Brier_Results.csv` | `516577acba4854ee1d0280584c75d712cb358f08278ce48bba7700c0882b62b7` |
| `V5_1_Eval_Heterogeneity_Tests.csv` | `4bc77e6cebf430b7c3b148b29f387f3958aaf1eb4093d735dc115fefac11a27a` |
| `V5_1_Eval_Hypotheses.csv` | `85db40ea9eb5760815002fe0279bf2e310d48e3c01b6b3e9914407a07e607d1e` |
| `V5_1_Eval_Run_Record.csv` | `9e37f042abc0386f8250f2f1bda6f7c22e85923b0e48531c4c9cbe6f813067b5` |
| `V5_1_Eval_Sample_Sizes.csv` | `4df16aa3f7ee21ce55575588893d2c39f1042b78c75d0a6774488c70dc63ba4c` |
| `V5_1_Eval_Split_Integrity.csv` | `9c2ac036f4a281bafa491079d2edc5f02ad64b8c874bfaf3aba88f2336274f9a` |
| `V5_1_Eval_Validation_Checks.csv` | `5a4e570c630f564109a3da55d48d50592b334a0a00ba4c38b52eda5f7d6a0738` |
| `V5_1_Evaluation_Reports.zip` | `773749ef1c94f7ddadc2597c6465258210b5f87da08c84345016927ad11ae4f9` |

The package ZIP also contains `SHA256SUMS.txt` listing these nine files and this report. The report's own hash cannot appear inside the report; it is given in the delivery message and in `SHA256SUMS.txt`.

### 9.5 Preserved artifacts
No V4.4 to V5.1 notebook, script, dataset, raw file, frozen protocol file or evaluation output was modified to prepare this report. The only files added are `V5_1_Final_Research_Report.md` and the package ZIP.
