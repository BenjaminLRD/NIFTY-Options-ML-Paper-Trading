# Strategy evaluation (`src/niftypaper/evaluation/`, `tools/strategy_evaluate.py`)

**Three different things, never to be confused.**

1. *Implementation correctness*: do the signal engine, replay, audit and this evaluator do what they say? Tests and the audit speak to that, using invented data.
2. *Simulated strategy performance*: what a rule did on invented 2031 data, under illustrative fees and slippage. Everything this evaluator prints is of this kind. It is **not** evidence that the strategy is, or would be, profitable, and a "profit" in a test is only a check that the arithmetic works.
3. *Evidence from real, appropriately permitted market data*: none exists in this repository. No data source is enabled and nothing here was downloaded.

The evaluator is read-only. It never writes to the replay artifacts (its optional `--out` creates one new file and refuses to overwrite or to write over or inside an input). It does not generate signals, run a replay or reconstruct accounting: it asks the existing audit (`historical/audit.py`) to rebuild the books from the event log, state and fixtures, and computes metrics from that audit report's rows.

## 1. When it refuses

A refusal report carries the reasons and **no** metrics (`metrics` and `baseline` are `{"unavailable": "EVALUATION_REFUSED"}`). It refuses unless all hold: audit verdict `CONSISTENT` and coverage `FULL`; every audit check `PASS`; event chain `VALID`; state `PAIRED`; input identity `MATCH`; declared intent plan `MATCH` (legacy or undeclared plans are not evaluated); any anchor or expected head supplied `MATCH`; the account held no trades before the replay (restored trades are refused, so every figure belongs to this replay); the strategy is `index_sma_trend` 1.0.0; and the metrics' own cross-checks below hold. Missing, empty, truncated or corrupt files, changed fixtures and foreign state therefore refuse through the audit. Fixtures are required (without them coverage is partial).

If no anchor or head is supplied the evaluation proceeds and says so (`anchor_status` `NOT_SUPPLIED`): the chain then only shows the file is internally intact. A supplied anchor's protection is the caller's unverified declaration, as in the audit.

## 2. Metric definitions and accounting

All money is in the replay's currency units as exact decimals rounded half-up to 0.01; ratios to 6 places. A value that cannot be known is `{"unavailable": REASON}`; a zero appears only where the evidence establishes it.

- **Trade (lifecycle).** A position in one contract from flat to flat, including every buy and sale in between. *Completed* once it returns to zero; otherwise *open*. A partial sale does not complete a trade.
- **Sale accounting** (taken from the audit's before/after positions): for each sale, premium allocated = basis before − basis after, buy fees allocated = buy fees before − after (both whole for the final sale), `gross = sale turnover − premium allocated`, `net = gross − buy fees allocated − the sale's fee`. The evaluator checks `net` equals the audit's realised P&L for that sale; if not it refuses.
- **Realized P&L (net)**: sum of all sales' `net`. Split into *on completed trades* and *on open trades* (the realised part of a partly sold position). The two add to the audit's account total (checked). A completed trade's P&L is the sum of its sales' `net`, so nothing is counted twice at trade and account level.
- **Gross realized before fees**: sum of sales' `gross`. It is after modelled slippage (fill prices include it) and before all fees.
- **Fees**: `fees_total` = every fill's fee. `fees_on_realized` = buy fees allocated to sales + sale fees. `fees_carried_by_open_positions` = buy fees still attached to open quantity. The last two add to the first (checked).
- **Modelled slippage**: sum of slippage per unit × quantity over fills. Informational: it is already inside the fill prices and is not deducted again.
- **Unrealized P&L**: from the audit's marks at the final checkpoint: bid × quantity − premium paid, using the newest quote that is recent enough (the replay's own quote-age limit). Unavailable if an open position has no usable mark.
- **Equity** at a recorded time = cash + premium paid for open quantity + unrealized P&L (i.e. cash + open quantity at the bid). **Final equity = starting balance + realized + unrealized − fees carried by open positions** (checked). Buy fees reduce cash when paid; they are not part of "unrealized", which is why the last term appears. If a mark is missing, equity at that time is unavailable.
- **Winning / losing / breakeven**: completed trades with net > 0 / < 0 / exactly 0. **Win rate** = winning ÷ completed trades (denominator reported; unavailable with none). **Average win / loss**: mean net of winning / losing trades (loss is negative); unavailable when there are none. **Profit factor** = sum of winning trades ÷ absolute sum of losing trades; undefined (unavailable) without a losing trade; 0 when there are losses and no wins.
- **Maximum drawdown**: the largest peak-to-trough fall (amount, and fraction of that peak) over the equity points sampled at the replay's open, each fill and the final checkpoint. It is a sampled-point measure, not a continuous one. Unavailable if any point lacks a mark. A zero drawdown is reported only when every point is known.
- **Exposure**: time with an open position between recorded event times ÷ the span from the replay's open to its final checkpoint (simulated clock, seconds), and the peak premium held. **Average holding time**: mean open-to-close seconds over completed trades.
- **Intents**: recorded, accepted (fills), rejected, grouped by (stage, code) and by category (`DATA_ELIGIBILITY`: no/unusable/not-yet-available quote, bid/ask or tick problems, displayed size; `RISK_OR_ACCOUNT_LIMIT`; `OTHER`).
- **Signals / no-signal decisions**: **unavailable** (`NOT_RECORDED_IN_EVENT_LOG`). The event log records intents, not the strategy's decisions to do nothing. The strategy name and version are the caller's declaration, labelled as such.

## 3. Baseline

Only a **no-trade baseline** is given: same starting balance, same replay window, valuation cash-only with no interest and no fees, so its final equity equals the starting balance and `strategy_minus_baseline` is the equity change. It says nothing about markets. No buy-and-hold or index benchmark is built: the paper model cannot trade the index, no comparable series exists in the saved artifacts, and inventing one would mean choosing a window and prices the replay did not use. `benchmark` is therefore `NO_COMPARABLE_BENCHMARK_IN_SAVED_ARTIFACTS`.

## 4. Reproducibility, not authenticity

The report is canonical JSON (sorted keys, no floats, no paths, no generation time). `digest` = SHA-256 of a domain label plus the canonical body without the digest. The report is tied to the input identity digest, plan digest, chain head and event count, the audit digest/verdict/coverage, and the event and state file hashes. Identical verified inputs give an identical report and digest. The digest does not show who made the inputs or that any market event was genuine; whoever can rewrite every file can recompute every hash.

## 5. Usage (PowerShell; quote paths with spaces)

```
python tools/strategy_evaluate.py --events "D:\my runs\run1\events.jsonl" --state "D:\my runs\run1\state\replay_synthetic_fixture.jsonl" --fixtures tests\fixtures\historical_synthetic
python tools/strategy_evaluate.py --events "D:\my runs\run1\events.jsonl" --state "D:\my runs\run1\state\replay_synthetic_fixture.jsonl" --fixtures "D:\my fixtures" --anchor "E:\kept apart\anchor.json" --anchor-trust independent --out "D:\reports\eval1.json"
```

Exit codes: 0 evaluated; 1 refused (reasons printed); 2 unusable arguments or a refused `--out`. Without `--out` the JSON goes to standard output and a one-line summary to standard error.

## 6. Limitations

Signals and no-signal decisions are not in the log; the equity curve and drawdown are sampled at event times; marks need a quote recent enough; fees and slippage are illustrative models; the baseline is trivial; only fresh accounts are evaluated; one strategy version is supported. Nothing here validates the strategy, and nothing here is real-market evidence.

## 7. Independent mutation check (what was and was not established)

An isolated harness (a temporary copy of the repository; the real tree is never written, and each mutant is restored in a `finally` block) applied 99 single-line mutations to the evaluator, the CLI, and the audit/store code the evaluator relies on (valuation quotes, plan and chain checks, exclusive file creation). A mutant counts as killed only when a test that targets that behaviour fails; a failure elsewhere is reported separately.

First pass (before the tests added in this step): 98 attempted, 78 killed, 20 survived. Of the 20 survivors, 16 were genuine gaps and are now pinned by direct tests: the trade-total and fee-total guards when the final mark is unavailable; average holding time; drawdown tie handling (the first time a peak or worst fall is reached is kept); the digest format (pinned by an independently computed answer); the baseline's final equity; the plan digest shown in a refusal; the exclusive-create output check (a file that appears between the existence check and the create is never replaced: tested for the CLI report and the state store); byte-exact canonical CLI output; the not-fill-eligible, crossed, wrong-contract, newest-quote and exact-age-limit valuation rules; and the chain link, sequence and hash checks each refusing on their own. A further plan check (an intent whose instant differs from the plan) showed as a survivor when only evaluator-level tests were run and is now pinned there too.

Final state: 99 attempted, 97 killed by a test aimed at that behaviour, 2 equivalent, 0 unexplained survivors, 0 not applicable. The two equivalent mutants: (1) `identity_always_match` replaces a status assignment that the next lines overwrite whenever differences exist (every difference is also a problem), so no input can distinguish it; (2) `gate_failing_checks` removes the "any audit check not PASS" refusal, which cannot change a real report because the audit's verdict and coverage are derived from those same check statuses; the rule is still tested directly on the function. The valuation quote's two time filters (available and observed) duplicate each other for ingested records (observed is never after available); each alone is therefore equivalent on real data, so they are pinned with hand-built records and the combined removal is killed by the end-to-end future-quote test.

Previously reported survivors: the realised-total guard and the fee clause survived only while a final mark masked them (now killed by the no-final-mark tests); the two "inapplicable" entries were the win/loss thresholds replaced by `classify`, now killed directly (including breakeven); the exclusive-create check was not redundant, because it protects against a file appearing after the existence check, now tested by simulating that race.

Limits of this evidence: single-line mutants only, matched to tests by test name; the harness covers the evaluator, its CLI and the audit/store lines it depends on, not every line of the audit; passing mutation checks show the tests are sensitive to these changes, not that the evaluator is free of defects, and say nothing about any strategy's merit.
