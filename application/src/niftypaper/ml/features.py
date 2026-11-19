"""Point-in-time features for the models in niftypaper.ml.models. Both features are computed only from records a `PointInTimeView` shows at the decision
time, so a record that is not yet available cannot reach a model. Nothing is filled in: a missing or doubtful input gives a Feature with value None and a
reason code.

ur1  underlying[t-1] / underlying[t-2] - 1     the two newest visible index closes (same definition as the research folder's `ur1`)
lr1  ln(settlement[t-1] / settlement[t-2])      the two newest visible settlement prices of the contract (same definition as `lr1`)

Same price floor as the research protocol: a settlement below 1.0 is not used.
"""
import math
from dataclasses import dataclass
from decimal import Decimal

from ..historical import gate
from ..historical import records as rec

UR1, LR1 = "ur1", "lr1"
PRICE_FLOOR = Decimal("1.0")
BENIGN_FLAGS = {"AVAILABILITY_RULE_DEFAULT", "TICK_SIZE_ABSENT"}      # a day-level availability default; and a tick size the exchange file never carries (settlements do not need it)
MAX_GAP_DAYS, MAX_AGE_DAYS = 5, 4          # same limits as the baseline strategy: a gap or an old close is treated as missing, not bridged


@dataclass(frozen=True)
class Feature:
    name: str
    value: "float | None"
    code: str                     # OK, or why the value is None
    detail: str
    inputs: tuple = ()            # ((trading_date, number as text, available_at ISO), ...) the values the feature was computed from
    observed_through: "object" = None     # newest available_at among the inputs (never after the decision time)
    sources: tuple = ()                   # ((origin_class, source_id), ...) of the records used


def _missing(name, code, detail):
    return Feature(name, None, code, detail)


def _two_newest(view, kind, key, field):
    """The two newest usable day-level records of (kind, key), oldest first, or (None, reason)."""
    now = view.now
    found = sorted((r for r in view.visible(kind) if r.key == key and gate.usable_at(r, now)), key=lambda r: r.time_key)
    if len(found) < 2:
        return None, ("INSUFFICIENT_HISTORY", f"{len(found)} usable {field} value(s) are visible, 2 are needed")
    used = found[-2:]
    for r in used:
        value = r.get(field)
        if not isinstance(value, Decimal) or value <= 0 or set(r.flags) - BENIGN_FLAGS:
            return None, ("BAD_RECORD", f"the {field} for {r.time_key} is missing, not positive or carries a quality flag")
    first, second = used[0].get("trading_date"), used[1].get("trading_date")
    if (second - first).days > MAX_GAP_DAYS:
        return None, ("GAPPED_SERIES", f"the two newest {field} values are more than {MAX_GAP_DAYS} days apart")
    if (now.astimezone(rec.IST).date() - second).days > MAX_AGE_DAYS:
        return None, ("STALE_INPUT", f"the newest {field} ({second.isoformat()}) is more than {MAX_AGE_DAYS} days old")
    return used, None


def _build(name, used, field, value):
    inputs = tuple((r.time_key, format(r.get(field), "f"), r.available_at.isoformat()) for r in used)
    return Feature(name, value, "OK", f"computed from {inputs[0][0]} and {inputs[1][0]}", inputs, max(r.available_at for r in used),
                   tuple(sorted({(r.origin_class, r.source_id) for r in used})))


def underlying_return(view, underlying="NIFTY"):
    used, why = _two_newest(view, rec.INDEX, underlying, "close")
    if used is None:
        return _missing(UR1, *why)
    a, b = (r.get("close") for r in used)
    return _build(UR1, used, "close", float(b / a - 1))


def settlement_log_return(view, contract):
    used, why = _two_newest(view, rec.EOD, contract, "settlement_price")
    if used is None:
        return _missing(LR1, *why)
    a, b = (r.get("settlement_price") for r in used)
    if a < PRICE_FLOOR or b < PRICE_FLOOR:
        return _missing(LR1, "BELOW_PRICE_FLOOR", f"a settlement is below {PRICE_FLOOR}: not used (same floor as the research protocol)")
    return _build(LR1, used, "settlement_price", math.log(b / a))


def compute(name, view, contract, underlying="NIFTY"):
    if name == UR1:
        return underlying_return(view, underlying)
    if name == LR1:
        return settlement_log_return(view, contract)
    return _missing(name, "UNKNOWN_FEATURE", f"{name!r} is not a feature this module knows")
