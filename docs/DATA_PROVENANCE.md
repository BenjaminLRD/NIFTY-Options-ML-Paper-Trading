# Data provenance

| Data | Status | Notes |
|---|---|---|
| `application/tests/fixtures/historical_synthetic/*.jsonl` | **Invented** | Test fixtures; every record carries a synthetic origin and a manifest with SHA-256 hashes. Stored with LF line endings (`application/.gitattributes`) because the hashes are over exact bytes |
| Replay data from `application/src/niftypaper/ml/fixtures.py` | **Invented** (2031 dates) | Random-walk index and premiums as a fixed function of the index; origin class `SYNTHETIC_FIXTURE`; the replay runner refuses any other class |
| Main dashboard quotes and chart history | **Generated** | A synthetic provider; chart history is made up from a fixed seed and is never given to any model or the paper engine |
| FYERS adapter code | Not connected | Read-only quote adapter kept behind fail-closed gates; no credentials exist in the repository; no provider is approved (see `application/docs/PROVIDER_FEASIBILITY.md`, `FYERS_VERIFICATION.md`) |
| Research data (NSE end-of-day F&O bhavcopy and contract-wise exports, 2025-10 to 2026-09) | **Real, but not included** | See `research/reproducibility/EXCLUDED_DATA.md`. Only manifests (names, hashes), aggregate results and code are included |
| `application/docs/historical_data/existing_data_inventory.json` | Derived metadata | File names/hashes and counts about the original research data (no rows, no local paths); a test reads it |

## NSE terms

NSE's terms of use (page updated 29/10/2025; checked 2026-10-27, recorded in `application/config/historical_data_sources.json`) say the user shall not use the data "for any gaming, virtual trading or simulation activities under any circumstances whatsoever" and prohibit automated data collection from its website; NSE's copyright policy separately allows personal, non-commercial or educational use. Whether these clauses reach the archive host that served the files is a legal question the project could not settle. Consequently: the application does not use NSE data; the NSE files are not redistributed here; and the earlier research downloads were made by script from the archive host (per the download logs), which should be considered against those terms. Not legal advice; written permission from NSE would settle it.

## Rules the application enforces
Invented data must be labelled as such on every screen; provider data can never exceed `UNVERIFIED` without evidence and a recorded terms permission; unpriced positions are never given invented prices.
