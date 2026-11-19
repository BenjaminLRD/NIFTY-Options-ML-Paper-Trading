# What was verified for this folder, and what cannot be

## Verified when this folder was assembled (2026-11-19)

- **Byte-identity:** all 55 research files copied here were compared by SHA-256 with their originals in the original research working folder: 0 mismatches. `SHA256SUMS.txt` lists every file in `research/` except this `reproducibility/` folder. Check with, from the `research/` folder:
  ```
  python -c "import hashlib,sys; [print(p, 'OK' if hashlib.sha256(open(p,'rb').read()).hexdigest()==h else 'MISMATCH') for l in open('reproducibility/SHA256SUMS.txt') for h,p in [l.rstrip('\n').split('  ',1)]]"
  ```
- **Frozen protocol:** `V5.1/V5_1_Frozen_Protocol.json` records SHA-256 hashes of 82 files. 14 of those files are in this folder (the research scripts, tests, download log and other small files) and **all 14 match** their recorded hashes. The `V5_1_Frozen_Protocol.json` file itself has SHA-256 `8cb02956ecb78a3ab0f500a73577a165913b86414ded0ff56532110c51c02607`, and its recorded combined SHA-256 is `6c63883d1f9055a451d16b33eb1e02614b4c801267973027f22e233d60a4993d` (the value the report says was supplied externally and verified before the evaluation).
- **Evaluation outputs:** the nine `V5_1_Eval_*` outputs match the hashes printed in section 9.4 of the final report (they are byte-identical to the originals, and the report's table was written from the originals).

## Not verified here, and why

- The other 68 entries in the protocol are the 65 raw NSE zip files and three NSE-derived datasets (`V5_0_Normalized_Study_Universe.csv`, `V5_0_Study_Dataset.csv`, `V5_1_Normalized_Bhavcopy_NIFTY_Options.csv`). They are **not included** (see `EXCLUDED_DATA.md`), so `python scripts/v5_1_freeze_protocol.py . --verify ...` **cannot be completed from this folder alone**. Their names and hashes are in `V5.1/V5_1_Source_Acquisition_Manifest.csv`, `V5.1/V5_1_Download_Log.csv` and `V5.0/V5_0_Raw_File_Hashes.csv`, so files you re-acquire can be checked against them.
- **The research tests (`scripts/test_v5_*.py`, `test_v4_*.py`) were not run for this repository.** The report states that 110 of them passed on the original machine on 2026-10-26 (Python 3.14.6, numpy 2.5.1, pandas 3.0.5); that is the report's statement, not something re-checked here.
- **The one-shot evaluation was not re-run** and must not be re-run in this folder (it refuses to run if `V5_1_Eval_Run_Record.csv` exists, by design). Nothing was retrained or tuned.
- The statement in the protocol that it was "frozen before any outcome was examined" is consistent with the recorded timestamps but is not independently attested (the report says so too).
