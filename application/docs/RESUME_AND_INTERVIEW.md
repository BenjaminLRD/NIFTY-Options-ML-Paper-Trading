# Resume entry and interview notes

The technical statements below describe what the repository contains and are supported by it. **They say nothing about who wrote what.** This project was developed with substantial assistance from an AI coding assistant (Claude Code). Before using any line on a resume, replace the bracketed parts with your own accurate description of your role, and do not claim work you did not do or cannot explain. Re-run the command beside any number before quoting it (counts change if the code does).

## Resume entry (template: edit the verbs to match your real contribution)

**NIFTY Options ML Paper-Trading Platform** — Python, standard library only; local web dashboard; unittest

*[Your role, e.g. "Directed, reviewed and verified (with AI coding assistance)" or whatever is true]: a simulation-only platform that connects a prediction layer to a risk-controlled paper-trading engine, with a leak-tested point-in-time data pipeline and a local dashboard. An earlier research study on NSE index-option data found no predictive edge, and the platform reports that honestly.*

* [Your contribution]: a prediction → decision → paper-execution pipeline (point-in-time features, a model registry that bars models without a demonstrated edge from trading, BUY/SELL/NO TRADE decisions with recorded explanations) feeding an engine with spread/slippage/fee modelling and enforced risk limits (lot, position, exposure and worst-case daily-loss limits); accepted trades are saved append-only and survive restart.
* [Your contribution]: reproducibility and integrity controls: `available_at` gating against look-ahead leakage, SHA-256 manifests and a hash-chained event log with an independent audit (consistent/full on every demo run), identical hashes on repeat runs, and an interrupted-run guard that never leaves half results.
* [Your contribution]: **904 automated tests** (5 skipped without Node.js) covering chronology, leakage, missing data, risk refusals, accounting (independently recomputed), persistence, duplicate submission and dashboard error handling; a stdlib-only, no-network, no-order-code constraint enforced by structural tests and a repository scanner.

Optional one-liner for a skills section: *Time-series ML evaluation discipline (chronological splits, purged labels, frozen one-shot protocol, Brier score vs baseline).*

## What NOT to claim

Profitability, accuracy, predictive edge, live or real-time trading, broker integration, use of real market data in the platform, or that the demo model is a trained ML model (its parameters are hand-set). The research models did not beat the baseline.

## 60-second explanation

(Adapt "I" and "my" to what you actually did; say plainly that an AI assistant helped write the code.) "It is a paper-trading platform for NIFTY options built around not fooling yourself. The earlier research in the project asked whether a simple lagged-return model predicts next-day option direction; on a frozen, untouched evaluation window it did not beat the baseline, so the report finds no edge. The platform is the infrastructure to test ideas honestly: data can only be used once it was available, fills include spread, slippage and fees, risk limits refuse trades, the account is saved append-only, and everything is audited. Because no written permission exists to use NSE data for simulation, the demo runs on invented data and says so on every screen."

## Interview notes

**Architecture.** See `docs/ARCHITECTURE.md`. Layers: fixtures/ingest → point-in-time gate → features → `predict()` → decision layer → intent → bridge → paper engine → append-only store; a read-only dashboard renders saved summaries. Separation of concerns: the ML layer never executes; the engine never sees a provider, only a verified market view; the dashboard never runs models.

**ML methodology.** Label: next settlement above current (same contract). Features: `lr1` = ln(settle[t-1]/settle[t-2]), `ur1` = underlying[t-1]/underlying[t-2] − 1. Models: single-feature logistic (winsorize at training 1st/99th percentile, standardize, L2) vs the training-rate prior. Metric: Brier score, paired difference with a moving-block bootstrap over dates and a Bonferroni interval; the V5.1 protocol was frozen and hash-verified before the evaluation window was looked at, and run once.

**Evaluation results.** CE/PE × B1/B2 Brier differences vs prior: +0.00218, +0.00048 (CE); +0.00004, +0.00001 (PE); every interval includes 0: not supported. Also an observed borderline instability of the CE `lr1` relationship over time. Interpretation: no usable edge; do not trade.

**Data limitations.** Research data is end-of-day settlement (not executable prices), one expiry, 22 strikes, single origin for most of the window (daily bhavcopy), no bid/ask, date-only timestamps; one holiday inferred from secondary sources. The label is a research target, not a tradable return.

**Why synthetic data.** NSE's terms of use bar simulation/virtual-trading use unless NSE agrees in writing; no data provider is approved (FYERS terms name paper trading as prohibited; others undocumented). So the replay runner accepts only labelled synthetic fixtures. Be ready to say what real data would need (`docs/ML_INTEGRATION.md` §6).

**Why research models cannot trade.** They have no demonstrated edge (V5.1) and B1/B2 have no saved parameters. The registry gives them standing `NONE`; the decision layer only acts on `ELIGIBLE` predictions, so they produce NO TRADE by construction (tested). The demo model has hand-set parameters purely to exercise the pipeline.

**Testing and engineering decisions.** Stdlib only to keep the surface small and auditable; `Decimal` for money; fail-closed everywhere (missing data → no value, not a guess); append-only persistence before applying a trade; rename-on-complete for replays; one-time form ids against double submit; structural tests that forbid network and order code; independent recomputation of cash/fees in tests rather than trusting the engine.

**Known weaknesses.** Invented data; one decision a day; no stop rule; lot size/tick not verified; JSONL not a database; the safety scan is pattern-based; a single squashed commit (no incremental history); no CI; no license chosen.

**Future work.** Obtain written data permission; add a real-data origin class with ingest tests; export B1/B2 parameters under that permission; pre-registered forward evaluation; intraday features; CI.

**Likely questions.** *Is this profitable?* No claim; research found no edge. *Did you tune on the test window?* No: V5.1 was frozen and run once; nothing here retrains. *How do you prevent leakage?* `available_at` gate, day-level records visible only after day end, tests that change the future and assert past predictions are unchanged. *What would you do with more time?* See future work.
