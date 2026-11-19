"""INVENTED scenario data for the strategy tests (2031 dates, every row marked synthetic; nothing observed in any market). Fixture folders are always built in
temporary directories, never inside the committed fixture folder."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from niftypaper.historical import ingest as ingest_module
from niftypaper.historical import manifest as mf
from niftypaper.historical.gate import PointInTimeView, ReplayClock
from niftypaper.historical.replay import ReplayConfig
from niftypaper.strategy.trend import IndexSmaTrend, TrendConfig
from tests import historical_fixtures as fx
from tests.historical_fixtures import CE, PE

IST = timezone(timedelta(hours=5, minutes=30))
AS_OF = datetime(2031, 3, 13, 12, 0, tzinfo=IST)
START, END = datetime(2031, 3, 10, 9, 0, tzinfo=IST), datetime(2031, 3, 11, 15, 30, tzinfo=IST)
T1, T2 = datetime(2031, 3, 10, 10, 15, 2, tzinfo=IST), datetime(2031, 3, 11, 10, 15, 2, tzinfo=IST)

RISING = [("2031-03-03", "25000.00"), ("2031-03-04", "25050.00"), ("2031-03-05", "25100.00"), ("2031-03-06", "25150.00"), ("2031-03-07", "25200.00")]
FALLING_AFTER = [("2031-03-10", "24800.00")]                          # the close of the day of the first decision: unavailable at that decision
FUTURE = [("2031-03-11", "30000.00"), ("2031-03-12", "1000.00")]      # dated after both decisions' data: must never matter
FLAT = [(d, "25000.00") for d, _ in RISING]


def at(day, h, m, s=0):
    return datetime(2031, 3, day, h, m, s, tzinfo=IST)


def q(side, day, h, m, bid, ask, s=0, **over):
    strike, kind = ("25000", "CE") if side == "CE" else ("24000", "PE")
    stamp = at(day, h, m, s).isoformat()
    return fx.quote(strike=strike, option_type=kind, observed_at=stamp, available_at=stamp, bid=bid, ask=ask, **over)


BASE_QUOTES = [q("CE", 10, 10, 15, "100.00", "100.20"), q("PE", 10, 10, 15, "60.00", "60.25"),
               q("CE", 11, 10, 15, "110.00", "110.20"), q("PE", 11, 10, 15, "55.00", "55.25")]


def build(folder, closes=RISING + FALLING_AFTER, quotes=BASE_QUOTES, index_flags=None):
    """Write a manifest-described fixture folder (index closes and quotes) and return it."""
    folder = Path(folder)
    folder.mkdir(parents=True)
    fx.write_rows(folder, "SYNTHETIC_index.jsonl", [fx.index(trading_date=d, close=c, open=c, high=c, low=c, **(index_flags or {})) for d, c in closes])
    fx.write_rows(folder, "SYNTHETIC_quotes.jsonl", quotes)
    fx.write_manifest(folder, {"SYNTHETIC_index.jsonl": "UNDERLYING_EOD", "SYNTHETIC_quotes.jsonl": "QUOTE_SNAPSHOT"})
    return folder


def records_of(folder):
    entries = mf.load_manifest(Path(folder) / mf.MANIFEST_NAME)
    return ingest_module.ingest(Path(folder), entries, as_of=AS_OF).records


def view_at(folder, now):
    return PointInTimeView(records_of(folder), ReplayClock(now))


def strategy(**over):
    return IndexSmaTrend(TrendConfig(call_contract=CE, put_contract=PE, **over))


def replay_config(fixtures, state_dir, **over):
    base = dict(fixture_dir=fixtures, state_dir=state_dir, as_of=AS_OF, start=START, end=END, permitted=True)
    base.update(over)
    return ReplayConfig(**base)
