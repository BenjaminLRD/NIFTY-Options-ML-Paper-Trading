"""Synthetic sample-data provider. Everything here is made up: fixed numbers, invented labels, no market input of any kind.

A provider is a callable `provider(now) -> Snapshot | None`. Returning None means "no data". The dashboard never substitutes
synthetic data for a provider that fails or returns None; only this module produces synthetic data, and it is chosen explicitly.
"""
from datetime import timedelta
from decimal import Decimal

from .status import EVIDENCE_SYNTHETIC, OptionRow, Snapshot

SOURCE = "SYNTHETIC_SAMPLE_PROVIDER"
SCENARIOS = ("simulated", "stale", "unavailable")
BASE_INDEX = Decimal("24000.00")
TICK = Decimal("0.05")
STALE_DEMO_AGE = timedelta(minutes=15)
SYNTHETIC_LOT_SIZE = 65            # a made-up contract size for the demo, not a statement about NSE contracts


def _tick(value):
    return (value / TICK).to_integral_value() * TICK


def _chain():
    rows = []
    for strike in range(23800, 24201, 100):
        distance = abs(BASE_INDEX - strike)
        time_value = max(Decimal("5"), Decimal("120") - distance / 5)
        for kind in ("CE", "PE"):
            intrinsic = max(BASE_INDEX - strike, Decimal(0)) if kind == "CE" else max(strike - BASE_INDEX, Decimal(0))
            price = _tick(intrinsic + time_value)
            rows.append(OptionRow(f"SYN-{kind}-{strike}", kind, strike, price, price - Decimal("0.10"), price + Decimal("0.10"),
                                  lot_size=SYNTHETIC_LOT_SIZE, tick=TICK))
    return tuple(rows)


class SyntheticProvider:
    def __init__(self, scenario="simulated"):
        if scenario not in SCENARIOS:
            raise ValueError(f"scenario must be one of {SCENARIOS}")
        self.scenario = scenario

    def __call__(self, now):
        if self.scenario == "unavailable":
            return None
        as_of = now - (STALE_DEMO_AGE if self.scenario == "stale" else timedelta(0))
        return Snapshot(source=SOURCE, synthetic=True, as_of=as_of, index_value=BASE_INDEX, pct_change=Decimal("0.25"), chain=_chain(), evidence=EVIDENCE_SYNTHETIC)
