# Research (frozen): do lagged returns predict next-settlement direction?

This folder holds the evidence behind the project's central finding: **no predictive improvement over a simple baseline was found.** It contains copies of selected files from the original research working folder. Every file here is **byte-identical** to its original (SHA-256 in `reproducibility/SHA256SUMS.txt`). Nothing was edited, re-run, retrained or tuned to prepare this repository.

Research and paper trading only. Labels compare settlement prices, which are not executable prices; no spread, fee, slippage or fill is modelled. **Nothing here is evidence of profitability.**

## Layout

| Path | Contents |
|---|---|
| `V5.1/` | The frozen protocol (`V5_1_Frozen_Protocol.json`), the final report (`V5_1_Final_Research_Report.md`), the one-shot evaluation outputs (`V5_1_Eval_*.csv`), the contamination and coverage audits, the source manifest and download log (file names and SHA-256 hashes of the NSE files), and the report packages (`V5_1_Final_Report_Package.zip` with its own `SHA256SUMS.txt`) |
| `V5.0/` | Stability-study outputs (walk-forward, heterogeneity tests, hypotheses, coverage, hashes of the raw files) |
| `scripts/` | The research code V4.7 to V5.1 and its tests. The frozen protocol records the SHA-256 of these files, and they import each other, so they sit together in one folder |
| `notebooks/` | The V4.8, V4.9 and two V5.0 notebooks (the Colab/Jupyter form of the scripts) |
| `reports/` | Stage READMEs for V4.8, V4.9 and V5.0 |
| `reproducibility/` | `SHA256SUMS.txt` (hashes of everything here), `VERIFICATION.md` (what was checked and what cannot be checked without the data), `EXCLUDED_DATA.md` (what was left out and why), `requirements.txt` |

There is no `models/` folder: **no fitted model artifact exists.** The two research models (B1 on `lr1`, B2 on `ur1`, single-feature logistic regressions) were fitted inside the evaluation script and the fitted numbers were never saved. The application therefore cannot load or trade them.

## The question and the answer

- **Question:** is the relationship between a lagged option-settlement return (`lr1`) or lagged index return (`ur1`) and the next settlement direction stable over time, and do models built on them beat a training-rate prior?
- **Protocol:** frozen and hash-verified before the evaluation window was looked at; fitted once on data up to 2026-06-29 (labels crossing the boundary purged); evaluated once on 2026-06-30 to 2026-09-28; paired Brier difference with a moving-block bootstrap and a Bonferroni 97.5% interval.
- **Result:** all four Brier differences (model minus prior) were slightly positive or about zero (CE `lr1` +0.00218, CE `ur1` +0.00048, PE `lr1` +0.00004, PE `ur1` +0.00001) and every interval included 0. **Not supported.** See `V5.1/V5_1_Eval_Brier_Results.csv`.
- **Caveats:** one expiry, a fixed strike grid, end-of-day settlements, two simple models, intervals about +/-0.002 to +/-0.004 wide. No predictive improvement was found; that does not prove none exists.

## Reproducing

Read `reproducibility/VERIFICATION.md` first. In short: the code and the result files can be checked against their hashes with no data. Re-running the pipeline needs the NSE files, which are **not** included (NSE's terms restrict redistribution and simulation use); their names and hashes are in the manifests so a re-acquired copy can be verified. The one-shot evaluation refuses to run twice by design, so it should not be re-run in this folder.

## Provenance and attribution

The final report is written in an assistant's first-person voice (for example "I re-ran the tests"), which indicates AI-assisted authoring; the repository does not record which assistant or tool produced the research code and report. The report's own text records which runs were executed locally and which were not (for example, Google Colab runs were not verified). The project owner's personal contribution is not recorded here and is not claimed on their behalf.
