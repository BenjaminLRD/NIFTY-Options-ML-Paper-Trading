# Historical NIFTY options data, Phase 1: what we have, what we can legitimately get

> **Attribution note.** This record was written by the Claude Code assistant during Claude-assisted development. "The assistant" (or "I", where it remains) means that assistant; "you" means the project owner. It records what the assistant did and found. It does not describe the owner's own contribution, which is not documented here.

Date: 2026-10-27. Scope: inventory, source evaluation, schema and replay design only. No strategy, no backtest, no purchase, no credential used, nothing committed.
Evidence kept in the repository: `docs/historical_data/existing_data_inventory.json` (made by `tools/inventory_bhavcopy.py`, read-only), `config/historical_data_sources.json` (source registry), `config/historical_schema_v1.json` (draft schema), `tests/test_historical_phase1.py`.
"Verified" below means the assistant read or measured it in this phase; "assumed" and "unknown" are labelled.

## 0. Findings that matter most
1. **We hold end-of-day data only.** 133 trading days of NSE F&O bhavcopy (2026-02-23 to 2026-09-29) plus a thin set of contract-wise downloads. There is no bid, ask, depth or time of day anywhere. Settlement, close and last prices cannot be used to simulate realistic fills.
2. **No free source we found offers historical bid/ask.** Free and low-cost routes give daily files or OHLCV candles. Realistic intraday replay needs quote or order-level data, which only NSE's research programme or its paid products appear to offer (not yet examined in detail).
3. **NSE's current Terms of Use, as written, rule out what V6 does with NSE data.** The page (updated 29/10/2025) says the user shall not "use or make the data or Content available on the Website / Mobile Application, for any gaming, virtual trading or simulation activities under any circumstances whatsoever", and prohibits "systematic or automated data collection activities (including scraping...)" on the Website. It defines the Website as `https://www.nseindia.com/`; the bhavcopy files are linked from its reports page but served from a separate archive host, so whether the clauses reach them is a legal question the assistant cannot settle. NSE's copyright policy separately lets users "view, print copies and download the Content for personal, non-commercial or educational purpose". **Treat NSE-sourced data as NOT cleared for virtual-trading replay until NSE says otherwise in writing.** NSE Indices has similar automation limits and "personal, non-commercial use only".
4. **Our own earlier acquisition was scripted.** The V5.0 and V5.1 downloads (120 zip files) were fetched by script from the archive host, per `V5_*_Download_Log.csv`. Given clause 3, the assistant recommended no further scripted downloading from NSE hosts until this is clarified; single manual downloads through the reports page are the safest route.
5. **The instrument token is not a permanent contract key** (reused for different contracts over time) and **volume/turnover units are a trap** (details in section 1). Both are handled in the draft schema.

Not legal advice. The clauses are quoted in the registry so you can judge them yourself.

## 1. Existing data inventory
Read-only audit of the research folder (`<path to research folder>`) (nothing there was changed; see the note on the PDF at the end). Command: `python tools/inventory_bhavcopy.py <ML folder> --compact --out docs/historical_data/existing_data_inventory.json` (about 2 minutes).

**NSE F&O UDiFF bhavcopy** (`BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000`)
| Item | Finding (verified) |
|---|---|
| Files | 201 copies (13 loose CSVs, 68 extracted in `V5_0_Run/data`, 55 zips in `V5_0_Acquired_Raw`, 65 zips in `V5_1_Acquired_Raw`) = **133 distinct trading days**, 443 MB of copies. Copies of the same day are identical (no conflicts). |
| Range | 2026-02-23 to 2026-09-29. **125 contiguous trading days from 2026-03-30**; every weekday gap in that stretch is an official NSE trading holiday (31-Mar, 3-Apr, 14-Apr, 1-May, 28-May, 26-Jun, 14-Sep; checked against NSE's published 2026 holiday list). Before 30-Mar only 8 days exist; **15 trading days (25-Feb to 24-Mar) are missing.** NSE lists files up to 2026-10-26, so 2026-09-30 to 2026-10-26 are not held. |
| Layout | One column layout in all 201 files (34 columns incl. reserved). Instrument types IDO (index options), IDF, STO, STF. NIFTY index options: **240,752 rows**, 1,534 to 2,213 a day, 49 expiries seen (32 already expired), strikes down to 12,000 and up to 34,500. |
| What the prices are | End-of-day values per contract: open, high, low, close, last, previous close, settlement, underlying value, volume, transactions, turnover, open interest, change in OI. **No bid or ask column of any kind.** |
| Timestamps | `TradDt` / `BizDt` are dates. **No time of day** anywhere; session id is always `F1`. They are trading dates, not observation times. |
| Trading vs carried prices | Of the NIFTY option rows, 55.8% traded that day (volume above zero). On a no-trade row open/high/low are 0.00, but **close is the carried previous close (100% of rows have close above zero) and last is also carried on many (292 of 701 no-volume rows on 2026-06-05)**. Settlement is a separate figure that can differ a lot from the carried close (5014.10 against 4442.25 on that day). So close and last prove a trade only when volume is above zero. |
| Units | NIFTY option volume looks like **contracts (lots)**, not units: only 9 of 1,059 traded rows on 2026-06-05 were multiples of the 65 lot, and turnover divided by volume is about lot size x strike (it looks like notional value, not premium). Stock options looked like units. Inferred from the numbers; NSE's field definitions (the "New Bhavcopy Format readme") were not read. |
| Contract identity | Each row carries underlying, expiry, strike and CE/PE, so the natural key is available. **`FinInstrmId` is reused**: 827 of 8,040 NIFTY option tokens belonged to different contracts at different times; no token was shared by two contracts on one day, and every contract kept one token for its life in these files. |
| Expired contracts | There is no list of expired contracts. A contract appears daily until its expiry date and then vanishes; the expiry-day row exists for 29 of 32 past expiries (the 3 missing are in the sparse period). Settlement on expiry day is therefore usually available. |
| Specifications | Lot size field `NewBrdLotQty` = 65 on every row held (2026-02 to 2026-09). **No tick size.** Lot size before 2026-02-23 is not in any file we hold. |
| Provenance | `V5_0_Download_Log.csv` and `V5_1_Download_Log.csv` give URL, status, bytes and SHA-256 per zip (`V5_1` also the retrieval time in UTC); `V5_0_Raw_File_Hashes.csv` and `V4_7_Audit_Input_Manifest.csv` give hashes. The 13 loose files from Feb to Jun 2026 have hashes but **no record of how they were obtained** (the manifest calls them "official_candidate"). |

**NSE contract-wise historical downloads** (`OPTIDX_NIFTY_{CE,PE}_<from>_TO_<to>*.csv`): 5 distinct files (10 with copies), 2025-10-01 to 2026-06-29, 4,042 distinct rows (8,084 counting the copies). Columns: open, high, low, close, LTP, settle, number of contracts, turnover, premium turnover, open interest, change in OI, underlying value; `-` marks no value. **All rows are for one expiry (29-Sep-2026) and about 12 strikes a day per option type**, so this is a thin slice, not a chain. The PE file for 30-Mar to 29-Jun-2026 is absent. Each download spans about 90 days. No bid/ask, no lot size.
**`FOSett_prce_12032026.csv`**: one day (12-Mar-2026) of settlement prices for futures and one value per underlying for options, not per option contract.
**Derived research files** (`V4_*`, `V5_*` CSVs, normalised bhavcopy, reports): derived from the above, not raw; frozen V5.1 artifacts were not touched.

**What is missing for realistic fills and costs:** bid and ask, displayed sizes and depth, time of day for any price, tick size, lot size before February 2026, freeze quantity, a verified field dictionary, and the fee schedule (brokerage, exchange charges, securities transaction tax, GST, stamp duty are rules to be sourced, not data fields). The trading-holiday list is available (NSE page) for the calendar.

## 2. Source comparison
| Source | Access and cost | Granularity and coverage | Bid/ask | Terms and permission | Supports |
|---|---|---|---|---|---|
| NSE UDiFF F&O bhavcopy | Reports page, free; archive host links | Daily, per contract; held 133 days; format start about July 2024 (assumed; sources conflict) | No (verified) | See finding 3; automation and simulation clauses | Settlement research yes; EOD replay yes; intraday no |
| NSE contract-wise download | Interactive form on nseindia.com, free; about 90-day windows (observed) | Daily, one contract set per request | No | Same terms; automation on the Website itself is clearly covered | As above |
| NSE contract master | Daily contract file (`NSE_FO_contract_*.csv.gz`) on the reports page; historical master is in NSE's chargeable "voluminous" basket | Specifications incl. lot size (per NSE's own list) | n/a | Same terms | Fills specs (not opened) |
| NSE Indices historical index data | niftyindices.com | Index series, coverage not checked | n/a | "personal, non-commercial use only"; automated collection prohibited without written consent | Not examined |
| NSE research programme | Request form to nseri@nse.co.in; "strictly limited to academic and non-commercial use"; free for reasonable volume, cost-based for large (SEBI circular of 20 Dec 2024, per summary) | NSE's SEBI-mandated list names tick-by-tick trades and orders history (no client or broker data) in a chargeable "voluminous (>2 GB)" basket | Unknown: order data could allow a book to be rebuilt | Eligibility of a private person, and whether paper-trading replay counts, unknown | Possibly intraday; unverified |
| NSE Data & Analytics (paid) | marketdata@nse.co.in; F&O trade data and order-and-trade data, specs and sample files offered; tariff not read | Order and trade level | Unknown | Undertaking not read | Possibly intraday; unverified; cost unknown |
| FYERS history API | Standard plan free with limits | Candles (seconds on Prime only); daily 5 years, intraday 1 year, expired F&O daily 2 years, intraday 10 requests a day | No | API Terms Section 11 bars paper trading; written confirmation not seen | Partial; no intraday fills |
| Other broker APIs (Groww, Dhan, Upstox, Kite Connect, Angel One) | Account needed; Groww and Upstox expired data on paid plans, Kite 500 rupees a month with no expired options, Dhan 5 years of rolling expired options at ATM +/-10, Angel One free candles (expired options unverified) | Candles with volume and OI | No | Not read apart from Groww and FYERS | Partial; no fills |
| Third-party datasets | Not evaluated | Unknown provenance and licence | Unknown | Mirrors of NSE data inherit NSE terms | Not recommended |

Official pages read in this phase: NSE Terms of Use, Copyright policy, reports page, holiday list, Research Initiative page, paid-data page, the "Data List under the NSE Data Sharing Policy" (text extracted with a rough tool, partly garbled), and NSE Indices terms. Broker comparison rests on documentation search results plus the earlier FYERS and Groww reports; the Dhan, Upstox, Kite and Angel One entries were not read in full.

## 3. Recommended initial dataset
There is **no verified free, permitted source of historical bid/ask**, so be clear about what each tier can do:
- **Now (no cost, no new downloads):** keep the 133-day UDiFF set as the only EOD dataset, status **"research and schema development only; NOT cleared for virtual-trading replay"**. Build and test the canonical layer on made-up fixtures, not on this data.
- **Ask NSE (no cost):** a short written request to the research programme (draft in the appendix) covering (a) whether a private individual qualifies, (b) whether paper-trading replay is a permitted use of bhavcopy and of research data, (c) whether script-assisted bulk download of the archive files is allowed, (d) what order-level data exists for NIFTY options, its look-back, format and cost.
- **Intraday realism:** only quote or order-level data can give spread, so pick between NSE research or paid data (if NSE agrees and the cost is acceptable) and forward-collecting live quotes under a provider whose terms allow storage. Candles from brokers are useful for signals but not for fills.
- Do **not** mix sources to fill gaps, and do not treat settlement or close as executable.

## 4. Canonical schema (draft, `config/historical_schema_v1.json`)
Record kinds are separate types so settlement-only data cannot be used as quotes: `EOD_CONTRACT_DAY` and `INTRADAY_BAR` and `UNDERLYING_EOD` have no bid or ask field at all (checked by test); only `QUOTE_SNAPSHOT` (bid, ask, tz-aware observed_at, documented quote-time semantics, lot and tick size) can be a fill source, and then the V6-0.1 eligibility boundary still applies at replay time. Plus `CONTRACT_SPEC` (lot and tick with verification status) and `EXCLUSION`.
- **Keys:** `contract_key = underlying|expiry|strike|CE/PE`; the source instrument id is stored but is not a key.
- **Missing values:** null, never filled. `0.00` / `-` for "no trade" becomes null with a quality flag (`PRICE_ZERO_NO_TRADE`, `CLOSE_CARRIED_NO_TRADE`, `LAST_CARRIED_NO_TRADE`, `VOLUME_UNIT_INFERRED`, `TURNOVER_MEANING_UNVERIFIED`, ...); the raw file keeps the original.
- **Timestamps:** EOD records state `observed_at = null`, precision `DAY`, and an `available_at` of trading date 23:59:59 IST (conservative; publication time not verified). Every record carries `available_at`.
- **Provenance on every record:** source id, file path, file hash, content hash, row, retrieval time (nullable), parser version, ingestion time, quality status.
- **Raw store:** unchanged files plus a `MANIFEST.jsonl` of hashes under `data/raw/{source_id}/` (git-ignored, never inside the research folder); a file whose hash differs from its manifest line is rejected.
- **Duplicate and conflict policy (deterministic):** identical copies keep the first by sorted path then file hash, the rest are excluded as duplicates with a pointer; **conflicting copies are all excluded and listed, never resolved by "newest" or "largest"**; different sources are never merged. Every exclusion is recorded.

## 5. Replay architecture (design only; nothing is implemented)
Components, all reading a hashed, immutable dataset:
- `ReplayDataset`: manifest hash, schema version, exclusion-ledger hash; opened read-only.
- `ReplayClock`: the only source of "now"; strictly increasing; passed explicitly (the engine already takes a clock, and the risk day rolls on it).
- `PointInTimeView(as_of)`: returns only records with `available_at <= as_of`. Outcome fields (next-day settlement, labels) live in a separate structure that signal code cannot import.
- `HistoricalProvider`: wraps the view in the existing provider shape (`provider(now) -> Snapshot`) with a new data origin `HISTORICAL` and a data state that is **never** `SIMULATED` or `LIVE`.
- `ReplayRunner`: advances the clock event by event and records a run manifest (dataset hashes, git commit, parameters, counts by exclusion reason, an event log with `as_of` and the record references used).
- `ReplayAccount`: a separate virtual account per run (`replay:<dataset id>`), its own state file; never shared with the synthetic or provider accounts.

How each hazard is prevented:
| Hazard | Control |
|---|---|
| Look-ahead | `available_at` gating; a test that poisons future records and checks no earlier decision changes; an EOD value is usable no earlier than after its session; settlement of day D is never visible on D. |
| Settlement or LTP as executable | Those kinds have no bid or ask; replay mode `EOD_RESEARCH` marks every view unverified so the paper engine refuses (existing `DATA_NOT_VERIFIED`); a settlement-proxy fill mode is out of scope and would need its own label and account. |
| Fills without prices or specs | Fills only from a `QUOTE_SNAPSHOT` observed after the decision time plus a latency setting, never at the same instant; missing lot or tick size refuses the trade (existing `LOT_SIZE_UNKNOWN`); a missing quote refuses. |
| Mixing synthetic and historical | Origin `HISTORICAL`; engines already refuse other origins (`ACCOUNT_MISMATCH`); the synthetic provider cannot be selected in a replay session; every ledger record names its dataset hash. |
| Touching frozen research | Reads only from `data/raw`; the research folder is read through a hash-checked copy-in or not at all; writers refuse any path under configured frozen roots; a test compares the research folder's hash inventory before and after. |
| Unauditable exclusions | `exclusions.jsonl` per dataset; run manifest lists counts per reason; nothing is dropped silently. |

## 6. Known limitations and the smallest next step
Limitations: terms unresolved (finding 3); EOD only; 15 early trading days missing and nothing after 2026-09-29; lot size before 2026-02-23 unknown; field definitions and publication times unread; tick size unknown; no fee schedule yet; look-back and format of NSE research data unknown; all comparison data on non-NSE sources is documentation-level, not hands-on.

**Smallest concrete next step (no paid access, no frozen-research risk):** implement the *canonical layer as code, on made-up fixtures only*: schema validators for the record kinds, the deterministic duplicate and conflict policy with an exclusion ledger, the manifest-hash check, and the `available_at` gate (a `PointInTimeView` over in-memory records), with tests that poison future data and prove an EOD record cannot reach the paper engine as a fill source. It needs no NSE data and no decision on the terms. In parallel, **you** send the written request in the appendix to NSE (the assistant has not sent anything).

## Notes on how this phase was run
- No data were downloaded, no API was called and no credential used. The assistant read NSE and NSE Indices pages in the in-app browser, and read the research folder with the inventory tool.
- **One side effect to report:** opening NSE's data-list PDF link in the in-app browser triggered a file download that was saved into the research folder (the session's working directory). The assistant confirmed its hash, moved it to the git-ignored `data/source_docs/` of this project, and verified the research folder again matches its pre-session inventory (425 files). The assistant did not intend to write there, and it avoided that kind of link afterwards.
- The earlier V4 to V5 notebooks and scripts in the research folder were not run or imported.

## Appendix: draft request to NSE (not sent; edit before use)
> Subject: Request for research access to NSE F&O historical data, and clarification of permitted use
> I am an individual researcher building a private, non-commercial project that replays historical NIFTY options data to study and paper-trade (virtual trading only; no real orders; no redistribution of any data).
> 1. May a private individual apply under the NSE Research Initiative / Data Sharing Policy, and what eligibility is needed?
> 2. Your Terms of Use state that data on the website may not be used for "gaming, virtual trading or simulation activities". Does this apply to the F&O bhavcopy and contract-wise files, and would research access under the Data Sharing Policy allow replay for virtual trading? If not, what licence would?
> 3. Is downloading the daily bhavcopy files with a script (one file a day, a few seconds apart) permitted, or should they be downloaded manually?
> 4. For NIFTY index options, which order-level or tick-level data are available, for what look-back, in what format, and at what cost?
> Please reply in writing; I will keep your reply with the project's records.
