# Dual-mode dashboard and virtual paper trading: implementation record

Scope: two selectable market-data modes in the same dashboard, a provider interface, status and gating rules for real data, and a virtual-only paper-trading engine.
Nothing was committed. No real order, no real-order endpoint, no signal generation. No request was made to any provider while building or testing this (the mocked tests
and one end-to-end run without credentials only); the FYERS adapter is still unproven against the real service.

## What changed
Added: `src/niftypaper/paper/{__init__,engine}.py` (virtual engine); `src/niftypaper/providers/{registry,permissions}.py`; `src/niftypaper/assembly.py`;
`src/niftypaper/dashboard/trading.py`; `config/provider_permissions.example.json`; `tests/test_modes.py` (30), `tests/test_paper_engine.py` (18); this record.
Changed: `dashboard/{server,render,status,sample_data,__main__}.py`, `providers/{fyers_provider,fyers_quotes}.py` (lot size and tick carried to option rows; two new error codes),
`.gitignore` (`config/local/`), `README.md`, `docs/SAFETY.md`, and tests `test_dashboard.py`, `test_fyers.py`, `test_structure.py` (adjusted, see below).
Unchanged, byte for byte: the V6-0.1 eligibility boundary and quote model, `contract_specs.py`, their tests, and `fyers_http.py`, `fyers_symbols.py`, `fyers_smoke.py`.

## Design
- **Mode setting:** the page has Synthetic Mode (Free) and Real-Time Mode (requires provider access) buttons plus a provider list; a POST to `/mode` switches. The starting mode is Synthetic Mode (`--mode realtime` starts in Real-Time Mode).
  This changes the V6-0.5 default (FYERS first): you asked for a free mode that needs no credentials, and starting there avoids any provider contact until you choose it. Revert by launching with `--mode realtime`.
- **Provider interface:** a provider is `provider(now) -> Snapshot | None` with an optional `last_error`. `providers/registry.py` describes each source (cost, credentials, permissions, terms, setup steps), `assembly.py` holds the factories, `permissions.py` the local terms record.
  Adding a provider = adapter + registry entry + factory + permission entry; the dashboard and engine do not change.
- **Separation:** the engine (`paper/`) imports no provider, dashboard, assembly or network module (tested). `dashboard/trading.py` turns a snapshot into `MarketView`s and decides whether prices are verified. No signal code exists.
- **Connection status:** `CONNECTED` (LIVE or provider-confirmed DELAYED: provider answered and timing is established), `UNVERIFIED`, `STALE`, `UNAVAILABLE`; synthetic data shows `SIMULATED`. The detailed data state is shown next to it.
- **Verified prices:** `SIMULATED` for synthetic data, `LIVE` for provider data. `LIVE` needs documented real-time delivery plus a documented quote time (last-trade times are never treated as quote times); no current adapter supplies that evidence.
  DELAYED shows as CONNECTED (the provider answered and says it is delayed) but is not tradable.
- **Terms:** virtual trading with provider data also needs `config/local/provider_permissions.json` to record the provider's written permission. Synthetic Mode does not. Viewing provider data does not.
- **Engine:** fills at the touch plus 1 tick adverse slippage; quote and fill are stored separately; missing, empty or crossed bid/ask is refused; provider quotes must pass `require_fillable` (needs quote time, displayed sizes, tick, freshness within 5 s); displayed size is checked when known;
  illustrative costs (₹20 + 0.05% turnover), lot sizing from the contract's lot size, limits (2 lots per trade, 3 positions, 10% exposure, 2% daily worst-case loss cap, see docs/PAPER_ACCOUNTING.md), no shorting, no partial fills. Separate account and ledger per mode. Records hold provider, instrument, quote, receive time, raw provider time, data state, connection, fill basis, costs and assumptions.
- **POST safety:** two endpoints only, per-run form key, Host and Origin checks, strict parsing, 2 KB limit, 303 redirect with a one-time server-side notice (nothing from the request is reflected).

## Tests changed and why
- `test_structure.py`: assembly code may carry the provider's name in identifiers (like `providers/`); every other broker name and all order-related names remain banned.
- `test_dashboard.py`: the CLI flag list (`--mode`, `--realtime-provider`, `--permissions-file`, no `--provider`) and the URL check also permits the loopback Origin string the server builds. The existing "no forms / 405 for every POST" tests still pass because a bare `App` (no modes, no trading) still accepts no POST at all.
- `test_fyers.py`: CLI tests follow the new flags; the option-symbol failure test now checks the lazy, fail-closed behaviour (provider `UNAVAILABLE`, nothing guessed).
- The key `virtual_trading_permitted` (not `paper_trading_...`) and `fee_per_trade` (not a broker-named word) were chosen so existing name checks stay intact.

## Results (Windows, Python 3.14.6, repository root, 2026-10-26)
| Command | Exit | Result |
|---|---|---|
| `python -B -W error::ResourceWarning -m unittest discover -s tests -t .` | 0 | 180 tests, OK (132 earlier + 48 new) |
| `python -B -m unittest tests.test_modes tests.test_paper_engine` | 0 | 48 tests, OK |
| `PYTHONPATH=src python -B -m niftypaper.safety.scan --root . --include-untracked` | 0 | 48 files, 0 findings, 0 skipped |
| `python -B v5_1_freeze_protocol.py . --verify --expected-combined 6c63883d...a4993d` (in `ML`) | 0 | `VERIFIED 6c63883d1f9055a451d16b33eb1e02614b4c801267973027f22e233d60a4993d` |
Also: SHA-256 inventory of the 425 files in `ML` identical to the snapshot taken before V6-0.2 (V5.1 not rerun).
End-to-end run of `run_dashboard.py` on a test port with no credentials: Synthetic Mode page served with the banner and two forms; a virtual BUY was recorded (fill 120.15 against quote 119.90 / 120.10); switching to Real-Time Mode showed `UNAVAILABLE`
("FYERS credentials are not configured"), no prices, no synthetic names, no trading form, header `X-Data-Synthetic: none`; a POST with a wrong form key got 403; switching back worked; the process was stopped. No outbound request was made.
Mutation check: 23 deliberate breaks (engine ignoring the verified flag, permission, slippage, crossed quotes, the eligibility boundary, displayed size, lot limit or shorting; unrealised P&L on unverified prices; UNVERIFIED treated as verified or CONNECTED; permission ignored;
fallback to synthetic when a provider fails to start; form key, Origin and path checks removed; duplicate form keys; merged accounts; the synthetic factory reading credentials or touching the network; permission file accepting a truthy value or a future date; the requirements list hidden) were each caught.

## A bug found while verifying
A full-suite run once failed with a connection reset (`WinError 10053`) on a refused POST: the server answered without first reading the request body, and on Windows closing a connection with unread data can reset it before the client reads the answer.
The server now reads the (size-limited) body before every POST and before other refused methods; a regression test sends 25 refused POSTs with bodies against both app kinds. A first attempt at the fix read the body twice and hung two tests; that was caught by the suite and corrected.
After the fix the two HTTP test modules were run 30 times in a row with no failure, and the full suite again (see results).

## Not tested / not done
- Nothing was tested against a real provider. FYERS still needs your manual smoke test (`python run_fyers_smoke.py`); its GET host and index symbol remain unconfirmed (see `docs/V6-0.5_RECORD.md`).
- The page was served and its HTML checked; it was not looked at in a graphical browser, so layout is unreviewed.
- With FYERS data the trading form stays blocked: FYERS data is `UNVERIFIED` and has no quote time or displayed sizes. This is intended until the provider documents real-time delivery and what its time field means. Real-provider trading paths are exercised only with a test provider that carries the evidence.
- Ledger and positions live in memory only. Costs, slippage and limits are illustrative, not actual charges. No partial fills, queue position, margin, shorting or expiry handling.
- The form key stops other web pages, not other programs on this computer. The permission file records a statement; it cannot verify the evidence.
- Switching to Real-Time Mode with `--option-*` downloads the roughly 16 MB symbol master inside the request, so the page may pause for a few seconds.

## Launch
From the repository root: `python run_dashboard.py` then open http://127.0.0.1:8765/ (Ctrl+C to stop). Choose a mode on the page. For Real-Time Mode with FYERS set `FYERS_APP_ID` and `FYERS_ACCESS_TOKEN` in that terminal first (README has the steps) and, to trade with provider data once it is verifiable, record the permission as described there.
