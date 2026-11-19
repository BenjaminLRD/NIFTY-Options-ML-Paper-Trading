"""Fail-closed execution-eligibility boundary.

The only way to obtain a price to fill against is:

    executable = require_fillable(quote, now=..., policy=...)   # raises IneligibleQuoteError unless every check passes
    price = fill_price(executable, Side.BUY)                    # accepts ONLY an ExecutableQuote

Design rules
- Default is rejection. Anything unexpected (wrong type, missing field, exception while checking) is ineligible.
- Only QuoteType.LIVE_QUOTE can pass. End-of-day / settlement, last-traded, delayed and unknown prices never can.
- No warnings and no substitution: an ineligible quote produces a rejection (a result or an exception), never a fallback
  to another price. There is no function in this package that turns a settlement or last-traded price into a fill price.
- The clock is an explicit argument (`now`), never read implicitly, so every decision is reproducible.
- Limits: this is an in-process guard. Deliberate bypasses (importing private names, patching functions, rebuilding objects)
  are not prevented. See docs/SAFETY.md.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from .quotes import Quote, QuoteType, has_valid_digest

FILLABLE_TYPES = frozenset({QuoteType.LIVE_QUOTE})


class Reason(str, enum.Enum):
    NOT_A_QUOTE = "NOT_A_QUOTE"
    TAMPERED = "TAMPERED"
    BAD_QUOTE_TYPE = "BAD_QUOTE_TYPE"
    QUOTE_TYPE_NOT_EXECUTABLE = "QUOTE_TYPE_NOT_EXECUTABLE"
    MISSING_SYMBOL = "MISSING_SYMBOL"
    MISSING_SOURCE = "MISSING_SOURCE"
    MISSING_TIMESTAMP = "MISSING_TIMESTAMP"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    NAIVE_TIMESTAMP = "NAIVE_TIMESTAMP"
    FUTURE_TIMESTAMP = "FUTURE_TIMESTAMP"
    STALE = "STALE"
    MISSING_BID = "MISSING_BID"
    MISSING_ASK = "MISSING_ASK"
    INVALID_BID = "INVALID_BID"
    INVALID_ASK = "INVALID_ASK"
    NON_POSITIVE_BID = "NON_POSITIVE_BID"
    NON_POSITIVE_ASK = "NON_POSITIVE_ASK"
    OFF_TICK_BID = "OFF_TICK_BID"
    OFF_TICK_ASK = "OFF_TICK_ASK"
    CROSSED = "CROSSED"
    LOCKED = "LOCKED"
    MISSING_BID_SIZE = "MISSING_BID_SIZE"
    MISSING_ASK_SIZE = "MISSING_ASK_SIZE"
    INVALID_BID_SIZE = "INVALID_BID_SIZE"
    INVALID_ASK_SIZE = "INVALID_ASK_SIZE"
    NON_POSITIVE_BID_SIZE = "NON_POSITIVE_BID_SIZE"
    NON_POSITIVE_ASK_SIZE = "NON_POSITIVE_ASK_SIZE"
    MISSING_NOW = "MISSING_NOW"
    NAIVE_NOW = "NAIVE_NOW"
    POLICY_INVALID = "POLICY_INVALID"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class Side(enum.Enum):
    BUY = "BUY"      # fills at the ask
    SELL = "SELL"    # fills at the bid


@dataclass(frozen=True, kw_only=True)
class EligibilityPolicy:
    """Every field is required (no defaults): a missing limit must be a visible error, never a silent loose default."""

    max_age: timedelta            # a quote older than this is stale (age == max_age is still accepted)
    max_future_skew: timedelta    # tolerated clock difference for timestamps slightly ahead of `now`
    tick_size: Decimal            # prices must be exact multiples

    def __post_init__(self) -> None:
        if not isinstance(self.max_age, timedelta) or self.max_age <= timedelta(0):
            raise ValueError("max_age must be a positive timedelta")
        if not isinstance(self.max_future_skew, timedelta) or self.max_future_skew < timedelta(0):
            raise ValueError("max_future_skew must be a non-negative timedelta")
        if not isinstance(self.tick_size, Decimal) or not self.tick_size.is_finite() or self.tick_size <= 0:
            raise ValueError("tick_size must be a finite positive Decimal")


@dataclass(frozen=True)
class EligibilityResult:
    eligible: bool
    reasons: tuple

    def __bool__(self) -> bool:
        # `if check_fill_eligibility(...):` would be always-true for a plain object; refuse to be used that way.
        raise TypeError("do not use an EligibilityResult as a boolean; read .eligible")


class IneligibleQuoteError(Exception):
    def __init__(self, result: EligibilityResult):
        self.result = result
        super().__init__("quote rejected as a fill source: " + ", ".join(r.value for r in result.reasons))


def _to_decimal(value: Any) -> Decimal | None:
    """Accept Decimal, int, float only (not bool, not str). Non-finite values are rejected."""
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value)) if value == value and value not in (float("inf"), float("-inf")) else None
    return None


def _check_price(raw: Any, tick: Decimal, side: str, reasons: list) -> Decimal | None:
    if raw is None:
        reasons.append(Reason[f"MISSING_{side}"])
        return None
    value = _to_decimal(raw)
    if value is None:
        reasons.append(Reason[f"INVALID_{side}"])
        return None
    if value <= 0:
        reasons.append(Reason[f"NON_POSITIVE_{side}"])
        return None
    if value % tick != 0:
        reasons.append(Reason[f"OFF_TICK_{side}"])
    return value


def _check_size(raw: Any, side: str, reasons: list) -> None:
    if raw is None:
        reasons.append(Reason[f"MISSING_{side}_SIZE"])
    elif isinstance(raw, bool) or not isinstance(raw, int):
        reasons.append(Reason[f"INVALID_{side}_SIZE"])
    elif raw <= 0:
        reasons.append(Reason[f"NON_POSITIVE_{side}_SIZE"])


def _evaluate(quote: Any, now: Any, policy: Any, reasons: list) -> None:
    if not isinstance(policy, EligibilityPolicy):
        reasons.append(Reason.POLICY_INVALID)
        return
    if now is None:
        reasons.append(Reason.MISSING_NOW)
    elif not isinstance(now, datetime) or now.utcoffset() is None:
        reasons.append(Reason.NAIVE_NOW)
    if type(quote) is not Quote:             # exact type: a subclass could override attributes
        reasons.append(Reason.NOT_A_QUOTE)
        return
    if not has_valid_digest(quote):
        reasons.append(Reason.TAMPERED)
        return
    if not isinstance(quote.quote_type, QuoteType):
        reasons.append(Reason.BAD_QUOTE_TYPE)
    elif quote.quote_type not in FILLABLE_TYPES:
        reasons.append(Reason.QUOTE_TYPE_NOT_EXECUTABLE)
    if not isinstance(quote.symbol, str) or not quote.symbol.strip():
        reasons.append(Reason.MISSING_SYMBOL)
    if not isinstance(quote.source, str) or not quote.source.strip():
        reasons.append(Reason.MISSING_SOURCE)
    ts = quote.ts
    if ts is None:
        reasons.append(Reason.MISSING_TIMESTAMP)
    elif not isinstance(ts, datetime):
        reasons.append(Reason.INVALID_TIMESTAMP)
    elif ts.utcoffset() is None:
        reasons.append(Reason.NAIVE_TIMESTAMP)
    elif isinstance(now, datetime) and now.utcoffset() is not None:
        age = now - ts
        if age > policy.max_age:
            reasons.append(Reason.STALE)
        if age < -policy.max_future_skew:
            reasons.append(Reason.FUTURE_TIMESTAMP)
    bid = _check_price(quote.bid, policy.tick_size, "BID", reasons)
    ask = _check_price(quote.ask, policy.tick_size, "ASK", reasons)
    if bid is not None and ask is not None:
        if bid > ask:
            reasons.append(Reason.CROSSED)
        elif bid == ask:
            reasons.append(Reason.LOCKED)
    _check_size(quote.bid_qty, "BID", reasons)
    _check_size(quote.ask_qty, "ASK", reasons)


def check_fill_eligibility(quote: Any, *, now: Any, policy: Any) -> EligibilityResult:
    """Never raises. Returns eligible=True only if every check passed; all failing reasons are listed."""
    reasons: list = []
    try:
        _evaluate(quote, now, policy, reasons)
    except Exception:                          # anything unexpected is ineligible, never "probably fine"
        reasons.append(Reason.INTERNAL_ERROR)
    unique = tuple(dict.fromkeys(reasons))
    return EligibilityResult(eligible=not unique, reasons=unique)


_TOKEN = object()


class ExecutableQuote:
    """An immutable snapshot of a quote that passed every check. Only `require_fillable` can create one."""

    __slots__ = ("symbol", "bid", "ask", "bid_qty", "ask_qty", "ts", "source")

    def __init__(self, token: object, symbol: str, bid: Decimal, ask: Decimal, bid_qty: int, ask_qty: int, ts: datetime, source: str):
        if token is not _TOKEN:
            raise TypeError("ExecutableQuote can only be created by require_fillable()")
        for name, value in (("symbol", symbol), ("bid", bid), ("ask", ask), ("bid_qty", bid_qty), ("ask_qty", ask_qty), ("ts", ts), ("source", source)):
            object.__setattr__(self, name, value)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("ExecutableQuote is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("ExecutableQuote is immutable")

    def __repr__(self) -> str:
        return f"ExecutableQuote({self.symbol!r}, bid={self.bid}, ask={self.ask}, ts={self.ts.isoformat()})"


def require_fillable(quote: Any, *, now: Any, policy: Any) -> ExecutableQuote:
    result = check_fill_eligibility(quote, now=now, policy=policy)
    if not result.eligible:
        raise IneligibleQuoteError(result)
    return ExecutableQuote(_TOKEN, quote.symbol, _to_decimal(quote.bid), _to_decimal(quote.ask), quote.bid_qty, quote.ask_qty, quote.ts, quote.source)


def fill_price(executable: Any, side: Any) -> Decimal:
    """The only price accessor for fills: ask for BUY, bid for SELL. Accepts nothing but an ExecutableQuote."""
    if type(executable) is not ExecutableQuote:
        raise TypeError("fill_price requires an ExecutableQuote obtained from require_fillable()")
    if side is Side.BUY:
        return executable.ask
    if side is Side.SELL:
        return executable.bid
    raise TypeError("side must be Side.BUY or Side.SELL")


__all__ = ["EligibilityPolicy", "EligibilityResult", "ExecutableQuote", "FILLABLE_TYPES", "IneligibleQuoteError", "Reason", "Side",
           "check_fill_eligibility", "fill_price", "require_fillable"]
