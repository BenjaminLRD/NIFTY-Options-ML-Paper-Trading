"""Quote data model.

A `Quote` records what a price source said, without judging it: the constructor accepts any values so that bad
input can be represented and then REJECTED by `niftypaper.eligibility`. Nothing in this module makes a quote fillable.

Every quote carries a `digest` computed at construction. `eligibility` recomputes it, so a field changed afterwards
(for example with `object.__setattr__`) is detected. This is tamper-evidence, not security: code that rebuilds the
whole object, or that edits the digest too, is not stopped (see docs/SAFETY.md).
"""
from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class QuoteType(enum.Enum):
    """What kind of price this is. Only LIVE_QUOTE can ever be a fill source (see eligibility.FILLABLE_TYPES)."""

    LIVE_QUOTE = "LIVE_QUOTE"            # timestamped bid/ask from a live feed
    DELAYED_QUOTE = "DELAYED_QUOTE"      # bid/ask from a delayed feed: not supported as a fill source in this version
    EOD_SETTLEMENT = "EOD_SETTLEMENT"    # end-of-day settlement / close / OHLC (for example bhavcopy): never executable
    LAST_TRADE = "LAST_TRADE"            # last traded price: not a quote, never executable
    UNKNOWN = "UNKNOWN"                  # provenance unknown: never executable


_FIELDS = ("symbol", "quote_type", "bid", "ask", "bid_qty", "ask_qty", "ts", "source")


def _canonical(value: Any) -> str:
    if isinstance(value, enum.Enum):
        return f"enum:{type(value).__name__}.{value.name}"
    if isinstance(value, datetime):
        return f"datetime:{value.isoformat()}|{value.utcoffset()!r}"
    try:
        return f"{type(value).__name__}:{value!r}"
    except Exception:                       # a value that cannot even be printed must still be representable (and then rejected)
        return f"{type(value).__name__}:<unprintable>"


def compute_digest(quote: "Quote") -> str:
    text = "\x1f".join(_canonical(getattr(quote, name, None)) for name in _FIELDS)
    return hashlib.sha256(text.encode("utf-8", "backslashreplace")).hexdigest()


@dataclass(frozen=True)
class Quote:
    """Expected types (NOT enforced here): symbol str, quote_type QuoteType, bid/ask Decimal|int|float, bid_qty/ask_qty int,
    ts timezone-aware datetime (the source's timestamp), source non-empty str naming where the price came from."""

    symbol: Any
    quote_type: Any
    bid: Any
    ask: Any
    bid_qty: Any
    ask_qty: Any
    ts: Any
    source: Any
    digest: str = field(init=False, repr=False, compare=False, default="")

    def __post_init__(self) -> None:
        object.__setattr__(self, "digest", compute_digest(self))


def has_valid_digest(quote: Quote) -> bool:
    return isinstance(getattr(quote, "digest", None), str) and quote.digest == compute_digest(quote)


__all__ = ["Quote", "QuoteType", "compute_digest", "has_valid_digest"]
