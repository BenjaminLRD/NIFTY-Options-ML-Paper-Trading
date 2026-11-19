"""The initialized-state contract: a replay account exists (as a header-only state file) before its first trade, so a valid run with no accepted trade can be
audited, while a missing, empty, damaged or mismatched state file is never mistaken for an empty account. Invented fixtures, temporary folders only."""
import contextlib
import importlib.util
import io
import json
import shutil
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from niftypaper.historical import anchor, audit, replay
from niftypaper.paper.engine import CostModel
from niftypaper.paper.store import JsonlStore, StoreError
from niftypaper.strategy import driver
from tests import ROOT
from tests.historical_fixtures import CE
from tests.strategy_fixtures import FLAT, RISING, T1, T2, FALLING_AFTER, BASE_QUOTES, replay_config, strategy
from tests.test_historical_audit import SCRIPT
from tests.test_historical_chain import lines_of
from tests.test_historical_inputs import Base as RunBase
from tests.test_strategy_execution import Base as StrategyBase

BINDING, OTHER = "a" * 64, "b" * 64
HEADER = {"kind": "header", "schema": 1, "starting_balance": "1000000", "account": "replay_x", "binding": BINDING}


def line(obj):
    return (json.dumps(obj, sort_keys=True) + "\n").encode("utf-8")


class TestStoreContract(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name).resolve() / "state dir" / "replay_x.jsonl"
        self.stores = []

    def store(self, binding=BINDING, account="replay_x", balance="1000000", **kw):
        s = JsonlStore(self.path, Decimal(balance), account=account, binding=binding, require_initialized=True, **kw)
        self.stores.append(s)
        self.addCleanup(s.close)
        return s

    def put(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(data)

    def code(self, call):
        with self.assertRaises(StoreError) as caught:
            call()
        return caught.exception.code

    def test_initialize_creates_a_header_only_account_once(self):
        s = self.store()
        self.assertEqual(s.initialize(), [])
        written = self.path.read_bytes()
        self.assertEqual(lines_of(written), [json.dumps(HEADER, sort_keys=True)])
        s.close()
        again = self.store()                                                          # idempotent: same account, same binding
        self.assertEqual(again.initialize(), [])
        self.assertEqual(self.path.read_bytes(), written)

    def test_a_header_only_file_is_an_account_with_zero_trades_and_can_take_a_trade(self):
        s = self.store()
        s.initialize()
        s.append({"seq": 1})
        self.assertEqual(len(lines_of(self.path.read_bytes())), 2)
        s.close()
        self.assertEqual(self.store().initialize(), [{"seq": 1}])

    def test_every_damaged_or_foreign_file_is_an_error_and_is_left_untouched(self):
        cases = {"empty": (b"", "EMPTY_FILE"), "truncated": (line(HEADER)[:-1], "TRUNCATED"), "corrupt": (b"{not json}\n", "CORRUPT"),
                 "no_header": (line({"kind": "trade"}), "CORRUPT"), "other_account": (line({**HEADER, "account": "replay_y"}), "DIFFERENT_ACCOUNT"),
                 "other_binding": (line({**HEADER, "binding": OTHER}), "DIFFERENT_ACCOUNT"),
                 "unbound": (line({k: v for k, v in HEADER.items() if k != "binding"}), "DIFFERENT_ACCOUNT"),
                 "no_account": (line({k: v for k, v in HEADER.items() if k != "account"}), "DIFFERENT_ACCOUNT"),
                 "other_balance": (line({**HEADER, "starting_balance": "5"}), "DIFFERENT_ACCOUNT"),
                 "trade_garbage": (line(HEADER) + line({"kind": "nope"}), "CORRUPT")}
        for name, (data, code) in cases.items():
            with self.subTest(name):
                self.put(data)
                s = self.store()
                self.assertEqual(self.code(s.initialize), code)
                self.assertEqual(self.path.read_bytes(), data)
                s.close()

    def test_a_missing_file_is_not_an_empty_account_for_load(self):
        self.assertFalse(self.path.exists())
        self.assertEqual(self.code(self.store().load), "MISSING")
        self.assertFalse(self.path.exists())                                          # load never creates it

    def test_only_a_bound_initialized_store_can_initialize(self):
        plain = JsonlStore(self.path, Decimal("1"), account="replay_x", binding=BINDING)
        self.assertEqual(self.code(plain.initialize), "NOT_BOUND")
        self.assertFalse(self.path.exists())

    def test_the_legacy_behaviour_of_other_stores_is_unchanged(self):
        self.put(b"")
        legacy = JsonlStore(self.path, Decimal("1000000"), account="synthetic")
        self.addCleanup(legacy.close)
        self.assertEqual(legacy.load(), [])                                           # dashboards and synthetic accounts still start from a zero-byte file
        other = Path(self._tmp.name) / "new.jsonl"
        fresh = JsonlStore(other, Decimal("1"))
        self.addCleanup(fresh.close)
        self.assertEqual(fresh.load(), [])
        self.assertFalse(other.exists())

    def test_the_new_header_is_flushed_to_disk(self):
        from unittest import mock
        with mock.patch("niftypaper.paper.store.os.fsync") as fsync:
            self.store().initialize()
        self.assertGreaterEqual(fsync.call_count, 1)

    def test_a_file_created_between_the_check_and_the_create_is_never_replaced(self):
        import pathlib
        from unittest import mock
        data = line(HEADER) + line({"kind": "trade", "seq": 1})
        self.put(data)
        real, calls = pathlib.Path.exists, []

        def exists(path, *a, **k):
            if str(path) == str(self.path) and not calls:
                calls.append(1)
                return False                                                              # the existence check sees nothing; the file is there when we create
            return real(path, *a, **k)

        s = self.store()
        with mock.patch.object(pathlib.Path, "exists", exists), mock.patch.object(pathlib.Path, "is_symlink", lambda path: False):
            self.assertEqual(s.initialize(), [{"seq": 1}])                                # validated like any existing file
        self.assertEqual(self.path.read_bytes(), data)

    def test_the_single_writer_lock_still_applies(self):
        first = self.store()
        first.initialize()
        second = self.store()
        self.assertEqual(self.code(second.initialize), "IN_USE")


class TestRunnerLifecycle(RunBase):
    def state_of(self, name, source="synthetic_fixture"):
        return self.tmp / name / "state" / f"replay_{source}.jsonl"

    def test_opening_a_replay_initializes_the_account_before_any_trade(self):
        runner = self.open("a", intents=[])
        path = self.state_of("a")
        header = json.loads(path.read_bytes())
        self.assertEqual((header["kind"], header["account"], header["binding"], runner.engine.restored, runner.engine.lock_reason),
                         ("header", "replay_synthetic_fixture", runner.inputs["account_binding"], 0, None))
        self.assertEqual(len(lines_of(path.read_bytes())), 1)

    def test_reopening_is_idempotent_and_a_trade_then_extends_the_same_file(self):
        first = self.open("a", intents=[])
        first.close()
        before = self.state_of("a").read_bytes()
        again = self.open("a", intents=[])
        self.assertEqual((again.engine.lock_reason, self.state_of("a").read_bytes()), (None, before))

    def test_an_existing_account_with_other_inputs_is_locked_and_not_overwritten(self):
        self.open("a", intents=[]).close()
        before = self.state_of("a").read_bytes()
        other = self.open("a", slippage_ticks=2)                                    # same folder, other settings: another replay identity
        self.assertIn("DIFFERENT_ACCOUNT", other.engine.lock_reason)
        self.assertEqual(self.state_of("a").read_bytes(), before)

    def test_empty_and_corrupt_existing_files_lock_the_account_and_stay_as_found(self):
        for name, data, code in (("empty", b"", "EMPTY_FILE"), ("junk", b"junk\n", "CORRUPT"), ("cut", b'{"kind":"header"', "TRUNCATED")):
            with self.subTest(name):
                path = self.state_of(name)
                path.parent.mkdir(parents=True)
                path.write_bytes(data)
                runner = self.open(name)
                self.assertIn(code, runner.engine.lock_reason)
                self.assertEqual(path.read_bytes(), data)
                self.assertEqual([e["type"] for e in runner.events][:2], ["REPLAY_OPENED", "STATE_LOCKED"])

    def test_a_locked_account_never_trades(self):
        path = self.state_of("locked")
        path.parent.mkdir(parents=True)
        path.write_bytes(b"")
        runner = self.open("locked")
        runner.run(SCRIPT)
        self.assertEqual([e for e in runner.events if e["type"] == "FILL"], [])
        self.assertEqual(path.read_bytes(), b"")

    def test_a_state_folder_that_cannot_be_created_refuses_the_replay(self):
        blocker = self.tmp / "blocker"
        blocker.write_text("a file where the state folder should be")
        with self.assertRaises(replay.ReplayRefused) as caught:
            replay.ReplayRunner.open(self.config("x", state_dir=blocker))
        self.assertEqual(caught.exception.code, "STATE_NOT_INITIALIZED")
        self.assertEqual(blocker.read_text(), "a file where the state folder should be")

    def test_runs_with_trades_still_extend_the_initialized_file(self):
        run = self.run_variant("t")
        lines = lines_of(run.state.read_bytes())
        self.assertEqual(json.loads(lines[0])["kind"], "header")
        self.assertGreater(len(lines), 1)
        self.assertEqual(self.report(run)["account"]["state_kind"], "INITIALIZED_WITH_TRADES")


class TestAuditOfZeroTradeRuns(RunBase):
    REFUSALS = [replay.Intent(__import__("tests.test_historical_replay", fromlist=["at"]).at(10, 20), "NIFTY", "BUY", 1)]

    def zero(self, name="zero", script=()):
        return self.run_variant(name, script=list(script))

    def test_a_valid_zero_trade_run_is_consistent_in_full(self):
        run = self.zero()
        r = self.report(run)
        self.assertEqual((r["verdict"], r["coverage"], r["account"]["state_kind"], r["inputs_identity"]["plan"]["status"], r["pairing"]["status"], r["chain"]["status"]),
                         ("CONSISTENT", "FULL", "INITIALIZED_NO_TRADES", "MATCH", "PAIRED", "VALID"))
        self.assertEqual((r["summary"]["saved_trades"], r["summary"]["rebuilt_cash"], r["summary"]["rebuilt_realized_total"]), (0, "1000000.00", "0.00"))
        self.assertTrue(all(c["status"] == "PASS" for c in r["checks"]))

    def test_a_run_whose_every_intent_was_refused_is_consistent_in_full(self):
        run = self.zero("refused", self.REFUSALS)
        r = self.report(run)
        self.assertEqual([e["type"] for e in self.objs(run) if e["type"].startswith("INTENT")], ["INTENT_RECEIVED", "INTENT_REFUSED"])
        self.assertEqual((r["verdict"], r["coverage"], r["account"]["state_kind"]), ("CONSISTENT", "FULL", "INITIALIZED_NO_TRADES"))

    def test_missing_or_damaged_state_is_not_reconcilable(self):
        run = self.zero()
        good = run.state.read_bytes()
        for name, data in (("missing", None), ("empty", b""), ("truncated", good[:-1]), ("corrupt", b"{{\n"), ("binary", b"\xff\xfe\n")):
            with self.subTest(name):
                damaged = self.tmp / name / run.state.name
                damaged.parent.mkdir()
                if data is not None:
                    damaged.write_bytes(data)
                r = self.report(type("R", (), {"events": run.events, "state": damaged}))
                self.assertEqual((r["verdict"], r["account"]["state_kind"]), ("NOT_RECONCILABLE", {"unavailable": "STATE_UNUSABLE"}))

    def test_state_of_another_source_or_inputs_is_not_reconcilable(self):
        run = self.zero()
        other_fx = self.fixture_copy("other_source", source_id="other_source")
        other = self.zero_for(other_fx, "other")
        for state in (other, self.zero_with("slip", slippage_ticks=2).state):
            r = self.report(type("R", (), {"events": run.events, "state": state}))
            self.assertEqual((r["verdict"], r["pairing"]["status"]), ("NOT_RECONCILABLE", "MISMATCH"))

    def zero_for(self, fixtures, name):
        return self.run_variant(name, fixtures=fixtures, script=[]).state

    def zero_with(self, name, **kw):
        return self.run_variant(name, script=[], **kw)

    def test_a_fill_the_state_does_not_hold_is_inconsistent(self):
        traded = self.run_variant("traded")
        empty = self.zero()
        r = self.report(type("R", (), {"events": traded.events, "state": empty.state}))
        self.assertEqual((r["verdict"], r["pairing"]["status"]), ("INCONSISTENT", "PAIRED"))                  # same inputs pair; the omitted trades are what fails
        self.assertEqual(next(c["status"] for c in r["checks"] if c["id"] == "X1_EVENT_TRADE_MATCH"), "FAIL")

    def test_trades_in_the_state_that_the_events_never_recorded_are_inconsistent(self):
        traded = self.run_variant("traded")
        empty = self.zero()
        r = self.report(type("R", (), {"events": empty.events, "state": traded.state}))
        self.assertEqual(r["verdict"], "INCONSISTENT")

    def test_a_wrong_claim_about_a_zero_trade_account_is_inconsistent(self):
        run = self.zero()
        for name, edit in (("cash", lambda o: o[-1].update(cash="1.00")), ("trades", lambda o: o[-1].update(trades=1)), ("realized", lambda o: o[-1].update(realized_total="5"))):
            with self.subTest(name):
                r = self.report(self.rechained(run, name, lambda objs: edit(objs)))
                self.assertEqual(r["verdict"], "INCONSISTENT")

    def test_a_header_with_the_wrong_starting_balance_is_inconsistent(self):
        run = self.zero()
        header = json.loads(run.state.read_bytes())
        header["starting_balance"] = "999999"
        bad = self.tmp / "bal" / run.state.name
        bad.parent.mkdir()
        bad.write_bytes(line(header))
        self.assertEqual(self.report(type("R", (), {"events": run.events, "state": bad}))["verdict"], "INCONSISTENT")

    def test_an_undeclared_plan_is_partial_even_with_a_valid_zero_trade_state(self):
        runner = self.open("noplan")
        runner.write_events(self.tmp / "noplan" / "events.jsonl")
        runner.close()
        r = self.report(type("R", (), {"events": self.tmp / "noplan" / "events.jsonl", "state": self.state_path("noplan")}))
        self.assertEqual((r["verdict"], r["coverage"]), ("CONSISTENT", "PARTIAL"))

    def state_path(self, name):
        return self.tmp / name / "state" / "replay_synthetic_fixture.jsonl"


def load_verify():
    spec = importlib.util.spec_from_file_location("vl_zero", ROOT / "tools" / "replay_verify_later.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool


class TestStrategyAndStrictVerification(StrategyBase):
    def setUp(self):
        super().setUp()
        self.tool = load_verify()

    def verify(self, out, fixtures, anchor_file, state=None, events=None):
        out_io = io.StringIO()
        state = state or out / "state" / "replay_synthetic_fixture.jsonl"
        with contextlib.redirect_stdout(out_io):
            code = self.tool.main(["--events", str(events or out / "events.jsonl"), "--state", str(state), "--fixtures", str(fixtures), "--anchor", str(anchor_file),
                                   "--anchor-trust", "independent"])
        return code, out_io.getvalue()

    def kept(self, out, name="a", events=None):
        path = self.tmp / "kept apart" / f"{name}.json"
        anchor.create(events or out / "events.jsonl", out / "state" / "replay_synthetic_fixture.jsonl", path)
        return path

    def assertPass(self, result):
        self.assertEqual((result[0], result[1].rstrip("\n").splitlines()[-1]), (0, "PASS"), result[1])

    def assertFail(self, result, *texts):
        self.assertNotEqual(result[0], 0)
        self.assertEqual(result[1].rstrip("\n").splitlines()[-1], "FAIL")
        for t in texts:
            self.assertIn(t, result[1])

    def zero_run(self, **kw):
        result, out, fixtures = self.drive(fixtures=self.fixtures(closes=FLAT), **kw)
        return result, out, fixtures

    def test_a_genuinely_complete_zero_trade_run_passes_strict_verification_with_a_kept_anchor(self):
        result, out, fixtures = self.zero_run()
        self.assertEqual((result.ledger, result.plan), ([], []))
        self.assertPass(self.verify(out, fixtures, self.kept(out)))

    def test_all_inputs_rejected_and_all_intents_refused_runs_also_pass(self):
        rejected, out1, fx1 = self.drive(fixtures=self.fixtures(quotes=[]), name="rejected")
        self.assertEqual({d.outcome for d in rejected.decisions}, {"INPUT_REJECTED"})
        self.assertPass(self.verify(out1, fx1, self.kept(out1, "r")))
        refused, out2, fx2 = self.drive(strat=strategy(lots=3), name="refused")
        self.assertEqual(refused.ledger, [])
        self.assertTrue(refused.plan)
        self.assertPass(self.verify(out2, fx2, self.kept(out2, "f")))

    def test_accepted_trades_and_entry_then_exit_still_pass(self):
        for name, kw in (("entry", {"schedule": (T1,)}), ("both", {})):
            with self.subTest(name):
                result, out, fixtures = self.drive(name=name, **kw)
                self.assertTrue(result.ledger)
                self.assertPass(self.verify(out, fixtures, self.kept(out, name)))

    def test_the_two_pass_process_leaves_no_scratch_account_behind(self):
        result, out, fixtures = self.zero_run()
        self.assertEqual(sorted(p.name for p in (out / "state").glob("*.jsonl")), ["replay_synthetic_fixture.jsonl"])
        self.assertEqual([p for p in self.tmp.glob("strategy_pass1_*")], [])

    def test_running_again_into_the_same_output_changes_nothing(self):
        result, out, fixtures = self.zero_run(name="same")
        before = {p: p.read_bytes() for p in out.rglob("*") if p.is_file() and p.suffix != ".lock"}
        with self.assertRaises(driver.StrategyError) as caught:
            driver.run_strategy(replay_config(fixtures, out / "state"), strategy(), (T1, T2), out / "events.jsonl")
        self.assertEqual(caught.exception.code, "STATE_NOT_EMPTY")
        self.assertEqual({p: p.read_bytes() for p in out.rglob("*") if p.is_file() and p.suffix != ".lock"}, before)

    def test_missing_empty_or_damaged_state_fails(self):
        result, out, fixtures = self.zero_run()
        path = self.kept(out)
        state = out / "state" / "replay_synthetic_fixture.jsonl"
        good = state.read_bytes()
        for name, data in (("missing", None), ("empty", b""), ("truncated", good[:-1]), ("corrupt", b"{{\n")):
            with self.subTest(name):
                bad = self.tmp / name / state.name
                bad.parent.mkdir()
                if data is not None:
                    bad.write_bytes(data)
                self.assertFail(self.verify(out, fixtures, path, state=bad))

    def test_invalid_chain_incomplete_plan_and_wrong_accounting_fail(self):
        result, out, fixtures = self.zero_run()
        path = self.kept(out)
        events = out / "events.jsonl"
        lines = lines_of(events.read_bytes())
        edited = self.tmp / "edited" / "events.jsonl"
        edited.parent.mkdir()
        edited.write_bytes(events.read_bytes().replace(b"RECORD_AVAILABLE", b"RECORD_AVAILABLEX", 1))
        self.assertFail(self.verify(out, fixtures, path, events=edited), "FAIL CHAIN")
        prefix = self.tmp / "prefix" / "events.jsonl"
        prefix.parent.mkdir()
        prefix.write_bytes(("\n".join(lines[:-2]) + "\n").encode("utf-8"))
        self.assertFail(self.verify(out, fixtures, path, events=prefix), "FAIL ANCHOR")
        forged_events = self.tmp / "forged" / "events.jsonl"
        forged_events.parent.mkdir()
        from tests.test_historical_chain import dump
        objs = [json.loads(x) for x in lines]
        objs[-1]["cash"] = "1.00"
        forged_events.write_bytes(dump(objs)[0])
        own_anchor = self.kept(out, "forged", events=forged_events)                       # a writer who also made its own anchor
        self.assertFail(self.verify(out, fixtures, own_anchor, events=forged_events), "FAIL ACCOUNTING")

    def test_a_plan_that_is_not_declared_fails(self):
        fixtures = self.fixtures(closes=FLAT)
        out = self.tmp / "noplan"
        runner = replay.ReplayRunner.open(replay_config(fixtures, out / "state"))
        runner.write_events(out / "events.jsonl")
        runner.close()
        self.assertFail(self.verify(out, fixtures, self.kept(out, "np")), "FAIL PLAN")

    def test_no_anchor_is_still_a_fail_for_a_zero_trade_run(self):
        result, out, fixtures = self.zero_run()
        out_io = io.StringIO()
        with contextlib.redirect_stdout(out_io):
            code = self.tool.main(["--events", str(out / "events.jsonl"), "--state", str(out / "state" / "replay_synthetic_fixture.jsonl"), "--fixtures", str(fixtures),
                                   "--anchor-trust", "independent"])
        self.assertNotEqual(code, 0)
        self.assertEqual(out_io.getvalue().rstrip("\n").splitlines()[-1], "FAIL")


if __name__ == "__main__":
    unittest.main()
