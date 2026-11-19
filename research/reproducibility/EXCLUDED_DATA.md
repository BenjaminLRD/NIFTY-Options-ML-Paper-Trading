# What was deliberately left out of this repository

The original research working folder was about 570 MB (427 files). Only what is needed to understand, audit and partly verify the V5.1 study was copied (55 files, about 1.5 MB). Nothing was deleted from the original folder.

## Left out because it is NSE data or derived row-level NSE data

NSE's terms of use restrict redistribution of its data and prohibit use for "gaming, virtual trading or simulation activities" without written agreement (see `application/config/historical_data_sources.json`; the final report also says to check NSE's terms before redistributing). Not legal advice; the files were excluded to stay on the safe side.

| Left out | Approx. size | Where its identity is recorded |
|---|---|---|
| Raw daily F&O bhavcopy files (13 loose CSVs, 55 zips in `V5_0_Acquired_Raw`, 65 zips in `V5_1_Acquired_Raw`, the extracted copies in `V5_0_Run/data`) | about 430 MB | names and SHA-256 in `V5.1/V5_1_Source_Acquisition_Manifest.csv`, `V5.1/V5_1_Download_Log.csv`, `V5.0/V5_0_Raw_File_Hashes.csv`, `V5.0/V5_0_Source_Acquisition_Manifest.csv` |
| Normalized datasets (`V5_0_Normalized_Bhavcopy_NIFTY_Options.csv`, `V5_1_Normalized_Bhavcopy_NIFTY_Options.csv`, `V5_0_Normalized_Study_Universe.csv`, `V5_0_Study_Dataset.csv`) | about 40 MB | recorded in `V5.1/V5_1_Frozen_Protocol.json` (hashes) |
| Contract-wise exports `OPTIDX_NIFTY_*.csv` and earlier model datasets (`NIFTY_Options_*.csv`, `V4_*_Dataset.csv`, `V4_*` row-level files) | about 6 MB | n/a (earlier stages, superseded) |
| Report zips that embed those datasets (`V5_0_Reports*.zip`, `V5_1_Reports.zip`, `V4_*_Reports*.zip`) | about 37 MB | their small result files are included loose where needed |

## Left out because it is superseded exploratory work

Notebooks V1 to V4.7 (`Indian_Options_Paper_Trading_V1`, `V2_XGBoost`, `V3_1`, `V4_*`, `V4_6_Leakage_Audit`, `V4_7_*`) and their per-stage result CSVs. The final report states V4.4 to V4.6 were "not re-reported here" and V4.7 to V4.9 findings that matter are summarised in the report and in `reports/README_V4_*.md`. The scripts that the frozen protocol hashes (`scripts/`) are all included.

## Left out because it is unrelated or unnecessary

- Coursework: `introduction to neural netwok-linear-non-linear sep (1) (1).pdf`, `MLP-activation fuction (1).pptx`, the two course PDFs added in November 2026.
- Scratch and duplicates: `nbcode.txt`, `outs.txt`, `* (1).csv` and `* (1).zip` copies, `V5_0_Reports_prelim_before_download.zip`, `FOSett_prce_12032026.csv`, `__pycache__/`.
- `V6_0_Paper_Trading_System_Plan.md`: a pre-implementation plan that is superseded by the application's documentation and contains personal machine paths.

## To re-acquire the data (only if you have the right to)

Download one file per trading day from NSE's reports page (https://www.nseindia.com/all-reports-derivatives). Check each file against the SHA-256 in the manifests above before using it. Do not script-download from NSE hosts without checking NSE's current terms.
