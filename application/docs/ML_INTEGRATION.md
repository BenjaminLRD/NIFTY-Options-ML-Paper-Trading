# ML-to-dashboard integration and historical replay (V6-0.3)

Status: implemented and tested on **invented fixture data only**. No provider is connected, no credentials are used, no network request is made, no market data is read, and nothing here shows that any model is profitable.

## 1. What was inspected, and what can be integrated

The research folder (in this project `../research/`, frozen; the original working folder also held notebooks V1 to V5.0 and was larger) holds the scripts `v4_7` to `v5_1`, the V5.1 protocol, results and report. The final research question (V5.1 report, `V5_1_Final_Research_Report.md`) compares two single-feature logistic models against a training-rate prior:

| Research model | Feature | Integrable without changing research results? | Standing here |
|---|---|---|---|
| B0 prior | none (training up-rate per option type: CE 0.4421, PE 0.4795, from the report) | Yes: two constants copied from the report | NONE (constant; cannot ask for a trade) |
| B1 | `lr1` = ln(settle[t-1] / settle[t-2]) | The functional form yes; **the fitted numbers were never saved**, so none can be used without a parameter file | NONE |
| B2 | `ur1` = underlying[t-1] / underlying[t-2] - 1 | Same as B1 | NONE |
| B3 to B4, XGBoost and earlier notebooks | various | Not integrated: not part of the final, frozen question, and V4.x results were superseded | n/a |

The V5.1 one-shot evaluation (trade dates 2026-06-30 to 2026-09-28) found **no model better than the prior** (all four Brier differences positive or about zero, every interval includes 0: NOT SUPPORTED). So the research models are registered with standing `NONE`: they can be shown, never traded. Nothing in this milestone refits, tunes or re-evaluates them, and the V5.1 window was not touched (the frozen protocol hash is unchanged; this program never reads the research folder).

A fitted-parameter file for B1/B2 can be supplied by the owner (schema in `src/niftypaper/ml/models.py`, location `config/local/model_params/<model id>.json`). Producing it means running the frozen research code on NSE data; whether that use is permitted is unresolved (see section 6), so **it was not done**.

A fourth model, `demo_synthetic_ur1`, exists only to exercise the pipeline: hand-set numbers (a trend slope on the index return, the opposite for puts), fitted to nothing, restricted to `SYNTHETIC_FIXTURE` inputs, standing `DEMONSTRATION_ONLY`. Its trades and "performance" on invented data mean nothing.

## 2. Architecture

```
invented fixtures (2031)          src/niftypaper/ml/fixtures.py   index closes, contract settlements, 10:15 quotes, manifest (SYNTHETIC_FIXTURE)
   -> ingest + point-in-time gate  historical/ (existing)          a record is visible only when available_at <= the replay clock
   -> features                     ml/features.py                  ur1, lr1 from visible records only; missing/doubtful input -> no value + reason
   -> prediction interface         ml/predict.py  predict(model, view, contract) -> Prediction
   -> decision layer               ml/strategy.py MlGatedStrategy.evaluate(view, held) -> BUY / SELL / NO TRADE + explanation
   -> existing driver              strategy/driver.py  (decide, then replay the declared plan, publish the hash-chained log)
   -> bridge, risk limits, engine  historical/bridge.py, paper/engine.py   unchanged: cost model, slippage, size and daily-loss limits, saved state
   -> outputs                      ml/session.py -> events.jsonl, decisions.jsonl, summary.json, state/
   -> dashboard                    dashboard/ml_page.py, route /ml and /api/ml (read-only)
```

**Prediction interface.** `predict()` takes a point-in-time view (everything available at its clock, nothing later) and returns a `Prediction`: model id, probability that the contract's next settlement is above its current one, features used, `data_observed_through`, provenance (origin classes, source ids, the input values with their availability instants, a plain-language data label), and an eligibility status:

* `ELIGIBLE`: a probability exists and the model's standing allows a simulated trade on this data.
* `DISPLAY_ONLY`: a probability exists but must not drive a trade (no demonstrated edge, or origin not allowed).
* `UNAVAILABLE`: no probability (parameters missing, input missing, too old, gapped, below the research price floor, unsupported contract).

**Decision layer.** Flat: score call and put; BUY the higher if it is `ELIGIBLE` and at least the buy threshold (0.55); otherwise NO TRADE with the gate that stopped it. Holding: SELL if the held contract's prediction is `ELIGIBLE` and below the exit threshold (0.50), else NO TRADE. A `DISPLAY_ONLY` or `UNAVAILABLE` prediction never produces BUY or SELL; there is no default action. Every decision, including NO TRADE, is written to `decisions.jsonl` with its reason code, a sentence of explanation and the full predictions it rested on. Thresholds are placeholders, not tuned on any data.

**Existing controls are unchanged.** A BUY/SELL becomes an intent through the existing conversion (it expires, and is re-evaluated at execution time), passes the bridge (quote must be fill-eligible, fresh, two-sided, on tick) and the engine (max 2 lots per trade, 3 positions, 10% exposure, 2% daily loss budget, cash, size, persistence). The ML layer adds no execution path. In the demonstration the engine refuses 4 of 20 requests with `RISK_DAILY_LOSS`; the page lists them.

## 3. Three kinds of data, kept apart

| Kind | Where | What it is |
|---|---|---|
| Historical replay | `/ml` | A replay, through the point-in-time gate, of **invented** fixture data with 2031 dates. Origin class `SYNTHETIC_FIXTURE`. The replay runner refuses any other origin class. |
| Synthetic (live-like) | `/` in Synthetic Mode | Generated quotes with no history and no models. Never ML input. The chart's SIMULATED HISTORY series is never given to a model. |
| Provider | `/` in Real-Time Mode | Not connected. No provider is approved. Provider data can at best be `UNVERIFIED` and cannot be traded. |

No real historical data is replayed: the only candidate is NSE bhavcopy, and NSE's terms of use (checked 2026-10-27, recorded in `config/historical_data_sources.json`) prohibit use for "gaming, virtual trading or simulation activities" unless NSE agrees in writing. Existing documentation treats it as NOT permitted, so the replay uses invented data, as the instructions for this milestone require when permitted real data is missing.

## 4. The dashboard `/ml`

Sections: which kind of data this is; system health (origin, provider, audit verdict and coverage, decision and fill counts, account, model registry); predictions and decisions (newest first); model performance (scoring on the invented replay, plus the V5.1 research record copied as a record); portfolio history (equity at each decision, an inline SVG line; no script); simulated trades with the refused ones grouped by reason; limitations. The page has no form, script or button; its only links are same-origin (navigation and run selection); it loads nothing, and the server accepts only GET/HEAD on it. The main page gained one same-origin link to `/ml` (interactive mode only).

## 5. How "model performance" is computed, and what it is not

On the replay, each probability is scored after the fact against whether the contract's next quote midpoint was above the current one, by Brier score against an expanding-mean prior that uses only earlier labels, plus a direction hit rate. On 40 invented days this is a check that the scoring works, not a result: a random walk can give any value. In the shipped demonstration the demo model scores a Brier difference of -0.032 on 64 probabilities; that is noise on invented data and **not** evidence of skill. The research record next to it shows the opposite for the real research models.

## 6. Limitations and next provider-integration requirements

* Invented data only. No claim about any market, any model's skill, or profitability.
* B1/B2 predict nothing until a parameter file exists, and cannot trade even then (standing NONE).
* No time stop or loss stop in the decision layer; positions close only on the model's exit rule. Open positions at the end of a replay remain open.
* Fills use the replay's illustrative cost and slippage model.
* One decision per day at 10:15; no intraday features; one expiry, one strike per side.
* Screenshots are in `../screenshots/` (outside `application/`: the safety scan fails closed on binary files, so images are kept out of the scanned tree) (headless Chrome, taken against a temporary account).

To feed this pipeline real data later, all of the following are needed (none is done): (1) written permission from the data source covering private simulated trading and local storage (NSE for bhavcopy; FYERS or another provider for quotes; see `docs/PROVIDER_FEASIBILITY.md`); (2) a data-origin class other than `SYNTHETIC_FIXTURE` with its own manifest, terms record and ingest tests, and a replay runner change to accept it (the runner is deliberately fixture-only today); (3) a documented quote timestamp meaning and real-time status; (4) an owner decision on exporting B1/B2 parameters; (5) for any model to trade, a pre-registered evaluation on untouched data showing an interval below the prior. Until then the application stays a demonstration of the plumbing.
