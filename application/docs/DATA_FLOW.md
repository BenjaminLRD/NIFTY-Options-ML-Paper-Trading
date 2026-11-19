# Data flow, integration status and data-source prerequisites (V6-0.2 integration)

Written from the code as it is now. Nothing here was measured against a real market, and no provider has been contacted or verified.

## 1. The two paths

```
DASHBOARD PATH (interactive, one quote at a time)
  provider(now) -> Snapshot                      SyntheticProvider (invented numbers) | FyersProvider (read-only quotes; optional; needs credentials)
    -> status.assess()  -> Health(state)         SIMULATED | LIVE | DELAYED | UNVERIFIED | STALE | UNAVAILABLE   (fail closed; provider data is at best UNVERIFIED
                                                  without explicit evidence of real-time delivery and a documented quote time)
    -> trading.build_views() -> {symbol: MarketView}   verified only for fresh SYNTHETIC data, or LIVE provider data; none at all for STALE / UNAVAILABLE
    -> PaperEngine.simulate(request, view)        the execution boundary: eligibility (live-type quote, quote time, sizes, tick, no crossed/locked market),
                                                  V6-0.1 boundary, lot/tick rules, risk limits, daily risk budget, costs, slippage
    -> JsonlStore.append()                        data/paper_state/paper_<account>.jsonl, append-only, single-writer lock, restored and re-checked at start
    -> dashboard page / GET /api/ledger           cards, tables, risk bar, ledger

CHART (display only)
  Snapshot rows -> trading.build_workspace() -> chart.py (deterministic SIMULATED HISTORY) -> JSON block in the page -> chart.js draws it
  Nothing flows back: chart.py imports only hashlib and decimal; only trading.py imports it; the engine, store, strategy and evaluator never import the dashboard.
  A failing chart degrades to a "No chart" message. Tests replace the chart with wild values or make it raise: fills, ledger and the saved state stay byte-identical.

FIXTURE REPLAY PATH (offline, invented 2031 data, no dashboard involved)
  manifest-checked fixture files -> ingest -> point-in-time gate -> bridge -> PaperEngine (origin HISTORICAL) -> hash-chained event log + replay state
    -> audit -> strict verify-later -> evaluator (simulated metrics)
  The strategy (index_sma_trend v1.0.0) produces intents only on this path.
```

## 2. What is and is not connected

- **Dashboard to engine to ledger:** connected and tested end to end (accepted BUY, risk refusal, SELL validation, valid exit, restart, stale data).
- **Dashboard to a decision layer:** **not connected, deliberately.** The dashboard shows no signals or predictions. `index_sma_trend` needs a series of daily index closes with known availability times; the only such series in the project is the invented fixtures. Wiring it to the dashboard would need a permitted source of index closes (section 4).
- **Chart to anything else:** not connected (above). The chart is not a feature, a quote, a stored value or an input to any decision.
- **Replay/evaluator to dashboard:** not connected; they audit saved fixture replays only.
- **Frozen research (V4.4 to V5.1, `ML`):** untouched and not imported.

## 3. What the page states

Data mode and source with the data state; the age of the data and the limit it is judged by (for provider data the age is measured from the local receive time, which says nothing about when the market observation was made); and one fixed line: *quotes are not trading*: "live" would describe quote data only; there is no broker connection, every trade is virtual and no real order can be sent. Synthetic values carry `SYN`, provider values carry their state name, the chart carries `SIMULATED HISTORY`, and provider mode never shows synthetic words or values.

## 4. Smallest real-data integration, and the exact missing prerequisites

The smallest supported integration already exists: the read-only FYERS quotes adapter (`providers/fyers_*`), off by default, never contacted by tests (they use test transports and a clearly named TEST_PROVIDER fixture). It cannot be called verified. Missing, in order:

1. **Permission evidence.** `docs/provider_terms/FYERS_TERMS_AND_CONFIRMATION.md` still has its confirmation table blank, and `config/local/provider_permissions.json` does not exist. FYERS's terms (section 11, quoted there) prohibit paper/virtual trading with its data unless FYERS allows it. Until the confirmation is recorded the dashboard refuses to trade with provider data.
2. **Credentials.** A FYERS data-only app and a daily access token, set by the user in the environment (never in files). None are present here, so nothing was called.
3. **Real-time evidence.** FYERS documents neither real-time delivery on the free plan nor the meaning of its time field, so its data is UNVERIFIED and trading with it stays blocked even with credentials and permission.
4. **Contract specifications.** NIFTY lot size 65 and tick 0.05 in `config/contract_specs.json` are `UNVERIFIED_OBSERVED`; no exchange circular is recorded.
5. **Cost and access terms.** FYERS's free Standard plan is documented as rate-limited (5 requests/s, 5,000/day); the exact terms for the intended use and any cost must be re-read from the live pages before any call.
6. **NSE data terms** for anything NSE-sourced used in simulation (see the README warning).

Until all of these hold, real-data integration is limited to what is tested with test transports. No real-market integration is claimed.

## 5. Fail-closed matrix (dashboard level, tested)

No order is recorded, and the page or the engine says why, for: provider silent about latency; confirmed delayed; last-trade time instead of a quote time; unknown time meaning; verified data without recorded permission; missing displayed sizes; crossed, locked, off-tick or zero-bid quotes; no data; stale data (new entries and exits alike, since there is no verified price to trade at); and the synthetic stale and unavailable scenarios.


## 6. ML path (V6-0.3)

A third, separate path exists and is never mixed with the two above: invented 2031 fixtures -> ingest and point-in-time gate -> features (`ur1`, `lr1`) -> `predict()` -> decision layer
(`MlGatedStrategy`) -> existing driver, bridge, risk limits and paper engine (account `replay_synthetic_ml_demo`, origin HISTORICAL) -> `events.jsonl`, `decisions.jsonl`, `summary.json`
-> `/ml` (read-only). The synthetic quote generator and the chart's SIMULATED HISTORY series are not inputs to it; provider data is not an input to it. Details: `docs/ML_INTEGRATION.md`.
