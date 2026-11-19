# Virtual paper-trading accounting conventions

Code: `src/niftypaper/paper/engine.py` (`PaperEngine.simulate`, `_apply`, `_check_buy`, `_check_sell`, `unrealized`, `summary`). Every figure is a `Decimal`; money is kept in whole paise.
Costs, slippage and limits are illustrative defaults, not actual charges. Audited 2026-11-02 against a reported dashboard trade; the audit found and fixed one rounding defect, and the daily loss limit was then changed from a stop to a projected-risk entry gate with saved state.

## Per trade
1. **Fill price** = touch + slippage: BUY at ask + 1 tick, SELL at bid - 1 tick. The quote (bid, ask, last) is stored separately and is never the fill.
2. **Quantity** = lots x the contract's lot size (65 for the synthetic demo). The lot limit (2) applies to `lots` in one request.
3. **Turnover** = fill price x quantity, rounded to paise.
4. **Fee** = 20 + 0.05% of turnover, rounded to paise, charged once per trade on each side (so a round trip pays two fees).

## Cash
There is no reserved or blocked premium and no margin. A BUY pays the whole premium straight out of cash: `cash -= turnover + fee`. A SELL receives it: `cash += turnover - fee`.
The premium of an open position therefore sits in the position (as its cost basis), not in cash.

## Position
`basis` = premium paid for the open quantity (exact paise). `cost_paid` = the buy fees carried by that quantity. `average_price = basis / quantity` is shown for display only; no money is computed from it.

## Realised P&L (only when a position is sold)
`realised = sale turnover - basis of the part sold - share of buy fees - sell fee`. A part sale takes its share of basis and buy fees, rounded to paise; the last part takes the exact remainder, so nothing drifts.
Buy fees therefore appear in realised P&L once, when their quantity is sold, while they left cash at purchase time (the fee is never taken from cash twice).
Opening a position changes cash but not realised P&L.

## Open (unrealised) P&L
`marked bid x quantity - basis`, shown as "marked at bid, before fees": no exit fee, no exit slippage, and the buy fee is not deducted. It is shown only while prices are verified; otherwise "unknown".
Net of the buy fee, a position is worth `open P&L - cost_paid`.

## Identities the tests enforce
- `cash = starting balance - sum(buy turnover + fee) + sum(sell turnover - fee)` (from the ledger).
- `cash = starting balance + realised P&L - sum(basis + cost_paid of open positions)`.
- When flat: `cash - starting balance = realised P&L`, exactly.
- Equity (cash + open quantity at the bid) = starting balance + realised P&L - open fees + open P&L.

## Worked example (the audited trade)
BUY 1 lot (65) SYN-CE-23800, quote 279.90 / 280.10: fill 280.15, turnover 18,209.75, fee 20 + 9.104875 = 29.10, so cash falls by 18,238.85 to 981,761.15.
Realised P&L stays 0.00. Open P&L = 279.90 x 65 - 18,209.75 = -16.25 (before the 29.10 fee; -45.35 after it). Equity = 981,761.15 + 18,193.50 = 999,954.65 = 1,000,000 - 29.10 - 16.25.

## Limits (checked on BUY only; a SELL is never blocked by a limit)
- **Lots per trade (2):** per request, not per position or instrument: repeated 2-lot buys can build a larger position, bounded by the exposure limit.
- **Open positions (3):** distinct instruments held.
- **Open premium (10% of the starting balance):** sum of the open positions' basis plus the new turnover, at fill prices, without fees; not marked to market.
- **Daily loss budget (2% of the configured starting balance, 20,000 by default):** a hard limit on NEW risk, defined in the next section.
- Insufficient cash refuses the BUY. The limits are settings of the run; the trades and therefore the daily budget's state are saved (see Restart).

## Daily risk-entry limit

**What it is.** Before any BUY (the only risk-increasing trade; there is no short selling) the engine projects the worst-case loss for the trading day and refuses the BUY if that would exceed the budget.
It limits *new* risk. It does **not** guarantee that the day's realised loss stays within the budget: a price gap, thin or no liquidity, a bad or stale price, a failed or impossible exit, or a loss that is already larger than the budget can all lose more.
The engine never sells anything by itself and never assumes an exit will fill; selling is always the user's action, and exits (SELLs) are never blocked by this limit.

**Budget.** `B = daily_loss_fraction (2%) x the configured starting balance`. "Starting daily equity" is this configured starting balance: it is fixed for the account and is not re-based when equity moves or a new day starts.

**Day.** The trading day is the calendar date in India Standard Time of the engine's clock; it rolls at 00:00 IST. There is no holiday or session calendar (a day without trades is just a day with no realised P&L).
A sale belongs to the day it is made. Open positions and their buy fees carry over into the next day and still count.

**Formula** (all amounts in rupees, loss positive; `PaperEngine.risk_report`, `PaperEngine._check_buy`):

    R        = realised P&L of today's sales, after fees (profit positive)   - taken from the ledger, so it survives a restart
    U        = sum over open positions of max(0, premium paid - max(0, bid - A x tick) x quantity)   - losses only, only where a verified price is known; gains never offset
               A = exit_slippage_ticks (default 2): the position is valued at the bid less A ticks per unit, never below zero
    O        = sum over open positions of (premium paid - its U)                  - what the position could still lose
    F        = buy fees paid on positions that are still open                      - not yet in R
    X        = sum over open positions of (lots held x 20 + 0.05% of max(premium paid, current value))   - fees for getting out, allowing for one exit trade per lot
    incurred = -R + U + F                                  committed = incurred + O + X = -R + (premium paid) + F + X
    new BUY  : N = its premium, c = its buy fee, x = lots x 20 + 0.05% of N  (estimated exit fees of that position)
    projected = committed + N + c + x         ->  refuse if projected > B   (exactly equal is allowed)
    limit reached: incurred >= B  ->  refuse with its own reason, even before projecting

**No double counting.** For each position U and O add up to the premium paid, whatever the price: a loss that is already unrealised is part of U and is taken out of O, never counted again.
So the committed total does not depend on marks at all; marks only change the split and whether "limit reached" can be detected. Fees are counted once: a buy fee is in F while the position is open and moves into R (through the position's fee share) when it is sold;
the sell fee is in R. A realised loss is in R and the position that produced it is gone from U, O, F and X. After a full round trip, `committed = -R` = premium and both fees minus the proceeds (tested).

**Conservative choices.** Unrealised gains are never credited. Realised profit of the day is credited (it is cash already in hand). The remaining loss of a long option is its whole premium. The exit-fee estimate allows one flat fee for every lot, because a position can be sold a lot at a time.
Not included: taxes, margin, a loss not tied to the premium paid, and anything that cannot be sold (the model assumes a sale is possible only to measure fees; it never assumes one happens).

**Exit slippage (audit).** Entry slippage is already in the premium paid. Exit slippage is handled in two places and deliberately NOT added to the committed total: (1) the worst case already assumes the whole premium is lost, and because sale proceeds cannot be negative no exit price can lose more than premium + fees, so adding slippage there would count the same premium a second time; (2) where the loss already incurred is judged (the unrealised part `U`, `incurred` and `limit reached`), a marked position is valued at the bid less `exit_slippage_ticks` ticks per unit, so a poor exit is allowed for. This moves loss from `O` to `U` by the same amount, so `U + O` is still the premium paid. The page shows how much of the unrealised loss is this allowance. It is a configurable assumption (`RiskLimits.exit_slippage_ticks`, a whole number of ticks, default 2), not a measurement of any real market.

**Exit fees (audit finding, fixed).** The first version estimated ONE exit fee per position. Selling a 2-lot position a lot at a time pays the flat fee twice, and a test showed the realised loss could end ₹6.99 above the estimate. The estimate now allows one flat fee per lot, and a property test checks that no sequence of lot-by-lot exits loses more than the estimate and that an exit never raises the committed total.

**Instrument specifications.** Quantity is lots x the lot size in the instrument's data (65 and tick 0.05 for the synthetic demo; for a provider, the symbol master's values). Bid and ask must be multiples of the tick, and a lot size that differs from the open position's refuses the trade (nothing is guessed).

**Atomic refusal.** All checks run before anything changes. A refused trade leaves cash, positions, fees, realised P&L, the ledger, the day's figures and the saved file exactly as they were. An accepted trade is written to disk first; if that fails the trade is refused.

**Virtual accounts.** There is one account per data source: `synthetic`, and one per provider (today `fyers`). Each has its own file (`data/paper_state/paper_<account>.jsonl`), cash, positions, ledger and daily risk budget, and each accepts only its own kind of prices (a synthetic account refuses provider prices and the reverse, `ACCOUNT_MISMATCH`). The file's header names the account and the starting balance, and every record's data origin is checked when it is replayed, so a file copied to another account's name locks instead of being used. Switching mode or provider never moves, merges or resets anything; it only chooses which account the page shows and trades on.

**Single writer.** While an account is in use its process holds an operating-system lock on `<file>.lock` (released by the system even if the process is killed, so there is no stale lock to clear). A second dashboard on the same state directory, or a second engine in the same process, finds the lock taken, locks that account with the reason `IN_USE`, and writes nothing. The file's size is also checked before each write; if something else changed it, the trade is refused and the account locks until restart. The lock is advisory: it stops two copies of this program, not a person editing the file.

**Restart.** The saved file (`data/paper_state/paper_<account>.jsonl`, one line per accepted trade, plus a header) is the source of truth. On start the engine replays it with the same code and rechecks every record, so cash, positions, the day's realised P&L and therefore the remaining budget are exactly what they were.
A restart on the same day does not reset the budget; a restart after 00:00 IST gives a new day's realised P&L but keeps the open positions. If the file is unreadable, truncated, out of sequence, inconsistent, or belongs to a different starting balance, the engine **locks**: every trade is refused (reason shown on the page), nothing is guessed, and the file is never changed.
To start a new virtual account you must move or delete the file yourself, which also resets the budget. Synthetic and provider modes have separate files.

**Limitations.** The file is not tamper-proof; the single-writer lock was tested with separate processes on Windows only (the Linux/macOS branch uses `flock` and is untested here); a write that fails half-way can leave a partial last line, which locks the account at the next start; the budget is a fraction of the configured starting balance (not of current equity); the day has no holiday calendar; marks come from the page's verified prices, so unrealised loss can be unknown (the committed total is not affected); and
all fees, slippage and limits are illustrative defaults, not actual charges.
