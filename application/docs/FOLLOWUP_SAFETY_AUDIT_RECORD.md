# Follow-up safety audit: exit costs, single-writer lock, account isolation

Scope: a focused audit of the daily risk limit, the saved-state file and the separation between the synthetic and provider accounts. Nothing was committed, no real-order functionality was added, and FYERS data is still not enabled for trading (it is `UNVERIFIED`, so trading with it stays blocked).

## Findings and what was done
| # | Question | Finding | Change |
|---|---|---|---|
| 1 | Is an exit-slippage allowance missing from the projected worst case? | Yes, there was none. But it cannot be added to the worst case: the model already assumes the whole premium is lost, and sale proceeds cannot be negative, so no exit price loses more than premium + fees. Adding slippage there would count the premium twice. | Added `RiskLimits.exit_slippage_ticks` (default 2, validated). A marked position is valued at the bid less that many ticks per unit (never below zero) when judging the loss already incurred, so `limit reached` allows for a poor exit. The loss moves from "could still lose" to "unrealised", so their sum is still the premium paid, and the committed total is unchanged. The page shows the allowance. |
| 1b | (found while checking) Are exit fees bounded? | **No.** The estimate allowed ONE flat exit fee per position. Selling a 2-lot position a lot at a time pays it twice: the realised loss ended 6.99 above the estimate, so committed could rise after a partial exit. | Estimate is now one flat fee per lot plus the proportional fee (`CostModel.exit_estimate`). Property tests: lot-by-lot exits never lose more than the estimate, and an exit never raises the committed total. For one-lot positions the numbers are unchanged. |
| 2 | Can two processes write one state file? | Yes: nothing prevented it. | Single-writer OS lock on `<file>.lock` (released by the system when the process dies, so no stale lock), `IN_USE` lock-out for the second process, a size check before every write (`CHANGED` refuses the trade and locks the account), and `NOT_LOCKED` if a write is attempted without the lock. Tested with separate processes. |
| 3 | Can the synthetic and provider accounts mix? | Not today, but the account was keyed by mode, so a second provider would have shared the provider account, and nothing stopped a synthetic file or price being used by the provider account. | One account per data source (`synthetic`, `fyers`, ...): own file, cash, positions, ledger, risk. Engines are bound to a data origin (`ACCOUNT_MISMATCH`), headers name the account, and replay checks each record's origin, so a copied file locks. |
| 4 | Restart, corrupt-file and atomic-refusal guarantees after the changes | Still hold; the earlier tests were kept, adapted only to close a file's lock before reopening it. | Added: lock-out leaves the file byte-identical; a changed or truncated file locks; account/origin mismatch refuses atomically. |

## The formula after this audit (details in `docs/PAPER_ACCOUNTING.md`)
    U  = sum of max(0, premium - max(0, bid - A x tick) x qty)  over marked positions        A = exit_slippage_ticks (2)
    O  = premium paid - U            F = open buy fees            R = today's realised P&L after fees
    X  = sum over positions of (lots held x 20 + 0.05% of max(premium, value))
    committed = -R + U + O + F + X  =  -R + premium paid + F + X          (independent of A and of any mark)
    refuse a BUY if committed + (N + c + lots x 20 + 0.05% of N) > budget;  limit reached if -R + U + F >= budget

## Results (Windows, Python 3.14.6, repository root)
| Command | Exit | Result |
|---|---|---|
| `python -B -W error::ResourceWarning -m unittest discover -s tests -t .` | 0 | 245 tests, OK (220 before) |
| Full suite repeated 5 times | 0 | no failures |
| `python -B -m unittest tests.test_state_safety tests.test_daily_risk` | 0 | 57 tests, OK |
| `PYTHONPATH=src python -B -m niftypaper.safety.scan --root . --include-untracked` | 0 | 54 files, 0 findings |
| `python -B v5_1_freeze_protocol.py . --verify --expected-combined 6c63883d...a4993d` (in `ML`) | 0 | `VERIFIED 6c63883d1f9055a451d16b33eb1e02614b4c801267973027f22e233d60a4993d` |
`ML` inventory (425 files) identical to the snapshot taken before V6-0.2; V5.1 not rerun.
Real-process checks: (a) the pytest-style tests start separate Python processes: an intruder is locked out while the owner holds the file and the file is byte-identical; a hard-killed owner leaves no stale lock and its trades are restored; six processes started together leave a file that replays cleanly with one header and an unbroken sequence 1..N equal to the trades the owners accepted.
(b) The real launcher was run as three processes on one state directory: B (started while A ran) showed LOCKED / IN_USE with no trade form and its BUY wrote nothing; after A was killed hard, C restored A's trade and continued the sequence.
Mutation check: 16 deliberate breaks (flat single exit fee, allowance added to the committed total, unbounded or ignored or unvalidated allowance, no lock, no change detection, append without the lock, close not releasing the lock, header account ignored, accounts keyed by mode, origin not checked in a trade or in replay, a changed file not locking, one shared file for all accounts, unknown account keys accepted) were each caught.

## Tests added
`tests/test_state_safety.py` (25): the exact budget boundary for 1-lot and 2-lot buys and its independence from the allowance (0 to 100000 ticks); the allowance moving loss between U and O, never below zero value, not applied to gains; the limit-reached boundary to the paisa and the cases where the allowance decides it; allowance validation;
lot-by-lot exits and the monotonic committed total; the lock in-process and across real processes; change and truncation detection; two dashboards on one directory; account separation (files, cash, positions, ledger, risk, restart, stale forms, origin binding, copied files, second provider, CLI account builder).
Adapted: `tests/test_daily_risk.py` (independent recomputation uses the per-lot estimate; stores are closed before a file is reopened; an unwritable directory now locks trading at the lock step).

## Unresolved risks
- The lock is advisory and was tested on Windows with separate processes. The Linux/macOS `flock` branch is untested here. It does not stop a person editing the file; edits are detected only if the size changes while running, or at the next start by the replay checks.
- A write that fails half-way can leave a partial last line: the running engine refuses the next write (size mismatch) and the next start locks that account until you repair or move the file.
- The allowance is an assumption (2 ticks), not a market measurement. The committed worst case has no slippage term by design; a real exit cannot lose more than premium and fees only if a sale at some price is possible at all.
- Marks for the unrealised part come from verified prices; with none, the loss so far is shown as not yet counted (the committed total is unaffected).
- A legacy state file with no account in its header is accepted by the account whose origin matches its records (not renamed or rewritten). Files from earlier runs named `paper_provider.jsonl` are not picked up by any account (none existed with trades).
- No holiday or session calendar; costs, slippage and limits remain illustrative defaults; the budget is a fraction of the configured starting balance, not of current equity.
- Locked accounts keep the file's lock until the process ends, which is intended: it stops another copy from opening a file the first one could not trust.
