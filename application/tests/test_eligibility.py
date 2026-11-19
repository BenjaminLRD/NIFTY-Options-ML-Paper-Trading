"""Tests for the fail-closed execution-eligibility boundary.

Every "prohibited" test asserts the fill was REFUSED: the check returns eligible=False with a specific reason AND
`require_fillable` raises, so no ExecutableQuote (and therefore no fill price) can exist. A warning would not satisfy these tests.
"""
import copy
import unittest
import warnings
from unittest import mock
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal

from niftypaper import (EligibilityPolicy, ExecutableQuote, IneligibleQuoteError, Quote, QuoteType, Reason, Side,
                        check_fill_eligibility, fill_price, require_fillable)

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 1, 5, 9, 30, 0, tzinfo=IST)
POLICY = EligibilityPolicy(max_age=timedelta(seconds=5), max_future_skew=timedelta(seconds=1), tick_size=Decimal("0.05"))


def make(**over):
    base = dict(symbol="TESTSYM", quote_type=QuoteType.LIVE_QUOTE, bid=Decimal("100.00"), ask=Decimal("100.10"),
                bid_qty=130, ask_qty=65, ts=NOW - timedelta(seconds=2), source="synthetic-test-feed")
    base.update(over)
    return Quote(**base)


class NoOffset(tzinfo):
    """A tzinfo that reports no UTC offset, so the datetime is effectively naive."""

    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return "none"


class Base(unittest.TestCase):
    def assertRefused(self, quote, *reasons, now=NOW, policy=POLICY):
        """The prohibited fill must be refused in every way it could be obtained."""
        result = check_fill_eligibility(quote, now=now, policy=policy)
        self.assertIs(result.eligible, False)
        for reason in reasons:
            self.assertIn(reason, result.reasons)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with self.assertRaises(IneligibleQuoteError) as ctx:
                require_fillable(quote, now=now, policy=policy)
        self.assertEqual(ctx.exception.result.reasons, result.reasons)
        self.assertEqual(caught, [])                    # refusal is an exception, not a warning
        return result


class TestPermitted(Base):
    def test_fresh_live_quote_is_eligible_and_fills_at_the_touch(self):
        quote = make()
        result = check_fill_eligibility(quote, now=NOW, policy=POLICY)
        self.assertIs(result.eligible, True)
        self.assertEqual(result.reasons, ())
        executable = require_fillable(quote, now=NOW, policy=POLICY)
        self.assertIsInstance(executable, ExecutableQuote)
        self.assertEqual(fill_price(executable, Side.BUY), Decimal("100.10"))
        self.assertEqual(fill_price(executable, Side.SELL), Decimal("100.00"))

    def test_int_and_float_prices_on_the_tick_are_accepted(self):
        self.assertIs(check_fill_eligibility(make(bid=100, ask=101), now=NOW, policy=POLICY).eligible, True)
        self.assertIs(check_fill_eligibility(make(bid=100.05, ask=100.15), now=NOW, policy=POLICY).eligible, True)

    def test_age_equal_to_the_limit_is_accepted_one_microsecond_more_is_stale(self):
        edge = make(ts=NOW - timedelta(seconds=5))
        self.assertIs(check_fill_eligibility(edge, now=NOW, policy=POLICY).eligible, True)
        self.assertRefused(make(ts=NOW - timedelta(seconds=5, microseconds=1)), Reason.STALE)

    def test_small_future_skew_is_tolerated_larger_is_not(self):
        self.assertIs(check_fill_eligibility(make(ts=NOW + timedelta(seconds=1)), now=NOW, policy=POLICY).eligible, True)
        self.assertRefused(make(ts=NOW + timedelta(seconds=1, microseconds=1)), Reason.FUTURE_TIMESTAMP)

    def test_timestamp_in_another_timezone_is_compared_in_absolute_time(self):
        utc_ts = (NOW - timedelta(seconds=2)).astimezone(timezone.utc)
        self.assertIs(check_fill_eligibility(make(ts=utc_ts), now=NOW, policy=POLICY).eligible, True)


class TestProhibitedTypes(Base):
    def test_every_non_live_quote_type_is_refused(self):
        for kind in (QuoteType.EOD_SETTLEMENT, QuoteType.LAST_TRADE, QuoteType.DELAYED_QUOTE, QuoteType.UNKNOWN):
            with self.subTest(kind=kind):
                self.assertRefused(make(quote_type=kind), Reason.QUOTE_TYPE_NOT_EXECUTABLE)

    def test_eod_settlement_with_perfect_bid_ask_and_fresh_timestamp_is_still_refused(self):
        eod = make(quote_type=QuoteType.EOD_SETTLEMENT, bid=Decimal("100.00"), ask=Decimal("100.05"), ts=NOW)
        self.assertRefused(eod, Reason.QUOTE_TYPE_NOT_EXECUTABLE)

    def test_type_given_as_text_none_or_wrong_object_is_refused(self):
        for bad in ("LIVE_QUOTE", "live_quote", None, 1, object()):
            with self.subTest(bad=bad):
                self.assertRefused(make(quote_type=bad), Reason.BAD_QUOTE_TYPE)

    def test_inputs_that_are_not_quotes_are_refused(self):
        as_dict = dict(symbol="X", quote_type="LIVE_QUOTE", bid=1, ask=2, bid_qty=1, ask_qty=1, ts=NOW, source="s")
        for bad in (None, as_dict, "100.05", 100.05, Decimal("100.05"), [], (100, 101), object()):
            with self.subTest(bad=repr(bad)[:30]):
                self.assertRefused(bad, Reason.NOT_A_QUOTE)

    def test_fill_price_cannot_be_obtained_from_a_prohibited_quote(self):
        for bad in (make(quote_type=QuoteType.EOD_SETTLEMENT), make(quote_type=QuoteType.LAST_TRADE), make()):
            with self.assertRaises(TypeError):
                fill_price(bad, Side.BUY)               # a Quote (even a good one) is not an ExecutableQuote


class TestMissingMetadata(Base):
    def test_missing_or_blank_symbol_and_source(self):
        for bad in (None, "", "   ", 5):
            with self.subTest(symbol=bad):
                self.assertRefused(make(symbol=bad), Reason.MISSING_SYMBOL)
            with self.subTest(source=bad):
                self.assertRefused(make(source=bad), Reason.MISSING_SOURCE)

    def test_missing_sides(self):
        self.assertRefused(make(bid=None), Reason.MISSING_BID)
        self.assertRefused(make(ask=None), Reason.MISSING_ASK)
        self.assertRefused(make(bid=None, ask=None), Reason.MISSING_BID, Reason.MISSING_ASK)

    def test_missing_or_invalid_sizes(self):
        self.assertRefused(make(bid_qty=None), Reason.MISSING_BID_SIZE)
        self.assertRefused(make(ask_qty=None), Reason.MISSING_ASK_SIZE)
        self.assertRefused(make(bid_qty=0), Reason.NON_POSITIVE_BID_SIZE)
        self.assertRefused(make(ask_qty=-65), Reason.NON_POSITIVE_ASK_SIZE)
        for bad in (True, 1.5, "65", Decimal("65")):
            with self.subTest(size=bad):
                self.assertRefused(make(bid_qty=bad), Reason.INVALID_BID_SIZE)
                self.assertRefused(make(ask_qty=bad), Reason.INVALID_ASK_SIZE)


class TestPriceValidity(Base):
    def test_invalid_prices(self):
        for bad in (float("nan"), float("inf"), float("-inf"), Decimal("NaN"), Decimal("Infinity"), "100.00", True, [1], object()):
            with self.subTest(bad=repr(bad)):
                self.assertRefused(make(bid=bad), Reason.INVALID_BID)
                self.assertRefused(make(ask=bad), Reason.INVALID_ASK)

    def test_zero_and_negative_prices(self):
        self.assertRefused(make(bid=0), Reason.NON_POSITIVE_BID)
        self.assertRefused(make(ask=Decimal("-0.05")), Reason.NON_POSITIVE_ASK)

    def test_off_tick_prices(self):
        self.assertRefused(make(bid=Decimal("100.02")), Reason.OFF_TICK_BID)
        self.assertRefused(make(ask=100.07), Reason.OFF_TICK_ASK)

    def test_crossed_and_locked_markets(self):
        self.assertRefused(make(bid=Decimal("100.20"), ask=Decimal("100.10")), Reason.CROSSED)
        self.assertRefused(make(bid=Decimal("100.10"), ask=Decimal("100.10")), Reason.LOCKED)


class TestTimestamps(Base):
    def test_missing_timestamp(self):
        self.assertRefused(make(ts=None), Reason.MISSING_TIMESTAMP)

    def test_non_datetime_timestamps(self):
        for bad in ("2026-01-05T09:29:58+05:30", 1767585598, 1767585598.0, NOW.date(), [NOW]):
            with self.subTest(bad=repr(bad)[:30]):
                self.assertRefused(make(ts=bad), Reason.INVALID_TIMESTAMP)

    def test_naive_timestamps(self):
        self.assertRefused(make(ts=datetime(2026, 1, 5, 9, 29, 58)), Reason.NAIVE_TIMESTAMP)
        self.assertRefused(make(ts=datetime(2026, 1, 5, 9, 29, 58, tzinfo=NoOffset())), Reason.NAIVE_TIMESTAMP)

    def test_stale_quotes(self):
        self.assertRefused(make(ts=NOW - timedelta(seconds=6)), Reason.STALE)
        self.assertRefused(make(ts=datetime(1970, 1, 1, tzinfo=timezone.utc)), Reason.STALE)
        self.assertRefused(make(ts=NOW - timedelta(days=1)), Reason.STALE)

    def test_missing_or_naive_clock_is_refused(self):
        self.assertRefused(make(), Reason.MISSING_NOW, now=None)
        self.assertRefused(make(), Reason.NAIVE_NOW, now=datetime(2026, 1, 5, 9, 30, 0))
        self.assertRefused(make(), Reason.NAIVE_NOW, now="2026-01-05T09:30:00+05:30")

    def test_missing_or_bad_policy_is_refused(self):
        for bad in (None, {"max_age": 5}, object()):
            with self.subTest(bad=repr(bad)[:20]):
                self.assertRefused(make(), Reason.POLICY_INVALID, policy=bad)

    def test_all_reasons_are_reported_not_just_the_first(self):
        result = self.assertRefused(make(quote_type=QuoteType.EOD_SETTLEMENT, ts=None, bid=None, bid_qty=0),
                                    Reason.QUOTE_TYPE_NOT_EXECUTABLE, Reason.MISSING_TIMESTAMP, Reason.MISSING_BID, Reason.NON_POSITIVE_BID_SIZE)
        self.assertGreaterEqual(len(result.reasons), 4)


class TestBypassAttempts(Base):
    def test_changing_the_type_after_construction_is_detected(self):
        eod = make(quote_type=QuoteType.EOD_SETTLEMENT)
        object.__setattr__(eod, "quote_type", QuoteType.LIVE_QUOTE)         # forge a live quote
        self.assertRefused(eod, Reason.TAMPERED)

    def test_changing_the_timestamp_or_price_after_construction_is_detected(self):
        stale = make(ts=NOW - timedelta(minutes=10))
        object.__setattr__(stale, "ts", NOW)
        self.assertRefused(stale, Reason.TAMPERED)
        crossed = make(bid=Decimal("101.00"), ask=Decimal("100.10"))
        object.__setattr__(crossed, "bid", Decimal("99.00"))
        self.assertRefused(crossed, Reason.TAMPERED)

    def test_quotes_are_frozen(self):
        quote = make()
        with self.assertRaises(Exception):
            quote.quote_type = QuoteType.EOD_SETTLEMENT
        with self.assertRaises(Exception):
            quote.digest = "x"

    def test_a_subclass_that_lies_about_its_type_is_refused(self):
        class LyingQuote(Quote):
            def __getattribute__(self, name):
                if name == "quote_type":
                    return QuoteType.LIVE_QUOTE
                return super().__getattribute__(name)
        sub = LyingQuote(symbol="X", quote_type=QuoteType.EOD_SETTLEMENT, bid=Decimal("100.00"), ask=Decimal("100.10"),
                         bid_qty=1, ask_qty=1, ts=NOW, source="s")
        self.assertIs(sub.quote_type, QuoteType.LIVE_QUOTE)                  # the lie works on attribute access ...
        self.assertRefused(sub, Reason.NOT_A_QUOTE)                          # ... and is still refused (exact-type check)

    def test_a_forged_digest_string_is_refused(self):
        eod = make(quote_type=QuoteType.EOD_SETTLEMENT)
        object.__setattr__(eod, "digest", make().digest)                     # digest of a different (live) quote
        self.assertRefused(eod, Reason.TAMPERED)
        object.__setattr__(eod, "digest", None)
        self.assertRefused(eod, Reason.TAMPERED)

    def test_objects_that_misbehave_fail_closed(self):
        class Exploding:
            def __getattr__(self, name):
                raise RuntimeError("boom")

        class Unprintable:
            def __repr__(self):
                raise RuntimeError("cannot represent")
        self.assertRefused(Exploding(), Reason.NOT_A_QUOTE)
        self.assertRefused(make(bid=Unprintable()), Reason.INVALID_BID)       # constructing it must not crash, checking it must refuse

    def test_an_internal_error_during_checking_is_a_refusal(self):
        with mock.patch("niftypaper.eligibility._check_price", side_effect=RuntimeError("injected")):
            result = check_fill_eligibility(make(), now=NOW, policy=POLICY)
            self.assertIs(result.eligible, False)
            self.assertIn(Reason.INTERNAL_ERROR, result.reasons)
            with self.assertRaises(IneligibleQuoteError):
                require_fillable(make(), now=NOW, policy=POLICY)

    def test_executable_quote_cannot_be_built_directly(self):
        with self.assertRaises(TypeError):
            ExecutableQuote(object(), "X", Decimal("1"), Decimal("2"), 1, 1, NOW, "s")
        with self.assertRaises(TypeError):
            ExecutableQuote(None, "X", Decimal("1"), Decimal("2"), 1, 1, NOW, "s")
        with self.assertRaises(TypeError):
            ExecutableQuote("token", "X", Decimal("1"), Decimal("2"), 1, 1, NOW, "s")

    def test_executable_quote_is_immutable_and_independent_of_the_source_quote(self):
        quote = make()
        executable = require_fillable(quote, now=NOW, policy=POLICY)
        with self.assertRaises(AttributeError):
            executable.ask = Decimal("1.00")
        with self.assertRaises(AttributeError):
            del executable.bid
        object.__setattr__(quote, "ask", Decimal("1.00"))                    # later edit of the original changes nothing
        self.assertEqual(fill_price(executable, Side.BUY), Decimal("100.10"))
        self.assertRefused(quote, Reason.TAMPERED)

    def test_fill_price_rejects_wrong_arguments(self):
        executable = require_fillable(make(), now=NOW, policy=POLICY)
        for bad in (None, "BUY", 1, "ask"):
            with self.subTest(side=bad):
                with self.assertRaises(TypeError):
                    fill_price(executable, bad)
        for bad in (None, {"ask": Decimal("100.10")}, copy.copy(make())):
            with self.assertRaises(TypeError):
                fill_price(bad, Side.BUY)

    def test_result_cannot_be_used_as_a_boolean(self):
        result = check_fill_eligibility(make(quote_type=QuoteType.EOD_SETTLEMENT), now=NOW, policy=POLICY)
        with self.assertRaises(TypeError):
            bool(result)
        with self.assertRaises(TypeError):
            if result:                                                       # the classic "always true" mistake
                pass


class TestPolicy(unittest.TestCase):
    def test_policy_requires_every_limit_and_rejects_nonsense(self):
        with self.assertRaises(TypeError):
            EligibilityPolicy()                                              # no silent defaults
        with self.assertRaises(TypeError):
            EligibilityPolicy(timedelta(seconds=5), timedelta(0), Decimal("0.05"))   # keyword-only
        for kwargs in (dict(max_age=timedelta(0), max_future_skew=timedelta(0), tick_size=Decimal("0.05")),
                       dict(max_age=timedelta(seconds=-1), max_future_skew=timedelta(0), tick_size=Decimal("0.05")),
                       dict(max_age=5, max_future_skew=timedelta(0), tick_size=Decimal("0.05")),
                       dict(max_age=timedelta(seconds=5), max_future_skew=timedelta(seconds=-1), tick_size=Decimal("0.05")),
                       dict(max_age=timedelta(seconds=5), max_future_skew=timedelta(0), tick_size=Decimal("0")),
                       dict(max_age=timedelta(seconds=5), max_future_skew=timedelta(0), tick_size=0.05),
                       dict(max_age=timedelta(seconds=5), max_future_skew=timedelta(0), tick_size=Decimal("NaN"))):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    EligibilityPolicy(**kwargs)

    def test_policy_has_no_way_to_allow_a_prohibited_type(self):
        fields = set(EligibilityPolicy.__dataclass_fields__)
        self.assertEqual(fields, {"max_age", "max_future_skew", "tick_size"})
        with self.assertRaises(TypeError):
            EligibilityPolicy(max_age=timedelta(seconds=5), max_future_skew=timedelta(0), tick_size=Decimal("0.05"),
                              allowed_types={QuoteType.EOD_SETTLEMENT})

    def test_policy_is_immutable(self):
        with self.assertRaises(Exception):
            POLICY.max_age = timedelta(days=365)


if __name__ == "__main__":
    unittest.main()
