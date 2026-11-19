"""The only path from historical data to the paper engine.

A MarketView with origin HISTORICAL is produced from exactly one thing: a QUOTE_SNAPSHOT record that is usable at the replay clock, is fill-eligible
(documented quote time, valid bid/ask on the tick, lot and tick present), comes from an origin class the caller explicitly allowed, and is covered by a
caller-supplied `permitted` flag that defaults to False. Settlement, closing, last-traded, candle and index records, synthetic data of unknown origin,
and anything else are refused here with a code and never turned into a quote.

The view carries an attestation (paper/attest.py) over the fields a fill depends on; the engine refuses a HISTORICAL view without a matching one, and also
re-checks the fields itself. Only this module may call attest.issue (a static test checks it; Python cannot enforce it).

The view is HISTORICAL, never PROVIDER: it cannot satisfy a provider account, and the engine still applies every one of its own checks to it (account
origin, tick and lot rules, the V6-0.1 eligibility boundary against the replay clock, displayed sizes, risk limits, the daily budget, the state store).
Nothing here enables a data source or a provider permission.
"""
from dataclasses import dataclass, replace

from ..paper import attest
from ..paper.engine import ORIGIN_HISTORICAL, MarketView, attestation_fields
from . import records as rec
from .gate import ReplayClock, usable_at

FILLABLE_ORIGIN_CLASSES = ("SYNTHETIC_FIXTURE", "UNVERIFIED_SOURCE")        # "UNKNOWN" is never fillable, whatever the caller allows


@dataclass(frozen=True)
class BridgeResult:
    view: "MarketView | None"
    code: str
    detail: str = ""

    @property
    def ok(self):
        return self.view is not None


def _refuse(code, detail=""):
    return BridgeResult(None, code, detail)


def market_view(record, clock, *, allowed_origin_classes=frozenset(), permitted=False):
    """The engine-facing view of one quote snapshot at the replay clock, or a refusal."""
    if not isinstance(record, rec.Record):
        return _refuse("NOT_A_RECORD")
    if record.kind != rec.QUOTE:
        return _refuse("NOT_A_QUOTE_SNAPSHOT", f"{record.kind} records are never executable quotes")
    if not isinstance(clock, ReplayClock):
        return _refuse("NO_REPLAY_CLOCK")
    if not usable_at(record, clock.now):
        return _refuse("NOT_AVAILABLE_YET", "available_at is unknown or after the replay clock")
    if not record.fill_eligible or record.ineligible_reasons:
        return _refuse("NOT_FILL_ELIGIBLE", ", ".join(record.ineligible_reasons) or "not marked fill-eligible")
    if record.origin_class not in FILLABLE_ORIGIN_CLASSES or record.origin_class not in allowed_origin_classes:
        return _refuse("ORIGIN_NOT_ALLOWED", record.origin_class)
    if permitted is not True:
        return _refuse("DATASET_NOT_PERMITTED", "paper trading on this dataset has not been enabled by the caller")
    view = MarketView(
        origin=ORIGIN_HISTORICAL, provider=f"historical:{record.source_id}", state="HISTORICAL_REPLAY", connection="REPLAY", symbol=record.key,
        bid=record.get("bid"), ask=record.get("ask"), last=record.get("last_price"), bid_qty=record.get("bid_qty"), ask_qty=record.get("ask_qty"),
        tick=record.get("tick_size"), lot_size=record.get("lot_size"), received_at=clock.now, provider_time_raw=record.observed_at.isoformat(),
        provider_time=record.observed_at, verified=True, permitted=True, reason=f"{record.origin_class} {record.source_file}#{record.source_row}")
    reference = f"{record.source_file}#{record.source_row}:{record.content_digest}"
    return BridgeResult(replace(view, attestation=attest.issue(attestation_fields(view), reference)), "OK")


def executable_view(view, contract_key, **kwargs):
    """The newest usable quote snapshot for `contract_key` in a PointInTimeView, as an engine view, or a refusal."""
    record = view.latest_quote(contract_key)
    if record is None:
        return _refuse("NO_VISIBLE_QUOTE", "no quote snapshot for this contract is available at the replay clock")
    return market_view(record, view.clock, **kwargs)
