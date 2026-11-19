"""The point-in-time gate. Four different times are kept apart and never substituted for each other:

observed_at   when the market was observed (None for day-level records)
available_at  the earliest instant a replay may use the record
retrieved_at  when the file was obtained (provenance only)
the replay clock  the instant the replay is "at"

A record is visible only when its available_at is known and not after the replay clock. Records with unknown availability, or whose times contradict
each other, are invisible. Replay-facing code gets records only through a PointInTimeView; it has no method that lists everything.
"""
from datetime import datetime

from . import records as rec


class ClockError(ValueError):
    pass


class ReplayClock:
    """A replay clock that only moves forward. Callable, so it can serve as the paper engine's clock."""

    def __init__(self, start):
        self._now = self._check(start)

    @staticmethod
    def _check(moment):
        if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
            raise ClockError("the replay clock needs a timezone-aware datetime")
        return moment

    def __call__(self):
        return self._now

    @property
    def now(self):
        return self._now

    def advance_to(self, moment):
        moment = self._check(moment)
        if moment < self._now:
            raise ClockError("the replay clock cannot move backwards")
        self._now = moment


def usable_at(record, now):
    """True only if `record` may inform a decision at replay time `now`. Re-checks the invariants even though ingestion enforced them, so a record
    built by hand (or altered in memory) still cannot leak the future."""
    if not isinstance(record, rec.Record) or record.available_at is None or record.available_at.tzinfo is None:
        return False
    if record.available_at > now:
        return False
    if record.observed_at is not None and (record.observed_at > now or record.available_at < record.observed_at):
        return False
    if record.kind in (rec.EOD, rec.INDEX):                         # a day-level record is never usable before its trading date has ended
        day = record.get("trading_date")
        if day is None or record.observed_at is not None or record.available_at < rec.day_end(day):
            return False
    if record.kind == rec.BAR and (record.get("bar_end") is None or record.available_at < record.get("bar_end") or record.get("bar_end") > now):
        return False
    return True


class PointInTimeView:
    def __init__(self, records, clock):
        if not isinstance(clock, ReplayClock):
            raise TypeError("a PointInTimeView needs a ReplayClock")
        self._records, self._clock = tuple(records), clock

    @property
    def now(self):
        return self._clock.now

    @property
    def clock(self):
        return self._clock

    def visible(self, kind=None):
        return tuple(r for r in self._records if (kind is None or r.kind == kind) and usable_at(r, self._clock.now))

    def latest(self, kind, key):
        """The most recent usable record of `kind` for `key` (a contract key, or an underlying for UNDERLYING_EOD), or None. Latest means the
        greatest available_at, then observed_at; ties between different records cannot occur because conflicts are excluded at ingestion."""
        found = [r for r in self.visible(kind) if r.key == key]
        return max(found, key=lambda r: (r.available_at, r.observed_at or r.available_at, r.time_key), default=None)

    def latest_quote(self, contract_key):
        return self.latest(rec.QUOTE, contract_key)

    def stale_by(self, record):
        """How old the record's observation is at the replay clock (a timedelta), or None for day-level records."""
        return None if record.observed_at is None else self._clock.now - record.observed_at


__all__ = ["ClockError", "PointInTimeView", "ReplayClock", "usable_at"]
