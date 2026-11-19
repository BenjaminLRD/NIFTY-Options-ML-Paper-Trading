"""Read-only strategy evaluation. Invented data in temporary folders; the committed fixtures are never touched. Every figure is simulated: a profitable example
here shows the arithmetic works, not that anything is profitable."""
import contextlib
import dataclasses
import hashlib
import importlib.util
import io
import json
import pathlib
import re
import shutil
import unittest
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from unittest import mock

from niftypaper.evaluation import evaluate as ev
from niftypaper.historical import anchor, audit, eventchain, identity, replay
from tests import ROOT
from tests.historical_fixtures import CE
from tests.strategy_fixtures import BASE_QUOTES, FALLING_AFTER, FLAT, RISING, T1, T2, at, build, q, replay_config, strategy
from tests.test_historical_chain import dump, lines_of
from tests.test_strategy_execution import Base

CENT = Decimal("0.01")
AS_OF_FOR_MARKS = at(13, 12, 0)
LOSING_QUOTES = [BASE_QUOTES[0], BASE_QUOTES[1], q("CE", 11, 10, 15, "90.00", "90.20"), BASE_QUOTES[3]]


def money(x):
    return Decimal(x).quantize(CENT, ROUND_HALF_UP)


def fee(turnover):
    return (Decimal("20") + Decimal("0.0005") * turnover).quantize(CENT, ROUND_HALF_UP)


def snapshot(*roots):
    seen = {}
    for root in roots:
        root = Path(root)
        for path in sorted(root.rglob("*")):
            if path.is_file():
                seen[str(path)] = (path.read_bytes(), path.stat().st_mtime_ns)
    return seen


def load_cli():
    spec = importlib.util.spec_from_file_location("strategy_evaluate_cli", ROOT / "tools" / "strategy_evaluate.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool


class EvalBase(Base):
    STATE = "replay_synthetic_fixture.jsonl"

    def config(self, out, fixtures, **kw):
        return audit.AuditConfig(out / "events.jsonl", out / "state" / self.STATE, fixtures, **kw)

    def evaluate(self, out, fixtures, **kw):
        return ev.evaluate(self.config(out, fixtures, **kw))

    def scripted(self, name, intents, quotes, until=None):
        """A replay of a hand-written script (declared as its plan) on invented quotes; returns (out folder, fixtures)."""
        fixtures = self.fixtures(quotes=quotes)
        out = self.tmp / name
        runner = replay.ReplayRunner.open(replay_config(fixtures, out / "state"), intents=intents, until=until)
        try:
            runner.run(intents, until=until)
            if until is not None:
                runner.advance_to(until)                       # the clock moves on with nothing happening, so older quotes stop being usable marks
            runner.write_events(out / "events.jsonl")
        finally:
            runner.close()
        return out, fixtures

    def refused_with(self, report, text):
        self.assertEqual(report["status"], "REFUSED", report.get("refusal_reasons"))
        self.assertIn(text, " | ".join(report["refusal_reasons"]))
        self.assertEqual((report["metrics"], report["baseline"]), ({"unavailable": "EVALUATION_REFUSED"},) * 2)


def trade_script():
    """A win then a loss, each a whole round trip of one lot, on invented quotes (see QUOTES below)."""
    return [replay.Intent(at(10, 10, 15, 1), CE, "BUY", 1), replay.Intent(at(10, 10, 15, 4), CE, "SELL", 1),
            replay.Intent(at(10, 10, 20, 1), CE, "BUY", 1), replay.Intent(at(10, 10, 20, 4), CE, "SELL", 1)]


QUOTES = [q("CE", 10, 10, 15, "100.00", "100.20"), q("CE", 10, 10, 15, "120.00", "120.20", s=3), q("CE", 10, 10, 20, "100.00", "100.20"), q("CE", 10, 10, 20, "90.00", "90.20", s=3)]


class TestEvaluatedReports(EvalBase):
    def test_a_profitable_simulated_entry_and_exit(self):
        result, out, fixtures = self.drive()
        r = self.evaluate(out, fixtures)
        m = r["metrics"]
        buy, sell = money("100.25") * 65, money("109.95") * 65
        net = sell - buy - fee(buy) - fee(sell)
        self.assertEqual((r["status"], r["simulated"], r["synthetic_data"]), ("EVALUATED", True, True))
        self.assertEqual((m["equity"]["initial"], m["equity"]["final"], m["equity"]["change"]), ("1000000.00", str(money(1000000) + net), str(net)))
        self.assertEqual((m["pnl"]["realized_net"], m["pnl"]["gross_realized_before_fees"], m["costs"]["fees_total"]), (str(net), str(sell - buy), str(fee(buy) + fee(sell))))
        self.assertEqual((m["trades"]["completed"], m["trades"]["open"], m["trades"]["winning"], m["trades"]["losing"], m["trades"]["breakeven"]), (1, 0, 1, 0, 0))
        self.assertEqual(m["trades"]["win_rate"], {"value": "1.000000", "wins": 1, "denominator": 1, "denominator_meaning": "completed trades"})
        self.assertEqual(m["trades"]["profit_factor"], {"unavailable": "NO_LOSING_TRADES"})
        self.assertEqual(m["trades"]["average_loss"], {"unavailable": "NO_LOSING_TRADES"})
        self.assertEqual(r["baseline"]["strategy_minus_baseline"], str(net))
        self.assertIn("not evidence", r["notice"])
        self.assertIn("SIMULATED", r["notice"])
        self.assertEqual(m["costs"]["modeled_slippage_total"], "6.50")                              # 2 fills x 65 units x 0.05, informational

    def test_a_losing_simulated_example(self):
        result, out, fixtures = self.drive(fixtures=self.fixtures(quotes=LOSING_QUOTES))
        m = self.evaluate(out, fixtures)["metrics"]
        self.assertLess(Decimal(m["pnl"]["realized_net"]), 0)
        self.assertEqual((m["trades"]["winning"], m["trades"]["losing"], m["trades"]["win_rate"]["value"], m["trades"]["profit_factor"]), (0, 1, "0.000000", "0.000000"))
        self.assertEqual(m["trades"]["average_win"], {"unavailable": "NO_WINNING_TRADES"})
        self.assertEqual(m["equity"]["change"], m["pnl"]["realized_net"])

    def test_two_round_trips_give_the_defined_ratios_and_a_correct_drawdown(self):
        out, fixtures = self.scripted("two", trade_script(), QUOTES)
        r = self.evaluate(out, fixtures)
        m = r["metrics"]
        b1, s1 = money("100.25") * 65, money("119.95") * 65
        b2, s2 = money("100.25") * 65, money("89.95") * 65
        win, loss = s1 - b1 - fee(b1) - fee(s1), s2 - b2 - fee(b2) - fee(s2)
        self.assertGreater(win, 0)
        self.assertLess(loss, 0)
        t = m["trades"]
        self.assertEqual((t["completed"], t["winning"], t["losing"], t["win_rate"]["value"], t["win_rate"]["denominator"]), (2, 1, 1, "0.500000", 2))
        self.assertEqual((t["average_win"], t["average_loss"]), (str(win), str(loss)))
        self.assertEqual(t["profit_factor"], format((win / abs(loss)).quantize(Decimal("0.000001"), ROUND_HALF_UP), "f"))
        self.assertEqual(m["pnl"]["realized_net"], str(win + loss))
        # equity points: open, after buy 1, after sell 1, after buy 2, after sell 2, checkpoint. Buys are marked at the bid, so a buy costs spread + slippage + fee at once.
        start = Decimal("1000000")
        after_buy1 = start - fee(b1) - (b1 - money("100.00") * 65)
        after_sell1 = start + win
        after_buy2 = after_sell1 - fee(b2) - (b2 - money("100.00") * 65)
        after_sell2 = after_sell1 + loss
        curve = [Decimal(p["equity"]) for p in m["equity_curve"]]
        self.assertEqual(curve, [money(start), money(after_buy1), money(after_sell1), money(after_buy2), money(after_sell2), money(after_sell2)])
        peak, worst = curve[0], Decimal("0")
        for x in curve:
            peak = max(peak, x)
            worst = max(worst, peak - x)
        self.assertEqual(m["drawdown"]["amount"], str(worst))
        self.assertEqual(m["drawdown"]["amount"], str(money(after_sell1) - money(after_sell2)))                    # the biggest fall is the losing trade

    def test_drawdown_function_on_known_curves(self):
        def pts(*values):
            return [{"clock": f"2031-03-10T10:00:0{i}+05:30", "equity": None if v is None else Decimal(v)} for i, v in enumerate(values)]
        self.assertEqual(ev._drawdown(pts("100", "110", "120"))["amount"], "0.00")
        d = ev._drawdown(pts("100", "120", "90", "130", "117"))
        self.assertEqual((d["amount"], d["fraction_of_peak"], d["peak_at"], d["trough_at"]), ("30.00", "0.250000", "2031-03-10T10:00:01+05:30", "2031-03-10T10:00:02+05:30"))
        self.assertEqual(ev._drawdown(pts("100", None, "90")), {"unavailable": "EQUITY_UNAVAILABLE_AT_1_OF_3_POINTS"})

    def test_a_zero_trade_replay_is_a_valid_report_with_unavailable_trade_statistics(self):
        result, out, fixtures = self.drive(fixtures=self.fixtures(closes=FLAT))
        r = self.evaluate(out, fixtures)
        m = r["metrics"]
        self.assertEqual(r["status"], "EVALUATED")
        self.assertEqual((m["trades"]["completed"], m["trades"]["open"], m["intents"]["recorded"], m["equity"]["change"], m["drawdown"]["amount"]),
                         (0, 0, 0, "0.00", "0.00"))
        none = {"unavailable": "NO_COMPLETED_TRADES"}
        self.assertEqual((m["trades"]["win_rate"], m["trades"]["average_win"], m["trades"]["profit_factor"], m["trades"]["winning"]), (none,) * 4)
        self.assertEqual((m["costs"]["fees_total"], m["costs"]["modeled_slippage_total"], m["pnl"]["gross_realized_before_fees"]), ({"unavailable": "NO_FILLS"},) * 3)
        self.assertEqual(r["baseline"]["strategy_minus_baseline"], "0.00")
        self.assertEqual(m["signals"], {"generated": {"unavailable": "NOT_RECORDED_IN_EVENT_LOG"}, "no_signal_decisions": {"unavailable": "NOT_RECORDED_IN_EVENT_LOG"}})

    def test_rejected_and_refused_intents_are_grouped_by_reason(self):
        result, out, fixtures = self.drive(strat=strategy(lots=3))
        i = self.evaluate(out, fixtures)["metrics"]["intents"]
        self.assertEqual((i["recorded"], i["accepted"], i["rejected"]), (2, 0, 2))
        self.assertEqual(i["rejected_by_reason"], [{"stage": "ENGINE", "code": "RISK_MAX_LOTS", "count": 2}])
        self.assertEqual((i["rejected_by_category"], i["data_eligibility_failures"]), ({"RISK_OR_ACCOUNT_LIMIT": 2}, 0))
        bridge = self.scripted("bridge", [replay.Intent(at(10, 10, 15, 1), "NIFTY", "BUY", 1), replay.Intent(at(10, 10, 15, 2), CE, "BUY", 1)], QUOTES[:1])
        j = self.evaluate(*bridge)["metrics"]["intents"]
        self.assertEqual((j["recorded"], j["accepted"], j["rejected"], j["data_eligibility_failures"]), (2, 1, 1, 1))
        self.assertEqual(j["rejected_by_reason"], [{"stage": "BRIDGE", "code": "NO_VISIBLE_QUOTE", "count": 1}])
        self.assertEqual(j["rejected_by_category"], {"DATA_ELIGIBILITY": 1})

    def test_an_open_position_with_a_usable_mark(self):
        result, out, fixtures = self.drive(schedule=(T1,))
        r = self.evaluate(out, fixtures)
        m = r["metrics"]
        buy = money("100.25") * 65
        self.assertEqual((m["trades"]["completed"], m["trades"]["open"]), (0, 1))
        self.assertEqual(m["pnl"]["unrealized"], str(money("100.00") * 65 - buy))                                  # marked at the bid
        self.assertEqual(m["costs"]["fees_carried_by_open_positions"], str(fee(buy)))
        self.assertEqual(m["equity"]["final"], str(money(1000000) + money("100.00") * 65 - buy - fee(buy)))
        self.assertEqual(m["trades"]["win_rate"], {"unavailable": "NO_COMPLETED_TRADES"})
        self.assertEqual(m["trades"]["open_list"][0]["realized_so_far"], "0.00")

    def test_an_open_position_without_a_usable_mark_leaves_equity_unavailable_not_zero(self):
        out, fixtures = self.scripted("stale", [replay.Intent(at(10, 10, 15, 1), CE, "BUY", 1)], QUOTES[:1], until=at(10, 10, 16, 30))     # the quote is 90 s old at the end
        r = self.evaluate(out, fixtures)
        m = r["metrics"]
        self.assertEqual(r["status"], "EVALUATED")
        self.assertEqual(m["pnl"]["unrealized"]["unavailable"], "NO_USABLE_MARK")
        self.assertEqual(m["equity"]["final"], {"unavailable": "OPEN_POSITION_HAS_NO_USABLE_MARK"})
        self.assertEqual(m["drawdown"]["unavailable"], "EQUITY_UNAVAILABLE_AT_1_OF_3_POINTS")
        self.assertEqual(r["baseline"]["strategy_minus_baseline"], {"unavailable": "FINAL_EQUITY_UNAVAILABLE"})
        self.assertEqual(m["pnl"]["realized_net"], "0.00")                                                          # a realised zero IS established

    def test_partial_exits_allocate_buy_fees_in_proportion(self):
        script = [replay.Intent(at(10, 10, 15, 1), CE, "BUY", 2), replay.Intent(at(10, 10, 15, 3), CE, "SELL", 1)]
        out, fixtures = self.scripted("partial", script, QUOTES[:2])
        m = self.evaluate(out, fixtures)["metrics"]
        buy, sell = money("100.25") * 130, money("119.95") * 65
        buy_fee = fee(buy)
        half = (buy_fee / 2).quantize(CENT, ROUND_HALF_UP)
        self.assertEqual((m["trades"]["completed"], m["trades"]["open"]), (0, 1))
        self.assertEqual(m["trades"]["completed_list"], [])
        self.assertEqual(m["pnl"]["realized_on_open_trades"], m["pnl"]["realized_net"])
        self.assertEqual(m["trades"]["open_list"][0]["realized_so_far"], m["pnl"]["realized_net"])
        self.assertEqual(Decimal(m["costs"]["fees_on_realized"]) + Decimal(m["costs"]["fees_carried_by_open_positions"]), Decimal(m["costs"]["fees_total"]))
        self.assertEqual(Decimal(m["costs"]["fees_total"]), buy_fee + fee(sell))
        self.assertEqual(Decimal(m["costs"]["fees_carried_by_open_positions"]), buy_fee - Decimal(m["costs"]["fees_on_realized"]) + fee(sell))
        self.assertAlmostEqual(float(m["costs"]["fees_carried_by_open_positions"]), float(half), places=1)             # roughly half the buy fee stays with the open half
        self.assertEqual(Decimal(m["pnl"]["realized_net"]), sell - money("100.25") * 65 - (buy_fee - Decimal(m["costs"]["fees_carried_by_open_positions"])) - fee(sell))
        self.assertEqual(m["pnl"]["gross_realized_before_fees"], str(sell - money("100.25") * 65))
        final = Decimal(m["equity"]["final"])
        self.assertEqual(final, money(1000000) + Decimal(m["pnl"]["realized_net"]) + Decimal(m["pnl"]["unrealized"]) - Decimal(m["costs"]["fees_carried_by_open_positions"]))

    def test_a_partial_exit_then_the_final_exit_is_one_completed_trade_without_double_counting(self):
        script = [replay.Intent(at(10, 10, 15, 1), CE, "BUY", 2), replay.Intent(at(10, 10, 15, 3), CE, "SELL", 1), replay.Intent(at(10, 10, 15, 4), CE, "SELL", 1)]
        out, fixtures = self.scripted("full", script, QUOTES[:2])
        m = self.evaluate(out, fixtures)["metrics"]
        self.assertEqual((m["trades"]["completed"], m["trades"]["open"], m["trades"]["completed_list"][0]["fills"]), (1, 0, 3))
        self.assertEqual(m["trades"]["completed_list"][0]["net"], m["pnl"]["realized_net"])
        self.assertEqual(m["pnl"]["realized_on_completed_trades"], m["pnl"]["realized_net"])
        self.assertEqual(m["costs"]["fees_total"], m["costs"]["fees_on_realized"])
        self.assertEqual(m["equity"]["change"], m["pnl"]["realized_net"])


class TestInternalGuards(EvalBase):
    def rows_of(self):
        result, out, fixtures = self.drive()
        return audit.build_report(self.config(out, fixtures))

    def test_breakeven_trades_are_neither_wins_nor_losses(self):
        wins, losses, even = ev.classify([Decimal("5"), Decimal("0.00"), Decimal("-1"), Decimal("0")])
        self.assertEqual((wins, losses, even), ([Decimal("5")], [Decimal("-1")], 2))
        self.assertEqual(ev.classify([]), ([], [], 0))

    def test_time_in_market_counts_only_intervals_that_start_with_an_open_position(self):
        def p(clock, open_, premium):
            return {"clock": f"2031-03-10T10:{clock}:00+05:30", "open": open_, "open_premium": Decimal(premium)}
        e = ev._exposure([p("00", False, "0"), p("10", True, "500"), p("30", False, "0"), p("40", False, "0")])
        self.assertEqual((e["time_in_market_seconds"], e["observed_span_seconds"], e["peak_open_premium"], e["fraction_of_observed_span"]), ("1200.0", "2400.0", "500.00", "0.500000"))
        self.assertEqual(ev._exposure([p("00", False, "0")]), {"unavailable": "TOO_FEW_RECORDED_TIMES"})

    def test_the_exposure_numbers_in_a_real_report(self):
        result, out, fixtures = self.drive()
        e = self.evaluate(out, fixtures)["metrics"]["exposure"]
        self.assertEqual((e["time_in_market_seconds"], e["observed_span_seconds"], e["fraction_of_observed_span"]), ("86400.0", "90902.0", "0.950474"))

    def test_rows_that_do_not_add_up_are_never_evaluated(self):
        good = self.rows_of()
        self.assertIsInstance(ev.compute_metrics(json.loads(json.dumps(good))), dict)
        def sale(report):
            return next(r for r in report["rows"] if r["type"] == "FILL" and r["fill"]["side"] == "SELL")
        edits = {"sale realised P&L": lambda r: sale(r)["realized_pnl"].update(this_trade="1.00"),
                 "sale fee": lambda r: sale(r)["fill"].update(fee="1.00"),
                 "account realised total": lambda r: r["summary"].update(rebuilt_realized_total="1.00"),
                 "total fees": lambda r: r["summary"].update(rebuilt_fees_total="1.00")}
        for name, edit in edits.items():
            with self.subTest(name):
                bad = json.loads(json.dumps(good))
                edit(bad)
                with self.assertRaises(ev.EvaluationError):
                    ev.compute_metrics(bad)

    def test_final_equity_must_reconcile_with_its_parts(self):
        result, out, fixtures = self.drive(schedule=(T1,))
        report = audit.build_report(self.config(out, fixtures))
        self.assertIsInstance(ev.compute_metrics(json.loads(json.dumps(report))), dict)
        bad = json.loads(json.dumps(report))
        last = next(r for r in reversed(bad["rows"]) if r["type"] == "CHECKPOINT")
        last["cash_after"] = "1.00"
        with self.assertRaises(ev.EvaluationError):
            ev.compute_metrics(bad)

    def test_a_row_set_that_fails_these_guards_is_reported_as_a_refusal(self):
        from unittest import mock
        result, out, fixtures = self.drive()
        real = audit.build_report

        def doctored(config):
            report = json.loads(json.dumps(real(config)))
            report["summary"]["rebuilt_realized_total"] = "1.00"
            return report

        with mock.patch.object(ev.audit, "build_report", doctored):
            r = ev.evaluate(self.config(out, fixtures))
        self.refused_with(r, "could not be evaluated safely")

    def test_the_restored_trades_rule_stands_on_its_own(self):
        result, out, fixtures = self.drive()
        report = json.loads(json.dumps(audit.build_report(self.config(out, fixtures))))
        self.assertEqual(ev.refusal_reasons(report, "index_sma_trend", "1.0.0"), [])
        report["summary"]["restored_trades"] = 2
        self.assertEqual(ev.refusal_reasons(report, "index_sma_trend", "1.0.0"), ["the account already held trades before this replay started; only fresh accounts are evaluated"])
        report["checks"][0]["status"] = "FAIL"
        self.assertIn("audit checks not PASS", " ".join(ev.refusal_reasons(report, "index_sma_trend", "1.0.0")))


class TestRefusals(EvalBase):
    def good(self):
        return self.drive()

    def test_missing_or_damaged_artifacts_are_refused_with_reasons(self):
        result, out, fixtures = self.good()
        state = out / "state" / self.STATE
        good = state.read_bytes()
        for name, data in (("missing", None), ("empty", b""), ("truncated", good[:-1]), ("garbage", b"\xff\n")):
            with self.subTest(name):
                copy = self.tmp / name / "state"
                copy.mkdir(parents=True)
                if data is not None:
                    (copy / self.STATE).write_bytes(data)
                r = ev.evaluate(audit.AuditConfig(out / "events.jsonl", copy / self.STATE, fixtures))
                self.refused_with(r, "NOT_RECONCILABLE")
        gone = ev.evaluate(audit.AuditConfig(self.tmp / "nope.jsonl", state, fixtures))
        self.refused_with(gone, "event chain is UNAVAILABLE")

    def test_an_inconsistent_accounting_claim_is_refused(self):
        result, out, fixtures = self.good()
        objs = [json.loads(x) for x in lines_of((out / "events.jsonl").read_bytes())]
        objs[-1]["cash"] = "1.00"
        forged = self.tmp / "forged"
        (forged / "state").mkdir(parents=True)
        shutil.copyfile(out / "state" / self.STATE, forged / "state" / self.STATE)
        (forged / "events.jsonl").write_bytes(dump(objs)[0])
        r = ev.evaluate(audit.AuditConfig(forged / "events.jsonl", forged / "state" / self.STATE, fixtures))
        self.refused_with(r, "audit verdict is INCONSISTENT")

    def test_an_invalid_chain_is_refused(self):
        result, out, fixtures = self.good()
        bad = self.tmp / "bad"
        bad.mkdir()
        (bad / "events.jsonl").write_bytes((out / "events.jsonl").read_bytes().replace(b"RECORD_AVAILABLE", b"RECORD_AVAILABLEX", 1))
        r = ev.evaluate(audit.AuditConfig(bad / "events.jsonl", out / "state" / self.STATE, fixtures))
        self.refused_with(r, "event chain is INVALID")

    def test_a_mismatched_anchor_or_head_is_refused_and_a_matching_one_is_recorded(self):
        result, out, fixtures = self.good()
        state = out / "state" / self.STATE
        other, out2, fx2 = self.drive(strat=strategy(lots=3), name="other")
        wrong = self.tmp / "kept" / "wrong.json"
        anchor.create(out2 / "events.jsonl", out2 / "state" / self.STATE, wrong)
        self.refused_with(ev.evaluate(audit.AuditConfig(out / "events.jsonl", state, fixtures, anchor_path=wrong, anchor_trust="independent")), "anchor comparison is MISMATCH")
        self.refused_with(ev.evaluate(audit.AuditConfig(out / "events.jsonl", state, fixtures, expected_head="0" * 64)), "anchor comparison is MISMATCH")
        right = self.tmp / "kept" / "right.json"
        anchor.create(out / "events.jsonl", state, right)
        r = ev.evaluate(audit.AuditConfig(out / "events.jsonl", state, fixtures, anchor_path=right, anchor_trust="independent"))
        self.assertEqual((r["status"], r["evidence"]["anchor_status"], r["evidence"]["anchor_protection"]), ("EVALUATED", "MATCH", "DECLARED_INDEPENDENT"))
        plain = ev.evaluate(audit.AuditConfig(out / "events.jsonl", state, fixtures))
        self.assertEqual((plain["status"], plain["evidence"]["anchor_status"]), ("EVALUATED", "NOT_SUPPLIED"))
        self.assertIn("held where the writer cannot change it", plain["evidence"]["reading"])

    def test_a_missing_plan_and_partial_coverage_are_refused(self):
        fixtures = self.fixtures()
        out = self.tmp / "noplan"
        runner = replay.ReplayRunner.open(replay_config(fixtures, out / "state"), None)
        runner.run([replay.Intent(at(10, 10, 15, 1), CE, "BUY", 1)])
        runner.write_events(out / "events.jsonl")
        runner.close()
        self.refused_with(self.evaluate(out, fixtures), "declared intent plan is NOT_DECLARED")
        result, good_out, good_fx = self.good()
        partial = ev.evaluate(audit.AuditConfig(good_out / "events.jsonl", good_out / "state" / self.STATE, None))
        self.refused_with(partial, "audit coverage is PARTIAL")

    def test_changed_fixtures_and_foreign_state_are_refused(self):
        result, out, fixtures = self.good()
        changed = self.tmp / "changed"
        shutil.copytree(fixtures, changed)
        target = next(changed.glob("SYNTHETIC_index.jsonl"))
        target.write_bytes(target.read_bytes() + b"\n")
        self.refused_with(self.evaluate(out, changed), "input identity")
        other = self.drive(strat=strategy(lots=3), name="slip", slippage_ticks=2)
        r = ev.evaluate(audit.AuditConfig(out / "events.jsonl", other[1] / "state" / self.STATE, fixtures))
        self.refused_with(r, "state pairing is MISMATCH")

    def test_an_unsupported_strategy_is_refused(self):
        result, out, fixtures = self.good()
        r = ev.evaluate(self.config(out, fixtures), "other_strategy", "9")
        self.refused_with(r, "not supported")
        self.refused_with(ev.evaluate(self.config(out, fixtures), "index_sma_trend", "2.0.0"), "not supported")

    def test_an_account_with_earlier_trades_is_not_evaluated_as_a_fresh_run(self):
        result, out, fixtures = self.good()
        second = replay.ReplayRunner.open(replay_config(fixtures, out / "state", start=at(11, 12, 0), end=at(11, 15, 30)), intents=[])
        second.write_events(out / "events2.jsonl")
        second.close()
        r = ev.evaluate(audit.AuditConfig(out / "events2.jsonl", out / "state" / self.STATE, fixtures))
        self.assertEqual(r["status"], "REFUSED")
        self.assertTrue(any("already held trades" in x or "audit" in x for x in r["refusal_reasons"]), r["refusal_reasons"])

    def test_a_refusal_still_ties_the_report_to_what_was_checked(self):
        result, out, fixtures = self.good()
        r = ev.evaluate(audit.AuditConfig(out / "events.jsonl", out / "state" / self.STATE, None))
        self.assertEqual((r["evidence"]["audit_coverage"], r["evidence"]["chain_status"], r["digest"]), ("PARTIAL", "VALID", ev.report_digest(r)))


class TestReproducibilityAndSafety(EvalBase):
    def test_identical_inputs_give_identical_reports_and_digest(self):
        result, out, fixtures = self.drive()
        a, b = self.evaluate(out, fixtures), self.evaluate(out, fixtures)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        self.assertRegex(a["digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(a["digest"], ev.report_digest(a))
        tampered = json.loads(json.dumps(a))
        tampered["metrics"]["pnl"]["realized_net"] = "999999.00"
        self.assertNotEqual(ev.report_digest(tampered), a["digest"])
        again, out2, _ = self.drive(name="second")
        self.assertEqual(a["digest"], self.evaluate(out2, fixtures)["digest"])                                  # a different folder, byte-identical logs: same digest

    def test_the_report_ties_to_identity_plan_log_and_audit_without_paths(self):
        result, out, fixtures = self.drive()
        r = self.evaluate(out, fixtures)
        raw = json.dumps(r)
        self.assertNotIn(str(self.tmp), raw)
        self.assertNotIn(str(self.tmp).replace("\\", "/"), raw)
        e, ident = r["evidence"], r["identity"]
        head = json.loads(lines_of((out / "events.jsonl").read_bytes())[-1])["hash"]
        self.assertEqual((e["chain_head"], e["audit_verdict"], e["audit_coverage"], e["plan"], e["pairing"]), (head, "CONSISTENT", "FULL", "MATCH", "PAIRED"))
        self.assertEqual((ident["plan_digest"], ident["input_identity_digest"], ident["source_id"], ident["account"]),
                         (result.plan_digest, result.identity_digest, "synthetic_fixture", "replay_synthetic_fixture"))
        self.assertEqual(ident["window"]["start"], "2031-03-10T09:00:00+05:30")
        self.assertEqual(e["events_file"]["sha256"], __import__("hashlib").sha256((out / "events.jsonl").read_bytes()).hexdigest())
        self.assertEqual(r["strategy"], {"name": "index_sma_trend", "version": "1.0.0", "declared_by": "caller (not recorded in the replay)"})

    def test_labels_limitations_and_the_baseline_are_explicit(self):
        result, out, fixtures = self.drive()
        r = self.evaluate(out, fixtures)
        text = " ".join(r["limitations"])
        for needle in ("not recorded in the replay's events", "no-trade account", "No buy-and-hold", "Nothing here is evidence from real"):
            self.assertIn(needle, text)
        self.assertEqual(r["baseline"]["kind"], "NO_TRADE")
        self.assertEqual(r["baseline"]["benchmark"], {"unavailable": "NO_COMPARABLE_BENCHMARK_IN_SAVED_ARTIFACTS"})
        self.assertEqual(r["baseline"]["same_window"], r["identity"]["window"])
        self.assertNotIn("profitable", r["notice"].replace("not evidence that the strategy is, or would be, profitable", ""))

    def test_evaluating_changes_no_input_and_creates_nothing(self):
        result, out, fixtures = self.drive()
        before = snapshot(out, fixtures)
        self.evaluate(out, fixtures)
        self.evaluate(out, fixtures, expected_head="0" * 64)
        self.assertEqual(snapshot(out, fixtures), before)

    def test_the_evaluator_is_separate_from_signals_replay_and_accounting(self):
        text = (ROOT / "src" / "niftypaper" / "evaluation" / "evaluate.py").read_text(encoding="utf-8")
        imports = " ".join(re.findall(r"^(?:from|import) .*$", text, re.M))
        for banned in ("strategy", "replay", "paper", "bridge", "ingest", "gate"):
            self.assertNotIn(banned, imports)
        for banned in ("open(", "write_", "unlink", "mkdir", "rename"):
            self.assertNotIn(banned, text)


class TestCli(EvalBase):
    def setUp(self):
        super().setUp()
        self.cli = load_cli()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def args(self, out, fixtures, *extra):
        return ["--events", str(out / "events.jsonl"), "--state", str(out / "state" / self.STATE), "--fixtures", str(fixtures), *extra]

    def test_stdout_json_and_exit_codes(self):
        result, out, fixtures = self.drive(name="my run")                                                          # a path with a space
        before = snapshot(out, fixtures)
        code, text, err = self.run_cli(*self.args(out, fixtures))
        report = json.loads(text)
        self.assertEqual((code, report["status"], report["digest"]), (0, "EVALUATED", self.evaluate(out, fixtures)["digest"]))
        self.assertIn("EVALUATED (SIMULATED)", err)
        code, text, err = self.run_cli(*self.args(out, fixtures, "--expected-head", "0" * 64))
        self.assertEqual((code, json.loads(text)["status"]), (1, "REFUSED"))
        self.assertEqual(snapshot(out, fixtures), before)

    def test_out_writes_a_new_file_and_never_overwrites(self):
        result, out, fixtures = self.drive()
        target = self.tmp / "reports dir" / "eval.json"
        target.parent.mkdir()
        code, text, err = self.run_cli(*self.args(out, fixtures, "--out", str(target)))
        self.assertEqual((code, json.loads(target.read_text(encoding="utf-8"))["status"]), (0, "EVALUATED"))
        self.assertIn("report digest", text)
        first = target.read_bytes()
        code, text, err = self.run_cli(*self.args(out, fixtures, "--out", str(target)))
        self.assertEqual((code, target.read_bytes()), (2, first))
        self.assertIn("refusing to overwrite", err)

    def test_out_inside_an_input_is_refused(self):
        result, out, fixtures = self.drive()
        before = snapshot(out, fixtures)
        for bad in (out / "events.jsonl", fixtures / "report.json"):
            code, _, err = self.run_cli(*self.args(out, fixtures, "--out", str(bad)))
            self.assertEqual(code, 2)
        self.assertEqual(snapshot(out, fixtures), before)

    def test_a_refusal_prints_the_reasons_and_no_metrics(self):
        result, out, fixtures = self.drive()
        target = self.tmp / "refused.json"
        code, text, err = self.run_cli(*self.args(out, fixtures, "--anchor-trust", "local", "--expected-head", "1" * 64, "--out", str(target)))
        self.assertEqual(code, 1)
        self.assertIn("REFUSED", text)
        self.assertIn("anchor comparison is MISMATCH", text)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["metrics"], {"unavailable": "EVALUATION_REFUSED"})


class TestMutationRegressions(EvalBase):
    """Each test pins behaviour that an independent mutation pass showed was not protected directly (see docs/STRATEGY_EVALUATION.md section 7)."""

    # ---- valuation quotes: which quote may mark an open position
    def marked(self, name, extra, until):
        quotes = [q("CE", 10, 10, 15, "100.00", "100.20")] + extra
        out, fixtures = self.scripted(name, [replay.Intent(at(10, 10, 15, 1), CE, "BUY", 1)], quotes, until=until)
        r = self.evaluate(out, fixtures)
        self.assertEqual(r["status"], "EVALUATED", r["refusal_reasons"])
        return r["metrics"]

    BID_100_MARK = str(money("100.00") * 65 - money("100.25") * 65)                                                   # -16.25

    def test_a_quote_that_is_not_available_yet_never_marks_a_position(self):
        m = self.marked("future", [q("CE", 10, 10, 15, "200.00", "200.20", s=30)], at(10, 10, 15, 2))
        self.assertEqual(m["pnl"]["unrealized"], self.BID_100_MARK)

    def test_a_quote_for_another_contract_never_marks_a_position(self):
        m = self.marked("wrong_contract", [q("PE", 10, 10, 15, "60.00", "60.25", s=1)], at(10, 10, 15, 2))
        self.assertEqual(m["pnl"]["unrealized"], self.BID_100_MARK)

    def test_a_newest_quote_that_is_not_fill_eligible_leaves_the_mark_unavailable(self):
        m = self.marked("not_fill_eligible", [q("CE", 10, 10, 15, "150.00", "150.20", s=2, time_semantics="RECEIVE_TIME")], at(10, 10, 15, 3))
        self.assertEqual(m["pnl"]["unrealized"], {"unavailable": "NO_USABLE_MARK"})
        self.assertEqual(m["equity"]["final"], {"unavailable": "OPEN_POSITION_HAS_NO_USABLE_MARK"})

    def test_a_crossed_quote_is_excluded_and_the_last_good_quote_marks(self):
        m = self.marked("crossed", [q("CE", 10, 10, 15, "300.00", "100.20", s=2)], at(10, 10, 15, 3))
        self.assertEqual(m["pnl"]["unrealized"], self.BID_100_MARK)

    def test_the_newest_usable_quote_marks_not_the_oldest(self):
        m = self.marked("newest", [q("CE", 10, 10, 15, "110.00", "110.20", s=2)], at(10, 10, 15, 3))
        self.assertEqual(m["pnl"]["unrealized"], str(money("110.00") * 65 - money("100.25") * 65))

    def test_a_mark_is_usable_at_exactly_the_quote_age_limit_and_not_a_moment_later(self):
        exactly = self.marked("age_exact", [], at(10, 10, 15, 5))                                                      # the quote is exactly 5 s old: allowed
        self.assertEqual(exactly["pnl"]["unrealized"], self.BID_100_MARK)
        later = self.marked("age_over", [], at(10, 10, 15, 5).replace(microsecond=1000))                                # 5.001 s: stale
        self.assertEqual(later["pnl"]["unrealized"], {"unavailable": "NO_USABLE_MARK"})

    # ---- reconciliation guards that must fire even when the final mark is unavailable
    def unmarked_report(self):
        script = [replay.Intent(at(10, 10, 15, 1), CE, "BUY", 2), replay.Intent(at(10, 10, 15, 3), CE, "SELL", 1)]
        out, fixtures = self.scripted("unmarked", script, QUOTES[:2], until=at(10, 10, 16, 30))
        report = audit.build_report(self.config(out, fixtures))
        self.assertEqual(report["verdict"], "CONSISTENT")
        self.assertEqual(next(r for r in reversed(report["rows"]) if r["type"] == "CHECKPOINT")["unrealized_pnl"]["status"], "UNAVAILABLE")
        return report

    def test_with_no_final_mark_the_trade_and_account_totals_must_still_agree(self):
        good = self.unmarked_report()
        metrics = ev.compute_metrics(json.loads(json.dumps(good)))
        self.assertEqual(metrics["equity"]["final"], {"unavailable": "OPEN_POSITION_HAS_NO_USABLE_MARK"})
        edits = {"realised total": (lambda r: r["summary"].update(rebuilt_realized_total="1.00"), "trade-level realised"),
                 "total fees": (lambda r: r["summary"].update(rebuilt_fees_total="1.00"), "fee allocation"),
                 "fees carried by the open position": (lambda r: next(x for x in reversed(r["rows"]) if x["type"] == "CHECKPOINT")["positions_after"][CE].update(buy_fees="999.00"),
                                                      "fee allocation")}
        for name, (edit, text) in edits.items():
            with self.subTest(name):
                bad = json.loads(json.dumps(good))
                edit(bad)
                with self.assertRaises(ev.EvaluationError) as caught:
                    ev.compute_metrics(bad)
                self.assertIn(text, str(caught.exception))

    # ---- metric details
    def test_average_holding_time_is_over_completed_trades(self):
        out, fixtures = self.scripted("hold", trade_script(), QUOTES)
        t = self.evaluate(out, fixtures)["metrics"]["trades"]
        self.assertEqual((t["completed"], t["average_holding_seconds"]), (2, "3.0"))
        self.assertEqual([x["opened_at"][11:19] for x in t["completed_list"]], ["10:15:01", "10:20:01"])

    def test_drawdown_keeps_the_first_time_a_peak_or_a_worst_fall_is_reached(self):
        def pts(*values):
            return [{"clock": f"2031-03-10T10:00:0{i}+05:30", "equity": Decimal(v)} for i, v in enumerate(values)]
        d = ev._drawdown(pts("100", "120", "110", "120", "100"))                                 # the peak value 120 is first reached at :01 and again at :03
        self.assertEqual((d["amount"], d["peak_at"], d["trough_at"]), ("20.00", "2031-03-10T10:00:01+05:30", "2031-03-10T10:00:04+05:30"))
        tie = ev._drawdown(pts("100", "90", "100", "90"))                                        # two equal falls: the first one is reported
        self.assertEqual((tie["amount"], tie["trough_at"]), ("10.00", "2031-03-10T10:00:01+05:30"))

    def test_the_no_trade_baseline_is_the_starting_balance_on_the_same_window(self):
        result, out, fixtures = self.drive(fixtures=self.fixtures(closes=FLAT))
        b = self.evaluate(out, fixtures)["baseline"]
        self.assertEqual((b["starting_equity"], b["final_equity"], b["kind"]), ("1000000.00", "1000000.00", "NO_TRADE"))
        self.assertIn("no interest", b["valuation"])

    # ---- serialisation and digest
    def test_the_mark_function_applies_each_quote_rule_on_its_own(self):
        """audit._mark directly, so a rule that another rule happens to duplicate on today's fixtures is still pinned."""
        from niftypaper.historical import ingest as ingest_module
        from niftypaper.historical import manifest as mf
        fixtures_dir = self.fixtures(quotes=[q("CE", 10, 10, 15, "100.00", "100.20"), q("CE", 10, 10, 15, "120.00", "120.20", s=30)])
        entries = mf.load_manifest(fixtures_dir / mf.MANIFEST_NAME)
        records = ingest_module.ingest(fixtures_dir, entries, as_of=AS_OF_FOR_MARKS).records
        fixtures = {"records": records}
        age = timedelta(seconds=5)
        self.assertEqual(audit._mark(fixtures, CE, at(10, 10, 15, 2), age), (Decimal("100.00"), None))
        self.assertEqual(audit._mark(fixtures, CE, at(10, 10, 14, 59), age), (None, "NO_VISIBLE_QUOTE"))                  # nothing is visible before the first quote
        self.assertEqual(audit._mark(fixtures, "OTHER", at(10, 10, 15, 2), age), (None, "NO_VISIBLE_QUOTE"))               # and a quote for another contract is never used
        self.assertEqual(audit._mark(fixtures, CE, at(10, 10, 15, 5), age), (Decimal("100.00"), None))                    # exactly at the age limit
        self.assertEqual(audit._mark(fixtures, CE, at(10, 10, 15, 6), age), (None, "MARK_STALE"))
        self.assertEqual(audit._mark(fixtures, CE, at(10, 10, 15, 30), timedelta(seconds=5))[0], Decimal("120.00"))      # once it is available the newer quote is the mark
        foreign = [dataclasses.replace(r, origin_class="UNVERIFIED_SOURCE") if r.kind == "QUOTE_SNAPSHOT" else r for r in records]
        self.assertEqual(audit._mark({"records": foreign}, CE, at(10, 10, 15, 2), age), (None, "NEWEST_QUOTE_NOT_USABLE"))      # another source is not a valuation quote
        late = [dataclasses.replace(r, observed_at=at(10, 10, 15, 0), available_at=at(10, 10, 15, 3)) if r.time_key.endswith("10:15:00+05:30") else r for r in records]
        self.assertEqual(audit._mark({"records": late}, CE, at(10, 10, 15, 2), age), (None, "NO_VISIBLE_QUOTE"))              # observed earlier but not available yet
        hand_made = [dataclasses.replace(r, observed_at=at(10, 10, 15, 3), available_at=at(10, 10, 15, 0)) if r.time_key.endswith("10:15:00+05:30") else r for r in records]
        self.assertEqual(audit._mark({"records": hand_made}, CE, at(10, 10, 15, 2), age), (None, "NO_VISIBLE_QUOTE"))        # observed in the future of the mark time, whatever it claims

    def test_the_digest_format_is_pinned_by_an_independently_computed_answer(self):
        body = {"b": ["x", 1], "a": {"z": "1", "y": True}, "digest": "ignored"}
        canonical = json.dumps({k: v for k, v in body.items() if k != "digest"}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        expected = hashlib.sha256(b"niftypaper.strategy.evaluation/1\n" + canonical.encode("ascii")).hexdigest()
        self.assertEqual(ev.report_digest(body), expected)
        self.assertEqual(expected, "eb4c191f924821e7e906cc95bcac7048ceb13e3b7c1b79afa13c148f3ed2ea9e")

    def cli_stdout(self, out, fixtures):
        cli = load_cli()
        buffer, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(err):
            code = cli.main(["--events", str(out / "events.jsonl"), "--state", str(out / "state" / self.STATE), "--fixtures", str(fixtures)])
        return code, buffer.getvalue()

    def test_cli_output_is_byte_exact_canonical_json(self):
        result, out, fixtures = self.drive()
        code, text = self.cli_stdout(out, fixtures)
        self.assertEqual(code, 0)
        self.assertEqual(text, json.dumps(json.loads(text), sort_keys=True, indent=2) + "\n")
        self.assertEqual(self.cli_stdout(out, fixtures), (0, text))

    def test_an_output_file_created_between_the_check_and_the_create_is_never_replaced(self):
        result, out, fixtures = self.drive()
        target = (self.tmp / "race.json").resolve()
        target.write_bytes(b"someone else's file")
        real_exists, calls = pathlib.Path.exists, []

        def exists(path, *a, **k):
            if str(path) == str(target) and not calls:
                calls.append(1)
                return False                                                                                         # the pre-check sees nothing; the file appears "just after"
            return real_exists(path, *a, **k)

        cli = load_cli()
        err = io.StringIO()
        with mock.patch.object(pathlib.Path, "exists", exists), mock.patch.object(pathlib.Path, "is_symlink", lambda path: False), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = cli.main(["--events", str(out / "events.jsonl"), "--state", str(out / "state" / self.STATE), "--fixtures", str(fixtures), "--out", str(target)])
        self.assertEqual((code, target.read_bytes()), (2, b"someone else's file"))
        self.assertIn("cannot write the report", err.getvalue())

    # ---- refusals that must come from the specific check, not from a neighbour
    def forged(self, out, name, edit):
        objs = [json.loads(x) for x in lines_of((out / "events.jsonl").read_bytes())]
        edit(objs)
        home = self.tmp / name
        (home / "state").mkdir(parents=True)
        shutil.copyfile(out / "state" / self.STATE, home / "state" / self.STATE)
        (home / "events.jsonl").write_bytes(dump(objs)[0])
        return home

    def resign(self, claim):
        plan = claim["plan"]
        plan["digest"] = identity.plan_digest({k: plan[k] for k in ("version", "intents", "until")})
        claim["digest"] = identity.full_digest(claim)

    def test_a_plan_that_disagrees_with_the_recorded_intents_is_refused(self):
        out, fixtures = self.scripted("planned", trade_script(), QUOTES)
        self.assertEqual(self.evaluate(out, fixtures)["status"], "EVALUATED")

        def extra(objs):
            objs[0]["inputs"]["plan"]["intents"].append({"at": "2031-03-10T05:00:00+00:00", "contract": CE, "side": "BUY", "lots": 1})
            self.resign(objs[0]["inputs"])

        def fewer(objs):
            objs[0]["inputs"]["plan"]["intents"].pop()
            self.resign(objs[0]["inputs"])

        def reordered(objs):
            i = objs[0]["inputs"]["plan"]["intents"]
            i[0], i[3] = i[3], i[0]
            self.resign(objs[0]["inputs"])

        def other_lots(objs):
            objs[0]["inputs"]["plan"]["intents"][1]["lots"] = 2
            self.resign(objs[0]["inputs"])

        def digest_only(objs):
            objs[0]["inputs"]["plan"]["digest"] = "0" * 64

        def shifted(objs):
            objs[0]["inputs"]["plan"]["intents"][1]["at"] = "2031-03-10T04:45:05+00:00"          # a second later than the intent was actually received; still in order
            self.resign(objs[0]["inputs"])

        for name, edit in (("extra", extra), ("fewer", fewer), ("reordered", reordered), ("other_lots", other_lots), ("digest_only", digest_only), ("shifted_instant", shifted)):
            with self.subTest(name):
                home = self.forged(out, "plan_" + name, edit)
                r = ev.evaluate(audit.AuditConfig(home / "events.jsonl", home / "state" / self.STATE, fixtures))
                self.refused_with(r, "declared intent plan is MISMATCH")
        home = self.forged(out, "plan_digest_shown", digest_only)
        r = ev.evaluate(audit.AuditConfig(home / "events.jsonl", home / "state" / self.STATE, fixtures))
        self.assertEqual(r["identity"]["plan_digest"], "0" * 64)                                                 # the refusal shows the digest the log claimed

    def test_an_intent_executed_outside_the_declared_plan_is_refused(self):
        fixtures = self.fixtures(quotes=QUOTES)
        out = self.tmp / "outside"
        script = trade_script()[:2]
        runner = replay.ReplayRunner.open(replay_config(fixtures, out / "state"), intents=script)
        try:
            runner.run(script)
            runner.try_trade(replay.Intent(at(10, 10, 15, 4), CE, "BUY", 1))
            runner.write_events(out / "events.jsonl")
        finally:
            runner.close()
        self.refused_with(self.evaluate(out, fixtures), "declared intent plan is MISMATCH")

    def test_the_chain_link_sequence_and_hash_checks_each_refuse_on_their_own(self):
        out, fixtures = self.scripted("chained", trade_script(), QUOTES)
        raw = lines_of((out / "events.jsonl").read_bytes())

        def evaluate_lines(name, lines):
            home = self.tmp / name
            (home / "state").mkdir(parents=True)
            shutil.copyfile(out / "state" / self.STATE, home / "state" / self.STATE)
            (home / "events.jsonl").write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
            return ev.evaluate(audit.AuditConfig(home / "events.jsonl", home / "state" / self.STATE, fixtures))

        broken_link = list(raw)                                                         # event 4 edited and re-hashed on its own: event 5 no longer points at it
        obj = json.loads(broken_link[3])
        obj["key"] = "EDITED"
        body = {k: v for k, v in obj.items() if k not in eventchain.RESERVED}
        broken_link[3] = eventchain.line(eventchain.seal(body, obj["prev"])).rstrip("\n")
        r = evaluate_lines("link", broken_link)
        self.refused_with(r, "event chain is INVALID")
        self.assertEqual(r["evidence"]["chain_status"], "INVALID")
        objs = [json.loads(x) for x in raw]
        objs[5]["n"] = 99                                                               # a wrong sequence number, everything re-chained consistently
        r = evaluate_lines("sequence", lines_of(dump(objs)[0]))
        self.refused_with(r, "event chain is INVALID")
        deleted = raw[:4] + raw[5:]
        self.refused_with(evaluate_lines("deleted", deleted), "event chain is INVALID")
        swapped = list(raw)
        swapped[4], swapped[5] = swapped[5], swapped[4]
        self.refused_with(evaluate_lines("swapped", swapped), "event chain is INVALID")
        edited = list(raw)
        edited[3] = edited[3].replace("RECORD_AVAILABLE", "RECORD_AVAILABLEX", 1)
        self.refused_with(evaluate_lines("hash", edited), "event chain is INVALID")

    def test_a_log_cut_cleanly_before_its_end_is_refused_even_though_its_chain_is_valid(self):
        out, fixtures = self.scripted("cut", trade_script(), QUOTES)
        raw = lines_of((out / "events.jsonl").read_bytes())
        home = self.tmp / "cut_home"
        (home / "state").mkdir(parents=True)
        shutil.copyfile(out / "state" / self.STATE, home / "state" / self.STATE)
        (home / "events.jsonl").write_bytes(("\n".join(raw[:-2]) + "\n").encode("utf-8"))
        r = ev.evaluate(audit.AuditConfig(home / "events.jsonl", home / "state" / self.STATE, fixtures))
        self.assertEqual(r["evidence"]["chain_status"], "VALID")
        self.refused_with(r, "audit")


if __name__ == "__main__":
    unittest.main()
