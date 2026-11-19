# Architecture (overview)

Everything runs locally on the Python standard library. No provider is connected; the ML path runs on invented fixture data. The detailed, diagrammed version is [application/docs/ARCHITECTURE.md](../application/docs/ARCHITECTURE.md).

```
research/ (frozen, real NSE settlement data, not used at run time)
    └─ definitions + one recorded result ──────────────┐
                                                       ▼
invented fixtures → ingest + manifest hashes → point-in-time gate → features (ur1, lr1)
   → predict() → decision layer (BUY / SELL / NO TRADE + explanation) → intent
   → bridge (quote checks) → paper engine (cost, slippage, risk limits) → append-only account + hash-chained event log
   → summary.json / decisions.jsonl → read-only dashboard page /ml

synthetic quote generator (live-like, no history, no models) → main dashboard page / → paper engine (separate account)
```

| Layer | Module (under `application/src/niftypaper/`) |
|---|---|
| Fixtures and replay session | `ml/fixtures.py`, `ml/session.py`, `historical/` (ingest, gate, replay, audit, event chain) |
| Features, models, prediction | `ml/features.py`, `ml/models.py`, `ml/predict.py` |
| Decision layer | `ml/strategy.py`, `strategy/` |
| Bridge and paper engine | `historical/bridge.py`, `paper/engine.py`, `paper/store.py` |
| Dashboard | `dashboard/` (`server.py` is the only module that imports `http`; loopback only) |
| Safety | `safety/scan.py` (repository scanner) and `tests/test_structure.py` (no network modules, no order or broker names, standard library only) |

Design rules: the ML layer never executes a trade; the engine never sees a provider; the dashboard never runs models; missing or doubtful data produces "no value" rather than a guess; invented data is labelled on every screen.
