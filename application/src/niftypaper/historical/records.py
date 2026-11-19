"""Canonical record kinds and their strict validators (config/historical_schema_v1.json).

Four kinds stay separate types: EOD_CONTRACT_DAY, INTRADAY_BAR, QUOTE_SNAPSHOT and UNDERLYING_EOD. Only a QUOTE_SNAPSHOT has bid/ask fields and only
it can ever be fill-eligible. A missing value is None; nothing is estimated, filled forward or taken from another record. Anything ambiguous is rejected
with a reason code, never converted by a guessed convention.
"""
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

IST = timezone(timedelta(hours=5, minutes=30))
SCHEMA_VERSION = "1.0.0-draft"
PARSER_VERSION = "canonical-2.0.0"
POLICY_VERSION = "1"

EOD, BAR, QUOTE, INDEX = "EOD_CONTRACT_DAY", "INTRADAY_BAR", "QUOTE_SNAPSHOT", "UNDERLYING_EOD"
KINDS = (EOD, BAR, QUOTE, INDEX)
ORIGIN_CLASSES = ("SYNTHETIC_FIXTURE", "UNVERIFIED_SOURCE", "UNKNOWN")
TIME_SEMANTICS = ("QUOTE_TIME", "LAST_TRADE_TIME", "RECEIVE_TIME", "DAY_MARKER", "UNKNOWN")
BAR_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "60m": 3600, "1d": None}
BAR_CONVENTIONS = ("START", "END", "UNKNOWN")
VOLUME_UNITS = ("CONTRACTS", "UNITS", "UNKNOWN")

# Every reason a record, a file or a pair of records can be excluded. config/historical_schema_v1.json lists the same codes (a test compares them).
REASONS = (
    "DUPLICATE_OF_EARLIER_COPY", "CONFLICTING_COPIES", "DUPLICATE_ROW_IN_FILE", "CONFLICTING_ROW_IN_FILE", "INVALID_NUMBER", "INVALID_DATE",
    "MISSING_REQUIRED_FIELD", "UNKNOWN_COLUMN_LAYOUT", "HASH_MISMATCH_WITH_MANIFEST", "OUTSIDE_REQUESTED_WINDOW",
    "MALFORMED_RECORD", "UNKNOWN_RECORD_KIND", "RECORD_KIND_MISMATCH", "FORBIDDEN_FIELD", "INVALID_TIMESTAMP", "INVALID_TIMEZONE", "INVALID_PRICE",
    "INVALID_OHLC", "OFF_TICK", "CROSSED_QUOTE", "INVALID_SPEC", "INCONSISTENT_TIMES", "FUTURE_OBSERVATION", "ORIGIN_LABEL_MISMATCH",
    "MISSING_FILE", "SIZE_MISMATCH", "UNLISTED_FILE", "MALFORMED_FILE",
)
QUALITY_FLAGS = (
    "PRICE_ZERO_NO_TRADE", "PRICE_DASH_NO_TRADE", "CLOSE_CARRIED_NO_TRADE", "LAST_CARRIED_NO_TRADE", "LOT_SIZE_ABSENT", "TICK_SIZE_ABSENT",
    "UNDERLYING_VALUE_ABSENT", "TURNOVER_MEANING_UNVERIFIED", "VOLUME_UNIT_INFERRED", "ID_NOT_STABLE", "INTERVAL_TIMESTAMP_CONVENTION_UNKNOWN",
    "AVAILABILITY_RULE_DEFAULT", "VOLUME_UNIT_UNKNOWN", "QUOTE_SIZES_ABSENT",
)
FORBIDDEN = {
    EOD: ("bid", "ask", "bid_qty", "ask_qty", "depth"),
    BAR: ("bid", "ask", "bid_qty", "ask_qty", "depth", "settlement_price"),
    INDEX: ("bid", "ask", "bid_qty", "ask_qty", "depth"),
}
IST_OFFSET = timedelta(hours=5, minutes=30)


class Reject(Exception):
    """A record cannot be used. `code` is one of REASONS."""

    def __init__(self, code, detail):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


@dataclass(frozen=True)
class Record:
    kind: str
    key: str                       # contract_key for contract records, the underlying for UNDERLYING_EOD
    time_key: str                  # trading date, bar interval + start, or observed_at: the time part of the natural key
    values: dict                   # canonical, typed values; absent data is None
    observed_at: "datetime | None"  # when the market was observed; None for day-level records
    available_at: "datetime | None"  # the earliest instant a replay may use the record; None means unknown and the record is never usable
    retrieved_at: "datetime | None"  # when the file was obtained (provenance only; never used as a market time)
    flags: tuple
    quality_status: str
    fill_eligible: bool
    ineligible_reasons: tuple
    source_id: str
    origin_class: str
    source_file: str
    source_file_sha256: str
    source_row: int                # physical 1-based line number in the file
    raw: str                       # the source line exactly as read
    ingested_at_utc: datetime
    content_digest: str
    schema_version: str = SCHEMA_VERSION
    parser_version: str = PARSER_VERSION

    @property
    def natural_key(self):
        return (self.kind, self.key, self.time_key, self.source_id)       # never the instrument token

    def get(self, name):
        return self.values.get(name)


@dataclass(frozen=True)
class Exclusion:
    source_id: str
    source_file: str
    source_file_sha256: "str | None"
    source_row: "int | None"
    kind: "str | None"
    key: "str | None"
    time_key: "str | None"
    reason: str
    detail: str
    other_copy: "str | None" = None
    policy_version: str = POLICY_VERSION

    def to_dict(self):
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class Built:
    key: str
    time_key: str
    values: dict
    observed_at: "datetime | None"
    available_at: datetime
    flags: tuple
    fill_eligible: bool = False
    ineligible_reasons: tuple = ()


# ---- field parsers (strict: JSON strings or integers for numbers, ISO text for dates and times, no guessing) ------------------------------------
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DECIMAL = re.compile(r"^-?\d{1,15}(\.\d{1,8})?$")
_DIGITS = re.compile(r"^\d{1,15}$")
_NAME = re.compile(r"^[A-Z0-9&-]{1,20}$")


def _blank(value):
    return value is None or value == ""


def _date_field(raw, name, required=True):
    value = raw.get(name)
    if _blank(value):
        if required:
            raise Reject("MISSING_REQUIRED_FIELD", name)
        return None
    if not isinstance(value, str) or not _DATE.match(value):
        raise Reject("INVALID_DATE", f"{name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise Reject("INVALID_DATE", f"{name} is not a calendar date") from None


def _stamp(raw, name, required=True):
    """An exchange-clock timestamp: ISO-8601 with an explicit +05:30 offset. Naive times and other offsets are refused, never converted or assumed."""
    value = raw.get(name)
    if _blank(value):
        if required:
            raise Reject("MISSING_REQUIRED_FIELD", name)
        return None
    if not isinstance(value, str) or "T" not in value:
        raise Reject("INVALID_TIMESTAMP", f"{name} must be an ISO-8601 date and time")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise Reject("INVALID_TIMESTAMP", f"{name} is not a valid timestamp") from None
    if moment.tzinfo is None:
        raise Reject("INVALID_TIMEZONE", f"{name} has no UTC offset")
    if moment.utcoffset() != IST_OFFSET:
        raise Reject("INVALID_TIMEZONE", f"{name} is not in +05:30 (India Standard Time)")
    return moment


def _number(value, name):
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, (str, int)):
        raise Reject("INVALID_NUMBER", f"{name}: numbers must be JSON strings or integers")
    text = str(value)
    if not _DECIMAL.match(text):
        raise Reject("INVALID_NUMBER", f"{name} is not a plain decimal number")
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise Reject("INVALID_NUMBER", f"{name} is not a number") from None
    return number


def _price(raw, name, flags, day_level=False):
    """A price: positive or None. On day-level records a source '0' or '-' means 'no trade': null plus a flag. Elsewhere zero is invalid."""
    value = raw.get(name)
    if _blank(value):
        return None
    if day_level and value == "-":
        flags.append("PRICE_DASH_NO_TRADE")
        return None
    number = _number(value, name)
    if number < 0:
        raise Reject("INVALID_PRICE", f"{name} is negative")
    if number == 0:
        if day_level:
            flags.append("PRICE_ZERO_NO_TRADE")
            return None
        raise Reject("INVALID_PRICE", f"{name} is zero")
    return number


def _required_price(raw, name):
    if _blank(raw.get(name)):
        raise Reject("MISSING_REQUIRED_FIELD", name)
    return _price(raw, name, [])


def _count(raw, name):
    value = raw.get(name)
    if _blank(value):
        return None
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, (str, int)) or not _DIGITS.match(str(value)):
        raise Reject("INVALID_NUMBER", f"{name} must be a non-negative whole number")
    return int(value)


def _text(raw, name):
    value = raw.get(name)
    if _blank(value):
        raise Reject("MISSING_REQUIRED_FIELD", name)
    if not isinstance(value, str) or not _NAME.match(value):
        raise Reject("INVALID_SPEC", f"{name} must be upper-case letters, digits, & or -")
    return value


def _spec(raw, name, kind):
    """lot_size (whole number > 0) or tick_size (decimal > 0): None when absent, Reject when present but invalid."""
    if _blank(raw.get(name)):
        return None
    number = _count(raw, name) if kind == "lot" else _number(raw[name], name)
    if number is None or number <= 0:
        raise Reject("INVALID_SPEC", f"{name} must be above zero")
    return number


def _identity(raw, on_or_before):
    underlying = _text(raw, "underlying")
    expiry = _date_field(raw, "expiry")
    if _blank(raw.get("strike")):
        raise Reject("MISSING_REQUIRED_FIELD", "strike")
    strike = _number(raw["strike"], "strike")
    if strike <= 0 or strike != strike.quantize(Decimal("0.01")):
        raise Reject("INVALID_SPEC", "strike must be above zero with at most 2 decimals")
    option_type = raw.get("option_type")
    if _blank(option_type):
        raise Reject("MISSING_REQUIRED_FIELD", "option_type")
    if option_type not in ("CE", "PE"):
        raise Reject("INVALID_SPEC", "option_type must be CE or PE")
    if expiry < on_or_before:
        raise Reject("INVALID_DATE", "the contract had already expired on the record's date")
    return f"{underlying}|{expiry.isoformat()}|{strike.quantize(Decimal('0.01'))}|{option_type}", underlying, expiry, strike, option_type


def _ohlc_check(values, names=("open", "high", "low", "close")):
    present = {n: values[n] for n in names if values.get(n) is not None}
    if not present:
        return
    low = present.get("low")
    high = present.get("high")
    if low is not None and high is not None and low > high:
        raise Reject("INVALID_OHLC", "low is above high")
    for name, price in present.items():
        if high is not None and price > high or low is not None and price < low:
            raise Reject("INVALID_OHLC", f"{name} is outside the low-high range")


def day_end(day):
    return datetime.combine(day, time(23, 59, 59), tzinfo=IST)


def _day_level_availability(raw, day, flags):
    """Day-level records say nothing about the time of day, so the only safe availability is the end of the trading date. An explicit
    earlier claim is refused rather than believed; an explicit later one is kept."""
    floor = day_end(day)
    stated = _stamp(raw, "available_at", required=False)
    if stated is None:
        flags.append("AVAILABILITY_RULE_DEFAULT")
        return floor
    if stated < floor:
        raise Reject("INCONSISTENT_TIMES", "a day-level record cannot be available before the end of its trading date")
    return stated


def _check_times(observed, available, as_of, retrieved):
    if available < observed:
        raise Reject("INCONSISTENT_TIMES", "available_at is before observed_at")
    if observed > as_of:
        raise Reject("FUTURE_OBSERVATION", "observed_at is after the ingest cut-off")
    if retrieved is not None and observed > retrieved:
        raise Reject("FUTURE_OBSERVATION", "observed_at is after the file's retrieval time")


def _forbid(raw, kind):
    for name in FORBIDDEN.get(kind, ()):
        if name in raw:
            raise Reject("FORBIDDEN_FIELD", f"{kind} records have no {name} field (only QUOTE_SNAPSHOT carries bid/ask)")


# ---- the four kinds ----------------------------------------------------------------------------------------------------------------------------
def _eod(raw, ctx):
    _forbid(raw, EOD)
    day = _date_field(raw, "trading_date")
    if _blank(raw.get("observed_at")) is False:
        raise Reject("INCONSISTENT_TIMES", "an end-of-day record has no time of day: observed_at must be absent")
    if day > ctx["as_of"].astimezone(IST).date():
        raise Reject("FUTURE_OBSERVATION", "trading_date is after the ingest cut-off")
    key, *_ = _identity(raw, day)
    flags = []
    values = {n: _price(raw, n, flags, True) for n in ("open", "high", "low", "close", "last_price", "previous_close", "settlement_price", "underlying_value")}
    _ohlc_check(values)
    volume, transactions = _count(raw, "volume"), _count(raw, "transactions")
    values.update(volume=volume, transactions=transactions, open_interest=_count(raw, "open_interest"), lot_size=_spec(raw, "lot_size", "lot"),
                  tick_size=_spec(raw, "tick_size", "tick"), trading_date=day, observation_precision="DAY", source_instrument_id=raw.get("instrument_token"))
    unit = raw.get("volume_unit", "UNKNOWN")
    if unit not in VOLUME_UNITS:
        raise Reject("INVALID_SPEC", "volume_unit must be CONTRACTS, UNITS or UNKNOWN")
    values["volume_unit"] = unit
    if volume is not None and unit == "UNKNOWN":
        flags.append("VOLUME_UNIT_UNKNOWN")
    reported = [v for v in (volume, transactions) if v is not None]
    values["traded"] = any(v > 0 for v in reported) if reported else None
    if values["traded"] is False:                                  # a value shown for a contract that did not trade is a carried one, not a price paid
        flags += [f for f, n in (("CLOSE_CARRIED_NO_TRADE", "close"), ("LAST_CARRIED_NO_TRADE", "last_price")) if values[n] is not None]
    if values["lot_size"] is None:
        flags.append("LOT_SIZE_ABSENT")
    if values["tick_size"] is None:
        flags.append("TICK_SIZE_ABSENT")
    available = _day_level_availability(raw, day, flags)
    return Built(key, day.isoformat(), values, None, available, tuple(flags))


def _index(raw, ctx):
    _forbid(raw, INDEX)
    day = _date_field(raw, "trading_date")
    if not _blank(raw.get("observed_at")):
        raise Reject("INCONSISTENT_TIMES", "an index end-of-day record has no time of day: observed_at must be absent")
    if day > ctx["as_of"].astimezone(IST).date():
        raise Reject("FUTURE_OBSERVATION", "trading_date is after the ingest cut-off")
    flags = []
    values = {n: _price(raw, n, flags, True) for n in ("open", "high", "low", "close")}
    _ohlc_check(values)
    values.update(trading_date=day, observation_precision="DAY")
    return Built(_text(raw, "underlying"), day.isoformat(), values, None, _day_level_availability(raw, day, flags), tuple(flags))


def _bar(raw, ctx):
    _forbid(raw, BAR)
    start, end = _stamp(raw, "bar_start"), _stamp(raw, "bar_end")
    interval = raw.get("interval")
    if interval not in BAR_SECONDS:
        raise Reject("INVALID_SPEC", "interval must be one of " + ", ".join(BAR_SECONDS))
    if end <= start or (BAR_SECONDS[interval] and end - start != timedelta(seconds=BAR_SECONDS[interval])):
        raise Reject("INCONSISTENT_TIMES", "bar_end does not follow bar_start by the interval")
    key, *_ = _identity(raw, start.date())
    convention = raw.get("timestamp_convention", "UNKNOWN")
    if convention not in BAR_CONVENTIONS:
        raise Reject("INVALID_SPEC", "timestamp_convention must be START, END or UNKNOWN")
    flags = ["INTERVAL_TIMESTAMP_CONVENTION_UNKNOWN"] if convention == "UNKNOWN" else []
    available = _stamp(raw, "available_at")                         # a candle is knowable only once it has closed; never defaulted
    _check_times(end, available, ctx["as_of"], ctx["retrieved_at"])
    values = {n: _price(raw, n, [], False) for n in ("open", "high", "low", "close")}
    _ohlc_check(values)
    values.update(interval=interval, bar_start=start, bar_end=end, timestamp_convention=convention, volume=_count(raw, "volume"),
                  open_interest=_count(raw, "open_interest"), source_instrument_id=raw.get("instrument_token"))
    return Built(key, f"{interval}@{start.isoformat()}", values, None, available, tuple(flags))


def _quote(raw, ctx):
    observed = _stamp(raw, "observed_at")
    key, *_ = _identity(raw, observed.date())
    semantics = raw.get("time_semantics")
    if _blank(semantics):
        raise Reject("MISSING_REQUIRED_FIELD", "time_semantics")
    if semantics not in TIME_SEMANTICS:
        raise Reject("INVALID_SPEC", "time_semantics is not one of the documented values")
    available = _stamp(raw, "available_at")                         # not defaulted: a quote with unknown availability cannot be used
    _check_times(observed, available, ctx["as_of"], ctx["retrieved_at"])
    bid, ask = _required_price(raw, "bid"), _required_price(raw, "ask")
    if bid >= ask:
        raise Reject("CROSSED_QUOTE", "bid is not below ask")
    lot, tick = _spec(raw, "lot_size", "lot"), _spec(raw, "tick_size", "tick")
    if lot is None or tick is None:
        raise Reject("MISSING_REQUIRED_FIELD", "lot_size and tick_size are required on a quote snapshot")
    if bid % tick or ask % tick:
        raise Reject("OFF_TICK", "bid or ask is not a multiple of tick_size")
    bid_qty, ask_qty = _count(raw, "bid_qty"), _count(raw, "ask_qty")
    last = _price(raw, "last_price", [], False)
    values = dict(bid=bid, ask=ask, bid_qty=bid_qty, ask_qty=ask_qty, last_price=last, lot_size=lot, tick_size=tick, time_semantics=semantics,
                  source_instrument_id=raw.get("instrument_token"))
    flags, why = [], []
    if bid_qty is None or ask_qty is None:
        flags.append("QUOTE_SIZES_ABSENT")
        why.append("SIZES_ABSENT")
    if semantics != "QUOTE_TIME":                                   # only a documented quote time may drive a fill
        why.append("TIME_NOT_A_QUOTE_TIME")
    return Built(key, observed.isoformat(), values, observed, available, tuple(flags), not why, tuple(why))


BUILDERS = {EOD: _eod, BAR: _bar, QUOTE: _quote, INDEX: _index}


def _jsonable(value):
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def digest_of(kind, built):
    body = {"kind": kind, "key": built.key, "time_key": built.time_key, "values": {k: _jsonable(v) for k, v in sorted(built.values.items())},
            "observed_at": _jsonable(built.observed_at), "available_at": _jsonable(built.available_at)}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def build(kind, raw, *, as_of, retrieved_at=None):
    """Validate one source object and return a Built, or raise Reject. `as_of` is the ingest cut-off (aware datetime), supplied by the caller."""
    if kind not in BUILDERS:
        raise Reject("UNKNOWN_RECORD_KIND", str(kind))
    return BUILDERS[kind](raw, {"as_of": as_of, "retrieved_at": retrieved_at})
