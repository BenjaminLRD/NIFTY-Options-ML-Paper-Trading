"""Resolves one NIFTY option to its FYERS symbol from the official NSE_FO symbol master (pure: takes the file text, does no I/O).

Documentation: https://fyers.in/api-docs/v3/api/market-information/symbol-master/symbol-master-file/ . The CSV has no header row; the columns
used here are (0-based) 1 symbol details, 3 lot size, 4 tick size, 7 last update date, 8 expiry (epoch seconds), 9 symbol ticker,
13 underlying, 15 strike, 16 option type. The ticker is copied exactly from the file, never composed. A contract is accepted only when
exactly one row matches AND the ticker and the details text agree with the requested expiry, strike and type; otherwise this raises and
nothing is guessed.
"""
import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

IST = timezone(timedelta(hours=5, minutes=30))
MASTER_FILE_NAME = "NSE_FO"
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class SymbolError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ResolvedOption:
    symbol: str
    expiry: date
    strike: int
    option_type: str
    lot_size: int
    tick_size: Decimal
    master_date: str


def _expected_details(expiry, strike, option_type):
    return f"NIFTY {expiry.day:02d} {_MONTHS[expiry.month - 1]} {expiry.year % 100:02d} {strike} {option_type}"


def _row_expiry(row):
    try:
        return datetime.fromtimestamp(int(row[8]), IST).date()
    except (ValueError, OverflowError, OSError):
        return None


def resolve_nifty_option(master_text, *, expiry, strike, option_type, today):
    if option_type not in ("CE", "PE"):
        raise SymbolError("BAD_REQUEST")
    if not isinstance(strike, int) or isinstance(strike, bool) or strike <= 0:
        raise SymbolError("BAD_REQUEST")
    if not isinstance(expiry, date) or not isinstance(today, date):
        raise SymbolError("BAD_REQUEST")
    if expiry < today:
        raise SymbolError("EXPIRED")
    candidates = rows_seen = 0
    matches = []
    for row in csv.reader(io.StringIO(master_text)):
        if len(row) < 18:
            continue
        rows_seen += 1
        if row[13] != "NIFTY" or row[16] != option_type:      # exact underlying: BANKNIFTY, FINNIFTY, NIFTYNXT50 ... are different
            continue
        candidates += 1
        try:
            same_strike = Decimal(row[15]) == strike
        except InvalidOperation:
            continue
        if same_strike and _row_expiry(row) == expiry:
            matches.append(row)
    if rows_seen == 0 or candidates == 0:
        raise SymbolError("MASTER_UNREADABLE")
    if not matches:
        raise SymbolError("NOT_FOUND")
    if len(matches) > 1:
        raise SymbolError("AMBIGUOUS")
    row = matches[0]
    ticker = row[9]
    if not (ticker.startswith("NSE:NIFTY") and ticker.endswith(f"{strike}{option_type}")) or row[1] != _expected_details(expiry, strike, option_type):
        raise SymbolError("INCONSISTENT")
    try:
        lot, tick = int(row[3]), Decimal(row[4])
    except (ValueError, InvalidOperation):
        raise SymbolError("INCONSISTENT")
    if lot <= 0 or not tick.is_finite() or tick <= 0:
        raise SymbolError("INCONSISTENT")
    return ResolvedOption(ticker, expiry, strike, option_type, lot, tick, row[7])
