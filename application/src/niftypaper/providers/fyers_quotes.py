"""Pure validation of a FYERS quotes response (no network, no clock). Documentation: https://fyers.in/api-docs/v3/api/market-data/quotes/

Documented shape: {"s": "ok", "code": 200, "message": "", "d": [{"n": symbol, "s": "ok"|"error", "v": {...}}]}.
Per symbol the checks are: status, symbol identity, numeric price fields, and for options a non-empty, uncrossed, on-tick bid/ask.

The time field `tt` is documented only as "Today's time" (an integer; the documentation's own examples are exact midnight values,
1691020800 and 1622160000, both multiples of 86400). It is preserved exactly as received and never interpreted as a quote time.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

INDEX_SYMBOL = "NSE:NIFTY50-INDEX"        # documented for the index stream; the quotes page does not show an index example

DESCRIPTIONS = {
    "CREDENTIALS_MISSING": "FYERS credentials are not configured in the environment",
    "CREDENTIALS_INVALID": "FYERS credentials in the environment have an unexpected format",
    "AUTH_FAILED": "FYERS rejected the credentials (missing, invalid or expired; access tokens expire daily at 06:30)",
    "ENTITLEMENT_DENIED": "FYERS denied access (HTTP 403): the app lacks the needed permission",
    "RATE_LIMITED": "FYERS rate limit reached; waiting before the next request",
    "INVALID_SYMBOL": "FYERS did not accept the requested symbol",
    "API_ERROR": "FYERS returned an error response",
    "MALFORMED_RESPONSE": "FYERS response was not in the documented shape",
    "SYMBOL_MISMATCH": "FYERS response was for a different symbol than requested",
    "MISSING_FIELD": "FYERS response is missing a required field",
    "INVALID_PRICE": "FYERS response has a non-numeric, non-positive or non-finite value",
    "EMPTY_QUOTE": "FYERS bid or ask is empty (zero)",
    "CROSSED_OR_LOCKED": "FYERS bid is not below ask (crossed or locked market)",
    "OFF_TICK": "FYERS bid or ask is not a multiple of the tick size",
    "NETWORK_ERROR": "could not reach FYERS",
    "TIMEOUT": "request to FYERS timed out",
    "REDIRECT_REFUSED": "FYERS redirected the request; redirects are refused",
    "RESPONSE_TOO_LARGE": "response was larger than the allowed size",
    "INTERNAL_ERROR": "unexpected internal error while reading FYERS data",
    "OPTION_UNRESOLVED": "the requested option contract could not be resolved from the official symbol master; nothing was guessed",
    "PROVIDER_START_FAILED": "the data provider could not be started",
}


class QuoteError(Exception):
    """Carries only a short code from DESCRIPTIONS, never response text, so nothing sensitive can travel with it."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class QuoteSpec:
    expect_bid_ask: bool
    tick: "Decimal | None" = None


@dataclass(frozen=True)
class ParsedQuote:
    symbol: str
    last_price: Decimal
    bid: "Decimal | None"
    ask: "Decimal | None"
    pct_change: "Decimal | None"
    provider_time_raw: object


@dataclass(frozen=True)
class SymbolResult:
    quote: "ParsedQuote | None"
    error: "str | None"


_AUTH_CODES = {-8, -15, -16, -17, -352}
_RATE_CODES = {-429, -360}


def code_for_api_code(api_code):
    """Map a documented FYERS error code to our short code."""
    if isinstance(api_code, bool) or not isinstance(api_code, int):
        return "API_ERROR"
    if api_code in _AUTH_CODES:
        return "AUTH_FAILED"
    if api_code in _RATE_CODES:
        return "RATE_LIMITED"
    if api_code == -300:
        return "INVALID_SYMBOL"
    return "API_ERROR"


def code_for_http_status(status):
    """None for 200, otherwise the short code for a documented HTTP status."""
    if status == 200:
        return None
    return {401: "AUTH_FAILED", 403: "ENTITLEMENT_DENIED", 429: "RATE_LIMITED"}.get(status, "API_ERROR")


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError
    if not number.is_finite():
        raise ValueError
    return number


def _check_entry(symbol, entry, spec):
    values = entry.get("v")
    if entry.get("s") != "ok":
        api_code = values.get("code") if isinstance(values, dict) else None
        return SymbolResult(None, "INVALID_SYMBOL" if api_code == -300 else "API_ERROR")
    if not isinstance(values, dict):
        return SymbolResult(None, "MALFORMED_RESPONSE")
    if "symbol" in values and values["symbol"] != symbol:
        return SymbolResult(None, "SYMBOL_MISMATCH")
    if "lp" not in values:
        return SymbolResult(None, "MISSING_FIELD")
    try:
        last = _number(values["lp"])
        if last <= 0:
            return SymbolResult(None, "INVALID_PRICE")
        change = _number(values["chp"]) if "chp" in values else None
        bid = ask = None
        if spec.expect_bid_ask:
            if "bid" not in values or "ask" not in values:
                return SymbolResult(None, "MISSING_FIELD")
            bid, ask = _number(values["bid"]), _number(values["ask"])
    except ValueError:
        return SymbolResult(None, "INVALID_PRICE")
    if spec.expect_bid_ask:
        if bid <= 0 or ask <= 0:
            return SymbolResult(None, "EMPTY_QUOTE")
        if bid >= ask:
            return SymbolResult(None, "CROSSED_OR_LOCKED")
        if spec.tick is not None and (bid % spec.tick != 0 or ask % spec.tick != 0):
            return SymbolResult(None, "OFF_TICK")
    return SymbolResult(ParsedQuote(symbol, last, bid, ask, change, values.get("tt")), None)


def parse_quotes(payload, specs):
    """Validate a decoded response against {symbol: QuoteSpec}. Whole-response failures raise QuoteError; per-symbol ones are returned."""
    if not isinstance(payload, dict):
        raise QuoteError("MALFORMED_RESPONSE")
    if payload.get("s") != "ok" or payload.get("code") != 200 or isinstance(payload.get("code"), bool):
        raise QuoteError(code_for_api_code(payload.get("code")) if payload.get("s") == "error" else "MALFORMED_RESPONSE")
    entries = payload.get("d")
    if not isinstance(entries, list):
        raise QuoteError("MALFORMED_RESPONSE")
    seen = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("n"), str):
            raise QuoteError("MALFORMED_RESPONSE")
        if entry["n"] in seen:
            raise QuoteError("MALFORMED_RESPONSE")
        seen[entry["n"]] = entry
    results = {}
    for symbol, spec in specs.items():
        results[symbol] = _check_entry(symbol, seen[symbol], spec) if symbol in seen else SymbolResult(None, "SYMBOL_MISMATCH")
    return results
