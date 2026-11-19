"""Intent conversion, the two-pass driver, the report and the audit of the resulting paper trades. Invented data in temporary folders. The conversion and driver
tests use a stub strategy where possible, so execution is tested without the rule's calculations."""
import dataclasses
import json
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from niftypaper.historical import anchor, audit, identity, replay
from niftypaper.paper.engine import RiskLimits
from niftypaper.strategy import driver, intents, report, signals
from niftypaper.strategy.signals import Evaluation, Signal
from tests import ROOT
from tests.historical_fixtures import CE, PE
from tests.strategy_fixtures import (BASE_QUOTES, END, FALLING_AFTER, FLAT, FUTURE, RISING, START, T1, T2, at, build, q, replay_config, strategy)
from tests.test_historical_chain import lines_of


def make_signal(now=T1, contract=CE, action="BUY_TO_OPEN", side="BUY", lots=1, valid=timedelta(minutes=10)):
    return Signal("stub", "0", now, now - timedelta(hours=1), contract, "BULLISH", action, side, lots, "STUB", "scripted", {}, {"valid_until": (now + valid).isoformat(), "cancel_if": "x",
                                                                                                                         "position_ends_when": "y"}, {"status": "ELIGIBLE"})


class Stub:
    """Returns scripted evaluations: no calculation, so the execution path is tested on its own."""
    name, version = "stub", "0"

    def __init__(self, script):
        self.script, self.calls = script, 0

    def evaluate(self, view, held):
        now = view.now
        self.calls += 1
        action = self.script.get(now)
        if action is None:
            return Evaluation(now, signals.NO_SIGNAL, "STUB_NONE", "scripted: nothing", {})
        return Evaluation(now, signals.SIGNAL, "STUB", "scripted", {}, make_signal(now, *action))


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.count = 0

    def fixtures(self, **kw):
        self.count += 1
        return build(self.tmp / f"fx{self.count}", **kw)

    def drive(self, strat=None, fixtures=None, schedule=(T1, T2), delay=timedelta(0), name=None, **config):
        self.count += 1
        name = name or f"run{self.count}"
        fixtures = fixtures or self.fixtures()
        out = self.tmp / name
        result = driver.run_strategy(replay_config(fixtures, out / "state", **config), strat or strategy(), schedule, out / "events.jsonl", execution_delay=delay)
        return result, out, fixtures

    def audited(self, out, fixtures):
        state = out / "state" / "replay_synthetic_fixture.jsonl"
        return audit.build_report(audit.AuditConfig(out / "events.jsonl", state, fixtures))


class TestIntentConversion(Base):
    def current(self, now=T1, **kw):
        return Evaluation(now, signals.SIGNAL, "STUB", "", {}, make_signal(now, **kw))

    def test_an_approved_signal_becomes_an_intent_in_the_existing_format(self):
        choice = intents.convert(make_signal(), T1, self.current())
        self.assertEqual((choice.outcome, choice.intent), ("SUBMITTED", replay.Intent(T1, CE, "BUY", 1)))
        later = intents.convert(make_signal(), T1 + timedelta(minutes=10), self.current(T1 + timedelta(minutes=10)))
        self.assertEqual(later.outcome, "SUBMITTED")                                                      # the boundary itself is still valid
        identity.plan([choice.intent])                                                                     # and it is a well-formed plan item

    def test_every_other_case_yields_no_intent_and_a_reason(self):
        sig = make_signal()
        end = T1 + timedelta(minutes=10, seconds=1)
        cases = (("expired", intents.convert(sig, end, self.current(end)), "SIGNAL_EXPIRED", "VALID_UNTIL_PASSED"),
                 ("rejected_now", intents.convert(sig, T1, Evaluation(T1, signals.INPUT_REJECTED, "STALE_QUOTE", "old", {})), "INVALIDATED_BEFORE_EXECUTION", "STALE_QUOTE"),
                 ("no_signal_now", intents.convert(sig, T1, Evaluation(T1, signals.NO_SIGNAL, "HOLD", "x", {})), "INVALIDATED_BEFORE_EXECUTION", "HOLD"),
                 ("other_action", intents.convert(sig, T1, self.current(action="SELL_TO_CLOSE", side="SELL")), "INVALIDATED_BEFORE_EXECUTION", "DIFFERENT_ACTION"),
                 ("other_lots", intents.convert(sig, T1, self.current(lots=2)), "INVALIDATED_BEFORE_EXECUTION", "DIFFERENT_ACTION"),
                 ("before_decision", intents.convert(sig, T1 - timedelta(seconds=1), self.current(T1 - timedelta(seconds=1))), "PRECONDITION_FAILED", "EXECUTION_BEFORE_DECISION"),
                 ("naive", intents.convert(sig, T1.replace(tzinfo=None), self.current()), "PRECONDITION_FAILED", "BAD_EXECUTION_TIME"),
                 ("stale_eval", intents.convert(sig, T1 + timedelta(seconds=1), self.current()), "PRECONDITION_FAILED", "STALE_EVALUATION"),
                 ("args", intents.convert("x", T1, self.current()), "PRECONDITION_FAILED", "BAD_ARGUMENTS"))
        for name, choice, outcome, code in cases:
            with self.subTest(name):
                self.assertEqual((choice.outcome, choice.reason_code, choice.intent), (outcome, code, None))


class TestDriverWithAStub(Base):
    def test_scripted_entry_then_exit_is_executed_by_the_existing_engine(self):
        stub = Stub({T1: (CE, "BUY_TO_OPEN", "BUY", 1), T2: (CE, "SELL_TO_CLOSE", "SELL", 1)})
        result, out, fixtures = self.drive(stub)
        self.assertEqual([(i.contract_key, i.side, i.lots) for i in result.plan], [(CE, "BUY", 1), (CE, "SELL", 1)])
        self.assertEqual([d.result["event"] for d in result.decisions], ["FILL", "FILL"])
        self.assertEqual(result.positions, {})
        self.assertEqual(stub.calls, 4)                                              # decision and execution-time evaluation for each of the two signals
        self.assertEqual(result.plan_digest, identity.plan(result.plan, result.until)["digest"])

    def test_a_signal_alone_trades_nothing(self):
        from tests.strategy_fixtures import view_at
        with mock.patch.object(replay.ReplayRunner, "try_trade", side_effect=AssertionError("a signal must not trade")):
            evaluation = strategy().evaluate(view_at(self.fixtures(), T1), {})
        self.assertEqual(evaluation.status, "SIGNAL")

    def test_a_signal_expired_or_invalidated_before_execution_is_visible_and_not_executed(self):
        stub = Stub({T1: (CE, "BUY_TO_OPEN", "BUY", 1)})
        late, out, _ = self.drive(stub, delay=timedelta(minutes=20), schedule=(T1,))
        d = late.decisions[0]
        self.assertEqual((d.outcome, d.reason_code, d.intent, late.plan, late.ledger), ("SIGNAL_EXPIRED", "VALID_UNTIL_PASSED", None, [], []))
        class Flip(Stub):
            def evaluate(self, view, held):                                          # asks at decision time, asks for nothing six seconds later
                return super().evaluate(view, held) if view.now == T1 else Evaluation(view.now, signals.INPUT_REJECTED, "STALE_QUOTE", "scripted", {})
        flipped, _, _ = self.drive(Flip({T1: (CE, "BUY_TO_OPEN", "BUY", 1)}), delay=timedelta(seconds=6), schedule=(T1,))
        self.assertEqual((flipped.decisions[0].outcome, flipped.decisions[0].reason_code, flipped.ledger), ("INVALIDATED_BEFORE_EXECUTION", "STALE_QUOTE", []))

    def test_a_bridge_refusal_stays_visible_with_its_reason(self):
        stub = Stub({T1: ("NIFTY", "BUY_TO_OPEN", "BUY", 1)})                        # an index has no quote
        result, out, fixtures = self.drive(stub, schedule=(T1,))
        self.assertEqual((result.decisions[0].outcome, result.decisions[0].result["stage"], result.decisions[0].result["code"]), ("SUBMITTED", "BRIDGE", "NO_VISIBLE_QUOTE"))
        types = [json.loads(x)["type"] for x in lines_of((out / "events.jsonl").read_bytes())]
        self.assertIn("INTENT_REFUSED", types)
        self.assertEqual(self.audited(out, fixtures)["verdict"], "CONSISTENT")                                # a refused intent leaves no trade; the initialized zero-trade state reconciles

    def test_a_partial_exit_is_not_a_completed_trade(self):
        stub = Stub({T1: (CE, "BUY_TO_OPEN", "BUY", 2), T2: (CE, "SELL_TO_CLOSE", "SELL", 1)})
        result, _, _ = self.drive(stub)
        rep = report.build_report(result, stub)
        self.assertEqual((rep["intents_accepted"], rep["completed_trades"], rep["open_trades"]), (2, 0, 1))
        self.assertIn("unrealized", rep["marks"][CE])
        self.assertEqual(Decimal(rep["realized_pnl"]), result.realized)

    def test_bad_schedules_and_states_are_refused_before_anything_is_written(self):
        stub = Stub({})
        for name, schedule, delay in (("naive", (T1.replace(tzinfo=None),), timedelta(0)), ("outside", (END + timedelta(hours=1),), timedelta(0)), ("order", (T2, T1), timedelta(0)),
                                      ("same", (T1, T1), timedelta(0)), ("overlap", (T1, T1 + timedelta(seconds=5)), timedelta(seconds=10)), ("delay", (T1,), -timedelta(seconds=1)),
                                      ("late_exec", (END,), timedelta(seconds=1))):
            with self.subTest(name), self.assertRaises(driver.StrategyError):
                self.drive(stub, schedule=schedule, delay=delay, name="bad_" + name)
            self.assertFalse((self.tmp / ("bad_" + name) / "events.jsonl").exists())
        used = self.tmp / "used"
        (used / "state").mkdir(parents=True)
        (used / "state" / "something").write_text("x")
        with self.assertRaises(driver.StrategyError) as caught:
            driver.run_strategy(replay_config(self.fixtures(), used / "state"), stub, [T1], used / "events.jsonl")
        self.assertEqual(caught.exception.code, "STATE_NOT_EMPTY")

    def test_a_replay_that_does_not_reproduce_the_decisions_writes_nothing(self):
        stub = Stub({T1: (CE, "BUY_TO_OPEN", "BUY", 1)})
        ticks = iter(range(100))
        with mock.patch.object(driver, "_signature", lambda event: next(ticks)):
            with self.assertRaises(driver.StrategyDivergence):
                self.drive(stub, schedule=(T1,), name="diverge")
        self.assertFalse((self.tmp / "diverge" / "events.jsonl").exists())


class TestBaselineScenarios(Base):
    def test_entry_then_exit_reconciles_in_the_audit_and_passes_strict_verification(self):
        result, out, fixtures = self.drive()
        self.assertEqual([(d.outcome, d.evaluation.reason_code) for d in result.decisions], [("SUBMITTED", "TREND_UP"), ("SUBMITTED", "TREND_ENDED")])
        self.assertEqual([(t.side, t.symbol, t.lots) for t in result.ledger], [("BUY", CE, 1), ("SELL", CE, 1)])
        r = self.audited(out, fixtures)
        self.assertEqual((r["verdict"], r["coverage"], r["inputs_identity"]["plan"]["status"], r["pairing"]["status"], r["chain"]["status"]), ("CONSISTENT", "FULL", "MATCH", "PAIRED", "VALID"))
        self.assertEqual(self.status(r, "X1_EVENT_TRADE_MATCH"), "PASS")
        kept = self.tmp / "kept apart" / "anchor.json"
        state = next((out / "state").glob("replay_*.jsonl"))
        anchor.create(out / "events.jsonl", state, kept)
        tool = self.load_verify()
        text = __import__("io").StringIO()
        with __import__("contextlib").redirect_stdout(text):
            code = tool.main(["--events", str(out / "events.jsonl"), "--state", str(state), "--fixtures", str(fixtures), "--anchor", str(kept), "--anchor-trust", "independent"])
        self.assertEqual((code, text.getvalue().rstrip("\n").splitlines()[-1]), (0, "PASS"), text.getvalue())

    def load_verify(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("vl_for_strategy", ROOT / "tools" / "replay_verify_later.py")
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        return tool

    def status(self, r, check):
        return next(c["status"] for c in r["checks"] if c["id"] == check)

    def test_the_report_counts_and_labels_everything_as_simulated(self):
        result, out, fixtures = self.drive()
        rep = report.build_report(result, strategy())
        self.assertTrue(rep["simulated"])
        self.assertIn("SIMULATED", rep["notice"])
        self.assertIn("Nothing here is evidence", rep["notice"])
        self.assertEqual((rep["observations"], rep["signals_generated"], rep["intents_submitted"], rep["intents_accepted"], rep["intents_rejected"], rep["completed_trades"], rep["open_trades"]),
                         (2, 2, 2, 2, [], 1, 0))
        self.assertEqual(rep["unrealized_pnl"], {"unavailable": "NO_OPEN_POSITIONS"})
        self.assertGreater(Decimal(rep["fees"]), 0)
        self.assertGreater(Decimal(rep["slippage"]), 0)
        self.assertEqual(Decimal(rep["realized_pnl"]), result.realized)
        self.assertEqual(rep["plan_digest"], json.loads(lines_of((out / "events.jsonl").read_bytes())[0])["inputs"]["plan"]["digest"])
        json.dumps(rep)

    def test_an_open_trade_is_marked_when_a_price_exists(self):
        result, out, fixtures = self.drive(schedule=(T1,))
        rep = report.build_report(result, strategy())
        self.assertEqual((rep["open_trades"], rep["completed_trades"], rep["realized_pnl"]), (1, 0, {"unavailable": "NO_CLOSED_TRADES"}))
        mark = rep["marks"][CE]
        self.assertIn("unrealized", mark)
        self.assertEqual(rep["unrealized_pnl"], mark["unrealized"])

    def test_unknown_values_are_explicit_never_zero(self):
        result, _, _ = self.drive(schedule=(T1,))
        broken = dataclasses.replace(result, marks={CE: {"unavailable": "NO_VISIBLE_QUOTE"}})
        self.assertEqual(report.build_report(broken, strategy())["unrealized_pnl"], {"unavailable": f"NO_MARK_FOR_{CE}"})

    def test_no_signal_run_has_an_empty_declared_plan_and_unavailable_figures(self):
        result, out, fixtures = self.drive(fixtures=self.fixtures(closes=FLAT))
        rep = report.build_report(result, strategy())
        self.assertEqual((rep["signals_generated"], rep["intents_submitted"], rep["evaluations"]), (0, 0, {"NO_SIGNAL": 2}))
        self.assertEqual((rep["fees"], rep["slippage"], rep["realized_pnl"], rep["unrealized_pnl"]),
                         ({"unavailable": "NO_FILLS"}, {"unavailable": "NO_FILLS"}, {"unavailable": "NO_CLOSED_TRADES"}, {"unavailable": "NO_OPEN_POSITIONS"}))
        r = self.audited(out, fixtures)
        self.assertEqual(len(list((out / "state").glob("replay_*.jsonl"))), 1)                                 # the account was initialized before any trade
        self.assertEqual((r["verdict"], r["coverage"], r["account"]["state_kind"], r["chain"]["status"]), ("CONSISTENT", "FULL", "INITIALIZED_NO_TRADES", "VALID"))

    def test_invalid_stale_or_unavailable_inputs_produce_no_intent(self):
        for name, kw in (("noquote", {"quotes": []}), ("stale", {"quotes": [q(s, d, 10, 14, "100.00", "100.20") for s in ("CE", "PE") for d in (10, 11)]}),
                         ("short", {"closes": RISING[:3]}), ("noindex", {"closes": []})):
            with self.subTest(name):
                result, out, fixtures = self.drive(fixtures=self.fixtures(**kw), name="in_" + name)
                self.assertEqual({d.outcome for d in result.decisions}, {"INPUT_REJECTED"})
                self.assertEqual((result.plan, result.ledger), ([], []))
                rep = report.build_report(result, strategy())
                self.assertEqual((rep["evaluations"], rep["intents_submitted"]), ({"INPUT_REJECTED": 2}, 0))
                self.assertEqual(self.audited(out, fixtures)["verdict"], "CONSISTENT")

    def test_future_data_cannot_change_the_plan_or_the_log(self):
        plain, out1, _ = self.drive(name="plain")
        loaded, out2, _ = self.drive(fixtures=self.fixtures(closes=RISING + FALLING_AFTER + FUTURE, quotes=BASE_QUOTES + [q("CE", 10, 10, 15, "1.00", "1.05", s=30)]), name="loaded")
        self.assertEqual((plain.plan_digest, [(i.at, i.contract_key, i.side, i.lots) for i in plain.plan]), (loaded.plan_digest, [(i.at, i.contract_key, i.side, i.lots) for i in loaded.plan]))
        self.assertEqual([t.fill_price for t in plain.ledger], [t.fill_price for t in loaded.ledger])

    def test_a_risk_rejected_intent_stays_visible_with_the_engines_reason(self):
        result, out, fixtures = self.drive(strat=strategy(lots=3))
        d = result.decisions[0]
        self.assertEqual((d.outcome, d.result["event"], d.result["stage"], d.result["code"]), ("SUBMITTED", "INTENT_REFUSED", "ENGINE", "RISK_MAX_LOTS"))
        rep = report.build_report(result, strategy(lots=3))
        self.assertEqual((rep["intents_submitted"], rep["intents_accepted"], rep["intents_rejected"][0]["code"]), (2, 0, "RISK_MAX_LOTS"))
        self.assertEqual(result.ledger, [])
        r = self.audited(out, fixtures)
        self.assertEqual((r["verdict"], r["coverage"]), ("CONSISTENT", "FULL"))                              # nothing was filled; the zero-trade state reconciles in full
        tight = self.drive(name="tight", limits=RiskLimits(max_lots_per_trade=1, max_open_positions=1))[0]
        self.assertEqual(tight.decisions[0].result["event"], "FILL")                                       # the same rule passes when the limit allows it

    def test_a_signal_invalidated_before_execution_by_the_real_rule(self):
        result, _, _ = self.drive(delay=timedelta(seconds=6), schedule=(T1,))                              # the 10:15:00 quote is 8 s old at 10:15:08
        d = result.decisions[0]
        self.assertEqual((d.outcome, d.reason_code, result.plan), ("INVALIDATED_BEFORE_EXECUTION", "STALE_QUOTE", []))
        rep = report.build_report(result, strategy())
        self.assertEqual(rep["not_submitted"], [{"decision_time": T1.isoformat(), "outcome": "INVALIDATED_BEFORE_EXECUTION", "reason_code": "STALE_QUOTE"}])

    def test_repeated_runs_on_identical_inputs_are_byte_identical(self):
        a, out_a, _ = self.drive(name="a")
        b, out_b, _ = self.drive(name="b")
        self.assertEqual((out_a / "events.jsonl").read_bytes(), (out_b / "events.jsonl").read_bytes())
        self.assertEqual(report.build_report(a, strategy()), report.build_report(b, strategy()))

    def test_the_real_exit_follows_the_holdings_not_a_script(self):
        flat_after = self.fixtures(closes=RISING + [("2031-03-10", "30000.00")])                          # the trend persists: hold, no exit
        result, _, _ = self.drive(fixtures=flat_after)
        self.assertEqual([(d.outcome, d.evaluation.reason_code) for d in result.decisions], [("SUBMITTED", "TREND_UP"), ("NO_SIGNAL", "HOLD")])
        self.assertEqual(len(result.positions), 1)

    def test_callers_that_do_not_use_a_strategy_are_unchanged(self):
        text = (ROOT / "src" / "niftypaper" / "historical" / "replay.py").read_text(encoding="utf-8")
        self.assertNotIn("niftypaper.strategy", text)
        self.assertNotIn("..strategy", text)
        cfg = replay_config(self.fixtures(), self.tmp / "plain" / "state")
        runner = replay.ReplayRunner.open(cfg)
        self.addCleanup(runner.close)
        self.assertNotIn("plan", runner.inputs)


if __name__ == "__main__":
    unittest.main()
