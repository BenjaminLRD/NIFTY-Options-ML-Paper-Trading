# Daily risk-entry limit: implementation record

> **Attribution note.** This record was written by the Claude Code assistant during Claude-assisted development. "The assistant" (or "I", where it remains) means that assistant; "you" means the project owner. It records what the assistant did and found. It does not describe the owner's own contribution, which is not documented here.

Scope: a hard daily risk-entry limit in the virtual paper-trading engine, with saved state so a restart does not reset it. Nothing was committed. The exact formula, day rule and restart behaviour are in `docs/PAPER_ACCOUNTING.md` ("Daily risk-entry limit").

## What the limit is, and is not
Before every BUY the engine projects the worst-case loss for the IST trading day and refuses the BUY if it would exceed 2% of the configured starting balance (20,000 with the defaults).
It limits *new* risk. It is **not** a guarantee that losses stay below the budget: gaps, thin or missing liquidity, bad prices and exits that cannot be made can lose more, and the engine never sells anything itself or assumes an exit fills. SELLs are never blocked.

    projected = ( -R + premium paid on open positions + buy fees on open positions + estimated exit costs of open positions )
              + ( new premium + new buy fee + estimated exit cost of the new premium )          refuse if projected > budget
    R = today's realised P&L after fees (profit credited); unrealised losses (losses only, never gains) and the remaining possible loss of each position add up to its premium, so nothing is counted twice.

## Decisions the assistant made where the request left room (please check)
- **"Configured starting daily equity"** = the configured starting balance, fixed. It is not re-based each day or when equity changes.
- **Realised profit of the day is credited**, because it is cash in hand and the request defines the first term as signed realised P&L. Unrealised gains are never credited. (The previous version gave no credit for profit.)
- **Exit-cost estimate** = cost of selling at the premium paid (or the current value if higher) for every open position and for the new premium. Exit slippage is not included.
- **Buy fees on carried positions** still count after midnight, because they are charged to realised P&L when the position is sold.
- **Restart:** implemented as durable state (an append-only file replayed on start), not as "fail safe only", because that keeps the budget exactly. If the file cannot be trusted the engine locks. Deleting the file is a deliberate reset.
- Added checks the request implied: quotes must be multiples of the tick (both data origins), and a lot size different from the open position's refuses the trade.

## Files
Added: `src/niftypaper/paper/store.py`; `tests/test_daily_risk.py` (32 tests); this record.
Changed: `paper/engine.py` (rewritten around `risk_report`, plan/commit, replay), `dashboard/{server,trading,render,__main__}.py` (risk panel, risk in `/api/ledger`, `--state-dir`, risk-aware marks), `README.md`, `docs/PAPER_ACCOUNTING.md`, `docs/SAFETY.md`, `tests/test_paper_engine.py` (old stop/cap tests retired, helper and the off-tick case adjusted), `tests/test_dashboard.py` (flag list).
Unchanged: the V6-0.1 eligibility boundary and quote model, the providers, and everything under the `ML` folder.

## Results (Windows, Python 3.14.6, repository root, 2026-11-02)
| Command | Exit | Result |
|---|---|---|
| `python -B -W error::ResourceWarning -m unittest discover -s tests -t .` | 0 | 220 tests, OK (194 before) |
| Full suite repeated 5 times | 0 | no failures |
| `python -B -m unittest tests.test_daily_risk tests.test_paper_engine tests.test_modes` | 0 | 88 tests, OK |
| `PYTHONPATH=src python -B -m niftypaper.safety.scan --root . --include-untracked` | 0 | 52 files, 0 findings |
| `python -B v5_1_freeze_protocol.py . --verify --expected-combined 6c63883d...a4993d` (in `ML`) | 0 | `VERIFIED 6c63883d1f9055a451d16b33eb1e02614b4c801267973027f22e233d60a4993d` |
`ML` inventory (425 files) identical to the snapshot taken before V6-0.2; V5.1 not rerun.
Real-process check: the launcher was run twice on one temporary state directory. Run 1: a 1-lot BUY of SYN-CE-23800 was accepted (committed 18,267.95, room 1,732.05) and a second was refused with the projected 36,535.90 shown. Run 2 (new process): "1 trade(s) restored", the same committed figure and room, the second BUY refused again, a SELL accepted, committed fell to 77.70 (the round trip's spread, slippage and two fees). No credentials, provider or network were used.
Mutation check: 23 deliberate breaks (gate ignoring committed risk, realised P&L, open buy fees, either exit-cost estimate; unrealised loss double counted or gains credited; boundary off by a paisa; every sale counted as today; UTC instead of IST; a refusal that changes cash; trades not saved; lock not enforced; replay skipping the realised-P&L, turnover or sequence checks; exits passing through the gate; limit-reached never detected; lot-size change or off-tick quote ignored; truncated or other-account files accepted; risk panel missing) were each caught.

## Tests added (all in `tests/test_daily_risk.py` unless noted)
A trade that fits (the audited 18,267.95 case) and one that exceeds it, with the numbers in the message; the exact boundary; existing unrealised losses (split, no double counting, unverified marks ignored, gains not credited); a limit already reached (its own reason, exits still allowed, no auto-liquidation);
multiple positions and fees without double counting, checked against an independent recomputation from the ledger; partial closes with exact fee shares; a random-sequence invariant; lot size, tick and instrument specification enforcement; day rollover at 00:00 IST (including UTC timestamps) with positions carried over;
atomic refusal in memory and on disk; a failed save refusing the trade; restart keeping positions, cash, the day's loss and the remaining budget; restart after midnight; 15 kinds of untrustworthy state file (truncated, corrupt, tampered, out of sequence, other account, ...) locking trading and leaving the file unchanged; the dashboard showing the budget, the remaining room, the blocked reason and the locked state.

## Remaining limitations
- Not a loss guarantee (see above). A position can be worth nothing, or unsellable, and fees and slippage on a real exit may differ from the estimate.
- The saved file is not tamper-proof and assumes one dashboard process per file; the day has no holiday or session calendar; the budget is a fraction of the configured starting balance, not of current equity.
- Marks for unrealised loss come from verified prices; with none, the loss so far is shown as not yet counted (the committed total is unaffected).
- Costs, slippage and limits are illustrative defaults. Provider-data trading is still blocked unless the data is verified (FYERS data is not), so the limit has so far only been exercised with synthetic and test-provider data.
- With the default 2% budget the limit allows about one lot at a time at the audited price level; raise `daily_loss_fraction` for more room.
