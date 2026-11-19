# Historical data, Phase 2: the canonical layer (invented fixtures only)

Date: 2026-10-27. Scope: strict validation, availability gating, duplicate/conflict handling, manifests and a narrow bridge to the paper engine, tested on made-up data.
Not in scope and not done: any download, scrape or ingest of NSE or other market data, any credential or provider call, any strategy or backtest, any change to the research folder or the V5.1 artifacts. No source in `config/historical_data_sources.json` was enabled. Nothing is committed.

## 1. What exists

| Part | File |
|---|---|
| Record kinds, validators, reason codes | `src/niftypaper/historical/records.py` |
| Manifests and byte-level integrity | `src/niftypaper/historical/manifest.py`, `tools/historical_manifest.py` |
| Ingestion, duplicate/conflict policy, ledger | `src/niftypaper/historical/ingest.py` |
| Replay clock and `available_at` gate | `src/niftypaper/historical/gate.py` |
| The only path to the paper engine | `src/niftypaper/historical/bridge.py` |
| Invented fixtures (generator + committed copy) | `tests/historical_fixtures.py`, `tests/fixtures/historical_synthetic/` |
| Tests | `tests/test_historical_phase2.py` (77 tests) |
| Engine change | `paper/engine.py`: a third data origin, `HISTORICAL` (section 7) |
| Schema file | `config/historical_schema_v1.json`: enums extended, status now "DRAFT ... fixtures only" |

Source files are JSON lines, one object per line. Numbers must be JSON strings or integers (floats are refused), dates `YYYY-MM-DD`, times ISO-8601 with an explicit `+05:30` offset.

## 2. Record kinds and validation rules

Four kinds stay separate. Only `QUOTE_SNAPSHOT` has bid/ask fields; the other three refuse them (`FORBIDDEN_FIELD`).

| Kind | Required | Rules |
|---|---|---|
| `EOD_CONTRACT_DAY` | underlying, expiry, strike, option_type, trading_date | OHLC consistent; prices positive or null; `0` or `-` become null plus a flag; no `observed_at`; availability rule below |
| `INTRADAY_BAR` | identity, interval, bar_start, bar_end, available_at | `bar_end - bar_start` must equal the interval; `available_at >= bar_end`; OHLC consistent; unknown timestamp convention is flagged |
| `QUOTE_SNAPSHOT` | identity, observed_at, available_at, time_semantics, bid, ask, lot_size, tick_size | `0 < bid < ask`; both on the tick; `available_at >= observed_at` |
| `UNDERLYING_EOD` (index) | underlying, trading_date | OHLC consistent; same day-level time rules as EOD |

Common rules:

- Identity: underlying (upper-case name), expiry date, strike above zero with at most 2 decimals, option type `CE` or `PE`. The key is `underlying|expiry|strike(2dp)|CE/PE`. A contract that had already expired on the record's date is refused. The instrument token is stored as `source_instrument_id` but is never part of any key.
- Missing values stay `null`. Nothing is filled, interpolated, forward-filled or copied from another record. Whether a day record `traded` is derived only from reported volume/transactions and is null when neither is reported.
- Prices: on day-level records a source `0` or `-` means "no trade": null plus `PRICE_ZERO_NO_TRADE` / `PRICE_DASH_NO_TRADE`; a value shown for a contract with zero volume is flagged `CLOSE_CARRIED_NO_TRADE` / `LAST_CARRIED_NO_TRADE`. On bars and quotes zero or negative is `INVALID_PRICE`.
- Lot and tick: required and valid on quotes; where present on other kinds they must be valid; absence is flagged (`LOT_SIZE_ABSENT`, `TICK_SIZE_ABSENT`), never defaulted from `config/contract_specs.json`.
- Provenance kept on every record: source id, origin class, file path, file SHA-256, physical line number, the raw source line, ingest time, schema and parser versions, retrieval time (from the manifest) and quality flags.
- Origin classes: `SYNTHETIC_FIXTURE`, `UNVERIFIED_SOURCE`, `UNKNOWN`. A row's `"synthetic": true` label must agree with its manifest's class (`ORIGIN_LABEL_MISMATCH` otherwise), so a made-up row cannot sit in a file that claims to be real, and an unlabelled row cannot sit in a fixture file.

### Rejection reason codes (the exclusion ledger vocabulary)

`INVALID_NUMBER`, `INVALID_PRICE`, `INVALID_OHLC`, `OFF_TICK`, `CROSSED_QUOTE`, `INVALID_SPEC`, `INVALID_DATE`, `INVALID_TIMESTAMP`, `INVALID_TIMEZONE`, `INCONSISTENT_TIMES`, `FUTURE_OBSERVATION`, `MISSING_REQUIRED_FIELD`, `FORBIDDEN_FIELD`, `MALFORMED_RECORD`, `MALFORMED_FILE`, `UNKNOWN_RECORD_KIND`, `RECORD_KIND_MISMATCH`, `ORIGIN_LABEL_MISMATCH`, `DUPLICATE_ROW_IN_FILE`, `DUPLICATE_OF_EARLIER_COPY`, `CONFLICTING_ROW_IN_FILE`, `CONFLICTING_COPIES`, `HASH_MISMATCH_WITH_MANIFEST`, `SIZE_MISMATCH`, `MISSING_FILE`, `UNLISTED_FILE`. Unused Phase 1 codes remain in the list (`UNKNOWN_COLUMN_LAYOUT`, `OUTSIDE_REQUESTED_WINDOW`). A test checks the code and the schema file list the same codes.

Examples from the committed fixtures (`SYNTHETIC_quotes_invalid.jsonl`, one defect per line): crossed bid/ask (`CROSSED_QUOTE`), bid `100.02` on a 0.05 tick (`OFF_TICK`), a time with no offset or in UTC (`INVALID_TIMEZONE`), missing ask, a float price (`INVALID_NUMBER`), `available_at` one second before `observed_at` (`INCONSISTENT_TIMES`), an observation after the ingest cut-off (`FUTURE_OBSERVATION`), an expired contract (`INVALID_DATE`).

Ambiguities that are documented rather than resolved: what UDiFF volume and turnover mean (not read from official definitions; volume unit is `UNKNOWN` unless stated and then flagged), what a candle timestamp means (`UNKNOWN` is accepted and flagged, never converted), the exact publication time of end-of-day files, the trading calendar (a weekend `trading_date` is not rejected), and whether a different instrument token for the same contract and day is harmless (treated as a conflict, the cautious reading).

## 3. Times and the `available_at` gate

Four times are kept apart and never substituted for one another:

| Time | Meaning | Where it lives |
|---|---|---|
| `observed_at` | when the market was observed (null for day-level records) | the record |
| `available_at` | the earliest instant a replay may use the record | the record |
| `retrieved_at` | when the file was obtained | the manifest; provenance only |
| replay clock | the instant the replay is at | `ReplayClock` |

Rules enforced at ingestion:

- Quotes and bars have no default `available_at`; it must be present, and `>= observed_at` (quotes) or `>= bar_end` (bars).
- Day-level records carry no time of day. Their availability is the end of the trading date (23:59:59 IST) unless the source states a later time; a stated time earlier than that is refused (`INCONSISTENT_TIMES`) because it would be inferring intraday availability from a date. The default is flagged `AVAILABILITY_RULE_DEFAULT`; it is a conservative assumption, not a verified publication time.
- Observations after the ingest cut-off `as_of` (a required argument; the code never reads the clock) or after the file's retrieval time are refused (`FUTURE_OBSERVATION`).
- Only IST (`+05:30`) times are accepted; naive and UTC times are refused rather than converted, because a wrong guess would shift availability by hours.
- Extra fields in a row (`retrieved_at`, `received_at`, `timestamp`, `tt`, ...) are kept in the raw line and ignored.

Rules enforced at read time (`gate.py`): a record is visible only if `available_at` is known, timezone-aware and `<= clock`, `observed_at <= clock`, and the kind's invariants still hold (day-level records have no time of day and are not available before their date ends; bars not before they close). These are re-checked even though ingestion already enforced them, so a record built by hand or altered in memory still cannot leak the future. `PointInTimeView` has no method that lists unavailable records; replay code gets `visible`, `latest` and `latest_quote` only. The clock moves forward only and refuses naive datetimes.

## 4. Duplicate and conflict policy (policy version 1)

- Natural key: `(kind, contract key or underlying, trading date / bar interval and start / observed_at, source_id)`. Never the instrument token alone: a token reused for a different contract is two records (tested).
- Identical copies (same normalised values and times): the first by (file path, row) is kept; the others are excluded as `DUPLICATE_ROW_IN_FILE` or `DUPLICATE_OF_EARLIER_COPY`, naming the kept copy.
- Conflicting copies (same key, different values): all are excluded (`CONFLICTING_ROW_IN_FILE` or `CONFLICTING_COPIES`), each naming another member. Never newest-wins, largest-wins or first-wins.
- Different `source_id`s are never merged or used to fill each other's gaps.
- Determinism: files in sorted path order, rows in file order, records and ledger sorted by explicit keys, no clock or randomness. A test runs the ingest repeatedly and with the manifest entries reversed and compares records, ledger text and a digest.

## 5. Manifests and integrity

Create and verify (read-only on the data files; `create` refuses to overwrite an existing manifest):

```
python tools/historical_manifest.py create --root DIR --source-id synthetic_fixture --origin-class SYNTHETIC_FIXTURE --origin "invented test data" --file SYNTHETIC_quotes.jsonl=QUOTE_SNAPSHOT
python tools/historical_manifest.py verify --root DIR
```

`verify` exits 0 when every listed file matches, 1 on a changed, missing or unlisted file, 2 on an unusable manifest. Each line of `MANIFEST.jsonl` holds: relative path, SHA-256, byte size, source id, declared record type, origin class, origin text, `retrieved_at_utc` (or null), `terms_url`, `terms_checked_on`. Unsafe paths (absolute, `..`, backslashes, drive letters), unknown fields, duplicate paths and symlinks refuse the whole manifest.

During ingestion the hash is computed from the same bytes that are then parsed, so a file cannot change between the check and its use. A changed file yields no records, an `integrity` ledger entry and an `HASH_MISMATCH_WITH_MANIFEST` exclusion; other files are still processed. Files in the folder but not in the manifest are reported (`UNLISTED_FILE`) and never ingested. Ingestion never writes to the raw folder (a test compares bytes and modification times before and after).

**A matching hash proves only that the bytes are the ones the manifest was written for.** It says nothing about whether the content is real market data, who produced it, or whether the manifest was honest.

## 6. Why settlement, closing, last-traded, candle and index records cannot become fill quotes

- The schema and validators give those kinds no bid or ask field, and refuse one if present.
- `fill_eligible` can be true only for a `QUOTE_SNAPSHOT` with `time_semantics = QUOTE_TIME`, valid bid/ask on the tick, lot and tick present, and displayed sizes. A snapshot whose time is a last-trade, receive or unknown time is stored but never eligible.
- `bridge.market_view` raises no exception and builds nothing for any other kind: it returns `NOT_A_QUOTE_SNAPSHOT`. `bridge.executable_view` looks only at quote snapshots. The tests route every EOD, bar and index record from the fixtures into the bridge and then try hand-built views against the engine.
- A fill price is always the touch (ask for BUY, bid for SELL) plus slippage, computed by the engine from the quote; no close, settlement or last price is ever read.

## 7. The boundary with the paper engine

`bridge.market_view(record, clock, allowed_origin_classes=..., permitted=...)` returns a `MarketView` only if all of these hold, else a refusal code: it is a record; it is a quote snapshot; the clock is a `ReplayClock`; the record is usable at the clock; it is fill-eligible; its origin class is `SYNTHETIC_FIXTURE` or `UNVERIFIED_SOURCE` **and** the caller listed it (the default list is empty, `UNKNOWN` never qualifies); and `permitted` is exactly `True` (default `False`).

Design decision (weakens nothing, but you should know about it): the engine had two data origins, `SYNTHETIC` and `PROVIDER`. A historical quote must not pass as either: as `PROVIDER` it would look like a verified provider quote; as `SYNTHETIC` it would skip the eligibility boundary. So the engine gained a third origin, `HISTORICAL` (three small edits in `paper/engine.py`). The bridge never produces `PROVIDER` or `SYNTHETIC` (a test greps the package for those names). A `HISTORICAL` view goes through the same V6-0.1 eligibility boundary as a provider quote, against the engine's clock, which a replay sets to the `ReplayClock` (it is callable). An account is bound to one origin: the historical, provider and synthetic accounts refuse each other's views (`ACCOUNT_MISMATCH`), and a state file written with historical trades will not restore into a provider account. Tests confirm the engine still applies, to historical views: lot/exposure/open-position limits, the daily worst-case budget (`RISK_DAILY_LOSS`), tick and lot rules, displayed size, freshness against the replay clock (`BOUNDARY_REJECTED`), `NO_POSITION`, and the verified/permitted flags. No provider code, permission file or registry entry is touched or imported.

Fills in the ledger carry `data_origin = HISTORICAL`, `provider = historical:<source id>` and `data_state = HISTORICAL_REPLAY`, so a replayed fill is never mistaken for a live or provider one.

## 8. Known gaps and what is not implemented

- (A fixture-only runner now exists, see `docs/HISTORICAL_REPLAY_PHASE3.md`.) No historical provider object, no replay account manager, no trading calendar or session-boundary check. This phase has no loop that advances the clock and trades.
- (Superseded by `docs/HISTORICAL_REPLAY_PHASE3.md`.) A `MarketView` could be built by hand; historical views now need a process-local attestation and are re-validated by the engine, but code in the same process can still mint one. `PROVIDER` views have no such protection.
- Day-level availability (end of trading date) is conservative and unverified; actual publication times of NSE files are unknown.
- The strict JSON-lines fixture format is invented for these tests. Real files (UDiFF bhavcopy zips and CSVs, the contract-wise CSVs, broker candle APIs) would each need a reader that maps columns to this format; none exists, and building one needs the data-use question below settled first.
- `quality_status` is `OK` or `OK_WITH_FLAGS`; `EXCLUDED` records live only in the ledger.
- Not implemented: `CONTRACT_SPEC` records, depth, the `UNKNOWN_COLUMN_LAYOUT` and `OUTSIDE_REQUESTED_WINDOW` checks (codes reserved), cross-source reconciliation.
- Rows are held in memory; no size limit is enforced beyond the machine's.
- Fixtures are invented, dated 2031 and labelled; they show the code works as specified, not that any real market behaves this way. No performance, strategy or backtest claim is made.
- Data-use terms are unchanged from Phase 1: NSE website data is treated as not cleared for virtual-trading replay until NSE confirms in writing. Nothing here changes that.

## 9. Verification run

Commands and results are in the hand-off message of this change; the test file runs with `python -B -W error::ResourceWarning -m unittest tests.test_historical_phase2`, the full suite with `python -B -m unittest discover -s tests -t .`, and the scanner with `PYTHONPATH=src python -B -m niftypaper.safety.scan --root . --include-untracked`.

## 10. Smallest next step

Write a read-only reader for exactly one local end-of-day layout (the UDiFF files already in the research folder, read in place, copied nowhere) that emits this phase's JSON-lines format into a git-ignored folder, build its manifest with the tool above, and report the exclusion ledger, **without** enabling replay. It is end-of-day data, so it can feed signal checks only, never fills. Do it only after the NSE terms question is answered, or treat the output strictly as local research input.
