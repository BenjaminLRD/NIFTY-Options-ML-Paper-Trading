"""Contract specification loader (local JSON file only).

Specs are effective-dated configuration, never constants in code. The shipped file marks its values UNVERIFIED_OBSERVED:
they were seen in sample data from the earlier research project and have not been checked against an exchange circular.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .eligibility import EligibilityPolicy

VERIFICATION_STATUSES = frozenset({"VERIFIED", "UNVERIFIED_OBSERVED"})
_KEYS = {"root_symbol", "instrument_type", "lot_size", "tick_size", "valid_from", "verification_status", "note"}


class ContractSpecError(ValueError):
    pass


@dataclass(frozen=True)
class ContractSpec:
    root_symbol: str
    instrument_type: str
    lot_size: int
    tick_size: Decimal
    valid_from: date | None
    verification_status: str
    note: str

    @property
    def is_verified(self) -> bool:
        return self.verification_status == "VERIFIED"

    def eligibility_policy(self, *, max_age: timedelta, max_future_skew: timedelta) -> EligibilityPolicy:
        return EligibilityPolicy(max_age=max_age, max_future_skew=max_future_skew, tick_size=self.tick_size)


def _parse_one(raw: object, index: int) -> ContractSpec:
    where = f"specs[{index}]"
    if not isinstance(raw, dict):
        raise ContractSpecError(f"{where}: must be an object")
    unknown, missing = set(raw) - _KEYS, _KEYS - set(raw)
    if unknown or missing:
        raise ContractSpecError(f"{where}: unknown keys {sorted(unknown)}, missing keys {sorted(missing)}")
    root = raw["root_symbol"]
    if not isinstance(root, str) or not root.strip():
        raise ContractSpecError(f"{where}: root_symbol must be a non-empty string")
    if not isinstance(raw["instrument_type"], str) or not raw["instrument_type"].strip():
        raise ContractSpecError(f"{where}: instrument_type must be a non-empty string")
    lot = raw["lot_size"]
    if isinstance(lot, bool) or not isinstance(lot, int) or lot <= 0:
        raise ContractSpecError(f"{where}: lot_size must be a positive integer")
    tick_raw = raw["tick_size"]
    if not isinstance(tick_raw, str):
        raise ContractSpecError(f"{where}: tick_size must be a decimal string such as \"0.05\"")
    try:
        tick = Decimal(tick_raw)
    except InvalidOperation as exc:
        raise ContractSpecError(f"{where}: tick_size is not a decimal") from exc
    if not tick.is_finite() or tick <= 0:
        raise ContractSpecError(f"{where}: tick_size must be a finite positive decimal")
    valid_from = raw["valid_from"]
    if valid_from is not None:
        try:
            valid_from = date.fromisoformat(valid_from)
        except (TypeError, ValueError) as exc:
            raise ContractSpecError(f"{where}: valid_from must be null or an ISO date") from exc
    if raw["verification_status"] not in VERIFICATION_STATUSES:
        raise ContractSpecError(f"{where}: verification_status must be one of {sorted(VERIFICATION_STATUSES)}")
    if not isinstance(raw["note"], str):
        raise ContractSpecError(f"{where}: note must be a string")
    return ContractSpec(root.strip(), raw["instrument_type"].strip(), lot, tick, valid_from, raw["verification_status"], raw["note"])


def load_contract_specs(path: str | Path) -> dict:
    """Return {root_symbol: ContractSpec}. Raises ContractSpecError on anything malformed, duplicated or unknown."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractSpecError(f"cannot read contract specs: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("specs"), list) or not data["specs"]:
        raise ContractSpecError("expected {\"schema_version\": 1, \"specs\": [ ... ]} with at least one spec")
    specs: dict = {}
    for i, raw in enumerate(data["specs"]):
        spec = _parse_one(raw, i)
        if spec.root_symbol in specs:
            raise ContractSpecError(f"duplicate root_symbol {spec.root_symbol!r}")
        specs[spec.root_symbol] = spec
    return specs
