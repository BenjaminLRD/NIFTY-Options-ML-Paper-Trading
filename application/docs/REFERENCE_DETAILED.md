# Detailed reference (the project's earlier long-form README)

> Kept for operational detail (providers, replay audit tooling, accounting notes). Some statements describe earlier milestones: where it says there is no prediction or no model, see `README.md` and `docs/ML_INTEGRATION.md` for the V6-0.3/0.4 ML replay; the test count is now 904.


A NIFTY options **paper-trading** system under construction. Simulated execution only. This repository is at an early step: a
minimal, tested scaffold with one safety-critical piece (the execution-eligibility boundary, V6-0.1) and a local read-only dashboard
with two selectable data modes (V6-0.2, V6-0.5, dual-mode update) and a virtual-only paper-trading engine. It makes no model, strategy or performance claims.

Where the data comes from, what is connected to what, and the exact prerequisites still missing for any real-data use: `docs/DATA_FLOW.md`. What FYERS's official documentation does and does not establish, and why no FYERS request has been made: `docs/FYERS_VERIFICATION.md`. What to do when an open position has no verified price: `docs/STALE_DATA_RECOVERY.md`.

## Quick start (Windows PowerShell, from the repository root; nothing to install, no credentials, no subscription)
```
python run_dashboard.py
```
Open **http://127.0.0.1:8765/**. It starts in **Synthetic Mode** (invented numbers, banner `SIMULATED DATA - NOT LIVE MARKET DATA`) and works offline. Stop with **Ctrl+C**.
1. *Trade (virtual only):* in the "Paper-trading workspace" pick an instrument, BUY or SELL and the lots, press **Simulate virtual trade**. The message above the form says whether the fill was recorded or refused and why (limits, a quote that is not fillable, an unknown instrument). There is no short selling and no real order exists anywhere in the program. Quantity is in **whole lots**; in the simulator 1 lot = 65 units, a synthetic size that is **not a verified exchange lot size**.
2. *See the result:* the page is a dark dashboard: a top bar (mode, status, virtual cash), data cards, the trading workspace, account and risk cards (a progress bar of committed risk against the daily budget, with the remaining room), open positions, the option chain and recent activity; the workspace has an interactive candlestick chart marked SIMULATED HISTORY (a deterministic series made up for visualisation: not market history, never a quote, never given to the paper engine; drawn by a small local script with no network access), a watchlist, readable contract details (Call/Put, strike, premium, bid, ask) and an order summary whose estimates come from the engine's own cost and risk figures; long explanations (data health, provider setup and terms, risk assumptions, persistence) are in collapsed panels at the bottom. It needs no internet, fonts or scripts. The ledger is also at `http://127.0.0.1:8765/api/ledger` has the full ledger as JSON.
3. *Restart:* every account is saved in `data\paper_state\paper_<mode>.jsonl` inside this repository (whatever folder you start from; the start-up line prints the exact path). Stopping and starting again restores positions, cash and the day's risk budget (the page's "Account and persistence" panel shows the trades restored when the session opened and, separately, the trades now). If a state file is damaged or belongs to another account the page says so and **blocks trading**; it never starts from a blank account on its own. To begin a new virtual account, move or delete that file yourself.
4. *Real-time provider data* is optional and off by default. Choosing it never falls back to synthetic data (it shows `UNAVAILABLE` or `UNVERIFIED` and withholds values), and trading with provider data stays blocked until the data is verified and the provider's terms permission is recorded locally (see below).
5. *Fixture replay, strict verification and evaluation* (all invented data, all optional): `python tools\replay_fixture_demo.py --out-dir C:\replay\run1` writes and checks a replay; the commands it prints create an anchor and run `tools\replay_verify_later.py`; `python tools\strategy_evaluate.py --events ... --state ... --fixtures tests\fixtures\historical_synthetic` gives a simulated evaluation (`docs/STRATEGY_EVALUATION.md`). Results are simulations on invented data and say nothing about real profitability.
6. *Tests:* `python -B -W error::ResourceWarning -m unittest discover -s tests -t .`

## What exists
| Part | File | Purpose |
|---|---|---|
| Quote model | `src/niftypaper/quotes.py` | `Quote` and `QuoteType` (live / delayed / end-of-day settlement / last-trade / unknown). Describes a price without trusting it. |
| Eligibility boundary | `src/niftypaper/eligibility.py` | Fail-closed check that decides whether a quote may be used as a **fill source**. Only a fresh, timestamped, well-formed `LIVE_QUOTE` can pass. |
| Contract specs | `src/niftypaper/contract_specs.py`, `config/contract_specs.json` | Strict loader for lot size, tick size and verification status. The shipped NIFTY entry is marked `UNVERIFIED_OBSERVED`. |
| Safety scan | `src/niftypaper/safety/scan.py` | Pattern-based scan of git-tracked files for accidental secrets, order-placement code, broker API hosts and broker SDK names. |
| Dashboard (V6-0.2) | `src/niftypaper/dashboard/`, `run_dashboard.py` | Local read-only page on `127.0.0.1`: status model (`status.py`), synthetic provider (`sample_data.py`), HTML/JSON rendering (`render.py`), server (`server.py`). Does not use the quote model or the eligibility boundary. |
| Virtual trading | `src/niftypaper/paper/` | Simulated fills only (no real orders): spread, slippage, illustrative costs, lot sizing, risk limits, a ledger of every simulated trade. It sees only a `MarketView`, never a provider. |
| Modes and providers | `src/niftypaper/providers/registry.py`, `permissions.py`, `src/niftypaper/assembly.py`, `dashboard/trading.py` | What each data source needs, the local record of provider-terms permission, and the factories that build a source only when it is chosen. |
| FYERS adapter (V6-0.5) | `src/niftypaper/providers/`, `run_fyers_smoke.py` | Read-only quotes client (the only outbound network code), response validation, symbol-master resolution, provider and manual smoke test. |
| Tests | `tests/` | 904 `unittest` tests (see below). |
| Optional hook | `.githooks/pre-commit` | Runs the scan before a commit. **Not enabled automatically.** |

## How the boundary works
```python
executable = require_fillable(quote, now=now, policy=policy)   # raises IneligibleQuoteError unless every check passes
price = fill_price(executable, Side.BUY)                       # ask for BUY, bid for SELL; accepts only an ExecutableQuote
```
- Rejected as a fill source: end-of-day / settlement, last-traded, delayed and unknown prices; non-`Quote` inputs; subclasses; quotes whose
  fields changed after construction; missing symbol/source/timestamp; naive or non-datetime timestamps; stale or future timestamps;
  missing, non-finite, non-positive or off-tick prices; locked or crossed markets; missing or non-positive sizes; a missing or naive clock;
  an invalid policy; any internal error while checking.
- A rejection is an exception (or `eligible=False` with reason codes). It is never a warning, and nothing falls back to another price.
- The clock is an explicit argument; the staleness limit, future-skew limit and tick size have no defaults.

## What is deliberately absent
No broker SDK, broker API client, order-placement or order-modification code, live-order endpoint, outbound network access, market-data download,
credential storage (credentials are read from environment variables only) or "paper_trading" switch. (The only network code is the dashboard's loopback-only, GET/HEAD-only local server and the read-only FYERS quotes client with two allow-listed hosts.) Safety does not rest on a flag: the code to place orders does not exist, a test checks the
source tree for it, and the safety scan checks tracked files for it. Details and limits: `docs/SAFETY.md`.

## Dependencies
**None** (`dependencies = []` in `pyproject.toml`). The code uses only the Python standard library, for these reasons:
`dataclasses`/`enum` (immutable data model), `decimal` (exact tick arithmetic, no floating-point error), `hashlib` (tamper-evidence digest),
`json` (contract specs, API responses), `csv` (symbol master), `urllib` (the read-only FYERS client only, two allow-listed hosts), `http.server` (local dashboard), `re` (scanner rules), `subprocess` (only to run the local command `git ls-files`; no network), `tomllib` and `ast`
(structure tests), `unittest` (tests). Tested on Python 3.14.6; `requires-python >=3.12`. No virtual environment or package installation is
needed. Any future dependency must be added with a written reason here, and may not be a broker SDK.

## Local dashboard with two market-data modes
The dashboard binds to **127.0.0.1 only** and shows no prediction or signal. Its page has a visible **Market data mode** setting:

| Mode | What it uses | Needs |
|---|---|---|
| **Synthetic Mode (Free)** (the starting mode) | Invented numbers from `SyntheticProvider`, clearly labelled `SYN` under the banner `SIMULATED DATA — NOT LIVE MARKET DATA`. For testing the application and the virtual-trading engine. | Nothing. No credentials are read and **no network request is made**. |
| **Real-Time Mode (requires provider access)** | A real provider you choose in the same setting (today: FYERS read-only quotes). | An account, API credentials, the provider's permissions or subscription, and recorded permission under the provider's terms for virtual trading. The page lists these for the selected provider, shows whether each environment variable is set (never its value), and gives setup steps. |

Switch on the page (two buttons and a provider list; the choice stays until you change it) or start with `python run_dashboard.py --mode realtime`.
"Real-Time Mode" means a real provider is used; it does **not** mean the data is verified real-time. The page shows the provider, a connection status and the
data state: `CONNECTED` (the provider answered and its timing is established), `UNVERIFIED` (it answered but real-time status or the meaning of its time field is not established),
`STALE` (the last answer is too old) or `UNAVAILABLE` (no usable answer). Unavailable real data is **never** replaced by synthetic data: you must choose Synthetic Mode yourself.

### Virtual paper trading
All trades are virtual and there is no real-order endpoint. The trading form appears only when current prices are **verified**: in Synthetic Mode (simulated data) or in Real-Time Mode when the data state is `LIVE`
(provider documents real-time delivery and a quote time, the quote is fresh, and it carries displayed sizes so it passes the V6-0.1 eligibility boundary) **and** the provider's terms permission is recorded locally.
`UNVERIFIED`, `DELAYED`, `STALE` and `UNAVAILABLE` data can be viewed but not traded, and the page says why. FYERS data is currently always `UNVERIFIED`, so with FYERS the trading form stays blocked until FYERS documents
real-time delivery and the meaning of its time field.
A fill is simulated at the touch (ask for BUY, bid for SELL) plus 1 tick of adverse slippage; missing, empty or crossed bid/ask means no fill. Costs, balance and limits are illustrative defaults
(shown on the page): ₹20 + 0.05% of turnover per trade, up to 2 lots per trade, 3 open positions, open premium up to 10% of the starting balance, and a BUY is refused if the projected worst-case loss for the day (realised result after fees, plus all open premium, buy fees and estimated exit costs, plus the new premium, fee and its exit estimate) would pass 2% of the starting balance (about one lot at a time with the defaults). This limits new risk; it is **not** a guarantee that losses stay below the budget, and nothing is ever sold automatically. The exact formula, day rollover (00:00 IST) and restart behaviour are in `docs/PAPER_ACCOUNTING.md`. How cash, fees and P&L are counted: `docs/PAPER_ACCOUNTING.md`.
There is no short selling and no partial fill. Each mode has its own virtual account and ledger. Every record keeps the provider, instrument, quote, receive time, raw provider time field, data state, fill assumptions and costs,
and `GET /api/ledger` lists them with the risk report. Each data source has its own virtual account, saved to `data/paper_state/paper_<account>.jsonl` (git-ignored; change with `--state-dir`), so a restart keeps positions and the day's remaining risk budget; accounts never share cash, positions, risk or ledger. Only one dashboard may use a state directory at a time: a second one finds it locked, shows LOCKED with the reason, and trades nothing (the lock is released automatically if the first one is closed or killed). If that file cannot be trusted, virtual trading is locked and the page says why; to start a new virtual account move or delete the file yourself (this also resets the budget).

### Recording a provider's terms permission (required before virtual trading with provider data)
Copy `config/provider_permissions.example.json` to `config/local/provider_permissions.json` (git-ignored), set `virtual_trading_permitted` to `true` only if the provider's written permission covers it,
and fill in `evidence` (where that permission is kept) and `confirmed_on`. The program cannot check your evidence; it only refuses to trade with provider data until you have recorded it.
Synthetic Mode needs no permission. For FYERS see `docs/provider_terms/FYERS_TERMS_AND_CONFIRMATION.md`.

## Read-only FYERS data (V6-0.5)

**What FYERS data means here.** A successful response does not prove a price is live. FYERS's documentation does not say the Standard-plan feed is real-time,
and its time field `tt` is documented only as "today's time" (the documentation's own examples are exact midnight values, so it is not a quote time). So FYERS
data is shown as **UNVERIFIED**, with the provider time field exactly as received and our own receive time shown separately. `LIVE` and `DELAYED` exist in the
status model but need explicit provider evidence that this adapter does not have. `last_trade_time`-style fields are not quote times either, and a locally
received timestamp does not prove when the market observation was made. Failures show `UNAVAILABLE` with no prices; they never fall back to synthetic data.

### One-time setup (no secrets are stored in the repository)
1. Make sure the written FYERS confirmation and terms are recorded in `docs/provider_terms/FYERS_TERMS_AND_CONFIRMATION.md`.
2. In the FYERS API dashboard create a **non-trading (data only)** app and complete FYERS's own login flow to obtain an access token
   (https://fyers.in/api-docs/v3/api/authentication/). This project does not do the login for you and never sees your app secret.
3. In the PowerShell window where you will run the dashboard, set the two environment variables for that window only (replace the placeholders; do not paste real values into files or chat):
```
$env:FYERS_APP_ID = "<your app id>"
$env:FYERS_ACCESS_TOKEN = "<today's access token>"
```
The token expires daily at 6:30 AM, so repeat step 3 each day. Prefer this over saving it permanently; if you do save it, keep it in your operating-system
user environment or password manager, never in a file inside this folder (`.env` files are git-ignored and flagged by the safety scan).

### Start and stop
```
python run_dashboard.py
```
Open **http://127.0.0.1:8765/** (JSON view: http://127.0.0.1:8765/api/snapshot). Stop with **Ctrl+C**.
FYERS is only contacted after you choose Real-Time Mode with FYERS. Without credentials the page then shows `UNAVAILABLE` with the reason (no prices). It makes at most one FYERS request every 5 seconds.

Optional, one NIFTY option with a symbol resolved from FYERS's official NSE_FO symbol master (the file is downloaded when you choose Real-Time Mode; if the contract cannot be matched exactly, Real-Time Mode shows `UNAVAILABLE` and says why, and nothing is guessed):
```
python run_dashboard.py --option-expiry 2026-11-03 --option-strike 24000 --option-type CE
```
Synthetic demo variants: `python run_dashboard.py --scenario stale` (or `unavailable`). Other options: `--mode synthetic|realtime`, `--realtime-provider`, `--port N`, `--starting-balance N`, `--permissions-file PATH`. There is no option to change the host.

### Manual read-only smoke test (you run it; automated tests never contact FYERS)
```
python run_fyers_smoke.py
python run_fyers_smoke.py --option-expiry 2026-11-03 --option-strike 24000 --option-type CE
```
It makes one quotes request, prints the state, reason, prices and the raw provider time field, and never prints credentials or the Authorization header.
Exit codes: 0 data read, 1 no usable data, 2 bad arguments or no credentials, 3 option symbol not resolved. `UNVERIFIED` is the expected good result.
The first run is also the first check that FYERS accepts the index symbol `NSE:NIFTY50-INDEX` on the quotes endpoint (it is documented for the index stream but not shown on the quotes page).

Data states: `LIVE` and `DELAYED` (explicit provider evidence only), `SIMULATED` (fresh synthetic), `UNVERIFIED` (well-formed provider data, timing not established),
`STALE` (older than 30 s: values withheld) and `UNAVAILABLE` (values withheld). Details and limits: `docs/V6-0.5_RECORD.md`.

## Historical data (Phase 1 design, Phase 2 canonical layer on invented fixtures)
Phase 2 (`src/niftypaper/historical/`, `docs/HISTORICAL_DATA_PHASE2.md`) adds strict record validators, SHA-256 manifests (`tools/historical_manifest.py`), a duplicate/conflict policy with an exclusion ledger, an `available_at` point-in-time gate and a narrow bridge to the paper engine. It is tested on made-up fixtures only (`tests/fixtures/historical_synthetic/`); no real market data has been downloaded or ingested and no source in the registry is enabled. A fixture-only replay runner and an engine-side attestation of historical views follow in `docs/HISTORICAL_REPLAY_PHASE3.md`, and a read-only reconciliation report (`tools/replay_audit.py`), a hash-chained event log with independent verification, and explicit anchor records (`tools/replay_anchor.py`, with a trust model that does not treat a local file as independent) in `docs/HISTORICAL_REPLAY_AUDIT.md`; a strict read-only PASS/FAIL check for saved logs is `tools/replay_verify_later.py`. A transparent, simulated-only baseline strategy and signal engine (`src/niftypaper/strategy/`, `docs/STRATEGY_BASELINE.md`) produces intents for the same declared-plan replay path; it makes no profitability claim. A read-only evaluation of an audited replay (`tools/strategy_evaluate.py`, `docs/STRATEGY_EVALUATION.md`) reports simulated metrics only after the audit passes in full.

**One-command fixture replay** (invented data only, no network, no credentials, no subscription):
```
python tools\replay_fixture_demo.py            # default folder: data\replay_demo\run1 (git-ignored)
python tools\replay_fixture_demo.py --out-dir C:\replay\run2
```
It runs the fixed scripted replay, publishes the hash-chained event log without ever overwriting, audits it, and only if the chain is `VALID`, the books are `CONSISTENT` and the receipt matches does it print the source, event count, head hash, log path and copyable PowerShell commands to create an anchor, show it and audit against it later. The log's first event states the run's input identity (manifest and fixture hashes, settings, source and account), the manifest and fixtures are re-checked when the replay finishes (a change is never reported as success), and the audit checks the state file belongs to the same replay. It does not create an anchor for you, and the anchor is local until you copy it where the replay writer cannot change it; even then nothing shows the events are genuine. Details, exit codes and limits: `docs/HISTORICAL_REPLAY_AUDIT.md` section 9.6.

`docs/HISTORICAL_DATA_PHASE1.md` inventories the end-of-day NSE data held in the research folder (133 trading days, no bid/ask, date-only timestamps), compares sources, proposes a canonical schema (`config/historical_schema_v1.json`, source registry in `config/historical_data_sources.json`) and a replay design.
**Important:** NSE's current Terms of Use say website data may not be used for "gaming, virtual trading or simulation activities" and prohibit systematic or automated collection. Treat NSE-sourced data as not cleared for virtual-trading replay until NSE confirms otherwise in writing. `python tools/inventory_bhavcopy.py <folder> --out report.json` is a read-only inventory tool (it refuses to write inside the folder it reads).

## Demo: ML replay and paper trading (V6-0.3 and V6-0.4, invented data only)

**No real orders are placed, no real market data or provider is used, no credentials are needed, and no profitability is established.**

Setup: Python 3.12 or newer, nothing else to install (standard library only). Start, from the repository root:

```powershell
python run_ml_demo.py
```

Then open `http://127.0.0.1:8765/ml` (main dashboard: `http://127.0.0.1:8765/`; stop with Ctrl+C). It replays 40 invented 2031 trading days through a prediction
interface, a decision layer (BUY / SELL / NO TRADE with a recorded explanation) and the paper engine with its risk limits. Replays are kept as `data/ml_replay/run1`, `run2`, ...
(never overwritten; `--new` makes another, `--list` lists them). Pages: **Main dashboard** (synthetic quotes, virtual cash, positions, risk budget, order form) and
**ML replay** (data status, predictions and explanations, risk limits and refusals, performance, portfolio history, trades, simulator assumptions, limitations).
Screenshots of the running app are in `../NIFTY_PaperTrading_V6_screenshots/` (outside the repository: the safety scan fails closed on binary files, so images are kept out of the scanned tree).

Simulator assumptions: fills at the touch plus one tick of slippage, illustrative fees (20 plus 0.05% of turnover), whole lots only (65 units, an invented size), no partial
fills, no shorting, quotes at most 5 seconds old, risk limits 2 lots per trade, 3 positions, 10% exposure, 2% daily loss budget (worst case, premium included). Known limitations:
invented data only; the research models B1/B2 have no fitted parameters here and no demonstrated edge (V5.1 found none better than the prior), so only a hand-set demonstration model
can trade; one decision a day; no stop rule other than the model's exit; screenshots and the browser check depend on a local Chrome; 5 JavaScript tests need Node.js and are skipped without it.

Guides: `docs/ML_DEMO_GUIDE.md` (commands, navigation, errors), `docs/ML_INTEGRATION.md` (architecture and data flow), `docs/V6-0.4_RECORD.md` (audit and fixes).

## Run the tests (from the repository root)
```
python -B -W error::ResourceWarning -m unittest discover -s tests -t .
```
(Add `-v` for a listing.)
Windows PowerShell scan command:
```
$env:PYTHONPATH = "src"; python -B -m niftypaper.safety.scan --root . --include-untracked
```
Scan exit codes: 0 clean, 1 findings, 2 incomplete scan or error (unreadable, oversized, binary or non-UTF-8 files are reported, never skipped silently).

## What remains unimplemented (not started)
Trading-session calendar and session-boundary checks; raw data storage, schema validation of feeds and checksums; any market-data adapter
(including the end-of-day historical adapter); the live/delayed quote recorder; partial fills and queue modelling; a tamper-evident audit ledger for the dashboard's accounts (its trades are saved append-only in `data/paper_state/`, which is not tamper-proof; the fixture replay has a hash-chained log, see below); a kill switch; (the ML replay with a decision layer exists since V6-0.3, on invented data only); scheduling, alerts and reports;
the forward paper-trading protocol. See `V6_0_Paper_Trading_System_Plan.md` in the earlier research project for the proposed phases.
Nothing here uses or imports the earlier research code or data.

## Known limitations (summary; full list in `docs/SAFETY.md`)
- The boundary is an in-process guard, not a security sandbox. Deliberate bypasses (importing private names, patching functions, rebuilding
  objects) are not prevented.
- It trusts the `quote_type` label an adapter assigns. A future adapter that labels end-of-day data as live would defeat it; adapters need their own tests.
- It does not know trading hours, holidays, order size or displayed depth yet.
- The safety scan is pattern matching and proves nothing about the absence of secrets or order code.
- The NIFTY lot size (65) and tick size (0.05) in `config/contract_specs.json` were observed in sample data and are **not verified** against an exchange circular.
