# Baseline strategy and signal engine (`src/niftypaper/strategy/`)

**Everything here is simulated and virtual-only.** The baseline is a plumbing exercise for the signal, intent and audit path. It is **not** a claim that the rule has any edge, nothing was tuned, and passing tests says nothing about profitability. No parameter was chosen by looking at results, and nothing was evaluated against the frozen V5.1 window (only invented 2031 data is used).

## 1. The rule, written before the code: `index_sma_trend` v1.0.0

Input it uses: `UNDERLYING_EOD` records for one underlying (the index end-of-day close), which the existing schema does provide as a dated series. It uses nothing else for the direction.

At a decision time T (a timezone-aware instant the caller names):

1. Take the index closes that are **visible at T** (`available_at <= T`; a day-level record is available only from the end of its trading date, i.e. the close of day D can first inform a decision after 23:59:59 IST on D).
2. Need at least `window` = 5 closes, the newest `window` of them. Reject (no signal, reason given) if: fewer closes; the newest close is more than `max_close_age_days` = 4 calendar days before T's date; two consecutive closes are more than `max_gap_days` = 5 days apart; a close is missing, zero or carries a quality flag other than the benign default-availability one.
3. `sma` = exact decimal mean of those closes. `last` = the newest close.
4. `last > sma` is BULLISH, `last < sma` is BEARISH, equal is NEUTRAL.
5. Not holding the strategy's contracts: BULLISH opens one long call (`BUY_TO_OPEN`), BEARISH one long put; NEUTRAL does nothing. Holding a call: exit (`SELL_TO_CLOSE`, all held lots) when the view is no longer BULLISH. Holding a put: exit when it is no longer BEARISH. Otherwise hold (no signal, reason `HOLD`).
6. Long options only. No shorting, no selling to open, no scaling, one position at a time.

Assumptions (all stated, none tuned): the numbers 5, 4, 5 are round placeholders; the call and put contract keys are chosen by the caller (the strategy does not select strikes or expiries, because no option-chain input is defined); the decision schedule is chosen by the caller; the quote used is the newest visible `QUOTE_SNAPSHOT`; lots default to 1.

Quote and data-quality status: a signal also requires, at decision time, that the contract's newest visible quote exists, is fill-eligible and no older than `max_quote_age` (the replay's own limit). Otherwise the evaluation is `INPUT_REJECTED` with a reason, never a signal and never a substitute quote. Nothing is substituted when data is missing.

## 2. Lifecycle

`evaluate` (pure: data visible at T + current holdings -> `Evaluation`) -> `Signal` (id, strategy name/version, decision time, contract, direction, action, side, lots, reason and input values, invalidation condition and `valid_until`, quote status, `data_observed_through`) -> **intent conversion** at a separate execution time E >= T (`valid_until` must not have passed; the rule is re-evaluated at E with data visible at E and must still ask for the same action; the holding precondition must still hold) -> an `Intent` in the existing replay format -> the existing runner and engine (bridge, risk limits, fees, slippage, account isolation) -> events, state, audit.

Three times are kept apart: decision time T, data observation time (`data_observed_through <= T`, enforced), execution time E.

A signal that is not turned into an intent stays in the decision log with its reason (`SIGNAL_EXPIRED`, `INVALIDATED_BEFORE_EXECUTION`, `PRECONDITION_FAILED`). An intent the bridge or engine refuses stays in the event log as `INTENT_REFUSED` with the engine's code (for example a risk limit).

## 3. How it meets the declared-plan contract

A replay declares its whole intent plan up front (`ReplayRunner.open(config, intents=...)`), but a strategy decides as the clock moves and what it holds depends on fills. So the driver (`strategy/driver.py`) works in two passes: **pass 1** runs the strategy against a throwaway isolated copy of the account (its own temporary state folder, removed afterwards) to learn which intents it submits; **pass 2** opens the real replay with exactly those intents declared, runs them and writes the event log. Pass 2's outcomes must equal pass 1's (otherwise `StrategyDivergence`). The audit then checks the plan against the events like any other plan, and the strict verify-later tool applies unchanged. Callers that do not use a strategy are untouched.

## 4. What is not covered / data missing for a richer baseline

- No intraday underlying series (the schema has only index end-of-day closes), so no intraday indicator can be computed honestly.
- No option chain / contract master, so strike and expiry selection is the caller's.
- Day-level closes only become usable after 23:59:59 IST of their date, so the baseline can act only on the next session.
- Position sizing, stop-loss levels and profit targets are not defined; exits are rule-based only. The engine's own risk limits are the only protection.
- Pass 1 uses the same deterministic engine, so pass 2 reproduces it; a strategy with any non-determinism would be refused by the divergence check.

## 5. Report, audit and strict verification notes

`strategy/report.py` gives: observations, signals generated, evaluations by status, signals not submitted (with reasons), intents submitted, accepted and rejected (with stage and code), completed and open trades, realized and unrealized P&L, fees and slippage. Anything unknowable is `{"unavailable": REASON}` (`NO_FILLS`, `NO_CLOSED_TRADES`, `NO_OPEN_POSITIONS`, `NO_MARK_FOR_<contract>`), never a zero. Unrealized P&L marks an open position at the bid of the newest visible quote, whose observation time is reported.

The resulting event log, state and fixtures go through the existing audit and `tools/replay_verify_later.py` unchanged (tested: entry then exit audits `CONSISTENT`, `FULL`, plan `MATCH`, and passes strict verification when a separately kept anchor is supplied). A run in which nothing is filled (no signal, rejected inputs, or every intent refused) has a header-only state file (see `docs/HISTORICAL_REPLAY_AUDIT.md` section 14), so it audits `CONSISTENT` with `FULL` coverage and can pass strict verification with a separately kept anchor.

Tests: `tests/test_strategy_signals.py` (signal generation alone, no runner or engine), `tests/test_strategy_execution.py` (conversion, a stub strategy driving the real runner, the baseline scenarios, report and audit), data in `tests/strategy_fixtures.py` (invented 2031 data built in temporary folders).
