# Historical replay runner and trust-boundary hardening (fixture only)

Date: 2026-10-27. Builds on `docs/HISTORICAL_DATA_PHASE2.md`. Still invented fixtures only (`tests/fixtures/historical_synthetic/`): no NSE or broker data, no credentials, no network, no registry source enabled, no strategy, no profitability claim. Nothing is committed.

## 1. Trust-boundary changes

| Change | Where | Effect |
|---|---|---|
| Attestation | `src/niftypaper/paper/attest.py`, `MarketView.attestation`, `attestation_fields()` | A `HISTORICAL` view needs an HMAC tag over bid, ask, sizes, tick, lot, quote time, symbol and source labels. Hand-built, copied (`dataclasses.replace`), edited or relabelled views fail. The key is random per process. |
| Engine revalidation | `PaperEngine._historical_problem` | Even with a valid tag the engine itself checks: labels (`historical:<source>`, `HISTORICAL_REPLAY`, `REPLAY`), a full contract key as symbol, finite Decimal prices with `0 < bid < ask`, tick above zero and both prices on it, positive whole lot size and displayed sizes, an aware quote time not after the clock. Failure: `PROVENANCE_INVALID`. |
| Source binding | `PaperEngine(provider=...)` | A replay account trades only on one `historical:<source id>` label (`ACCOUNT_MISMATCH` otherwise), and a stored trade from another label locks the account on restore (`DIFFERENT_ACCOUNT`). |
| Bridge | `historical/bridge.py` | Issues the attestation. A static test requires that only `bridge.py` (and `attest.py` itself) contains `issue(`. |
| Existing checks | unchanged | Account-origin, terms/verified flags, lot/tick, displayed size, V6-0.1 freshness against the replay clock, risk limits, daily budget, atomic refusal, store lock and replay verification all still run for `HISTORICAL` views. |

**Not enforceable by this architecture (stated plainly):** Python cannot hide the key or `attest.issue` from code running in the same process. Anything that imports `niftypaper.paper.attest` can mint a valid tag for fields it chooses. The "only the bridge issues" rule is a static source test, not a language guarantee, and a malicious or careless in-process caller defeats it. What the attestation does stop is accidental or casual forgery and any alteration after issue. The engine's own field checks limit what a forged-but-well-formed view could do; they cannot tell a made-up quote from a real one. A real guarantee would need the replay logic in a separate process or an OS-level boundary.

## 2. Replay runner (`src/niftypaper/historical/replay.py`)

`ReplayRunner.open(ReplayConfig(...))` then `run(script, until=None)`, `advance_to(t)`, `try_trade(intent)`, `event_lines()`, `ledger_lines()`, `digest()`, `close()`. The script is a list of `Intent(at, contract_key, side, lots)` written by the caller; the runner has no logic that chooses trades.

Opening refuses (`ReplayRefused`, with a code) when: times are naive or `start <= end <= as_of` fails (`BAD_CONFIG`); the manifest is missing, malformed or unsafe (`INVALID_MANIFEST`); any entry is not `SYNTHETIC_FIXTURE` (`NOT_FIXTURE_ONLY`); more than one source id (`MIXED_SOURCES`); any changed, missing or unlisted file (`INTEGRITY_FAILURE`); or the saved replay account already holds a trade after `start` (`STATE_AHEAD_OF_CLOCK`, lock released). Rows that fail validation, conflict, are future-dated or of the wrong kind do not stop the replay: they go to the exclusion ledger and are never shown.

Behaviour: the clock only moves forward (`ClockError` otherwise, and `PAST_END`); a record produces a `RECORD_AVAILABLE` event only when the clock reaches its `available_at` (day-level records at 23:59:59 of their date); `run` steps to each availability instant and intent time up to `until`, trying intents after the records of that instant. An intent goes bridge first (`NO_VISIBLE_QUOTE`, `NOT_FILL_ELIGIBLE`, `ORIGIN_NOT_ALLOWED`, `DATASET_NOT_PERMITTED`, ...), then the engine. The newest visible quote decides: if it is ineligible the intent is refused, not routed to an older eligible quote. EOD, settlement, candle and index records cannot be found by the quote lookup at all. Fixture quotes are fillable only as `HISTORICAL`, only from `SYNTHETIC_FIXTURE` files, and only when `permitted=True` is set on the config; unlabelled, unknown-origin and unverified-source data is refused.

Account and state: a dedicated file `<state_dir>/replay_<source id>.jsonl`, account name `replay_<source id>`, an engine bound to origin `HISTORICAL` and that source, the store's single-writer lock (a second runner on the same file is locked out: `STATE_LOCKED` event, `STATE_UNAVAILABLE` refusals), and replay-verified restore. Corrupt, truncated, reordered or other-account state locks the account and is never rewritten. A restart restores positions and the daily budget (it is not reset).

Output: events and the ledger contain no wall-clock time or machine path, so identical fixtures and configuration give identical `event_lines()`, `ledger_lines()`, state file bytes and `digest()`.

## 3. Tests

`tests/test_historical_replay.py` (33 tests): scripted replay outcomes, availability timing (including settlement only at day end), no future record ever shown, clock movement (backwards, past end, naive), bad scripts and intents, forbidden record kinds, wrong-kind rows, permission and origin class, mixed sources, invalid/missing/unsafe manifests, tampered/missing/unlisted/appended files, look-ahead poisoning (late-delivered, conflicting, after-cut-off and available-before-observed quotes, an extreme EOD close), conflicting data, ledger completeness, cross-source and cross-origin views, daily budget and lot limits in replay, atomic refusal on disk, restart (restore, budget kept, start before last trade refused, lock released), second-process lock, corrupt/truncated/tampered state, other-account state files in both directions, determinism across fresh state directories and a copy of the fixtures, no wall-clock text. `tests/test_historical_phase2.py` gained attestation tests (altered, forged, re-attested bad fields, only-the-bridge-issues). Eighteen engine/runner mutations (removing attestation, source binding, each field check, integrity, fixture-only, clock and restart guards) were each caught.

## 4. Limitations

- The in-process forgery limit above.
- The runner executes scripted intents only; there is no strategy, sizing, exit logic or reporting, and no performance claim.
- Files are verified once at open. A file changed afterwards does not affect a running replay (records are in memory) and is not re-checked until the next open.
- `as_of` and the clock are caller supplied and trusted; fixtures dated 2031 stand in for a session.
- Day-level availability (end of date) is conservative and unverified; candle timestamp conventions are unknown and only flagged.
- Replay account names are derived from the source id; two different datasets sharing a source id would share an account file (the store checks balance and account name, not dataset content).
- The runner cannot read real files: no reader exists for UDiFF or broker candles, and NSE terms remain unresolved (Phase 1).

## 5. Smallest next step

(Done: see `docs/HISTORICAL_REPLAY_AUDIT.md`, including the hash-chained event log in its section 8.) Anchors for the log's head hash are in its section 9, and input identity, change detection and pairing in its section 10. After that, and only after the NSE terms question is answered, a read-only reader for one local end-of-day layout that emits the fixture format into a git-ignored folder.
