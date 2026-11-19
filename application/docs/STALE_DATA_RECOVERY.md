# Stale-data incidents and unpriced positions: what the app does and what you do

**Situation.** You hold an open virtual position and the data feed goes stale (older than 30 s), becomes unavailable, or is only unverified. There is then no verified price for the position.

## What the app does (and never does)

- Shows a red-bordered **UNPRICED POSITION** panel above the positions table, with the data state and its reason; the row's P&L reads `UNPRICED — P&L unknown`.
- `/api/ledger` lists every position with `priced: false`, `unrealized: null`, `unpriced_positions: n`, `data_state` and `data_reason`.
- Keeps counting the position's whole premium in the daily risk budget (the committed worst case does not depend on marks), so the incident does not release budget.
- Blocks new orders, **entries and exits alike**: with no verified price there is nothing to fill at.
- **Never** invents a price, a mark, a fill or an exit, never closes a position by itself, and offers no "close without a price" action. The saved ledger is append-only and is not touched by the incident.

## What you do

1. Leave the positions open; do not try to force an exit.
2. Read the data status at the top and the reason in the panel. Reload the page: provider data is re-requested at most every 5 seconds (Standard plan data limit: 5/s, 50/min, 5,000/day).
3. Each mode has its own account (synthetic and each provider are separate files under `data/paper_state/`). Switching modes does not move positions; a position opened under one source can only be priced and closed under that same source.
4. If the cause is the provider (credentials expired: FYERS access tokens expire daily at 06:30; rate limit; network; no permission recorded), fix that cause using Details, Provider connection and setup. Unverified provider data can never price a position for trading, even when it flows again.
5. When a fresh, verified price returns the row re-prices by itself. A SELL then fills at that new quote (touch plus slippage), never at an older one.
6. If verified data cannot return, the positions stay open and unpriced. Write down the date, the position, the data state and the reason in your own notes; the ledger keeps the original trades and the account is never reset by the program. To start a new virtual account you move or delete the state file yourself, a deliberate act that also resets the daily risk budget.

## Limits

This describes simulated positions in a virtual account. It is not a procedure for real positions, and nothing here is a statement about what any exchange or provider would do.
