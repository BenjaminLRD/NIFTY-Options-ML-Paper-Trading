# Limitations and future work

## Research
- One expiry (2026-09-29), a fixed grid of 22 strikes, end-of-day settlements (not executable prices), CE and PE separately, two single-feature models.
- No spread, fee, slippage or fill model in the research labels; **no profitability inference is possible**.
- Intervals about +/-0.002 to +/-0.004 Brier units wide; small effects cannot be excluded. The permutation stability test is anti-conservative under serial dependence; the borderline CE `lr1` instability is unexplained.
- Data provenance gaps (a holiday assumed from secondary sources, single-origin periods, hashes that prove local integrity only, and upstream raw files the freeze does not cover) are listed in section 2.3 of the final report.
- "Frozen before any outcome was examined" is consistent with timestamps but not independently attested.
- The raw data is not in this repository, so the full verification chain cannot be re-run from the repository alone.
- No fitted model artifact exists; B1/B2 cannot be loaded.

## Application
- Invented data only; the demonstration model's parameters are hand-set.
- One decision per day at 10:15; no intraday features; one expiry and one strike per side; no stop-loss or time stop (positions close only on the model's exit rule).
- Lot size (65) and tick size are illustrative, not verified exchange values; costs are an illustrative model (₹20 plus 0.05% of turnover, one tick of slippage).
- Accounts are append-only JSONL files, not a database; no concurrency beyond a single-writer lock.
- The safety scan is pattern-based and does not prove the absence of secrets or order code; it fails closed on binary files, so it covers `application/` but not the screenshots.
- Manual browser testing (sell, duplicate submit, restart) was not performed; automated tests only. The JavaScript chart tests were skipped for lack of Node.js. No CI.
- Tests and the scan need a git repository; the fixtures need LF line endings (handled by `.gitattributes`).
- No license chosen; the project owner's personal contribution is not documented.

## Future work
Written permission for a data source; a real-data origin class with its own ingest tests; a pre-registered forward evaluation on unseen data and other expiries; intraday features; stop rules; CI; a license decision.
