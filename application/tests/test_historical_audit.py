"""Read-only replay reconciliation report (historical/audit.py) on INVENTED fixtures: success, accounting mismatches, corruption, tampering, determinism, isolation."""
import contextlib
import importlib.util
import io
import json
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from niftypaper.historical import audit, ingest, manifest, replay
from niftypaper.paper.engine import ORIGIN_SYNTHETIC, CostModel, MarketView, PaperEngine, TradeRequest
from niftypaper.paper.store import JsonlStore
from tests import ROOT
from tests.historical_fixtures import AS_OF, CE, PE
from tests.test_historical_replay import END, FIXTURE_DIR, START, STATE_FILE, at

SCRIPT = [
    replay.Intent(at(10, 15, 2), PE, "BUY", 1),
    replay.Intent(at(10, 15, 31), CE, "BUY", 2),
    replay.Intent(at(10, 15, 31), PE, "SELL", 1),          # refused: the PE quote is stale
    replay.Intent(at(10, 15, 32), CE, "SELL", 1),          # partial exit
    replay.Intent(at(10, 15, 33), CE, "SELL", 1),          # final exit
    replay.Intent(at(10, 16, 2), CE, "BUY", 1),            # refused by the bridge: the newest quote has a receive time
    replay.Intent(at(10, 20), "NIFTY", "BUY", 1),          # refused: no quote for an index
]


def load_tool():
    spec = importlib.util.spec_from_file_location("replay_audit_tool", ROOT / "tools" / "replay_audit.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def make_run(self, name="run", script=SCRIPT, state_dir=None, start=START, **kw):
        folder = self.tmp / name
        folder.mkdir(exist_ok=True)
        config = replay.ReplayConfig(fixture_dir=FIXTURE_DIR, state_dir=state_dir or folder / "state", as_of=AS_OF, start=start, end=END, permitted=True, **kw)
        runner = replay.ReplayRunner.open(config, intents=script)
        try:
            runner.run(script)
            runner.write_events(folder / "events.jsonl")
            engine = runner.engine
            return type("Run", (), {"events": folder / "events.jsonl", "state": Path(config.state_dir) / STATE_FILE, "cash": engine.cash,
                                    "realized": engine.realized_pnl, "positions": dict(engine.positions)})
        finally:
            runner.close()

    def report(self, run, fixtures=FIXTURE_DIR, **kw):
        return audit.build_report(audit.AuditConfig(run.events, run.state, fixtures, **kw))

    def variant(self, run, name, events=None, state=None):
        """Copies of a run's files with a JSON-level edit (a function on the list of objects) or a byte-level edit applied. The original is untouched."""
        folder = self.tmp / name
        folder.mkdir()
        outputs = []
        for source, fn, target in ((run.events, events, folder / "events.jsonl"), (run.state, state, folder / STATE_FILE)):
            data = source.read_bytes()
            if callable(fn):
                data = fn(data) if getattr(fn, "raw", False) else "".join(json.dumps(o, sort_keys=True) + "\n" for o in fn([json.loads(x) for x in data.decode().splitlines()])).encode()
            target.write_bytes(data)
            outputs.append(target)
        return type("Run", (), {"events": outputs[0], "state": outputs[1]})

    @staticmethod
    def status(report, check):
        return next(c["status"] for c in report["checks"] if c["id"] == check)

    def assertStatus(self, report, check, status):
        self.assertEqual(self.status(report, check), status, [c for c in report["checks"] if c["id"] == check])


def raw(fn):
    fn.raw = True
    return fn


class TestSuccessfulReconciliation(Base):
    def test_a_clean_replay_reconciles_in_full(self):
        run = self.make_run()
        report = self.report(run)
        self.assertEqual((report["verdict"], report["coverage"]), ("CONSISTENT", "FULL"))
        self.assertTrue(all(c["status"] == "PASS" for c in report["checks"]), [c for c in report["checks"] if c["status"] != "PASS"])
        self.assertEqual(report["summary"]["rebuilt_cash"], format(run.cash, "f") if "." in str(run.cash) else str(run.cash) + ".00")
        self.assertEqual(Decimal(report["summary"]["rebuilt_realized_total"]), run.realized)
        self.assertEqual(set(report["summary"]["rebuilt_open_positions"]), set(run.positions))

    def test_the_rebuild_matches_hand_computed_figures(self):
        rows = self.report(self.make_run())["rows"]
        fills = [r for r in rows if r["type"] == "FILL"]
        pe_buy, ce_buy, ce_part, ce_last = fills
        self.assertEqual((pe_buy["fill"]["fill_price"], pe_buy["fill"]["turnover"], pe_buy["fill"]["fee"]), ("60.30", "3919.50", "21.96"))      # 60.25 ask + 0.05 tick; 20 + 0.05%
        self.assertEqual((pe_buy["cash_before"], pe_buy["cash_after"]), ("1000000.00", "996058.54"))
        self.assertEqual((ce_buy["fill"]["fill_price"], ce_buy["fill"]["quantity"], ce_buy["fill"]["turnover"], ce_buy["fill"]["fee"]), ("100.65", 130, "13084.50", "26.54"))
        self.assertEqual(ce_buy["cash_after"], "982947.50")
        self.assertEqual(ce_part["positions_after"][CE]["quantity"], 65)
        self.assertEqual(ce_part["positions_after"][CE]["basis"], "6542.25")                       # half of 13,084.50: the cost basis is preserved
        self.assertEqual(ce_part["positions_after"][CE]["buy_fees"], "13.27")
        # 65 x (100.40 bid - 0.05) = 6,522.75 proceeds, fee 20 + 0.05% = 23.26: 6,522.75 - 6,542.25 - 13.27 - 23.26
        self.assertEqual(ce_part["realized_pnl"]["this_trade"], "-56.03")
        self.assertEqual(ce_part["realized_pnl"]["cumulative"], "-56.03")
        self.assertNotIn(CE, ce_last["positions_after"])
        self.assertEqual((ce_last["realized_pnl"]["this_trade"], ce_last["realized_pnl"]["cumulative"]), ("-56.03", "-112.06"))       # the remainder carries the exact rest of the basis

    def test_every_event_has_a_row_with_the_required_columns(self):
        run = self.make_run()
        report = self.report(run)
        events = [json.loads(line) for line in run.events.read_text().splitlines()]
        self.assertEqual([r["n"] for r in report["rows"]], [e["n"] for e in events])
        self.assertEqual([r["clock"] for r in report["rows"]], [e["clock"] for e in events])
        for row in report["rows"]:
            for column in ("seq", "n", "clock", "type", "source_id", "instrument", "intent", "decision", "quote", "fill", "assumptions", "cash_before", "cash_after",
                           "positions_before", "positions_after", "realized_pnl", "unrealized_pnl", "reason"):
                self.assertIn(column, row)
            self.assertEqual(row["source_id"], "synthetic_fixture")
        fill = next(r for r in report["rows"] if r["type"] == "FILL")
        self.assertEqual(fill["intent"], {"contract": PE, "side": "BUY", "lots": 1})
        self.assertEqual(fill["quote"]["bid"], "60.00")
        self.assertEqual(fill["quote"]["ask"], "60.25")
        self.assertIn("SYNTHETIC_quotes.jsonl#", fill["quote"]["reference"])
        self.assertEqual(fill["assumptions"]["slippage_per_unit"], "0.05")
        self.assertEqual(fill["assumptions"]["slippage_ticks"], "1.00")
        self.assertIn("20 + 0.0005", fill["assumptions"]["fee_model"])
        refused = [r for r in report["rows"] if r["type"] == "INTENT_REFUSED"]
        self.assertEqual([(r["decision"]["stage"], r["decision"]["code"]) for r in refused],
                         [("ENGINE", "BOUNDARY_REJECTED"), ("BRIDGE", "NOT_FILL_ELIGIBLE"), ("BRIDGE", "NO_VISIBLE_QUOTE")])
        self.assertTrue(all(r["reason"] for r in refused))
        self.assertTrue(all(r["cash_before"] == r["cash_after"] for r in refused))

    def test_unavailable_values_are_never_shown_as_zero(self):
        rows = self.report(self.make_run())["rows"]
        first = rows[0]
        self.assertEqual(first["unrealized_pnl"], {"status": "AVAILABLE", "value": "0.00", "note": "no open positions"})       # a real zero
        fresh = next(r for r in rows if r["type"] == "FILL")
        self.assertEqual(fresh["unrealized_pnl"]["status"], "AVAILABLE")                                                      # PE marked 2 s after its quote
        self.assertEqual(fresh["unrealized_pnl"]["value"], "-19.50")                                                          # 60.00 x 65 - 3,919.50
        stale = [r for r in rows if r["clock"].startswith("2031-03-03T10:20")][-1]
        self.assertEqual(stale["unrealized_pnl"], {"status": "UNAVAILABLE", "reason": "NO_USABLE_MARK", "positions": {PE: "MARK_STALE"}})
        for row in rows:
            if row["type"] != "FILL":
                self.assertEqual(row["fill"], {"unavailable": "NOT_A_FILL"})
                self.assertEqual(row["assumptions"], {"unavailable": "NOT_A_FILL"})
                self.assertEqual(row["realized_pnl"]["this_trade"], {"unavailable": "NOT_A_FILL"})
            self.assertNotIn(row["quote"].get("bid"), ("0", "0.00"))
        buy = next(r for r in rows if r["type"] == "FILL")
        self.assertEqual(buy["realized_pnl"]["this_trade"], {"unavailable": "BUY_HAS_NO_REALISED_PNL"})
        self.assertEqual(buy["realized_pnl"]["cumulative"], "0.00")

    def test_without_fixtures_marks_and_quote_checks_are_not_available(self):
        run = self.make_run()
        report = self.report(run, fixtures=None)
        self.assertEqual((report["verdict"], report["coverage"]), ("CONSISTENT", "PARTIAL"))
        self.assertStatus(report, "F1_FIXTURES", "NOT_CHECKED")
        self.assertStatus(report, "M1_MARKS", "NOT_CHECKED")
        self.assertEqual(report["exclusions"], {"unavailable": "FIXTURES_NOT_VERIFIED"})
        open_row = next(r for r in report["rows"] if r["type"] == "FILL")
        self.assertEqual(open_row["unrealized_pnl"], {"status": "UNAVAILABLE", "reason": "NO_VERIFIED_QUOTE_DATA"})

    def test_exclusions_are_listed_with_reason_and_source(self):
        exclusions = self.report(self.make_run())["exclusions"]
        self.assertEqual(len(exclusions), 22)
        self.assertTrue(all(e["reason"] and e["source_file"] for e in exclusions))
        self.assertIn(("SYNTHETIC_quotes_conflict.jsonl", 1, "CONFLICTING_ROW_IN_FILE"), [(e["source_file"], e["source_row"], e["reason"]) for e in exclusions])

    def test_a_restart_reconciles_and_older_events_against_newer_state_do_not(self):
        first = self.make_run("one", script=SCRIPT[:2])
        second = self.make_run("two", script=[replay.Intent(at(10, 15, 33), CE, "SELL", 1)], state_dir=first.state.parent, start=at(10, 15, 31))
        report = self.report(second)
        self.assertEqual(report["verdict"], "CONSISTENT", [c for c in report["checks"] if c["status"] != "PASS"])
        self.assertEqual(report["summary"]["restored_trades"], 2)
        stale = self.report(type("Run", (), {"events": first.events, "state": second.state}))
        self.assertEqual(stale["verdict"], "INCONSISTENT")                                         # the state holds a trade these events never saw
        self.assertStatus(stale, "X1_EVENT_TRADE_MATCH", "FAIL")

    def test_the_audit_catches_an_engine_that_is_consistently_wrong(self):
        from niftypaper.paper import engine as engine_module
        original = PaperEngine._plan_sell

        def generous(self_, held, quantity, turnover, cost):
            basis_out, buy_cost, realized = original(self_, held, quantity, turnover, cost)
            return basis_out, buy_cost, realized + Decimal("1.00")                                  # the engine books a rupee too much on every sale

        with mock.patch.object(PaperEngine, "_plan_sell", generous):
            run = self.make_run("bug1")
        report = self.report(run)
        self.assertEqual(report["verdict"], "INCONSISTENT")
        self.assertStatus(report, "A5_REALIZED", "FAIL")
        original_cost = CostModel.cost
        with mock.patch.object(CostModel, "cost", lambda self_, turnover: original_cost(self_, turnover) + Decimal("5")):
            run = self.make_run("bug2")
        report = self.report(run)
        self.assertStatus(report, "A3_FEES_ONCE", "FAIL")
        original_commit = PaperEngine._commit

        def double_fee(self_, side, symbol, quantity, lot_size, turnover, cost, plan):
            original_commit(self_, side, symbol, quantity, lot_size, turnover, cost, plan)
            if side == "BUY":
                self_.cash -= cost                                                                  # the buy fee is taken twice
        with mock.patch.object(PaperEngine, "_commit", double_fee):
            run = self.make_run("bug3")
        report = self.report(run)
        self.assertStatus(report, "A1_CASH", "FAIL")
        self.assertIsNotNone(engine_module)


class TestAccountingMismatches(Base):
    def setUp(self):
        super().setUp()
        self.run_ = self.make_run()

    def fills(self, objs):
        return [o for o in objs if o.get("type") == "FILL"]

    def trades(self, objs):
        return [o for o in objs if o.get("kind") == "trade"]

    def expect(self, name, check, events=None, state=None, verdict="INCONSISTENT", fixtures=FIXTURE_DIR):
        variant = self.variant(self.run_, name, events=events, state=state)
        report = self.report(variant, fixtures=fixtures)
        self.assertEqual(report["verdict"], verdict, name)
        self.assertStatus(report, check, "FAIL")
        return report

    def test_altered_realised_pnl_in_the_state(self):
        def change(objs):
            self.trades(objs)[3]["realized_pnl"] = "1.00"
            return objs
        self.expect("a", "A5_REALIZED", state=change)

    def test_altered_fee_in_the_state(self):
        def change(objs):
            self.trades(objs)[0]["cost"] = "22.96"
            return objs
        report = self.expect("b", "A3_FEES_ONCE", state=change)
        self.assertStatus(report, "A1_CASH", "FAIL")                                               # the cash claims no longer follow either

    def test_altered_cash_claim_in_a_fill_event(self):
        def change(objs):
            self.fills(objs)[1]["cash"] = "982947.51"
            return objs
        self.expect("c", "A1_CASH", events=change)

    def test_altered_fill_price_in_an_event(self):
        def change(objs):
            self.fills(objs)[0]["fill_price"] = "60.10"
            return objs
        self.expect("d", "X1_EVENT_TRADE_MATCH", events=change)

    def test_a_refused_intent_that_changed_the_account(self):
        def change(objs):
            next(o for o in objs if o.get("type") == "INTENT_REFUSED")["cash"] = "1.00"
            return objs
        self.expect("e", "A6_REJECTIONS", events=change)

    def test_altered_quote_in_the_state(self):
        def change(objs):
            self.trades(objs)[0]["quote_ask"] = "60.20"
            return objs
        report = self.expect("f", "S4_TRADE_ARITHMETIC", state=change)
        self.assertStatus(report, "F1_FIXTURES", "FAIL")

    def test_altered_position_claim(self):
        def change(objs):
            self.fills(objs)[1]["positions"][CE]["quantity"] = 129
            return objs
        self.expect("g", "A2_POSITIONS", events=change)

    def test_a_saved_trade_with_no_event(self):
        def change(objs):
            last = dict(self.trades(objs)[-1])
            last["seq"] += 1
            objs.append(last)
            return objs
        report = self.expect("h", "X1_EVENT_TRADE_MATCH", state=change)
        self.assertStatus(report, "E5_EVENTS_COMPLETE", "FAIL")

    def test_events_cut_off_cleanly_before_the_final_checkpoint(self):
        report = self.expect("i", "E5_EVENTS_COMPLETE", events=lambda objs: objs[:-1])
        self.assertEqual(report["verdict"], "INCONSISTENT")

    def test_a_fill_that_points_at_an_older_quote(self):
        def change(objs):
            fill = self.fills(objs)[1]
            fill["quote_ref"] = self.fills(objs)[0]["quote_ref"]
            return objs
        self.expect("j", "F1_FIXTURES", events=change)

    def test_a_fill_event_with_no_matching_trade(self):
        report = self.report(self.variant(self.run_, "k", state=lambda objs: objs[:-1]))
        self.assertEqual(report["verdict"], "INCONSISTENT")
        self.assertStatus(report, "X1_EVENT_TRADE_MATCH", "FAIL")

    def notes(self, report, check):
        return " | ".join(next(c["notes"] for c in report["checks"] if c["id"] == check))

    def test_altered_turnover_in_the_state(self):
        def change(objs):
            self.trades(objs)[0]["turnover"] = "3919.51"
            return objs
        report = self.expect("t1", "S4_TRADE_ARITHMETIC", state=change)
        self.assertIn("turnover", self.notes(report, "S4_TRADE_ARITHMETIC"))

    def test_altered_realised_total_in_the_final_checkpoint(self):
        def change(objs):
            objs[-1]["realized_total"] = "0"
            return objs
        report = self.expect("t2", "A5_REALIZED", events=change)
        self.assertIn("realised total", self.notes(report, "A5_REALIZED"))

    def test_altered_ingest_digest_recorded_at_open(self):
        def change(objs):
            objs[0]["ingest_digest"] = "0" * 64
            return objs
        report = self.expect("t3", "F1_FIXTURES", events=change)
        self.assertIn("different digest", self.notes(report, "F1_FIXTURES"))

    def test_a_fill_must_cite_the_newest_quote_that_was_visible_and_already_available(self):
        found = ingest.ingest(FIXTURE_DIR, manifest.load_manifest(FIXTURE_DIR / manifest.MANIFEST_NAME), as_of=AS_OF)
        first = next(r for r in found.records if r.source_file == "SYNTHETIC_quotes.jsonl" and r.source_row == 1)
        older_ref = f"{first.source_file}#{first.source_row}:{first.content_digest}"
        def older(objs):
            fills = self.fills(objs)
            fills[1]["quote_ref"] = older_ref                                  # the CE buy cites the 10:15:00 CE quote instead of the 10:15:30 one
            return objs
        report = self.expect("t4", "F1_FIXTURES", events=older)
        self.assertIn("newest visible quote", self.notes(report, "F1_FIXTURES"))

        def early(objs):
            fills = self.fills(objs)
            fills[0]["quote_ref"] = fills[1]["quote_ref"]                      # the 10:15:02 fill cites a quote that only became available at 10:15:30
            return objs
        report = self.expect("t5", "F1_FIXTURES", events=early)
        self.assertIn("not yet available", self.notes(report, "F1_FIXTURES"))

    def test_an_intent_with_no_decision(self):
        def change(objs):
            last = dict(objs[-2])
            last.update(type="INTENT_RECEIVED", contract=CE, side="BUY", lots=1)
            return objs[:-1] + [last]
        report = self.expect("t6", "E4_INTENT_PAIRING", events=lambda objs: [dict(o, n=i) for i, o in enumerate(change(objs), 1)])
        self.assertIn("no decision", self.notes(report, "E4_INTENT_PAIRING"))

    def test_a_different_fee_model_given_to_the_audit(self):
        report = self.report(self.run_, fee_per_trade=Decimal("21"))
        self.assertEqual(report["verdict"], "INCONSISTENT")
        self.assertStatus(report, "A3_FEES_ONCE", "FAIL")

    def test_a_modified_announcement_is_found_only_with_the_fixtures(self):
        def change(objs):
            next(o for o in objs if o.get("type") == "RECORD_AVAILABLE")["ref"] = "SYNTHETIC_quotes.jsonl#99"
            return objs
        self.expect("m", "F1_FIXTURES", events=change)
        blind = self.report(self.variant(self.run_, "m2", events=change), fixtures=None)
        self.assertEqual((blind["verdict"], blind["coverage"]), ("CONSISTENT", "PARTIAL"))        # stated limitation: not detectable without the fixtures

    def test_missing_availability_events(self):
        def change(objs):
            index = next(i for i, o in enumerate(objs) if o.get("type") == "RECORD_AVAILABLE")
            del objs[index]
            for number, o in enumerate(objs, 1):
                o["n"] = number                                                                     # renumbered: the gap is hidden, the omission is not
            return objs
        self.expect("n", "F1_FIXTURES", events=change)

    def test_fixtures_that_changed_since_the_replay(self):
        folder = self.tmp / "fx"
        shutil.copytree(FIXTURE_DIR, folder)
        (folder / "SYNTHETIC_quotes.jsonl").write_bytes((folder / "SYNTHETIC_quotes.jsonl").read_bytes().replace(b"100.20", b"100.30"))
        report = self.report(self.run_, fixtures=folder)
        self.assertEqual(report["verdict"], "INCONSISTENT")
        self.assertStatus(report, "F1_FIXTURES", "FAIL")
        self.assertStatus(report, "M1_MARKS", "SKIPPED")

    def test_a_different_source_in_the_events(self):
        def change(objs):
            objs[0]["source_id"] = "other_source"
            return objs
        self.expect("x", "X2_SOURCE_IDENTITY", events=change)


class TestCorruption(Base):
    def setUp(self):
        super().setUp()
        self.run_ = self.make_run()

    def blocked(self, name, check, events=None, state=None):
        report = self.report(self.variant(self.run_, name, events=events, state=state))
        self.assertEqual(report["verdict"], "NOT_RECONCILABLE", name)
        self.assertStatus(report, check, "BLOCKED")
        self.assertTrue(all(isinstance(r["cash_after"], dict) and r["cash_after"] == {"unavailable": "RECONCILIATION_BLOCKED"} for r in report["rows"]) or not report["rows"])
        self.assertEqual(report["summary"]["rebuilt_cash"], {"unavailable": "RECONCILIATION_BLOCKED"})
        return report

    def test_event_file_damage(self):
        self.blocked("e_gap", "E2_EVENTS_SEQUENCE", events=lambda objs: objs[:5] + objs[6:])
        self.blocked("e_dup", "E2_EVENTS_SEQUENCE", events=lambda objs: objs[:5] + [objs[4]] + objs[5:])
        self.blocked("e_swap", "E2_EVENTS_SEQUENCE", events=lambda objs: objs[:4] + [objs[5], objs[4]] + objs[6:])
        self.blocked("e_cut", "E1_EVENTS_PARSE", events=raw(lambda data: data[: len(data) // 2]))
        self.blocked("e_blank", "E1_EVENTS_PARSE", events=raw(lambda data: data.replace(b"\n", b"\n\n", 1)))
        self.blocked("e_junk", "E1_EVENTS_PARSE", events=raw(lambda data: b"junk\n" + data))
        self.blocked("e_empty", "E1_EVENTS_PARSE", events=raw(lambda data: b""))
        self.blocked("e_nonobj", "E1_EVENTS_PARSE", events=raw(lambda data: data + b"[1]\n"))
        self.blocked("e_dupkey", "E1_EVENTS_PARSE", events=raw(lambda data: data + b'{"n": 1, "n": 2}\n'))

    def test_event_clock_and_type_damage(self):
        def backwards(objs):
            objs[8]["clock"] = "2031-03-03T10:14:00+05:30"
            return objs

        def naive(objs):
            objs[3]["clock"] = "2031-03-03T10:15:00"
            return objs

        def unknown(objs):
            objs[3]["type"] = "SOMETHING"
            return objs

        def two_opens(objs):
            objs[1]["type"] = "REPLAY_OPENED"
            return objs
        self.blocked("c_back", "E3_EVENTS_CLOCK", events=backwards)
        self.blocked("c_naive", "E3_EVENTS_CLOCK", events=naive)
        self.blocked("c_type", "E3_EVENTS_CLOCK", events=unknown)
        self.blocked("c_two", "E3_EVENTS_CLOCK", events=two_opens)

    def test_state_file_damage(self):
        self.blocked("s_gap", "S3_STATE_SEQUENCE", state=lambda objs: objs[:2] + objs[3:])
        self.blocked("s_dup", "S3_STATE_SEQUENCE", state=lambda objs: objs[:2] + [objs[1]] + objs[2:])
        self.blocked("s_swap", "S3_STATE_SEQUENCE", state=lambda objs: [objs[0], objs[2], objs[1]] + objs[3:])
        self.blocked("s_cut", "S1_STATE_PARSE", state=raw(lambda data: data[:-40]))
        self.blocked("s_junk", "S1_STATE_PARSE", state=raw(lambda data: data + b"junk\n"))
        self.blocked("s_empty", "S1_STATE_PARSE", state=raw(lambda data: b""))
        self.blocked("s_nohead", "S1_STATE_PARSE", state=lambda objs: objs[1:])

        def bad_field(objs):
            objs[1]["turnover"] = "abc"
            return objs
        self.blocked("s_field", "S1_STATE_PARSE", state=bad_field)

        def later_time(objs):
            objs[2]["engine_time"] = "2031-03-03T10:00:00+05:30"
            return objs
        self.blocked("s_time", "S3_STATE_SEQUENCE", state=later_time)

    def test_missing_files(self):
        gone = type("Run", (), {"events": self.tmp / "none_events.jsonl", "state": self.tmp / "none" / STATE_FILE})
        report = self.report(gone)
        self.assertEqual(report["verdict"], "NOT_RECONCILABLE")
        self.assertStatus(report, "E1_EVENTS_PARSE", "BLOCKED")
        self.assertStatus(report, "S1_STATE_PARSE", "BLOCKED")
        self.assertEqual(report["inputs"]["events"], {"unavailable": "MISSING_FILE"})

    def test_a_state_file_of_another_account_is_not_reconciled(self):
        for account in ("synthetic", "fyers", "replay_", "replay"):
            def change(objs, account=account):
                objs[0]["account"] = account
                return objs
            self.blocked("acct_" + account, "S2_ACCOUNT_IDENTITY", state=change)

    def test_a_synthetic_or_provider_account_file_cannot_be_audited_as_a_replay(self):
        state = self.tmp / "other"
        state.mkdir()
        path = state / "paper_synthetic.jsonl"
        engine = PaperEngine(Decimal("1000000"), store=JsonlStore(path, Decimal("1000000"), account="synthetic"), origin=ORIGIN_SYNTHETIC)
        view = MarketView(ORIGIN_SYNTHETIC, "synthetic", "SIMULATED", "SIMULATED", CE, Decimal("100.00"), Decimal("100.20"), None, 650, 650, Decimal("0.05"), 65, None, None, None, True, True)
        self.assertTrue(engine.simulate(TradeRequest(CE, "BUY", 1), view).accepted)
        engine.close()
        report = audit.build_report(audit.AuditConfig(self.run_.events, path, FIXTURE_DIR))
        self.assertEqual(report["verdict"], "NOT_RECONCILABLE")
        self.assertStatus(report, "S2_ACCOUNT_IDENTITY", "BLOCKED")

    def test_a_consistent_forgery_passes_which_is_why_the_report_proves_nothing_about_authenticity(self):
        # a quote edited consistently in the fixtures AND re-hashed, with events regenerated, would pass every check: the report only compares files
        report = self.report(self.run_)
        self.assertEqual(report["verdict"], "CONSISTENT")
        self.assertIn("does not prove", report["notice"])


class TestReadOnlyDeterminismAndIsolation(Base):
    def test_the_report_changes_nothing_and_takes_no_lock(self):
        run = self.make_run()
        files = [run.events, run.state] + list(FIXTURE_DIR.iterdir())
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}
        listing = sorted(p.name for p in run.state.parent.iterdir()), sorted(p.name for p in FIXTURE_DIR.iterdir())
        self.report(run)
        self.report(run, fixtures=None)
        self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}, before)
        self.assertEqual((sorted(p.name for p in run.state.parent.iterdir()), sorted(p.name for p in FIXTURE_DIR.iterdir())), listing)
        self.assertEqual(sorted(p.name for p in run.state.parent.iterdir()), [STATE_FILE, STATE_FILE + ".lock"])      # only what the replay itself left

    def test_a_live_replay_can_be_audited_without_disturbing_its_lock(self):
        folder = self.tmp / "live"
        config = replay.ReplayConfig(fixture_dir=FIXTURE_DIR, state_dir=folder / "state", as_of=AS_OF, start=START, end=END, permitted=True)
        runner = replay.ReplayRunner.open(config)
        self.addCleanup(runner.close)
        runner.run(SCRIPT[:2])
        runner.checkpoint()
        (folder / "events.jsonl").write_bytes(runner.event_lines().encode())
        lock = folder / "state" / (STATE_FILE + ".lock")
        self.assertTrue(lock.exists())
        report = audit.build_report(audit.AuditConfig(folder / "events.jsonl", folder / "state" / STATE_FILE, FIXTURE_DIR))
        self.assertEqual(report["verdict"], "CONSISTENT")
        runner.run([replay.Intent(at(10, 15, 33), CE, "SELL", 1)])                               # the replay is unaffected and still holds its lock
        self.assertEqual(len([e for e in runner.events if e["type"] == "FILL"]), 3)

    def test_identical_inputs_give_identical_reports_and_digests(self):
        run = self.make_run("a")
        first = self.report(run)
        self.assertEqual(self.report(run), first)
        again = self.make_run("b")
        self.assertEqual(self.report(again), first)                                                 # a second, separate run of the same fixtures
        self.assertEqual(json.dumps(self.report(again), sort_keys=True), json.dumps(first, sort_keys=True))
        text = json.dumps(first)
        self.assertNotIn("generated", text.lower())
        self.assertNotIn(str(self.tmp), text)
        self.assertNotIn("2026-", text)
        copy = self.variant(run, "c")
        self.assertEqual(self.report(copy)["digest"], first["digest"])                              # the location of the files does not matter

    def test_the_digest_changes_with_any_input_byte(self):
        run = self.make_run()
        base = self.report(run)["digest"]
        digests = {base}
        for index in (30, 400, 900):
            variant = self.variant(run, f"d{index}", events=raw(lambda data, index=index: data[:index] + bytes([data[index] ^ 1]) + data[index + 1:]))
            digests.add(self.report(variant)["digest"])
        self.assertEqual(len(digests), 4)

    def test_the_report_digest_covers_the_report_body(self):
        report = self.report(self.make_run())
        import hashlib
        body = {k: v for k, v in report.items() if k != "digest"}
        self.assertEqual(report["digest"], hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest())

    def test_the_module_is_independent_of_the_engine_and_other_accounts(self):
        text = Path(audit.__file__).read_text(encoding="utf-8")
        for banned in ("paper.engine", "PaperEngine", "paper.store", "JsonlStore", "_plan_sell", "ORIGIN_PROVIDER", "ORIGIN_SYNTHETIC", "providers", "dashboard",
                       "attest", "open(", "write_text", "write_bytes", "unlink", "mkdir", "datetime.now", "os.environ", "socket"):
            self.assertNotIn(banned, text, banned)

    def test_the_command_line_tool(self):
        tool = load_tool()
        run = self.make_run()
        out = self.tmp / "report.json"
        buffer, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(error):
            code = tool.main(["--events", str(run.events), "--state", str(run.state), "--fixtures", str(FIXTURE_DIR), "--out", str(out)])
            self.assertEqual(code, 0)
            saved = json.loads(out.read_text())
            self.assertEqual(saved["digest"], self.report(run)["digest"])
            self.assertEqual(tool.main(["--events", str(run.events), "--state", str(run.state), "--out", str(out)]), 2)                  # no overwrite
            self.assertEqual(tool.main(["--events", str(run.events), "--state", str(run.state), "--out", str(run.events)]), 2)           # never over an input
            self.assertEqual(tool.main(["--events", str(run.events), "--state", str(run.state), "--out", str(run.state.parent / "r.json"), "--fixtures",
                                        str(FIXTURE_DIR)]), 0)                                      # next to an input file is fine; over or inside an input folder is not
            self.assertEqual(tool.main(["--events", str(run.events), "--state", str(run.state), "--fixtures", str(FIXTURE_DIR), "--text"]), 0)
            self.assertEqual(tool.main(["--events", str(self.tmp / "nope"), "--state", str(run.state)]), 2)
        self.assertIn("verdict CONSISTENT", buffer.getvalue())
        self.assertIn("digest", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
