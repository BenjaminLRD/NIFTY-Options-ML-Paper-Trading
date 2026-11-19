"""Phase 2: the canonical historical-data layer, on INVENTED fixtures only (tests/historical_fixtures.py). No real data is read or downloaded."""
import contextlib
import dataclasses
import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from niftypaper.historical import bridge, gate, ingest, manifest as mf, records as rec
from niftypaper.paper import attest
from niftypaper.paper.engine import (ORIGIN_HISTORICAL, ORIGIN_PROVIDER, ORIGIN_SYNTHETIC, MarketView, PaperEngine, RiskLimits, TradeRequest, attestation_fields)
from tests import ROOT
from tests import historical_fixtures as fx
from tests.historical_fixtures import AS_OF, CE, IST, PE, bar, eod, index, quote

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "historical_synthetic"


def reattest(view, **change):
    """A view with `change` applied and a freshly minted valid attestation: simulates a bridge that attested something bad."""
    altered = dataclasses.replace(view, **change)
    return dataclasses.replace(altered, attestation=attest.issue(attestation_fields(altered), "test"))


def build(kind, row, as_of=AS_OF, retrieved_at=None):
    return rec.build(kind, row, as_of=as_of, retrieved_at=retrieved_at)


def reject_code(kind, row, **kw):
    try:
        build(kind, row, **kw)
    except rec.Reject as error:
        return error.code
    return None


class Workspace(unittest.TestCase):
    """A temporary raw folder with helpers to write fixtures, a manifest and ingest them."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def put(self, name, rows, kind=None):
        fx.write_rows(self.root, name, rows)
        return {name: kind or rows[0]["record_kind"]}

    def run_ingest(self, files, origin_class="SYNTHETIC_FIXTURE", source_id="synthetic_fixture", as_of=AS_OF):
        if not (self.root / mf.MANIFEST_NAME).exists():
            fx.write_manifest(self.root, files, origin_class, source_id)
        return ingest.ingest(self.root, mf.load_manifest(self.root / mf.MANIFEST_NAME), as_of=as_of)


class TestValidation(unittest.TestCase):
    def test_every_invalid_quote_is_rejected_with_its_reason(self):
        for name, (builder, overrides, expected) in fx.INVALID_QUOTES.items():
            with self.subTest(name):
                self.assertEqual(reject_code(rec.QUOTE, builder(**overrides)), expected)

    def test_a_valid_quote_snapshot_is_fill_eligible_and_typed(self):
        built = build(rec.QUOTE, quote())
        self.assertTrue(built.fill_eligible)
        self.assertEqual((built.values["bid"], built.values["ask"], built.values["lot_size"], built.values["tick_size"]),
                         (Decimal("100.00"), Decimal("100.20"), 65, Decimal("0.05")))
        self.assertEqual(built.key, CE)

    def test_only_a_documented_quote_time_with_sizes_makes_a_quote_fill_eligible(self):
        for semantics in ("LAST_TRADE_TIME", "RECEIVE_TIME", "DAY_MARKER", "UNKNOWN"):
            built = build(rec.QUOTE, quote(time_semantics=semantics))
            self.assertFalse(built.fill_eligible, semantics)
            self.assertIn("TIME_NOT_A_QUOTE_TIME", built.ineligible_reasons)
        self.assertIn("SIZES_ABSENT", build(rec.QUOTE, quote(bid_qty=None)).ineligible_reasons)
        self.assertEqual(reject_code(rec.QUOTE, quote(time_semantics="MID_PRICE")), "INVALID_SPEC")

    def test_only_quote_snapshots_can_ever_be_fill_eligible(self):
        for kind, row in ((rec.EOD, eod()), (rec.BAR, bar()), (rec.INDEX, index())):
            self.assertFalse(build(kind, row).fill_eligible, kind)

    def test_bid_and_ask_fields_are_refused_on_every_other_kind(self):
        for kind, row in ((rec.EOD, eod()), (rec.BAR, bar()), (rec.INDEX, index())):
            for name in ("bid", "ask", "bid_qty", "ask_qty", "depth"):
                with self.subTest(kind=kind, field=name):
                    self.assertEqual(reject_code(kind, {**row, name: "1"}), "FORBIDDEN_FIELD")
        self.assertEqual(reject_code(rec.BAR, bar(settlement_price="100")), "FORBIDDEN_FIELD")
        self.assertEqual(rec.FORBIDDEN[rec.EOD], tuple(json.loads((ROOT / "config" / "historical_schema_v1.json").read_text())["record_kinds"]["EOD_CONTRACT_DAY"]["forbidden_fields"]))

    def test_missing_values_stay_null_and_nothing_is_filled(self):
        built = build(rec.EOD, eod(high=None, low="", open=None, settlement_price=None, previous_close=None, volume=None, transactions=None))
        for name in ("open", "high", "low", "settlement_price", "previous_close", "volume", "transactions"):
            self.assertIsNone(built.values[name], name)
        self.assertIsNone(built.values["traded"])                       # neither volume nor transactions reported: not guessed
        self.assertIsNone(build(rec.BAR, bar(volume=None, open=None)).values["volume"])

    def test_no_trade_markers_become_null_with_a_flag_on_day_records_only(self):
        built = build(rec.EOD, eod(close="0", last_price="-", volume=0, transactions=0))
        self.assertIsNone(built.values["close"])
        self.assertIsNone(built.values["last_price"])
        self.assertIn("PRICE_ZERO_NO_TRADE", built.flags)
        self.assertIn("PRICE_DASH_NO_TRADE", built.flags)
        self.assertIs(built.values["traded"], False)
        self.assertEqual(reject_code(rec.BAR, bar(close="0")), "INVALID_PRICE")
        self.assertEqual(reject_code(rec.BAR, bar(close="-")), "INVALID_NUMBER")

    def test_a_carried_value_on_a_row_that_did_not_trade_is_flagged(self):
        built = build(rec.EOD, eod(volume=0, transactions=0))
        self.assertIn("CLOSE_CARRIED_NO_TRADE", built.flags)
        self.assertIn("LAST_CARRIED_NO_TRADE", built.flags)

    def test_impossible_ohlc_is_rejected(self):
        for kind, base in ((rec.EOD, eod), (rec.BAR, bar)):
            for over in ({"low": "105", "high": "100"}, {"close": "999"}, {"open": "1"}):
                with self.subTest(kind=kind, over=over):
                    self.assertEqual(reject_code(kind, base(**over)), "INVALID_OHLC")
        self.assertEqual(reject_code(rec.INDEX, index(high="24000.00")), "INVALID_OHLC")

    def test_identity_is_required_and_strict(self):
        for name in ("underlying", "expiry", "strike", "option_type"):
            for kind, base in ((rec.EOD, eod), (rec.BAR, bar), (rec.QUOTE, quote)):
                with self.subTest(kind=kind, field=name):
                    self.assertEqual(reject_code(kind, base(**{name: None})), "MISSING_REQUIRED_FIELD")
        self.assertEqual(reject_code(rec.QUOTE, quote(strike="25000.005")), "INVALID_SPEC")
        self.assertEqual(reject_code(rec.QUOTE, quote(strike="0")), "INVALID_SPEC")
        self.assertEqual(reject_code(rec.QUOTE, quote(underlying="nifty")), "INVALID_SPEC")
        self.assertEqual(reject_code(rec.INDEX, index(underlying=None)), "MISSING_REQUIRED_FIELD")

    def test_dates_must_be_calendar_dates_in_iso_form(self):
        for bad in ("20310303", "2031-3-3", "2031-02-30", "03/03/2031", 20310303):
            with self.subTest(bad=bad):
                self.assertEqual(reject_code(rec.EOD, eod(trading_date=bad)), "INVALID_DATE")

    def test_timestamps_need_the_ist_offset(self):
        for bad, code in (("2031-03-03T10:15:00", "INVALID_TIMEZONE"), ("2031-03-03T04:45:00Z", "INVALID_TIMEZONE"), ("2031-03-03T10:15:00-05:00", "INVALID_TIMEZONE"),
                          ("2031-03-03 10:15:00+05:30", "INVALID_TIMESTAMP"), ("not a time", "INVALID_TIMESTAMP"), (20310303, "INVALID_TIMESTAMP")):
            with self.subTest(bad=bad):
                self.assertEqual(reject_code(rec.QUOTE, quote(observed_at=bad, available_at=bad)), code)

    def test_numbers_must_be_plain_strings_or_integers(self):
        for bad in (100.5, True, "1e3", "1_0", " 100", "NaN", "inf", "0x10", "100.123456789", [100]):
            with self.subTest(bad=bad):
                self.assertEqual(reject_code(rec.QUOTE, quote(bid=bad)), "INVALID_NUMBER")
        for bad in (-1, "1.5", True, 2.0, "x"):
            with self.subTest(qty=bad):
                self.assertEqual(reject_code(rec.QUOTE, quote(bid_qty=bad)), "INVALID_NUMBER")

    def test_lot_and_tick_must_be_valid_where_present_and_present_on_quotes(self):
        self.assertEqual(reject_code(rec.EOD, eod(lot_size="0")), "INVALID_SPEC")
        self.assertEqual(reject_code(rec.EOD, eod(tick_size="-0.05")), "INVALID_SPEC")
        absent = build(rec.EOD, eod(lot_size=None))
        self.assertIn("LOT_SIZE_ABSENT", absent.flags)
        self.assertIn("TICK_SIZE_ABSENT", absent.flags)
        self.assertEqual(reject_code(rec.QUOTE, quote(tick_size=None)), "MISSING_REQUIRED_FIELD")

    def test_bars_check_interval_and_timestamp_convention(self):
        self.assertEqual(reject_code(rec.BAR, bar(bar_end="2031-03-03T10:16:00+05:30")), "INCONSISTENT_TIMES")
        self.assertEqual(reject_code(rec.BAR, bar(bar_end="2031-03-03T10:05:00+05:30")), "INCONSISTENT_TIMES")
        self.assertEqual(reject_code(rec.BAR, bar(interval="7m")), "INVALID_SPEC")
        self.assertEqual(reject_code(rec.BAR, bar(timestamp_convention="MIDDLE")), "INVALID_SPEC")
        self.assertIn("INTERVAL_TIMESTAMP_CONVENTION_UNKNOWN", build(rec.BAR, bar(timestamp_convention="UNKNOWN")).flags)
        self.assertNotIn("INTERVAL_TIMESTAMP_CONVENTION_UNKNOWN", build(rec.BAR, bar()).flags)

    def test_volume_unit_is_never_inferred(self):
        self.assertIn("VOLUME_UNIT_UNKNOWN", build(rec.EOD, eod(volume_unit="UNKNOWN")).flags)
        self.assertNotIn("VOLUME_UNIT_UNKNOWN", build(rec.EOD, eod()).flags)
        self.assertEqual(reject_code(rec.EOD, eod(volume_unit="LOTS")), "INVALID_SPEC")

    def test_the_instrument_token_is_kept_but_is_not_part_of_any_key(self):
        a, b = build(rec.QUOTE, quote(instrument_token="A")), build(rec.QUOTE, quote(instrument_token="B"))
        self.assertEqual((a.key, a.time_key), (b.key, b.time_key))
        self.assertEqual((a.values["source_instrument_id"], b.values["source_instrument_id"]), ("A", "B"))
        self.assertNotIn("A", a.key)

    def test_unknown_kind_is_rejected(self):
        self.assertEqual(reject_code("QUOTE", quote()), "UNKNOWN_RECORD_KIND")


class TestAvailability(unittest.TestCase):
    """Time correctness: observed, available, retrieved and the replay clock are different things."""

    def test_day_records_default_to_the_end_of_their_trading_date(self):
        built = build(rec.EOD, eod())
        self.assertEqual(built.available_at, datetime(2031, 3, 3, 23, 59, 59, tzinfo=IST))
        self.assertIsNone(built.observed_at)
        self.assertEqual(built.values["observation_precision"], "DAY")
        self.assertIn("AVAILABILITY_RULE_DEFAULT", built.flags)
        self.assertEqual(build(rec.INDEX, index()).available_at, built.available_at)

    def test_an_earlier_availability_claim_on_a_day_record_is_refused_not_believed(self):
        for kind, row in ((rec.EOD, eod(available_at="2031-03-03T15:45:00+05:30")), (rec.INDEX, index(available_at="2031-03-03T16:00:00+05:30"))):
            self.assertEqual(reject_code(kind, row), "INCONSISTENT_TIMES")
        later = build(rec.EOD, eod(available_at="2031-03-04T08:00:00+05:30"))
        self.assertEqual(later.available_at, datetime(2031, 3, 4, 8, 0, tzinfo=IST))
        self.assertNotIn("AVAILABILITY_RULE_DEFAULT", later.flags)

    def test_a_day_record_cannot_claim_a_time_of_day(self):
        self.assertEqual(reject_code(rec.EOD, eod(observed_at="2031-03-03T15:30:00+05:30")), "INCONSISTENT_TIMES")
        self.assertEqual(reject_code(rec.INDEX, index(observed_at="2031-03-03T15:30:00+05:30")), "INCONSISTENT_TIMES")

    def test_quotes_and_bars_never_get_a_default_availability(self):
        self.assertEqual(reject_code(rec.QUOTE, quote(available_at=None)), "MISSING_REQUIRED_FIELD")
        self.assertEqual(reject_code(rec.BAR, bar(available_at=None)), "MISSING_REQUIRED_FIELD")

    def test_future_dated_observations_are_refused_at_ingest(self):
        self.assertEqual(reject_code(rec.EOD, eod(trading_date="2031-03-11")), "FUTURE_OBSERVATION")
        self.assertEqual(reject_code(rec.INDEX, index(trading_date="2031-03-11")), "FUTURE_OBSERVATION")
        self.assertEqual(reject_code(rec.BAR, bar(bar_start="2031-03-11T10:10:00+05:30", bar_end="2031-03-11T10:15:00+05:30",
                                                  available_at="2031-03-11T10:15:00+05:30")), "FUTURE_OBSERVATION")
        retrieved = datetime(2031, 3, 3, 9, 0, tzinfo=IST)               # the file was obtained before the quote it holds was observed
        self.assertEqual(reject_code(rec.QUOTE, quote(), retrieved_at=retrieved), "FUTURE_OBSERVATION")

    def test_bar_availability_cannot_precede_the_end_of_the_bar(self):
        self.assertEqual(reject_code(rec.BAR, bar(available_at="2031-03-03T10:12:00+05:30")), "INCONSISTENT_TIMES")


class Pipeline(Workspace):
    def ingest_valid(self):
        self.put("q.jsonl", [quote(), quote(observed_at="2031-03-03T10:15:30+05:30", available_at="2031-03-03T10:15:40+05:30", bid="100.40", ask="100.60")])
        self.put("e.jsonl", [eod(), eod(trading_date="2031-03-04")])
        self.put("b.jsonl", [bar()])
        self.put("i.jsonl", [index()])
        files = {"q.jsonl": rec.QUOTE, "e.jsonl": rec.EOD, "b.jsonl": rec.BAR, "i.jsonl": rec.INDEX}
        return self.run_ingest(files)


class TestGate(Pipeline):
    def view_at(self, result, moment):
        return gate.PointInTimeView(result.records, gate.ReplayClock(moment))

    def test_a_record_becomes_visible_only_when_the_clock_reaches_available_at(self):
        result = self.ingest_valid()
        before = self.view_at(result, datetime(2031, 3, 3, 10, 15, 39, tzinfo=IST))
        self.assertEqual(before.latest_quote(CE).get("bid"), Decimal("100.00"))              # the 10:15:30 quote exists but is not yet available
        after = self.view_at(result, datetime(2031, 3, 3, 10, 15, 40, tzinfo=IST))
        self.assertEqual(after.latest_quote(CE).get("bid"), Decimal("100.40"))
        self.assertIsNone(self.view_at(result, datetime(2031, 3, 3, 10, 14, 59, tzinfo=IST)).latest_quote(CE))

    def test_a_settlement_record_is_not_visible_during_its_own_trading_day(self):
        result = self.ingest_valid()
        for moment in (datetime(2031, 3, 3, 9, 15, tzinfo=IST), datetime(2031, 3, 3, 15, 30, tzinfo=IST), datetime(2031, 3, 3, 23, 59, 58, tzinfo=IST)):
            view = self.view_at(result, moment)
            self.assertIsNone(view.latest(rec.EOD, CE), moment)
            self.assertIsNone(view.latest(rec.INDEX, "NIFTY"), moment)
        view = self.view_at(result, datetime(2031, 3, 3, 23, 59, 59, tzinfo=IST))
        self.assertIsNotNone(view.latest(rec.EOD, CE))
        self.assertIsNone(view.latest(rec.EOD, CE).observed_at)

    def test_a_future_settlement_is_invisible_whatever_the_earlier_clock_time(self):
        view = self.view_at(self.ingest_valid(), datetime(2031, 3, 4, 10, 0, tzinfo=IST))
        self.assertEqual(view.latest(rec.EOD, CE).time_key, "2031-03-03")                    # 03-04's settlement is not knowable yet

    def test_a_bar_is_visible_only_after_it_has_closed(self):
        result = self.ingest_valid()
        self.assertIsNone(self.view_at(result, datetime(2031, 3, 3, 10, 14, 59, tzinfo=IST)).latest(rec.BAR, CE))
        self.assertIsNotNone(self.view_at(result, datetime(2031, 3, 3, 10, 15, tzinfo=IST)).latest(rec.BAR, CE))

    def test_unknown_or_contradictory_times_make_a_record_invisible(self):
        result = self.ingest_valid()
        q = next(r for r in result.records if r.kind == rec.QUOTE)
        e = next(r for r in result.records if r.kind == rec.EOD)
        b = next(r for r in result.records if r.kind == rec.BAR)
        late = datetime(2031, 3, 9, tzinfo=IST)
        tampered = {
            "unknown availability": dataclasses.replace(q, available_at=None),
            "naive availability": dataclasses.replace(q, available_at=q.available_at.replace(tzinfo=None)),
            "available before observed": dataclasses.replace(q, available_at=q.observed_at - timedelta(seconds=1)),
            "observed in the future": dataclasses.replace(q, observed_at=late + timedelta(days=1), available_at=late + timedelta(days=1)),
            "day record with a time of day": dataclasses.replace(e, observed_at=e.available_at),
            "day record available intraday": dataclasses.replace(e, available_at=datetime(2031, 3, 3, 15, 45, tzinfo=IST)),
            "day record without a date": dataclasses.replace(e, values={**e.values, "trading_date": None}),
            "bar available before it ends": dataclasses.replace(b, available_at=b.values["bar_end"] - timedelta(minutes=1)),
        }
        for name, record in tampered.items():
            with self.subTest(name):
                self.assertFalse(gate.usable_at(record, late))
                self.assertEqual(gate.PointInTimeView([record], gate.ReplayClock(late)).visible(), ())
        self.assertFalse(gate.usable_at("not a record", late))

    def test_misleading_extra_time_fields_in_a_row_change_nothing(self):
        row = quote(observed_at="2031-03-03T10:15:00+05:30", available_at="2031-03-03T12:00:00+05:30", retrieved_at="2031-03-03T09:00:00+05:30",
                    received_at="2031-03-03T09:00:00+05:30", timestamp="2031-03-03T09:00:00+05:30", ts=1, tt="2031-03-03T09:00:00+05:30", published_at="2031-01-01T00:00:00+05:30")
        self.put("q.jsonl", [row])
        result = self.run_ingest({"q.jsonl": rec.QUOTE})
        self.assertEqual(len(result.records), 1)
        self.assertIsNone(result.records[0].retrieved_at)
        self.assertEqual(result.records[0].available_at, datetime(2031, 3, 3, 12, 0, tzinfo=IST))
        self.assertIsNone(self.view_at(result, datetime(2031, 3, 3, 11, 59, tzinfo=IST)).latest_quote(CE))
        self.assertIn("received_at", result.records[0].raw)                                  # the raw source line is preserved untouched

    def test_a_timezone_mistake_cannot_make_a_record_available_early(self):
        for stamp in ("2031-03-03T10:15:00Z", "2031-03-03T10:15:00+00:00", "2031-03-03T10:15:00"):
            self.assertIn(reject_code(rec.QUOTE, quote(observed_at=stamp, available_at=stamp)), ("INVALID_TIMEZONE",), stamp)
        with self.assertRaises(gate.ClockError):
            gate.ReplayClock(datetime(2031, 3, 3, 10, 15))                                   # a naive clock is refused too

    def test_the_replay_clock_only_moves_forward(self):
        clock = gate.ReplayClock(fx.CLOCK_START)
        clock.advance_to(fx.CLOCK_START + timedelta(minutes=1))
        with self.assertRaises(gate.ClockError):
            clock.advance_to(fx.CLOCK_START)
        with self.assertRaises(gate.ClockError):
            clock.advance_to(datetime(2031, 3, 4))
        self.assertEqual(clock(), clock.now)

    def test_the_view_exposes_no_way_to_list_unavailable_records(self):
        public = {n for n in dir(gate.PointInTimeView) if not n.startswith("_")}
        self.assertEqual(public, {"now", "clock", "visible", "latest", "latest_quote", "stale_by"})
        with self.assertRaises(TypeError):
            gate.PointInTimeView((), lambda: AS_OF)


class TestDuplicatesAndConflicts(Pipeline):
    def test_identical_duplicates_keep_the_first_and_are_listed(self):
        self.put("a.jsonl", [quote(), quote(), quote()])
        result = self.run_ingest({"a.jsonl": rec.QUOTE})
        self.assertEqual([r.source_row for r in result.records], [1])
        self.assertEqual([(e.source_row, e.reason, e.other_copy) for e in result.exclusions],
                         [(2, "DUPLICATE_ROW_IN_FILE", "a.jsonl#1"), (3, "DUPLICATE_ROW_IN_FILE", "a.jsonl#1")])

    def test_identical_copies_in_two_files_keep_the_one_that_sorts_first(self):
        self.put("b.jsonl", [quote()])
        self.put("a.jsonl", [quote()])
        result = self.run_ingest({"a.jsonl": rec.QUOTE, "b.jsonl": rec.QUOTE})
        self.assertEqual([r.source_file for r in result.records], ["a.jsonl"])
        self.assertEqual([(e.source_file, e.reason, e.other_copy) for e in result.exclusions], [("b.jsonl", "DUPLICATE_OF_EARLIER_COPY", "a.jsonl#1")])

    def test_conflicting_records_are_all_excluded_never_newest_wins(self):
        later = {"observed_at": "2031-03-03T10:15:00+05:30", "available_at": "2031-03-03T10:15:00+05:30"}
        self.put("a.jsonl", [quote(**later), quote(bid="50.00", ask="50.10", **later), quote(**later)])
        result = self.run_ingest({"a.jsonl": rec.QUOTE})
        self.assertEqual(result.records, ())
        self.assertEqual([(e.source_row, e.reason) for e in result.exclusions], [(1, "CONFLICTING_ROW_IN_FILE"), (2, "CONFLICTING_ROW_IN_FILE"), (3, "CONFLICTING_ROW_IN_FILE")])
        self.assertTrue(all(e.other_copy for e in result.exclusions))

    def test_conflicts_across_files_use_the_copies_reason(self):
        self.put("a.jsonl", [quote()])
        self.put("b.jsonl", [quote(bid="99.00", ask="99.10")])
        result = self.run_ingest({"a.jsonl": rec.QUOTE, "b.jsonl": rec.QUOTE})
        self.assertEqual(result.records, ())
        self.assertEqual({e.reason for e in result.exclusions}, {"CONFLICTING_COPIES"})

    def test_a_reused_instrument_token_on_different_contracts_is_not_a_duplicate(self):
        self.put("a.jsonl", [quote(instrument_token="T9"), quote(strike="24000", option_type="PE", instrument_token="T9")])
        result = self.run_ingest({"a.jsonl": rec.QUOTE})
        self.assertEqual(sorted(r.key for r in result.records), [PE, CE])
        self.assertEqual(result.exclusions, ())

    def test_the_same_contract_under_two_tokens_is_a_conflict_not_a_merge(self):
        self.put("a.jsonl", [eod(instrument_token="T1"), eod(instrument_token="T2")])
        result = self.run_ingest({"a.jsonl": rec.EOD})
        self.assertEqual(result.records, ())
        self.assertEqual({e.reason for e in result.exclusions}, {"CONFLICTING_ROW_IN_FILE"})

    def test_different_sources_are_never_merged_or_used_to_fill_each_other(self):
        self.put("a.jsonl", [quote()])
        fx.write_manifest(self.root, {"a.jsonl": rec.QUOTE}, source_id="synthetic_one")
        one = self.run_ingest({})
        (self.root / mf.MANIFEST_NAME).unlink()
        fx.write_manifest(self.root, {"a.jsonl": rec.QUOTE}, source_id="synthetic_two")
        two = self.run_ingest({})
        self.assertEqual((len(one.records), len(two.records)), (1, 1))
        both = ingest.IngestResult(*[a + b for a, b in zip((one.records, (), ()), (two.records, (), ()))])
        self.assertEqual(len({r.natural_key for r in both.records}), 2)                    # same record, different source: two natural keys

    def test_repeated_runs_and_entry_order_give_identical_results(self):
        files = {"q.jsonl": rec.QUOTE, "e.jsonl": rec.EOD}
        self.put("q.jsonl", [quote(), quote(), quote(bid="1.00", ask="1.05", observed_at="2031-03-03T10:20:00+05:30", available_at="2031-03-03T10:20:00+05:30"),
                             quote(bid="1.05", ask="1.10", observed_at="2031-03-03T10:20:00+05:30", available_at="2031-03-03T10:20:00+05:30"), quote(ask=None)])
        self.put("e.jsonl", [eod(), eod(trading_date="2031-03-04", close="0")])
        fx.write_manifest(self.root, files)
        entries = mf.load_manifest(self.root / mf.MANIFEST_NAME)
        first = ingest.ingest(self.root, entries, as_of=AS_OF)
        again = ingest.ingest(self.root, entries, as_of=AS_OF)
        reversed_ = ingest.ingest(self.root, tuple(reversed(entries)), as_of=AS_OF)
        self.assertEqual(first, again)
        self.assertEqual(first, reversed_)
        self.assertEqual(first.ledger_lines(), reversed_.ledger_lines())
        self.assertEqual(first.digest(), again.digest())
        self.assertTrue(first.ledger_lines().endswith("\n"))

    def test_every_rejected_row_has_a_ledger_entry_with_a_known_reason(self):
        self.put("a.jsonl", [quote(), {"record_kind": "QUOTE_SNAPSHOT", "synthetic": True}, quote(bid="x")])
        (self.root / "a.jsonl").write_bytes((self.root / "a.jsonl").read_bytes() + b"not json\n[1]\n\n{\"record_kind\": \"FOO\", \"synthetic\": true}\n")
        result = self.run_ingest({"a.jsonl": rec.QUOTE})
        self.assertEqual(len(result.records), 1)
        self.assertEqual(len(result.exclusions), 5)
        self.assertEqual([e.source_row for e in result.exclusions], [2, 3, 4, 5, 7])
        self.assertEqual([e.reason for e in result.exclusions], ["MISSING_REQUIRED_FIELD", "INVALID_NUMBER", "MALFORMED_RECORD", "MALFORMED_RECORD", "UNKNOWN_RECORD_KIND"])
        for e in result.exclusions:
            self.assertIn(e.reason, rec.REASONS)
            self.assertEqual(e.source_file_sha256, hashlib.sha256((self.root / "a.jsonl").read_bytes()).hexdigest())
            self.assertEqual(e.policy_version, "1")

    def test_strict_json_rejects_duplicate_keys_nan_and_bad_encoding(self):
        (self.root / "a.jsonl").write_bytes(b'{"record_kind": "QUOTE_SNAPSHOT", "bid": "1", "bid": "2"}\n{"record_kind": "QUOTE_SNAPSHOT", "bid": NaN}\n')
        (self.root / "b.jsonl").write_bytes(b"\xff\xfe\n")
        result = self.run_ingest({"a.jsonl": rec.QUOTE, "b.jsonl": rec.QUOTE})
        self.assertEqual([(e.source_file, e.reason) for e in result.exclusions], [("a.jsonl", "MALFORMED_RECORD"), ("a.jsonl", "MALFORMED_RECORD"), ("b.jsonl", "MALFORMED_FILE")])

    def test_a_row_of_the_wrong_kind_or_with_the_wrong_origin_label_is_excluded(self):
        self.put("a.jsonl", [quote(), eod(), quote(synthetic=False), {k: v for k, v in quote(strike="24000").items() if k != "synthetic"}])
        result = self.run_ingest({"a.jsonl": rec.QUOTE})
        self.assertEqual([r.source_row for r in result.records], [1])
        self.assertEqual([e.reason for e in result.exclusions], ["RECORD_KIND_MISMATCH", "ORIGIN_LABEL_MISMATCH", "ORIGIN_LABEL_MISMATCH"])
        unlabelled = quote()
        unlabelled.pop("synthetic")
        self.put("c.jsonl", [unlabelled])
        (self.root / mf.MANIFEST_NAME).unlink()
        result = self.run_ingest({"c.jsonl": rec.QUOTE}, origin_class="UNVERIFIED_SOURCE")
        self.assertEqual(len(result.records), 1)                                              # a row that is not labelled synthetic may sit in a non-synthetic file...
        (self.root / mf.MANIFEST_NAME).unlink()
        self.put("d.jsonl", [quote()])
        result = self.run_ingest({"d.jsonl": rec.QUOTE}, origin_class="UNVERIFIED_SOURCE")
        self.assertEqual([e.reason for e in result.exclusions], ["ORIGIN_LABEL_MISMATCH"])   # ...but a synthetic-labelled row can never pass as real data

    def test_provenance_and_the_raw_source_line_are_kept(self):
        self.put("a.jsonl", [quote(), quote(strike="24000", option_type="PE")])
        result = self.run_ingest({"a.jsonl": rec.QUOTE})
        record = next(r for r in result.records if r.key == PE)
        data = (self.root / "a.jsonl").read_bytes()
        self.assertEqual((record.source_id, record.source_file, record.source_row, record.origin_class), ("synthetic_fixture", "a.jsonl", 2, "SYNTHETIC_FIXTURE"))
        self.assertEqual(record.source_file_sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual(record.raw, data.split(b"\n")[1].decode())
        self.assertEqual(json.loads(record.raw)["instrument_token"], "T1")
        self.assertEqual((record.schema_version, record.parser_version, record.ingested_at_utc), (rec.SCHEMA_VERSION, rec.PARSER_VERSION, AS_OF.astimezone(record.ingested_at_utc.tzinfo)))
        self.assertEqual(record.quality_status, "OK")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            record.key = "x"

    def test_ingest_needs_an_aware_cutoff(self):
        self.put("a.jsonl", [quote()])
        fx.write_manifest(self.root, {"a.jsonl": rec.QUOTE})
        with self.assertRaises(ValueError):
            ingest.ingest(self.root, mf.load_manifest(self.root / mf.MANIFEST_NAME), as_of=datetime(2031, 3, 10))


class TestManifest(Pipeline):
    def test_a_manifest_records_path_hash_size_type_and_retrieval_metadata(self):
        self.put("a.jsonl", [quote()])
        entries = mf.describe(self.root, {"a.jsonl": rec.QUOTE}, source_id="synthetic_fixture", origin_class="SYNTHETIC_FIXTURE", origin="invented",
                              retrieved_at_utc="2031-03-03T05:00:00+00:00", terms_url=None)
        data = (self.root / "a.jsonl").read_bytes()
        entry = entries[0].to_dict()
        self.assertEqual((entry["path"], entry["sha256"], entry["bytes"], entry["record_type"], entry["retrieved_at_utc"]),
                         ("a.jsonl", hashlib.sha256(data).hexdigest(), len(data), rec.QUOTE, "2031-03-03T05:00:00+00:00"))
        self.assertEqual(set(entry), set(mf.FIELDS))
        mf.write_manifest(self.root / mf.MANIFEST_NAME, entries)
        self.assertEqual(mf.load_manifest(self.root / mf.MANIFEST_NAME), tuple(entries))
        with self.assertRaises(FileExistsError):                                              # a manifest is never rewritten in place
            mf.write_manifest(self.root / mf.MANIFEST_NAME, entries)

    def test_retrieval_time_is_provenance_and_does_not_change_the_data_identity(self):
        self.put("a.jsonl", [quote()])
        entries = mf.describe(self.root, {"a.jsonl": rec.QUOTE}, source_id="s", origin_class="SYNTHETIC_FIXTURE", origin="invented", retrieved_at_utc="2031-03-03T05:00:00+00:00")
        result = ingest.ingest(self.root, entries, as_of=AS_OF)
        self.assertEqual(result.records[0].retrieved_at, datetime.fromisoformat("2031-03-03T05:00:00+00:00"))
        self.assertEqual(result.records[0].observed_at, datetime(2031, 3, 3, 10, 15, tzinfo=IST))
        self.assertNotEqual(result.records[0].retrieved_at, result.records[0].available_at)

    def test_a_file_changed_after_the_manifest_was_written_is_rejected_and_recorded(self):
        self.put("a.jsonl", [quote(), quote(strike="24000", option_type="PE")])
        fx.write_manifest(self.root, {"a.jsonl": rec.QUOTE})
        original = (self.root / "a.jsonl").read_bytes()
        (self.root / "a.jsonl").write_bytes(original.replace(b"100.00", b"199.00"))                         # same size, one price edited
        changed = (self.root / "a.jsonl").read_bytes()
        result = ingest.ingest(self.root, mf.load_manifest(self.root / mf.MANIFEST_NAME), as_of=AS_OF)
        self.assertEqual(result.records, ())
        self.assertEqual([(f.path, f.code) for f in result.integrity_failures], [("a.jsonl", "HASH_MISMATCH_WITH_MANIFEST")])
        self.assertEqual([(e.source_file, e.reason, e.source_row) for e in result.exclusions], [("a.jsonl", "HASH_MISMATCH_WITH_MANIFEST", None)])
        self.assertEqual((self.root / "a.jsonl").read_bytes(), changed)                                       # ingestion never repairs or rewrites the file
        self.assertIn("integrity", result.ledger_lines())

    def test_size_missing_and_unlisted_files_are_integrity_failures(self):
        self.put("a.jsonl", [quote()])
        self.put("b.jsonl", [quote(strike="24000")])
        fx.write_manifest(self.root, {"a.jsonl": rec.QUOTE, "b.jsonl": rec.QUOTE})
        self.put("c.jsonl", [quote(strike="23000")])                                                          # present but not listed
        (self.root / "a.jsonl").write_bytes((self.root / "a.jsonl").read_bytes() + b"\n")
        (self.root / "b.jsonl").unlink()
        entries = mf.load_manifest(self.root / mf.MANIFEST_NAME)
        self.assertEqual(sorted((f.path, f.code) for f in mf.verify(self.root, entries)),
                         [("a.jsonl", "SIZE_MISMATCH"), ("b.jsonl", "MISSING_FILE"), ("c.jsonl", "UNLISTED_FILE")])
        result = ingest.ingest(self.root, entries, as_of=AS_OF)
        self.assertEqual(result.records, ())                                                                  # the unlisted file is reported, never ingested
        self.assertEqual(sorted(f.code for f in result.integrity_failures), ["MISSING_FILE", "SIZE_MISMATCH", "UNLISTED_FILE"])

    def test_a_good_file_is_still_ingested_when_another_one_fails(self):
        self.put("a.jsonl", [quote()])
        self.put("b.jsonl", [quote(strike="24000", option_type="PE")])
        fx.write_manifest(self.root, {"a.jsonl": rec.QUOTE, "b.jsonl": rec.QUOTE})
        (self.root / "b.jsonl").write_bytes(b"{}\n")
        result = ingest.ingest(self.root, mf.load_manifest(self.root / mf.MANIFEST_NAME), as_of=AS_OF)
        self.assertEqual([r.source_file for r in result.records], ["a.jsonl"])
        self.assertEqual([f.path for f in result.integrity_failures], ["b.jsonl"])

    def test_source_files_are_never_modified_by_ingestion(self):
        result = self.ingest_valid()
        self.assertTrue(result.records)
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()}
        mf.verify(self.root, mf.load_manifest(self.root / mf.MANIFEST_NAME))
        ingest.ingest(self.root, mf.load_manifest(self.root / mf.MANIFEST_NAME), as_of=AS_OF)
        self.assertEqual({p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()}, before)

    def test_unsafe_or_malformed_manifests_are_refused_whole(self):
        self.put("a.jsonl", [quote()])
        good = mf.describe(self.root, {"a.jsonl": rec.QUOTE}, source_id="s", origin_class="SYNTHETIC_FIXTURE", origin="invented")[0].to_dict()
        for name, change in {"parent path": {"path": "../a.jsonl"}, "absolute path": {"path": "/etc/a.jsonl"}, "backslash": {"path": "x\\a.jsonl"}, "drive": {"path": "C:/a.jsonl"},
                             "short hash": {"sha256": "abc"}, "upper hash": {"sha256": "A" * 64}, "negative size": {"bytes": -1}, "bool size": {"bytes": True},
                             "bad kind": {"record_type": "QUOTE"}, "bad origin class": {"origin_class": "REAL"}, "no origin": {"origin": ""}, "bad source id": {"source_id": "Bad Id"},
                             "naive retrieval": {"retrieved_at_utc": "2031-03-03T05:00:00"}}.items():
            with self.subTest(name), self.assertRaises(mf.ManifestError):
                mf.entry_from_dict({**good, **change})
        with self.assertRaises(mf.ManifestError):
            mf.entry_from_dict({**good, "extra": 1})
        path = self.root / "M.jsonl"
        for text in ("", json.dumps(good) + "\n" + json.dumps(good) + "\n", "{not json}\n", '{"path": "a", "path": "b"}\n'):
            path.write_text(text)
            with self.subTest(text=text[:20]), self.assertRaises(mf.ManifestError):
                mf.load_manifest(path)
        with self.assertRaises(mf.ManifestError):
            mf.load_manifest(self.root / "missing.jsonl")

    def test_symlinks_and_directories_are_not_regular_files(self):
        (self.root / "sub").mkdir()
        with self.assertRaises(mf.ManifestError):
            mf.describe(self.root, {"sub": rec.QUOTE}, source_id="s", origin_class="SYNTHETIC_FIXTURE", origin="x")
        with self.assertRaises(mf.ManifestError):
            mf.describe(self.root, {"nothing.jsonl": rec.QUOTE}, source_id="s", origin_class="SYNTHETIC_FIXTURE", origin="x")

    def test_the_command_line_tool_creates_and_verifies(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("historical_manifest_tool", ROOT / "tools" / "historical_manifest.py")
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        self.put("a.jsonl", [quote()])
        out, err = io.StringIO(), io.StringIO()
        base = ["--root", str(self.root)]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(tool.main(["create", *base, "--source-id", "synthetic_fixture", "--origin-class", "SYNTHETIC_FIXTURE", "--origin", "invented",
                                        "--file", "a.jsonl=QUOTE_SNAPSHOT"]), 0)
            self.assertEqual(tool.main(["verify", *base]), 0)
            self.assertEqual(tool.main(["create", *base, "--source-id", "synthetic_fixture", "--origin-class", "SYNTHETIC_FIXTURE", "--origin", "invented",
                                        "--file", "a.jsonl=QUOTE_SNAPSHOT"]), 2)                 # refuses to overwrite
            (self.root / "a.jsonl").write_bytes((self.root / "a.jsonl").read_bytes().replace(b"100.00", b"199.00"))
            self.assertEqual(tool.main(["verify", *base]), 1)
            self.assertIn("HASH_MISMATCH_WITH_MANIFEST", out.getvalue())


class TestBridgeAndEngine(Pipeline):
    """Only a validated, fill-eligible, available quote snapshot reaches the engine, and the engine keeps all of its own checks."""
    ALLOW = {"allowed_origin_classes": frozenset({"SYNTHETIC_FIXTURE"}), "permitted": True}

    def setUp(self):
        super().setUp()
        self.result = self.ingest_valid()
        self.clock = gate.ReplayClock(datetime(2031, 3, 3, 10, 15, 40, tzinfo=IST))
        self.view = gate.PointInTimeView(self.result.records, self.clock)

    def engine(self, origin=ORIGIN_HISTORICAL, **kw):
        kw.setdefault("max_quote_age", timedelta(seconds=30))
        return PaperEngine(Decimal("1000000"), clock=self.clock, origin=origin, **kw)

    def record(self, kind):
        return next(r for r in self.result.records if r.kind == kind)

    def test_an_eligible_visible_quote_becomes_a_historical_view(self):
        result = bridge.executable_view(self.view, CE, **self.ALLOW)
        self.assertTrue(result.ok, result.detail)
        market = result.view
        self.assertEqual((market.origin, market.state, market.connection, market.symbol), (ORIGIN_HISTORICAL, "HISTORICAL_REPLAY", "REPLAY", CE))
        self.assertEqual((market.bid, market.ask, market.bid_qty, market.ask_qty, market.lot_size, market.tick), (Decimal("100.40"), Decimal("100.60"), 650, 650, 65, Decimal("0.05")))
        self.assertEqual(market.provider_time, datetime(2031, 3, 3, 10, 15, 30, tzinfo=IST))          # the observation time, not the availability or replay time
        self.assertEqual(market.received_at, self.clock.now)
        self.assertIn("synthetic_fixture", market.provider)
        self.assertNotEqual(market.origin, ORIGIN_PROVIDER)

    def test_the_engine_fills_it_and_labels_the_trade_historical(self):
        engine = self.engine()
        market = bridge.executable_view(self.view, CE, **self.ALLOW).view
        trade = engine.simulate(TradeRequest(CE, "BUY", 1), market)
        self.assertTrue(trade.accepted, trade.message)
        self.assertEqual((trade.record.data_origin, trade.record.provider, trade.record.data_state), (ORIGIN_HISTORICAL, "historical:synthetic_fixture", "HISTORICAL_REPLAY"))
        self.assertEqual(trade.record.fill_price, Decimal("100.65"))                                  # ask 100.60 + 1 tick slippage: never the close or last price
        self.assertEqual(trade.record.quantity, 65)

    def test_each_forbidden_record_kind_is_refused_before_the_engine(self):
        calls = []

        class Spy(PaperEngine):
            def simulate(self, *a, **k):
                calls.append(a)
                return super().simulate(*a, **k)

        late = gate.ReplayClock(datetime(2031, 3, 9, tzinfo=IST))                                      # everything is available: only the kind is wrong
        for kind in (rec.EOD, rec.BAR, rec.INDEX):
            for record in (r for r in self.result.records if r.kind == kind):
                with self.subTest(kind=kind, key=record.key):
                    outcome = bridge.market_view(record, late, **self.ALLOW)
                    self.assertFalse(outcome.ok)
                    self.assertEqual(outcome.code, "NOT_A_QUOTE_SNAPSHOT")
        self.assertEqual(calls, [])
        self.assertEqual(Spy(Decimal("1000"), clock=late).ledger, [])
        for junk in (None, "NIFTY", {"bid": 1}, 5):
            self.assertEqual(bridge.market_view(junk, late, **self.ALLOW).code, "NOT_A_RECORD")

    def test_a_contract_with_only_settlement_and_candle_data_has_no_executable_quote(self):
        later = gate.PointInTimeView([r for r in self.result.records if r.kind != rec.QUOTE], gate.ReplayClock(datetime(2031, 3, 9, tzinfo=IST)))
        outcome = bridge.executable_view(later, CE, **self.ALLOW)
        self.assertEqual((outcome.ok, outcome.code), (False, "NO_VISIBLE_QUOTE"))

    def test_a_future_quote_is_not_an_executable_quote(self):
        early = gate.PointInTimeView(self.result.records, gate.ReplayClock(datetime(2031, 3, 3, 10, 14, tzinfo=IST)))
        self.assertEqual(bridge.executable_view(early, CE, **self.ALLOW).code, "NO_VISIBLE_QUOTE")
        future = next(r for r in self.result.records if r.kind == rec.QUOTE and r.observed_at.second == 30)
        self.assertEqual(bridge.market_view(future, gate.ReplayClock(datetime(2031, 3, 3, 10, 15, 35, tzinfo=IST)), **self.ALLOW).code, "NOT_AVAILABLE_YET")

    def test_quotes_without_a_documented_quote_time_or_sizes_are_refused(self):
        for override, code in (({"time_semantics": "LAST_TRADE_TIME"}, "TIME_NOT_A_QUOTE_TIME"), ({"time_semantics": "UNKNOWN"}, "TIME_NOT_A_QUOTE_TIME"),
                               ({"bid_qty": None, "ask_qty": None}, "SIZES_ABSENT")):
            with self.subTest(override):
                self.put("x.jsonl", [quote(**override)])
                (self.root / mf.MANIFEST_NAME).unlink()
                result = self.run_ingest({"x.jsonl": rec.QUOTE})
                outcome = bridge.market_view(result.records[0], gate.ReplayClock(datetime(2031, 3, 3, 10, 16, tzinfo=IST)), **self.ALLOW)
                self.assertEqual((outcome.ok, outcome.code), (False, "NOT_FILL_ELIGIBLE"))
                self.assertIn(code, outcome.detail)

    def test_origin_must_be_allowed_and_unknown_origin_is_never_fillable(self):
        quote_record = self.record(rec.QUOTE)
        clock = gate.ReplayClock(datetime(2031, 3, 3, 10, 15, 40, tzinfo=IST))
        self.assertEqual(bridge.market_view(quote_record, clock, permitted=True).code, "ORIGIN_NOT_ALLOWED")                          # default: nothing is allowed
        self.assertEqual(bridge.market_view(quote_record, clock, permitted=True, allowed_origin_classes=frozenset({"UNVERIFIED_SOURCE"})).code, "ORIGIN_NOT_ALLOWED")
        for origin in ("UNKNOWN", "REAL", "PROVIDER"):
            forged = dataclasses.replace(quote_record, origin_class=origin)
            self.assertEqual(bridge.market_view(forged, clock, permitted=True, allowed_origin_classes=frozenset({origin})).code, "ORIGIN_NOT_ALLOWED", origin)

    def test_the_dataset_must_be_explicitly_permitted(self):
        for flag in (False, None, 1, "yes"):
            self.assertEqual(bridge.executable_view(self.view, CE, allowed_origin_classes=frozenset({"SYNTHETIC_FIXTURE"}), permitted=flag).code, "DATASET_NOT_PERMITTED", flag)

    def test_unknown_origin_data_is_ingested_but_never_reaches_a_fill(self):
        self.put("u.jsonl", [{k: v for k, v in quote().items() if k != "synthetic"}])
        (self.root / mf.MANIFEST_NAME).unlink()
        result = self.run_ingest({"u.jsonl": rec.QUOTE}, origin_class="UNKNOWN", source_id="mystery")
        self.assertEqual(len(result.records), 1)
        clock = gate.ReplayClock(datetime(2031, 3, 3, 10, 16, tzinfo=IST))
        outcome = bridge.market_view(result.records[0], clock, permitted=True, allowed_origin_classes=frozenset(rec.ORIGIN_CLASSES))
        self.assertEqual(outcome.code, "ORIGIN_NOT_ALLOWED")

    def test_other_accounts_cannot_trade_on_historical_prices(self):
        market = bridge.executable_view(self.view, CE, **self.ALLOW).view
        for origin in (ORIGIN_PROVIDER, ORIGIN_SYNTHETIC):
            engine = self.engine(origin=origin)
            self.assertEqual(engine.simulate(TradeRequest(CE, "BUY", 1), market).code, "ACCOUNT_MISMATCH", origin)
            self.assertEqual(engine.ledger, [])
        provider_view = dataclasses.replace(market, origin=ORIGIN_PROVIDER)
        self.assertEqual(self.engine().simulate(TradeRequest(CE, "BUY", 1), provider_view).code, "ACCOUNT_MISMATCH")        # and the reverse

    def test_the_bridge_never_produces_provider_or_synthetic_origin(self):
        text = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "src" / "niftypaper" / "historical").glob("*.py"))
        self.assertNotIn("ORIGIN_PROVIDER", text)
        self.assertNotIn("ORIGIN_SYNTHETIC", text)
        for p in (ROOT / "src" / "niftypaper" / "historical").glob("*.py"):
            for banned in ("providers", "fyers", "permissions", "access_token", "FYERS_"):
                self.assertNotIn(banned, p.read_text(encoding="utf-8").lower() if banned.islower() else p.read_text(encoding="utf-8"), p.name)

    def test_the_engines_own_checks_still_apply_to_historical_views(self):
        market = bridge.executable_view(self.view, CE, **self.ALLOW).view
        engine = self.engine()
        self.assertEqual(engine.simulate(TradeRequest(CE, "BUY", 3), market).code, "RISK_MAX_LOTS")                      # per-trade lot limit
        self.assertEqual(engine.simulate(TradeRequest(CE, "SELL", 1), market).code, "NO_POSITION")                      # no short selling
        self.assertEqual(self.engine(limits=RiskLimits(max_exposure_fraction=Decimal("0.001"))).simulate(TradeRequest(CE, "BUY", 1), market).code, "RISK_EXPOSURE")
        small = PaperEngine(Decimal("1000000"), clock=self.clock, origin=ORIGIN_HISTORICAL, max_quote_age=timedelta(seconds=30),
                            limits=RiskLimits(max_exposure_fraction=Decimal("1"), daily_loss_fraction=Decimal("0.001")))
        self.assertEqual(small.simulate(TradeRequest(CE, "BUY", 1), market).code, "RISK_DAILY_LOSS")                    # the daily budget gate
        self.assertEqual(small.ledger, [])
        self.assertEqual(engine.simulate(TradeRequest("OTHER", "BUY", 1), market).code, "UNSUPPORTED_INSTRUMENT")
        for change, code in (({"ask_qty": 64}, "EXCEEDS_DISPLAYED_SIZE"), ({"permitted": False}, "TERMS_NOT_CONFIRMED"), ({"verified": False}, "DATA_NOT_VERIFIED")):
            self.assertEqual(engine.simulate(TradeRequest(CE, "BUY", 1), reattest(market, **change)).code, code, change)       # valid, attested, still refused
        self.assertEqual(engine.ledger, [])

    def test_an_altered_or_hand_built_historical_view_is_refused_even_if_it_looks_fine(self):
        market = bridge.executable_view(self.view, CE, **self.ALLOW).view
        engine = self.engine()
        for change in ({"bid": Decimal("100.35")}, {"ask": Decimal("100.05")}, {"ask_qty": 6500}, {"tick": Decimal("0.01")}, {"lot_size": 1}, {"symbol": PE},
                       {"provider_time": market.provider_time + timedelta(seconds=1)}, {"provider": "historical:other"}, {"state": "SIMULATED"}, {"origin": ORIGIN_SYNTHETIC}):
            with self.subTest(change):
                self.assertIn(engine.simulate(TradeRequest(change.get("symbol", CE), "BUY", 1), dataclasses.replace(market, **change)).code,
                              ("PROVENANCE_INVALID", "ACCOUNT_MISMATCH"))
        forged = dataclasses.replace(market, attestation=None)
        self.assertEqual(engine.simulate(TradeRequest(CE, "BUY", 1), forged).code, "PROVENANCE_INVALID")
        for fake in ("a" * 64, attest.Attestation("a" * 64, "x"), attest.Attestation(market.attestation.tag[:-1] + ("1" if market.attestation.tag[-1] == "0" else "0"), "x"), 1):
            self.assertEqual(engine.simulate(TradeRequest(CE, "BUY", 1), dataclasses.replace(market, attestation=fake)).code, "PROVENANCE_INVALID")
        hand_built = MarketView(ORIGIN_HISTORICAL, "historical:synthetic_fixture", "HISTORICAL_REPLAY", "REPLAY", CE, Decimal("100.40"), Decimal("100.60"), None, 650, 650,
                                Decimal("0.05"), 65, self.clock.now, "x", market.provider_time, True, True)
        self.assertEqual(engine.simulate(TradeRequest(CE, "BUY", 1), hand_built).code, "PROVENANCE_INVALID")
        self.assertEqual(engine.ledger, [])

    def test_the_engine_rechecks_the_fields_of_a_view_that_carries_a_valid_attestation(self):
        market = bridge.executable_view(self.view, CE, **self.ALLOW).view
        engine = self.engine()
        future = market.provider_time + timedelta(minutes=5)
        bad = {"crossed": {"bid": Decimal("100.60"), "ask": Decimal("100.40")}, "off tick": {"bid": Decimal("100.42")}, "zero tick": {"tick": Decimal("0")},
               "no tick": {"tick": None}, "no lot": {"lot_size": None}, "bool lot": {"lot_size": True}, "no bid size": {"bid_qty": None}, "zero ask size": {"ask_qty": 0},
               "float bid": {"bid": 100.4}, "short key": {"symbol": "NIFTY"}, "naive quote time": {"provider_time": datetime(2031, 3, 3, 10, 15, 30)},
               "future quote time": {"provider_time": future}, "no quote time": {"provider_time": None}, "provider label": {"provider": "fyers"},
               "wrong state": {"state": "LIVE"}, "wrong connection": {"connection": "LIVE"}, "nan bid": {"bid": Decimal("NaN")}}
        for name, change in bad.items():
            with self.subTest(name):
                result = engine.simulate(TradeRequest(change.get("symbol", CE), "BUY", 1), reattest(market, **change))
                self.assertEqual(result.code, "PROVENANCE_INVALID", result.message)
        self.assertEqual(engine.ledger, [])

    def test_only_the_bridge_may_issue_attestations(self):
        users = set()
        for path in (ROOT / "src").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "def issue(" in text or "attest.issue(" in text or "import issue" in text:
                users.add(path.relative_to(ROOT / "src").as_posix())
        self.assertEqual(users, {"niftypaper/historical/bridge.py", "niftypaper/paper/attest.py"})

    def test_the_eligibility_boundary_runs_against_the_replay_clock(self):
        market = bridge.executable_view(self.view, CE, **self.ALLOW).view                                # observed 10:15:30
        self.clock.advance_to(datetime(2031, 3, 3, 10, 16, 30, tzinfo=IST))                              # a minute later: older than the engine's 30 s age limit
        engine = self.engine()
        self.assertEqual(engine.simulate(TradeRequest(CE, "BUY", 1), market).code, "BOUNDARY_REJECTED")
        self.assertEqual(engine.ledger, [])

    def test_a_view_without_a_quote_time_or_sizes_never_fills(self):
        market = bridge.executable_view(self.view, CE, **self.ALLOW).view
        engine = self.engine()
        for change in ({"provider_time": None}, {"bid_qty": None}, {"ask_qty": None}):
            self.assertEqual(engine.simulate(TradeRequest(CE, "BUY", 1), reattest(market, **change)).code, "PROVENANCE_INVALID", change)

    def test_historical_trades_are_kept_out_of_other_accounts_on_restore(self):
        from niftypaper.paper.store import JsonlStore as StateStore
        market = bridge.executable_view(self.view, CE, **self.ALLOW).view
        path = self.root / "replay_state.jsonl"
        store = StateStore(path, starting_balance=Decimal("1000000"), account="replay_fixture")
        engine = PaperEngine(Decimal("1000000"), clock=self.clock, origin=ORIGIN_HISTORICAL, max_quote_age=timedelta(seconds=30), store=store)
        self.assertTrue(engine.simulate(TradeRequest(CE, "BUY", 1), market).accepted)
        engine.close()
        store = StateStore(path, starting_balance=Decimal("1000000"), account="replay_fixture")
        other = PaperEngine(Decimal("1000000"), clock=self.clock, origin=ORIGIN_PROVIDER, store=store)
        self.assertIn("DIFFERENT_ACCOUNT", other.lock_reason)
        other.close()


class TestRepositoryConsistency(unittest.TestCase):
    schema = json.loads((ROOT / "config" / "historical_schema_v1.json").read_text(encoding="utf-8"))

    def test_the_code_and_the_schema_agree_on_codes_and_flags(self):
        enums = self.schema["enums"]
        self.assertEqual(tuple(enums["exclusion_reason"]), rec.REASONS)
        self.assertEqual(tuple(enums["quality_flags"]), rec.QUALITY_FLAGS)
        self.assertEqual(tuple(enums["origin_class"]), rec.ORIGIN_CLASSES)
        self.assertEqual(tuple(enums["time_semantics"]), rec.TIME_SEMANTICS)
        self.assertEqual(tuple(enums["bar_interval"]), tuple(rec.BAR_SECONDS))
        self.assertEqual(len(set(rec.REASONS)), len(rec.REASONS))

    def test_only_the_quote_snapshot_kind_is_a_fill_source(self):
        kinds = self.schema["record_kinds"]
        self.assertEqual({k for k, v in kinds.items() if v.get("fill_source")}, {rec.QUOTE})
        for kind in (rec.EOD, rec.BAR, rec.INDEX):
            self.assertEqual(set(rec.FORBIDDEN[kind]) & {"bid", "ask"}, {"bid", "ask"})
            self.assertTrue(set(kinds[kind].get("forbidden_fields", ())) <= set(rec.FORBIDDEN[kind]))

    def test_fixtures_use_no_registered_source_and_no_source_is_enabled(self):
        registry = json.loads((ROOT / "config" / "historical_data_sources.json").read_text(encoding="utf-8"))
        ids = {s["id"] for s in registry["sources"]}
        self.assertFalse({"synthetic_fixture", "synthetic_one", "synthetic_two", "mystery"} & ids)
        self.assertTrue(all(s["enabled_for_v6"] is False for s in registry["sources"]))

    def test_the_committed_fixture_set_is_current_labelled_and_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx.fixture_set(tmp)
            for path in Path(tmp).iterdir():
                self.assertEqual(path.read_bytes(), (FIXTURE_DIR / path.name).read_bytes(), path.name)
        entries = mf.load_manifest(FIXTURE_DIR / mf.MANIFEST_NAME)
        self.assertEqual(mf.verify(FIXTURE_DIR, entries), [])
        for p in FIXTURE_DIR.glob("*.jsonl"):
            if p.name != mf.MANIFEST_NAME:
                self.assertTrue(p.name.startswith("SYNTHETIC_"))
                for line in p.read_text(encoding="utf-8").splitlines():
                    self.assertIs(json.loads(line)["synthetic"], True, p.name)
        self.assertTrue(all(e.origin_class == "SYNTHETIC_FIXTURE" and "invented" in e.origin for e in entries))

    def test_the_committed_fixture_set_ingests_to_the_documented_result(self):
        entries = mf.load_manifest(FIXTURE_DIR / mf.MANIFEST_NAME)
        result = ingest.ingest(FIXTURE_DIR, entries, as_of=AS_OF)
        self.assertEqual(result.integrity_failures, ())
        counts = {}
        for r in result.records:
            counts[r.kind] = counts.get(r.kind, 0) + 1
        self.assertEqual(counts, {rec.QUOTE: 5, rec.EOD: 3, rec.BAR: 2, rec.INDEX: 2})
        reasons = {}
        for e in result.exclusions:
            reasons[e.reason] = reasons.get(e.reason, 0) + 1
            self.assertIn(e.reason, rec.REASONS)
        self.assertEqual(reasons["CONFLICTING_ROW_IN_FILE"], 2)
        self.assertEqual(reasons["DUPLICATE_ROW_IN_FILE"], 2)
        self.assertEqual(sum(reasons.values()), len(result.exclusions))
        self.assertEqual({r.kind for r in result.records if r.fill_eligible}, {rec.QUOTE})
        self.assertEqual(len([r for r in result.records if r.fill_eligible]), 3)
        self.assertTrue(all(json.loads(r.raw)["synthetic"] is True for r in result.records))
        self.assertEqual(result.digest(), ingest.ingest(FIXTURE_DIR, entries, as_of=AS_OF).digest())

    def test_the_fixture_directory_is_not_modified_by_reading_it(self):
        before = {p.name: p.read_bytes() for p in FIXTURE_DIR.iterdir()}
        ingest.ingest(FIXTURE_DIR, mf.load_manifest(FIXTURE_DIR / mf.MANIFEST_NAME), as_of=AS_OF)
        self.assertEqual({p.name: p.read_bytes() for p in FIXTURE_DIR.iterdir()}, before)

    def test_the_package_reads_no_environment_clock_or_network(self):
        for path in (ROOT / "src" / "niftypaper" / "historical").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for banned in ("os.environ", "getenv", "datetime.now", "utcnow", "time.time", "urlopen", "import socket", "random"):
                self.assertNotIn(banned, text, f"{path.name}: {banned}")


if __name__ == "__main__":
    unittest.main()
