"""Deterministic baseline strategy and signal engine (simulated, virtual-only). Made-up data only; see docs/STRATEGY_BASELINE.md.

signals   Signal and Evaluation types (signal generation only)
trend     the index_sma_trend v1.0.0 baseline rule
intents   conversion of an approved signal into a replay Intent (no calculation, no execution)
driver    two-pass run: decide on an isolated copy, then replay the declared plan
report    a basic report in which unknown values are explicit
"""
