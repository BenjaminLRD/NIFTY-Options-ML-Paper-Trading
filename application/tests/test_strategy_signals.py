"""Signal generation only: no runner, no engine, no execution. Invented data in temporary folders."""
import dataclasses
import re
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from niftypaper.historical import records as rec
from niftypaper.historical.gate import PointInTimeView, ReplayClock
from niftypaper.strategy import signals, trend
from tests import ROOT
from tests.historical_fixtures import CE, PE
from tests.strategy_fixtures import (BASE_QUOTES, FALLING_AFTER, FLAT, FUTURE, RISING, T1, T2, at, build, q, records_of, strategy, view_at)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.count = 0

    def folder(self, **kw):
        self.count += 1
        return build(self.tmp / f"fx{self.count}", **kw)

    def evaluate(self, now=T1, held=None, strat=None, **kw):
        return (strat or strategy()).evaluate(view_at(self.folder(**kw), now), held or {})


class TestValidSignals(Base):
    def test_a_valid_bullish_entry(self):
        ev = self.evaluate()
        s = ev.signal
        self.assertEqual((ev.status, ev.reason_code), (signals.SIGNAL, "TREND_UP"))
        self.assertEqual((s.strategy, s.version, s.decision_time, s.contract, s.direction, s.action, s.side, s.lots),
                         ("index_sma_trend", "1.0.0", T1, CE, "BULLISH", "BUY_TO_OPEN", "BUY", 1))
        self.assertEqual((s.inputs["window"], str(int(float(s.inputs["sma"])))), (5, "25100"))
        self.assertEqual([c["trading_date"] for c in s.inputs["closes"]], [d for d, _ in RISING])
        self.assertEqual(s.inputs["last_close"], "25200.00")
        self.assertEqual(s.data_observed_through, at(7, 23, 59, 59))
        self.assertLessEqual(s.data_observed_through, s.decision_time)
        self.assertEqual(s.quote_status["status"], "ELIGIBLE")
        self.assertEqual(s.invalidation["valid_until"], (T1 + timedelta(minutes=10)).isoformat())
        self.assertTrue(s.reason and s.invalidation["cancel_if"] and s.invalidation["position_ends_when"])

    def test_a_valid_bearish_entry_trades_the_put(self):
        falling = [(d, c) for (d, _), (_, c) in zip(RISING, reversed(RISING))]
        s = self.evaluate(closes=falling).signal
        self.assertEqual((s.contract, s.direction, s.action, s.reason_code), (PE, "BEARISH", "BUY_TO_OPEN", "TREND_DOWN"))

    def test_an_exit_when_the_trend_ends(self):
        ev = self.evaluate(T2, held={CE: 65})
        s = ev.signal
        self.assertEqual((ev.status, s.contract, s.action, s.side, s.lots, s.direction, s.reason_code), ("SIGNAL", CE, "SELL_TO_CLOSE", "SELL", 1, "BULLISH", "TREND_ENDED"))

    def test_no_signal_cases_say_why(self):
        for name, now, held, kw, code in (("flat", T1, {}, {"closes": FLAT}, "NEUTRAL"), ("hold", T1, {CE: 65}, {}, "HOLD"), ("hold_put", T2, {PE: 65}, {}, "HOLD")):
            with self.subTest(name):
                ev = self.evaluate(now, held, **kw)
                self.assertEqual((ev.status, ev.reason_code, ev.signal), (signals.NO_SIGNAL, code, None))

    def test_a_signal_cannot_rest_on_data_from_after_its_decision(self):
        s = self.evaluate().signal
        with self.assertRaises(ValueError):
            dataclasses.replace(s, signal_id="", data_observed_through=s.decision_time + timedelta(seconds=1))


class TestRejectedInputs(Base):
    def reject(self, code, now=T1, held=None, **kw):
        ev = self.evaluate(now, held, **kw)
        self.assertEqual((ev.status, ev.reason_code, ev.signal), (signals.INPUT_REJECTED, code, None), ev.detail)
        return ev

    def test_each_kind_of_bad_input_is_rejected_with_its_reason(self):
        self.reject("INSUFFICIENT_HISTORY", closes=RISING[:3])
        self.reject("INSUFFICIENT_HISTORY", closes=[])
        self.reject("STALE_INDEX", now=at(16, 10, 0))                                                    # the newest visible close (10 March) is six days old
        self.reject("GAPPED_SERIES", closes=[("2031-02-20", "25000.00")] + RISING[1:])
        self.reject("BAD_INDEX_RECORD", closes=RISING[:4] + [("2031-03-07", "0")])                       # a zero close is a no-trade marker, not a price
        self.reject("NO_QUOTE", quotes=[BASE_QUOTES[1]])
        self.reject("STALE_QUOTE", quotes=[q("CE", 10, 10, 14, "100.00", "100.20")])                      # 62 s old at the decision
        self.reject("QUOTE_NOT_FILL_ELIGIBLE", quotes=[q("CE", 10, 10, 15, "100.00", "100.20", time_semantics="RECEIVE_TIME")])
        self.reject("FOREIGN_POSITION", held={"OTHER": 65})
        self.reject("BOTH_HELD", held={CE: 65, PE: 65})
        self.reject("BAD_HOLDINGS", held={CE: 0})
        self.reject("BAD_HOLDINGS", held={CE: True})
        self.reject("BAD_HOLDINGS", held=[CE])

    def test_a_held_quantity_that_is_not_whole_lots_is_rejected(self):
        self.reject("LOT_SIZE_MISMATCH", T2, held={CE: 66})

    def test_a_missing_exit_quote_is_not_replaced_by_anything(self):
        self.reject("NO_QUOTE", T2, held={CE: 65}, quotes=[BASE_QUOTES[1]])
        self.reject("STALE_QUOTE", T2, held={CE: 65}, quotes=[BASE_QUOTES[0]])                       # yesterday's quote is not an exit price

    def test_inputs_of_a_rejection_are_still_visible(self):
        self.assertEqual(self.reject("NO_QUOTE", quotes=[]).inputs["quote"]["status"], "MISSING")


class TestWindowAndFlags(Base):
    def test_only_the_newest_window_of_closes_is_used(self):
        closes = [("2031-03-03", "1.00"), ("2031-03-04", "100.00"), ("2031-03-05", "100.00"), ("2031-03-06", "100.00"), ("2031-03-07", "100.00"), ("2031-03-10", "99.00")]
        ev = self.evaluate(T2, closes=closes)
        self.assertEqual((ev.reason_code, len(ev.inputs["closes"]), ev.inputs["closes"][0]["trading_date"]), ("TREND_DOWN", 5, "2031-03-04"))   # with the old 1.00 included it would be bullish

    def test_an_index_record_with_a_quality_flag_is_rejected(self):
        folder = self.folder()
        flagged = [dataclasses.replace(r, flags=("PRICE_ZERO_NO_TRADE",)) if r.kind == rec.INDEX and r.time_key == "2031-03-07" else r for r in records_of(folder)]
        ev = strategy().evaluate(PointInTimeView(flagged, ReplayClock(T1)), {})
        self.assertEqual(ev.reason_code, "BAD_INDEX_RECORD")


class TestNoLookAhead(Base):
    def test_future_index_rows_and_quotes_cannot_change_earlier_evaluations(self):
        plain = build(self.tmp / "plain")
        later = [q("CE", 10, 10, 15, "100.00", "100.20", s=30), q("CE", 11, 10, 15, "200.00", "200.20", s=3)]
        loaded = build(self.tmp / "loaded", closes=RISING + FALLING_AFTER + FUTURE, quotes=BASE_QUOTES + later)
        for now in (T1, T2):
            for held in ({}, {CE: 65}):
                with self.subTest(now=now.isoformat(), held=held):
                    a = strategy().evaluate(view_at(plain, now), held).to_dict()
                    b = strategy().evaluate(view_at(loaded, now), held).to_dict()
                    self.assertEqual(a, b)

    def test_the_close_of_the_decision_day_is_not_usable_that_day_but_is_the_next(self):
        low = build(self.tmp / "low", closes=RISING + [("2031-03-10", "1.00")])
        high = build(self.tmp / "high", closes=RISING + [("2031-03-10", "99999.00")])
        self.assertEqual(strategy().evaluate(view_at(low, T1), {}).to_dict(), strategy().evaluate(view_at(high, T1), {}).to_dict())
        self.assertNotEqual(strategy().evaluate(view_at(low, T2), {}).to_dict(), strategy().evaluate(view_at(high, T2), {}).to_dict())

    def test_a_decision_exactly_at_day_end_still_cannot_see_that_days_close_early(self):
        folder = build(self.tmp / "edge", closes=RISING)
        before = strategy().evaluate(view_at(folder, at(7, 23, 59, 58)), {})
        self.assertEqual(before.reason_code, "INSUFFICIENT_HISTORY")                                    # only four closes are visible one second before day end

    def test_an_altered_record_cannot_leak_the_future(self):
        folder = build(self.tmp / "alt", closes=RISING)
        altered = [dataclasses.replace(r, available_at=at(1, 0, 0)) if r.kind == rec.INDEX and r.time_key == "2031-03-07" else r for r in records_of(folder)]
        ev = strategy().evaluate(PointInTimeView(altered, ReplayClock(T1)), {})
        self.assertEqual(ev.reason_code, "INSUFFICIENT_HISTORY")                                        # the record claims to be available before its own trading date ended

    def test_the_data_it_used_is_listed_and_all_of_it_was_available(self):
        s = self.evaluate().signal
        for item in s.inputs["closes"]:
            self.assertLessEqual(__import__("datetime").datetime.fromisoformat(item["available_at"]), s.decision_time)


class TestDeterminismAndSeparation(Base):
    def test_identical_inputs_give_identical_signals_and_ids(self):
        a, b = self.evaluate(), self.evaluate()
        self.assertEqual(a.to_dict(), b.to_dict())
        self.assertRegex(a.signal.signal_id, r"^[0-9a-f]{64}$")
        self.assertNotEqual(a.signal.signal_id, self.evaluate(closes=RISING[:4] + [("2031-03-07", "25300.00")]).signal.signal_id)

    def test_signal_modules_do_not_import_execution_or_intents(self):
        for name in ("signals.py", "trend.py"):
            text = (ROOT / "src" / "niftypaper" / "strategy" / name).read_text(encoding="utf-8")
            imports = " ".join(re.findall(r"^(?:from|import) .*$", text, re.M))
            for banned in ("replay", "paper", "bridge", "intents", "driver"):
                self.assertNotIn(banned, imports, f"{name} imports {banned}")

    def test_the_execution_side_does_not_depend_on_the_rule(self):
        text = (ROOT / "src" / "niftypaper" / "strategy" / "intents.py").read_text(encoding="utf-8")
        self.assertNotIn("trend", " ".join(re.findall(r"^(?:from|import) .*$", text, re.M)))

    def test_bad_configurations_are_refused(self):
        for kw in ({"window": 1}, {"window": True}, {"lots": 0}, {"max_quote_age": timedelta(0)}, {"valid_for": timedelta(seconds=-1)}, {"put_contract": CE}):
            with self.subTest(kw), self.assertRaises(ValueError):
                trend.TrendConfig(**{"call_contract": CE, "put_contract": PE, **kw})
        with self.assertRaises(TypeError):
            trend.IndexSmaTrend("nope")


if __name__ == "__main__":
    unittest.main()
