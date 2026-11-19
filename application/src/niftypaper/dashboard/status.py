"""Status model for the dashboard: what a data provider hands over, and how fresh/available it is judged to be.

States: SIMULATED (fresh synthetic), UNVERIFIED (provider data that is well-formed but whose real-time status or timestamp meaning is not
established), LIVE and DELAYED (provider data with explicit evidence, see below), STALE and UNAVAILABLE (values withheld).
Anything this module cannot positively classify is UNAVAILABLE.

LIVE needs ALL of: the provider's documentation states the feed is real-time, the snapshot carries a time documented as the quote time,
and that time is fresh. DELAYED needs the provider to confirm a delay. A successful request alone never produces either. The FYERS adapter
supplies neither piece of evidence, so it can only produce UNVERIFIED (or STALE / UNAVAILABLE).
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum

MAX_FUTURE_SKEW = timedelta(seconds=1)

# what a provider's timestamp is documented to mean
TIME_UNKNOWN = "UNKNOWN"
# where a snapshot's values came from: the label a reader needs to tell a real provider response from test data
EVIDENCE_PROVIDER_RESPONSE = "PROVIDER_RESPONSE"        # produced by the real HTTPS client talking to the provider
EVIDENCE_FIXTURE_TRANSPORT = "FIXTURE_TRANSPORT"        # parsed by the real adapter code, but from a test transport: NOT a provider response
EVIDENCE_SYNTHETIC = "SYNTHETIC_GENERATOR"              # invented by this project
EVIDENCE_UNSPECIFIED = "UNSPECIFIED"
TIME_QUOTE = "QUOTE_TIME"
TIME_LAST_TRADE = "LAST_TRADE_TIME"
# what the provider documents about latency
LATENCY_UNKNOWN = "UNKNOWN"
LATENCY_REALTIME_DOCUMENTED = "REALTIME_DOCUMENTED"
LATENCY_DELAYED_CONFIRMED = "DELAYED_CONFIRMED"


class DataState(Enum):
    LIVE = "LIVE"                  # provider data, documented real-time, documented quote time, fresh
    DELAYED = "DELAYED"            # provider data the provider confirms is delayed
    SIMULATED = "SIMULATED"        # fresh, and positively identified as synthetic
    UNVERIFIED = "UNVERIFIED"      # provider data, well-formed, real-time status or timestamp meaning not established
    STALE = "STALE"                # older than the allowed age: values are withheld
    UNAVAILABLE = "UNAVAILABLE"    # no data, an error, an invalid timestamp or an unrecognised kind of data: values are withheld


VALUE_STATES = frozenset({DataState.LIVE, DataState.DELAYED, DataState.SIMULATED, DataState.UNVERIFIED})


@dataclass(frozen=True)
class OptionRow:
    symbol: str                    # synthetic label such as SYN-CE-24000, or a provider symbol for provider data
    kind: str                      # "CE" or "PE"
    strike: int
    price: Decimal
    bid: Decimal
    ask: Decimal
    bid_qty: "int | None" = None       # displayed size at the best bid / ask, when the provider supplies it
    ask_qty: "int | None" = None
    lot_size: "int | None" = None      # contract lot size and tick size, when known from the provider's instrument data
    tick: "Decimal | None" = None


@dataclass(frozen=True)
class Snapshot:
    source: str
    synthetic: bool                # True only for data made up by this project; False only for a provider snapshot with received_at set
    as_of: datetime                # for provider data this is the local receive time
    index_value: Decimal
    pct_change: "Decimal | None"
    chain: tuple
    received_at: "datetime | None" = None          # our clock when the response arrived; proves nothing about when the market observation was made
    provider_time_raw: object = None               # the provider's time field exactly as received, never reinterpreted
    provider_time: "datetime | None" = None        # only set when time_semantics says it is a quote time
    time_semantics: str = TIME_UNKNOWN
    latency_class: str = LATENCY_UNKNOWN
    notes: tuple = ()
    evidence: str = EVIDENCE_UNSPECIFIED          # see EVIDENCE_*; it labels the data, it never upgrades it (state is decided by assess())


@dataclass(frozen=True)
class PaperAccount:
    """Configurable example figures. Nothing here records a trade or a fill."""
    starting_balance: Decimal = Decimal("1000000")
    positions: tuple = ()
    pnl: Decimal = Decimal("0")
    orders_executed: int = 0


@dataclass(frozen=True)
class Health:
    state: DataState
    reason: str
    source: str            # "-" when unknown
    as_of: "datetime | None"
    received_at: "datetime | None" = None
    provider_time_raw: object = None


def _aware(value):
    return isinstance(value, datetime) and value.utcoffset() is not None


def assess(snapshot, now, max_age):
    """Fail closed: SIMULATED needs a fresh synthetic snapshot; provider data is at best UNVERIFIED unless explicit evidence says otherwise."""
    if not isinstance(snapshot, Snapshot):
        return Health(DataState.UNAVAILABLE, "no data from provider", "-", None)
    source = str(snapshot.source)
    as_of = snapshot.as_of
    if not _aware(as_of):
        return Health(DataState.UNAVAILABLE, "missing or timezone-naive timestamp", source, None)
    if not _aware(now):
        return Health(DataState.UNAVAILABLE, "no valid clock", source, as_of)
    if snapshot.synthetic is True:
        return _assess_synthetic(snapshot, now, max_age, source, as_of)
    if snapshot.synthetic is False:
        return _assess_provider(snapshot, now, max_age, source)
    return Health(DataState.UNAVAILABLE, "unrecognised kind of data", source, as_of)


def _assess_synthetic(snapshot, now, max_age, source, as_of):
    age = now - as_of
    if age < -MAX_FUTURE_SKEW:
        return Health(DataState.UNAVAILABLE, "timestamp is in the future", source, as_of)
    if age > max_age:
        return Health(DataState.STALE, f"data is {int(age.total_seconds())} s old (limit {int(max_age.total_seconds())} s)", source, as_of)
    return Health(DataState.SIMULATED, "synthetic sample data, fresh", source, as_of)


def _assess_provider(snapshot, now, max_age, source):
    received = snapshot.received_at
    raw = snapshot.provider_time_raw
    if not _aware(received):
        return Health(DataState.UNAVAILABLE, "provider data has no valid local receive time", source, snapshot.as_of, None, raw)

    def health(state, reason):
        return Health(state, reason, source, snapshot.as_of, received, raw)

    age = now - received
    if age < -MAX_FUTURE_SKEW:
        return health(DataState.UNAVAILABLE, "local receive time is in the future")
    if age > max_age:
        return health(DataState.STALE, f"last response was received {int(age.total_seconds())} s ago (limit {int(max_age.total_seconds())} s); "
                                       "when the market observation was made is not known")
    if snapshot.time_semantics == TIME_QUOTE:
        quote_time = snapshot.provider_time
        if not _aware(quote_time):
            return health(DataState.UNAVAILABLE, "provider time declared as quote time but unusable")
        quote_age = now - quote_time
        if quote_age < -MAX_FUTURE_SKEW:
            return health(DataState.UNAVAILABLE, "provider quote time is in the future")
        if quote_age > max_age:
            return health(DataState.STALE, f"provider quote time is {int(quote_age.total_seconds())} s old (limit {int(max_age.total_seconds())} s)")
        if snapshot.latency_class == LATENCY_REALTIME_DOCUMENTED:
            return health(DataState.LIVE, "documented real-time feed with a fresh documented quote time")
    if snapshot.latency_class == LATENCY_DELAYED_CONFIRMED:
        return health(DataState.DELAYED, "provider confirms the data is delayed")
    return health(DataState.UNVERIFIED, "response is well-formed, but real-time status and the meaning of the provider timestamp are not established")


# What the user sees as the connection status. CONNECTED means the provider answered AND its timing is established (LIVE or DELAYED, the latter
# marked as delayed); a well-formed answer with unestablished timing is only UNVERIFIED.
CONNECTED, NOT_A_CONNECTION = "CONNECTED", "SIMULATED"


def connection_status(state, provider_mode):
    if not provider_mode:
        return NOT_A_CONNECTION
    if state in (DataState.LIVE, DataState.DELAYED):
        return CONNECTED
    return state.value if state in (DataState.UNVERIFIED, DataState.STALE) else DataState.UNAVAILABLE.value
