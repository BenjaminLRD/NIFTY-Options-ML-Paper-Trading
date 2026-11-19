"""INVENTED test data for the Phase 2 canonical layer. Nothing here was observed in any market: every price, size, date and token is made up, every row
carries "synthetic": true, and the file names start with SYNTHETIC_. The dates are in 2031 so they cannot be mistaken for recorded sessions.

Run `python -m tests.historical_fixtures tests/fixtures/historical_synthetic` to regenerate the committed fixture set (a test checks it is current).
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from niftypaper.historical import manifest as mf

IST = timezone(timedelta(hours=5, minutes=30))
AS_OF = datetime(2031, 3, 10, 12, 0, tzinfo=IST)           # the ingest cut-off used by the tests
EXPIRY = "2031-03-27"
CE = "NIFTY|2031-03-27|25000.00|CE"
PE = "NIFTY|2031-03-27|24000.00|PE"
CLOCK_START = datetime(2031, 3, 3, 10, 16, tzinfo=IST)


def quote(**over):
    row = {"record_kind": "QUOTE_SNAPSHOT", "synthetic": True, "underlying": "NIFTY", "expiry": EXPIRY, "strike": "25000", "option_type": "CE",
           "instrument_token": "T1", "observed_at": "2031-03-03T10:15:00+05:30", "available_at": "2031-03-03T10:15:00+05:30", "time_semantics": "QUOTE_TIME",
           "bid": "100.00", "ask": "100.20", "bid_qty": 650, "ask_qty": 650, "lot_size": 65, "tick_size": "0.05"}
    row.update(over)
    return row


def eod(**over):
    row = {"record_kind": "EOD_CONTRACT_DAY", "synthetic": True, "underlying": "NIFTY", "expiry": EXPIRY, "strike": "25000", "option_type": "CE",
           "instrument_token": "T1", "trading_date": "2031-03-03", "open": "98.00", "high": "104.00", "low": "96.50", "close": "101.00",
           "last_price": "100.90", "previous_close": "97.00", "settlement_price": "101.00", "volume": 1200, "volume_unit": "CONTRACTS", "transactions": 340,
           "open_interest": 5000, "lot_size": 65}
    row.update(over)
    return row


def bar(**over):
    row = {"record_kind": "INTRADAY_BAR", "synthetic": True, "underlying": "NIFTY", "expiry": EXPIRY, "strike": "25000", "option_type": "CE",
           "interval": "5m", "bar_start": "2031-03-03T10:10:00+05:30", "bar_end": "2031-03-03T10:15:00+05:30", "timestamp_convention": "START",
           "available_at": "2031-03-03T10:15:00+05:30", "open": "99.00", "high": "101.00", "low": "98.50", "close": "100.50", "volume": 800}
    row.update(over)
    return row


def index(**over):
    row = {"record_kind": "UNDERLYING_EOD", "synthetic": True, "underlying": "NIFTY", "trading_date": "2031-03-03", "open": "24900.00", "high": "25100.00",
           "low": "24850.00", "close": "25000.00"}
    row.update(over)
    return row


def write_rows(directory, name, rows):
    """Write JSON lines as bytes (so Windows does not rewrite the newlines and the hashes stay the same everywhere)."""
    data = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows).encode("utf-8")
    (Path(directory) / name).write_bytes(data)


def write_manifest(directory, files, origin_class="SYNTHETIC_FIXTURE", source_id="synthetic_fixture"):
    entries = mf.describe(Path(directory), files, source_id=source_id, origin_class=origin_class, origin="invented test data, not market data")
    mf.write_manifest(Path(directory) / mf.MANIFEST_NAME, entries)
    return entries


# One row per rejection reason the committed fixture set demonstrates (name -> (builder, overrides, expected reason)).
INVALID_QUOTES = {
    "crossed": (quote, {"bid": "100.30", "ask": "100.20"}, "CROSSED_QUOTE"),
    "off_tick": (quote, {"bid": "100.02"}, "OFF_TICK"),
    "naive_time": (quote, {"observed_at": "2031-03-03T10:15:00", "available_at": "2031-03-03T10:15:00"}, "INVALID_TIMEZONE"),
    "utc_time": (quote, {"observed_at": "2031-03-03T04:45:00+00:00", "available_at": "2031-03-03T04:45:00+00:00"}, "INVALID_TIMEZONE"),
    "missing_ask": (quote, {"ask": None}, "MISSING_REQUIRED_FIELD"),
    "zero_bid": (quote, {"bid": "0"}, "INVALID_PRICE"),
    "negative_bid": (quote, {"bid": "-1.00"}, "INVALID_PRICE"),
    "float_price": (quote, {"bid": 100.0}, "INVALID_NUMBER"),
    "available_before_observed": (quote, {"available_at": "2031-03-03T10:14:59+05:30"}, "INCONSISTENT_TIMES"),
    "no_available_at": (quote, {"available_at": None}, "MISSING_REQUIRED_FIELD"),
    "future_observation": (quote, {"observed_at": "2031-03-11T10:15:00+05:30", "available_at": "2031-03-11T10:15:00+05:30"}, "FUTURE_OBSERVATION"),
    "no_lot_size": (quote, {"lot_size": None}, "MISSING_REQUIRED_FIELD"),
    "zero_tick": (quote, {"tick_size": "0"}, "INVALID_SPEC"),
    "bad_option_type": (quote, {"option_type": "XX"}, "INVALID_SPEC"),
    "bad_date": (quote, {"expiry": "2031-02-30"}, "INVALID_DATE"),
    "expired_contract": (quote, {"expiry": "2031-03-02"}, "INVALID_DATE"),
    "no_time_semantics": (quote, {"time_semantics": None}, "MISSING_REQUIRED_FIELD"),
}


def fixture_set(directory):
    """Write the committed demonstration set into `directory` and return its manifest entries."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    later = "2031-03-03T10:15:30+05:30"
    write_rows(directory, "SYNTHETIC_quotes.jsonl", [
        quote(),
        quote(observed_at=later, available_at=later, bid="100.40", ask="100.60", instrument_token="T1"),
        quote(strike="24000", option_type="PE", instrument_token="T1", bid="60.00", ask="60.25"),                       # a reused token on a different contract
        quote(observed_at="2031-03-03T10:16:00+05:30", available_at="2031-03-03T10:16:00+05:30", time_semantics="RECEIVE_TIME"),   # not a quote time
        quote(observed_at="2031-03-03T10:17:00+05:30", available_at="2031-03-03T10:17:00+05:30", bid_qty=None, ask_qty=None),     # sizes absent
        quote(), quote(),                                                                                              # an identical duplicate pair of the first row
    ])
    write_rows(directory, "SYNTHETIC_quotes_invalid.jsonl", [builder(**over) for builder, over, _ in INVALID_QUOTES.values()])
    write_rows(directory, "SYNTHETIC_quotes_conflict.jsonl", [quote(bid="90.00", ask="90.20", observed_at="2031-03-03T11:00:00+05:30", available_at="2031-03-03T11:00:00+05:30"),
                                                             quote(bid="91.00", ask="91.20", observed_at="2031-03-03T11:00:00+05:30", available_at="2031-03-03T11:00:00+05:30")])
    write_rows(directory, "SYNTHETIC_eod.jsonl", [eod(), eod(trading_date="2031-03-04", close="0", last_price="0", volume=0, transactions=0, high="0", low="0", open="0"),
                                                  eod(strike="24000", option_type="PE", instrument_token="T1"), eod(trading_date="2031-03-12")])
    write_rows(directory, "SYNTHETIC_bars.jsonl", [bar(), bar(bar_start="2031-03-03T10:15:00+05:30", bar_end="2031-03-03T10:20:00+05:30",
                                                       available_at="2031-03-03T10:20:00+05:30", timestamp_convention="UNKNOWN")])
    write_rows(directory, "SYNTHETIC_index.jsonl", [index(), index(trading_date="2031-03-04", close="25100.00", high="25150.00")])
    return write_manifest(directory, {"SYNTHETIC_quotes.jsonl": "QUOTE_SNAPSHOT", "SYNTHETIC_quotes_invalid.jsonl": "QUOTE_SNAPSHOT",
                                      "SYNTHETIC_quotes_conflict.jsonl": "QUOTE_SNAPSHOT", "SYNTHETIC_eod.jsonl": "EOD_CONTRACT_DAY",
                                      "SYNTHETIC_bars.jsonl": "INTRADAY_BAR", "SYNTHETIC_index.jsonl": "UNDERLYING_EOD"})


if __name__ == "__main__":
    fixture_set(sys.argv[1])
