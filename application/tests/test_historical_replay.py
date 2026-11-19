"""Fixture-only replay runner: chronology, trust boundary, isolation, recovery and determinism. Invented data only (tests/fixtures/historical_synthetic/)."""
import dataclasses
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from niftypaper.historical import gate, replay
from niftypaper.historical import manifest as mf
from niftypaper.paper.engine import ORIGIN_HISTORICAL, ORIGIN_PROVIDER, ORIGIN_SYNTHETIC, MarketView, PaperEngine, RiskLimits, TradeRequest
from niftypaper.paper.store import JsonlStore
from tests import ROOT
from tests import historical_fixtures as fx
from tests.historical_fixtures import AS_OF, CE, IST, PE, bar, eod, index, quote
from tests.test_historical_phase2 import reattest

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "historical_synthetic"
START = datetime(2031, 3, 3, 10, 14, tzinfo=IST)
END = datetime(2031, 3, 3, 10, 30, tzinfo=IST)
STATE_FILE = "replay_synthetic_fixture.jsonl"


def at(h, m, s=0, day=3):
    return datetime(2031, 3, day, h, m, s, tzinfo=IST)


SCRIPT = [
    replay.Intent(at(10, 14, 30), CE, "BUY", 1),       # before any quote exists
    replay.Intent(at(10, 15, 2), PE, "BUY", 1),        # fills on the 10:15:00 quote
    replay.Intent(at(10, 15, 31), CE, "BUY", 1),       # fills on the 10:15:30 quote
    replay.Intent(at(10, 15, 31), PE, "SELL", 1),      # the PE quote is 31 s old: stale at the engine boundary
    replay.Intent(at(10, 16, 2), CE, "BUY", 1),        # newest CE quote has a receive time, not a quote time
    replay.Intent(at(10, 17, 2), CE, "BUY", 1),        # newest CE quote has no displayed sizes
    replay.Intent(at(10, 20), "NIFTY", "BUY", 1),      # an index key: there is no quote for it
]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"
        self.opened = []

    def config(self, directory=FIXTURE_DIR, **kw):
        base = dict(fixture_dir=directory, state_dir=self.state, as_of=AS_OF, start=START, end=END, permitted=True)
        base.update(kw)
        return replay.ReplayConfig(**base)

    def open(self, directory=FIXTURE_DIR, **kw):
        runner = replay.ReplayRunner.open(self.config(directory, **kw))
        self.addCleanup(runner.close)
        self.opened.append(runner)
        return runner

    def copy_fixture(self, name="fix"):
        target = self.tmp / name
        target.mkdir()
        for path in FIXTURE_DIR.iterdir():
            (target / path.name).write_bytes(path.read_bytes())
        return target

    def make_set(self, name, files, **write_kw):
        """files: {file name: (kind, rows)} -> a fresh fixture folder with a manifest."""
        target = self.tmp / name
        target.mkdir()
        for file_name, (_, rows) in files.items():
            fx.write_rows(target, file_name, rows)
        fx.write_manifest(target, {n: k for n, (k, _) in files.items()}, **write_kw)
        return target

    @staticmethod
    def kinds(runner, kind):
        return [e for e in runner.events if e["type"] == kind]


class TestChronology(Base):
    def test_a_scripted_replay_behaves_as_specified(self):
        runner = self.open()
        runner.run(SCRIPT)
        outcomes = [(e["contract"], e["side"], e["type"], e.get("stage"), e.get("code")) for e in runner.events if e["type"] in ("FILL", "INTENT_REFUSED")]
        self.assertEqual(outcomes, [
            (CE, "BUY", "INTENT_REFUSED", "BRIDGE", "NO_VISIBLE_QUOTE"),
            (PE, "BUY", "FILL", None, None),
            (CE, "BUY", "FILL", None, None),
            (PE, "SELL", "INTENT_REFUSED", "ENGINE", "BOUNDARY_REJECTED"),
            (CE, "BUY", "INTENT_REFUSED", "BRIDGE", "NOT_FILL_ELIGIBLE"),
            (CE, "BUY", "INTENT_REFUSED", "BRIDGE", "NOT_FILL_ELIGIBLE"),
            ("NIFTY", "BUY", "INTENT_REFUSED", "BRIDGE", "NO_VISIBLE_QUOTE"),
        ])
        fills = self.kinds(runner, "FILL")
        self.assertEqual([f["fill_price"] for f in fills], ["60.30", "100.65"])            # ask + one tick; never a close, last or settlement price
        self.assertTrue(all(f["data_origin"] == ORIGIN_HISTORICAL and f["provider"] == "historical:synthetic_fixture" for f in fills))
        self.assertTrue(all("SYNTHETIC_quotes.jsonl#" in f["quote_ref"] for f in fills))
        self.assertEqual(sorted(runner.engine.positions), [PE, CE])

    def test_records_appear_only_after_their_available_at(self):
        runner = self.open()
        self.assertEqual(self.kinds(runner, "RECORD_AVAILABLE"), [])                       # nothing is available at 10:14
        runner.advance_to(at(10, 15))
        first = self.kinds(runner, "RECORD_AVAILABLE")
        self.assertEqual({e["ref"].split("#")[0] for e in first}, {"SYNTHETIC_quotes.jsonl", "SYNTHETIC_bars.jsonl"})
        runner.run([], until=END)
        self.assertTrue(all(datetime.fromisoformat(e["available_at"]) <= datetime.fromisoformat(e["clock"]) for e in self.kinds(runner, "RECORD_AVAILABLE")))
        self.assertTrue(all(e["kind"] not in ("EOD_CONTRACT_DAY", "UNDERLYING_EOD") for e in self.kinds(runner, "RECORD_AVAILABLE")))    # settlement is end-of-day

    def test_day_level_records_appear_exactly_at_the_end_of_their_date(self):
        runner = self.open(end=at(0, 0, day=5), as_of=at(12, 0, day=10))
        runner.run([], until=at(0, 0, day=5))
        days = [(e["kind"], e["time_key"], e["clock"]) for e in self.kinds(runner, "RECORD_AVAILABLE") if e["kind"] in ("EOD_CONTRACT_DAY", "UNDERLYING_EOD")]
        self.assertTrue(days)
        for kind, day, clock in days:
            self.assertEqual(clock, f"{day}T23:59:59+05:30", (kind, day))

    def test_no_event_ever_mentions_a_record_from_the_future(self):
        runner = self.open()
        runner.run(SCRIPT)
        refs = {e["ref"] for e in self.kinds(runner, "RECORD_AVAILABLE")}
        later = {f"{r.source_file}#{r.source_row}" for r in runner.result.records if r.available_at > END}
        self.assertTrue(later)                                                              # the fixtures do hold later records...
        self.assertFalse(refs & later)                                                      # ...and none was shown

    def test_clock_cannot_go_backwards_or_past_the_end_and_must_be_aware(self):
        runner = self.open()
        runner.advance_to(at(10, 16))
        for bad in (at(10, 15), START):
            with self.assertRaises(gate.ClockError):
                runner.advance_to(bad)
        with self.assertRaises(gate.ClockError):
            runner.advance_to(datetime(2031, 3, 3, 10, 20))
        with self.assertRaises(replay.ReplayRefused) as caught:
            runner.advance_to(END + timedelta(seconds=1))
        self.assertEqual(caught.exception.code, "PAST_END")
        self.assertEqual(runner.clock.now, at(10, 16))                                       # unchanged by every refused move

    def test_a_script_with_intents_in_the_past_or_after_the_end_is_refused_whole(self):
        runner = self.open()
        runner.advance_to(at(10, 16))
        for intent, code in ((replay.Intent(at(10, 15), CE, "BUY", 1), "INTENT_BEFORE_CLOCK"), (replay.Intent(END + timedelta(minutes=1), CE, "BUY", 1), "INTENT_AFTER_END"),
                             ("buy", "BAD_INTENT"), (replay.Intent(datetime(2031, 3, 3, 10, 20), CE, "BUY", 1), "BAD_CONFIG")):
            with self.assertRaises(replay.ReplayRefused) as caught:
                runner.run([replay.Intent(at(10, 17), CE, "BUY", 1), intent])
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(runner.clock.now, at(10, 16))
        self.assertEqual(self.kinds(runner, "FILL"), [])

    def test_bad_configuration_is_refused(self):
        for change in ({"start": datetime(2031, 3, 3, 10, 14)}, {"end": at(10, 0)}, {"end": AS_OF + timedelta(hours=1)}, {"as_of": datetime(2031, 3, 10)}):
            with self.subTest(change), self.assertRaises(replay.ReplayRefused) as caught:
                replay.ReplayRunner.open(self.config(**change))
            self.assertEqual(caught.exception.code, "BAD_CONFIG")

    def test_bad_intents_are_refused_not_executed(self):
        runner = self.open()
        runner.advance_to(at(10, 15, 2))
        for intent in (replay.Intent(at(10, 15, 2), PE, "HOLD", 1), replay.Intent(at(10, 15, 2), PE, "BUY", True), replay.Intent(at(10, 15, 2), PE, "BUY", 0),
                       replay.Intent(at(10, 15, 2), 5, "BUY", 1)):
            event = runner.try_trade(intent)
            self.assertEqual(event["type"], "INTENT_REFUSED")
        self.assertEqual(runner.engine.ledger, [])


class TestTrustBoundary(Base):
    def test_only_eligible_historical_quote_snapshots_reach_a_fill(self):
        folder = self.make_set("kinds", {"e.jsonl": ("EOD_CONTRACT_DAY", [eod()]), "b.jsonl": ("INTRADAY_BAR", [bar()]), "i.jsonl": ("UNDERLYING_EOD", [index()])})
        runner = self.open(folder, end=at(23, 59, 59), as_of=at(23, 59, 59))
        script = [replay.Intent(at(10, 16), CE, "BUY", 1), replay.Intent(at(23, 59, 59), CE, "BUY", 1), replay.Intent(at(23, 59, 59), "NIFTY", "BUY", 1)]
        runner.run(script)
        self.assertEqual(self.kinds(runner, "FILL"), [])
        self.assertEqual({e["code"] for e in self.kinds(runner, "INTENT_REFUSED")}, {"NO_VISIBLE_QUOTE"})
        self.assertEqual(runner.engine.ledger, [])

    def test_rows_of_the_wrong_kind_in_a_file_are_excluded_and_never_traded(self):
        folder = self.make_set("wrong", {"e.jsonl": ("EOD_CONTRACT_DAY", [quote()])})
        runner = self.open(folder)
        self.assertEqual(runner.result.records, ())
        self.assertEqual([e.reason for e in runner.result.exclusions], ["RECORD_KIND_MISMATCH"])
        runner.run([replay.Intent(at(10, 15, 1), CE, "BUY", 1)])
        self.assertEqual(self.kinds(runner, "FILL"), [])

    def test_the_replay_needs_explicit_permission_and_allows_only_fixture_data(self):
        runner = self.open(permitted=False)
        runner.run([replay.Intent(at(10, 15, 2), PE, "BUY", 1)])
        self.assertEqual([e["code"] for e in self.kinds(runner, "INTENT_REFUSED")], ["DATASET_NOT_PERMITTED"])
        for origin in ("UNVERIFIED_SOURCE", "UNKNOWN"):
            row = {k: v for k, v in quote().items() if k != "synthetic"}
            folder = self.make_set("o_" + origin, {"q.jsonl": ("QUOTE_SNAPSHOT", [row])}, origin_class=origin)
            with self.assertRaises(replay.ReplayRefused) as caught:
                replay.ReplayRunner.open(self.config(folder))
            self.assertEqual(caught.exception.code, "NOT_FIXTURE_ONLY", origin)

    def test_a_manifest_that_mixes_sources_is_refused(self):
        folder = self.make_set("mixed", {"a.jsonl": ("QUOTE_SNAPSHOT", [quote()]), "b.jsonl": ("QUOTE_SNAPSHOT", [quote(strike="24000", option_type="PE")])})
        (folder / mf.MANIFEST_NAME).unlink()
        entries = mf.describe(folder, {"a.jsonl": "QUOTE_SNAPSHOT"}, source_id="source_one", origin_class="SYNTHETIC_FIXTURE", origin="invented")
        entries += mf.describe(folder, {"b.jsonl": "QUOTE_SNAPSHOT"}, source_id="source_two", origin_class="SYNTHETIC_FIXTURE", origin="invented")
        mf.write_manifest(folder / mf.MANIFEST_NAME, entries)
        with self.assertRaises(replay.ReplayRefused) as caught:
            replay.ReplayRunner.open(self.config(folder))
        self.assertEqual(caught.exception.code, "MIXED_SOURCES")

    def test_invalid_or_missing_manifests_are_refused(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        garbage = self.make_set("garbage", {"q.jsonl": ("QUOTE_SNAPSHOT", [quote()])})
        (garbage / mf.MANIFEST_NAME).write_bytes(b"{not json}\n")
        escape = self.make_set("escape", {"q.jsonl": ("QUOTE_SNAPSHOT", [quote()])})
        line = json.loads((escape / mf.MANIFEST_NAME).read_text())
        line["path"] = "../q.jsonl"
        (escape / mf.MANIFEST_NAME).write_text(json.dumps(line) + "\n")
        for folder in (empty, garbage, escape):
            with self.subTest(folder.name), self.assertRaises(replay.ReplayRefused) as caught:
                replay.ReplayRunner.open(self.config(folder))
            self.assertEqual(caught.exception.code, "INVALID_MANIFEST")
        self.assertFalse(self.state.exists())

    def test_tampered_missing_or_unlisted_files_stop_the_replay_before_it_starts(self):
        for name, damage in (("changed", lambda d: (d / "SYNTHETIC_quotes.jsonl").write_bytes((d / "SYNTHETIC_quotes.jsonl").read_bytes().replace(b"100.00", b"001.00"))),
                             ("missing", lambda d: (d / "SYNTHETIC_bars.jsonl").unlink()),
                             ("unlisted", lambda d: (d / "extra.jsonl").write_bytes(b"{}\n")),
                             ("appended", lambda d: (d / "SYNTHETIC_index.jsonl").write_bytes((d / "SYNTHETIC_index.jsonl").read_bytes() + b"\n"))):
            with self.subTest(name):
                folder = self.copy_fixture(name)
                damage(folder)
                before = {p.name: p.read_bytes() for p in folder.iterdir()}
                with self.assertRaises(replay.ReplayRefused) as caught:
                    replay.ReplayRunner.open(self.config(folder))
                self.assertEqual(caught.exception.code, "INTEGRITY_FAILURE")
                self.assertEqual({p.name: p.read_bytes() for p in folder.iterdir()}, before)           # the damaged data is left as it is
        self.assertFalse((self.state / STATE_FILE).exists())

    def test_the_committed_fixtures_are_never_modified_by_a_replay(self):
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in FIXTURE_DIR.iterdir()}
        self.open().run(SCRIPT)
        self.assertEqual({p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in FIXTURE_DIR.iterdir()}, before)

    def test_look_ahead_poisoning_changes_nothing(self):
        late = {"observed_at": "2031-03-03T10:15:00+05:30", "available_at": "2031-03-03T10:30:00+05:30", "bid": "0.50", "ask": "0.55"}      # old quote delivered late
        files = {"q.jsonl": ("QUOTE_SNAPSHOT", [
            quote(),                                                                                                         # honest: 100.00 / 100.20 at 10:15
            quote(**late),                                                                                                   # same key and time: a conflict with the honest one
            quote(strike="24000", option_type="PE", observed_at="2031-03-03T10:15:00+05:30", available_at="2031-03-03T10:25:00+05:30", bid="1.00", ask="1.05"),   # delivered late
            quote(strike="23000", observed_at="2031-03-11T10:15:00+05:30", available_at="2031-03-11T10:15:00+05:30", bid="1.00", ask="1.05"),               # after the cut-off
            quote(strike="22000", observed_at="2031-03-03T10:20:00+05:30", available_at="2031-03-03T10:19:00+05:30", bid="1.00", ask="1.05"),               # available before observed
        ]), "e.jsonl": ("EOD_CONTRACT_DAY", [eod(close="0.05", settlement_price="0.05", low="0.05")])}
        runner = self.open(self.make_set("poison", files), end=at(10, 30))
        script = [replay.Intent(at(10, 15, 1), CE, "BUY", 1), replay.Intent(at(10, 15, 1), PE, "BUY", 1), replay.Intent(at(10, 25), PE, "BUY", 1), replay.Intent(at(10, 30), PE, "BUY", 1)]
        runner.run(script)
        prices = [Decimal(f["fill_price"]) for f in self.kinds(runner, "FILL")]
        self.assertEqual(prices, [])                                                       # the honest CE quote was in conflict, so excluded; the PE ones were late/stale
        self.assertTrue(all(p > Decimal("50") for p in prices))
        self.assertEqual({(e["contract"], e["code"]) for e in self.kinds(runner, "INTENT_REFUSED")},
                         {(CE, "NO_VISIBLE_QUOTE"), (PE, "NO_VISIBLE_QUOTE"), (PE, "BOUNDARY_REJECTED")})
        reasons = sorted(e.reason for e in runner.result.exclusions)
        self.assertEqual(reasons, ["CONFLICTING_ROW_IN_FILE", "CONFLICTING_ROW_IN_FILE", "FUTURE_OBSERVATION", "INCONSISTENT_TIMES"])
        announced = {e["ref"] for e in self.kinds(runner, "RECORD_AVAILABLE")}
        self.assertNotIn("q.jsonl#2", announced)
        self.assertNotIn("q.jsonl#1", announced)

    def test_a_late_delivered_quote_is_used_only_from_its_availability_and_only_if_fresh(self):
        row = quote(strike="24000", option_type="PE", observed_at="2031-03-03T10:15:00+05:30", available_at="2031-03-03T10:15:03+05:30", bid="60.00", ask="60.25")
        runner = self.open(self.make_set("late", {"q.jsonl": ("QUOTE_SNAPSHOT", [row])}))
        runner.run([replay.Intent(at(10, 15, 2), PE, "BUY", 1), replay.Intent(at(10, 15, 4), PE, "BUY", 1)])
        self.assertEqual([(e["type"], e.get("code")) for e in runner.events if e["type"] in ("FILL", "INTENT_REFUSED")], [("INTENT_REFUSED", "NO_VISIBLE_QUOTE"), ("FILL", None)])

    def test_conflicting_data_is_excluded_and_cannot_be_traded(self):
        runner = self.open(self.copy_fixture("conf"), end=at(11, 5), as_of=AS_OF)
        runner.run([replay.Intent(at(11, 0, 1), CE, "BUY", 1)])
        self.assertEqual([r for r in runner.result.records if r.observed_at == at(11, 0)], [])
        self.assertEqual([(e["stage"], e["code"]) for e in self.kinds(runner, "INTENT_REFUSED")], [("BRIDGE", "NOT_FILL_ELIGIBLE")])      # newest visible CE quote is the 10:17 one with no sizes
        conflicts = [e for e in runner.result.exclusions if e.reason == "CONFLICTING_ROW_IN_FILE"]
        self.assertEqual(sorted((e.source_file, e.source_row) for e in conflicts), [("SYNTHETIC_quotes_conflict.jsonl", 1), ("SYNTHETIC_quotes_conflict.jsonl", 2)])

    def test_every_excluded_record_is_in_the_ledger_with_reason_and_source(self):
        runner = self.open()
        lines = [json.loads(line) for line in runner.ledger_lines().splitlines()]
        self.assertEqual(len(lines), len(runner.result.exclusions))
        self.assertEqual(len(lines), 22)
        for line in lines:
            self.assertTrue(line["reason"] and line["source_file"] and line["source_file_sha256"])
            self.assertTrue(line["source_row"] is None or line["source_row"] >= 1)
        self.assertEqual({line["source_file"] for line in lines}, {"SYNTHETIC_quotes.jsonl", "SYNTHETIC_quotes_invalid.jsonl", "SYNTHETIC_quotes_conflict.jsonl", "SYNTHETIC_eod.jsonl"})


class TestEngineBoundary(Base):
    def test_a_replay_account_refuses_a_view_from_another_source_even_if_attested(self):
        runner = self.open()
        runner.advance_to(at(10, 15, 2))
        market = runner.engine and __import__("niftypaper.historical.bridge", fromlist=["x"]).executable_view(
            runner._view, PE, allowed_origin_classes=frozenset({"SYNTHETIC_FIXTURE"}), permitted=True).view
        other = reattest(market, provider="historical:some_other_source")
        self.assertEqual(runner.engine.simulate(TradeRequest(PE, "BUY", 1), other).code, "ACCOUNT_MISMATCH")
        self.assertEqual(runner.engine.simulate(TradeRequest(PE, "BUY", 1), dataclasses.replace(market, provider="historical:some_other_source")).code, "ACCOUNT_MISMATCH")
        for origin in (ORIGIN_PROVIDER, ORIGIN_SYNTHETIC):
            self.assertEqual(runner.engine.simulate(TradeRequest(PE, "BUY", 1), dataclasses.replace(market, origin=origin)).code, "ACCOUNT_MISMATCH")
        hand_built = MarketView(ORIGIN_SYNTHETIC, "synthetic", "SIMULATED", "SIMULATED", PE, Decimal("60.00"), Decimal("60.25"), None, None, None, Decimal("0.05"), 65, None, None, None, True, True)
        self.assertEqual(runner.engine.simulate(TradeRequest(PE, "BUY", 1), hand_built).code, "ACCOUNT_MISMATCH")
        self.assertEqual(runner.engine.ledger, [])

    def test_a_historical_account_ignores_the_replay_unless_the_view_is_fresh_against_the_replay_clock(self):
        runner = self.open()
        runner.run([replay.Intent(at(10, 15, 2), PE, "BUY", 1)])
        self.assertEqual(len(self.kinds(runner, "FILL")), 1)
        runner.advance_to(at(10, 15, 20))
        event = runner.try_trade(replay.Intent(at(10, 15, 20), PE, "BUY", 1))
        self.assertEqual((event["stage"], event["code"]), ("ENGINE", "BOUNDARY_REJECTED"))


class TestIsolationAndRecovery(Base):
    LIMITS = RiskLimits(daily_loss_fraction=Decimal("0.006"))                                # a 6,000 daily budget on 1,000,000

    def two_buys(self, runner):
        return runner.run([replay.Intent(at(10, 15, 2), PE, "BUY", 1), replay.Intent(at(10, 15, 31), CE, "BUY", 1)])

    def test_the_daily_budget_and_all_limits_apply_in_a_replay(self):
        runner = self.open(limits=self.LIMITS)
        self.two_buys(runner)
        self.assertEqual([(e["type"], e.get("code")) for e in runner.events if e["type"] in ("FILL", "INTENT_REFUSED")], [("FILL", None), ("INTENT_REFUSED", "RISK_DAILY_LOSS")])
        runner2 = self.open(self.copy_fixture("two"), state_dir=self.tmp / "other_state")
        event = runner2.run([replay.Intent(at(10, 15, 31), CE, "BUY", 3)])[-1]
        self.assertEqual(event["code"], "RISK_MAX_LOTS")

    def test_refused_trades_change_nothing_on_disk_or_in_the_account(self):
        runner = self.open(limits=self.LIMITS)
        runner.run([replay.Intent(at(10, 15, 2), PE, "BUY", 1)])
        path = self.state / STATE_FILE
        data, cash, positions = path.read_bytes(), runner.engine.cash, dict(runner.engine.positions)
        runner.run([replay.Intent(at(10, 15, 31), CE, "BUY", 1)])                         # refused by the daily budget
        self.assertEqual((path.read_bytes(), runner.engine.cash, dict(runner.engine.positions)), (data, cash, positions))

    def test_a_restart_restores_the_account_and_does_not_reset_the_daily_budget(self):
        first = self.open(limits=self.LIMITS)
        first.run([replay.Intent(at(10, 15, 2), PE, "BUY", 1)])
        cash, held = first.engine.cash, dict(first.engine.positions)
        first.close()
        second = self.open(limits=self.LIMITS, start=at(10, 15, 2))
        self.assertEqual((second.engine.restored, second.engine.cash, dict(second.engine.positions)), (1, cash, held))
        second.run([replay.Intent(at(10, 15, 31), CE, "BUY", 1)])
        self.assertEqual(self.kinds(second, "INTENT_REFUSED")[-1]["code"], "RISK_DAILY_LOSS")                  # the earlier premium still counts
        self.assertEqual(self.kinds(second, "FILL"), [])

    def test_a_restart_before_the_last_saved_trade_is_refused(self):
        first = self.open()
        first.run([replay.Intent(at(10, 15, 2), PE, "BUY", 1)])
        first.close()
        with self.assertRaises(replay.ReplayRefused) as caught:
            replay.ReplayRunner.open(self.config(start=START))
        self.assertEqual(caught.exception.code, "STATE_AHEAD_OF_CLOCK")
        again = self.open(start=at(10, 15, 2))                                                # and the lock was released by the refusal
        self.assertEqual(again.engine.restored, 1)

    def test_a_second_replay_on_the_same_state_is_locked_out(self):
        first = self.open()
        second = self.open()
        self.assertIn("IN_USE", second.engine.lock_reason)
        second.run([replay.Intent(at(10, 15, 2), PE, "BUY", 1)])
        self.assertEqual(self.kinds(second, "INTENT_REFUSED")[-1]["code"], "STATE_UNAVAILABLE")
        self.assertTrue(self.kinds(second, "STATE_LOCKED"))
        first.run([replay.Intent(at(10, 15, 2), PE, "BUY", 1)])
        self.assertEqual(len(self.kinds(first, "FILL")), 1)

    def test_corrupt_or_truncated_state_locks_the_replay_and_is_left_alone(self):
        for name, content in (("garbage", b"not json\n"), ("truncated", b'{"kind": "header", "schema": 1, "starting_balance": "1000000"'), ("empty line", b"\n"),
                              ("no header", b'{"kind": "trade"}\n')):
            with self.subTest(name):
                state = self.tmp / ("s_" + name.replace(" ", "_"))
                state.mkdir()
                (state / STATE_FILE).write_bytes(content)
                runner = self.open(state_dir=state)
                self.assertTrue(runner.engine.lock_reason, name)
                self.assertTrue(self.kinds(runner, "STATE_LOCKED"))
                runner.run([replay.Intent(at(10, 15, 2), PE, "BUY", 1)])
                self.assertEqual(self.kinds(runner, "INTENT_REFUSED")[-1]["code"], "STATE_UNAVAILABLE")
                self.assertEqual((state / STATE_FILE).read_bytes(), content)
                runner.close()

    def test_a_tampered_state_file_is_detected_on_restart(self):
        first = self.open()
        first.run([replay.Intent(at(10, 15, 2), PE, "BUY", 1), replay.Intent(at(10, 15, 31), CE, "BUY", 1)])
        first.close()
        path = self.state / STATE_FILE
        lines = path.read_bytes().split(b"\n")
        path.write_bytes(b"\n".join([lines[0], lines[2], lines[1], b""]))                    # swap the two trades: sequence numbers no longer follow
        second = self.open(start=at(10, 15, 31))
        self.assertIn("INCONSISTENT", second.engine.lock_reason)

    def test_state_from_another_account_or_source_is_not_accepted_as_a_replay_account(self):
        self.state.mkdir()
        path = self.state / STATE_FILE
        synthetic = PaperEngine(Decimal("1000000"), store=JsonlStore(path, Decimal("1000000"), account="synthetic"), origin=ORIGIN_SYNTHETIC)
        view = MarketView(ORIGIN_SYNTHETIC, "synthetic", "SIMULATED", "SIMULATED", CE, Decimal("100.00"), Decimal("100.20"), None, 650, 650, Decimal("0.05"), 65, None, None, None, True, True)
        self.assertTrue(synthetic.simulate(TradeRequest(CE, "BUY", 1), view).accepted)
        synthetic.close()
        runner = self.open()
        self.assertIn("DIFFERENT_ACCOUNT", runner.engine.lock_reason)
        runner.run([replay.Intent(at(10, 15, 2), PE, "BUY", 1)])
        self.assertEqual(self.kinds(runner, "FILL"), [])
        runner.close()
        # the reverse: a replay account's file is not usable by a provider or synthetic account
        other_state = self.tmp / "rs"
        again = self.open(state_dir=other_state)
        again.run([replay.Intent(at(10, 15, 2), PE, "BUY", 1)])
        again.close()
        for origin, account in ((ORIGIN_PROVIDER, "fyers"), (ORIGIN_SYNTHETIC, "synthetic")):
            engine = PaperEngine(Decimal("1000000"), store=JsonlStore(other_state / STATE_FILE, Decimal("1000000"), account=account), origin=origin)
            self.assertTrue(engine.lock_reason, origin)
            engine.close()
        # a different source label inside a replay account's file
        engine = PaperEngine(Decimal("1000000"), store=JsonlStore(other_state / STATE_FILE, Decimal("1000000"), account="replay_synthetic_fixture"),
                             origin=ORIGIN_HISTORICAL, provider="historical:another_source")
        self.assertIn("DIFFERENT_ACCOUNT", engine.lock_reason)
        engine.close()


class TestDeterminism(Base):
    def run_once(self, name, **kw):
        state = self.tmp / name
        runner = replay.ReplayRunner.open(self.config(state_dir=state, **kw))
        try:
            runner.run(SCRIPT)
            return runner.event_lines(), runner.ledger_lines(), runner.digest(), (state / STATE_FILE).read_bytes()
        finally:
            runner.close()

    def test_identical_fixtures_and_configuration_give_identical_output(self):
        first = self.run_once("a")
        for name in ("b", "c"):
            self.assertEqual(self.run_once(name), first)
        self.assertEqual(self.run_once("d", limits=RiskLimits()), first)                 # explicitly passing the default limits changes nothing
        self.assertNotEqual(self.run_once("e", slippage_ticks=2)[2], first[2])           # but a different configuration is visible in the digest

    def test_a_copy_of_the_fixtures_gives_the_same_events_and_ledger(self):
        original = self.run_once("a")
        copy = self.copy_fixture("copy")
        runner = replay.ReplayRunner.open(self.config(copy, state_dir=self.tmp / "z"))
        runner.run(SCRIPT)
        self.assertEqual((runner.event_lines(), runner.ledger_lines()), original[:2])
        runner.close()

    def test_output_has_no_wall_clock_time_or_machine_paths(self):
        events, ledger, _, state = self.run_once("a")
        for text in (events, ledger, state.decode()):
            self.assertNotIn(str(self.tmp), text)
            self.assertNotIn("2026-", text)                                              # the fixtures are 2031; a 2026 date would be the real clock
        self.assertIn("2031-03-03T10:15:02+05:30", events)

    def test_the_replay_never_reads_the_real_clock(self):
        import niftypaper.historical.replay as module
        text = Path(module.__file__).read_text(encoding="utf-8")
        for banned in ("datetime.now", "utcnow", "time.time", "os.environ"):
            self.assertNotIn(banned, text)


if __name__ == "__main__":
    unittest.main()
