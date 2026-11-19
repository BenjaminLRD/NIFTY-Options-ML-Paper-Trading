"""Follow-up safety audit tests: the exit-slippage allowance and exit-fee estimate, the single-writer lock (including real separate processes),
and isolation between the synthetic account and each provider's account. All trades are virtual and all prices are made up."""
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from niftypaper.dashboard.sample_data import SyntheticProvider
from niftypaper.dashboard.server import App
from niftypaper.paper.engine import BUY, SELL, CostModel, PaperEngine, RiskLimits, TradeRequest
from niftypaper.paper.store import JsonlStore, StoreError
from tests import ROOT, SRC
from tests.test_daily_risk import UNIT, budget_engine, file_bytes, snapshot
from tests.test_modes import Served, TestProvider
from tests.test_paper_engine import Clock, buy, live_view, quote_view, sell, view

CENT = Decimal("0.01")
COSTS = CostModel()


class TestExitSlippageAllowance(unittest.TestCase):
    def test_the_allowance_never_changes_the_committed_total_or_the_exact_budget_boundary(self):
        """The worst case already assumes the whole premium is lost, and no exit price can lose more, so slippage must not be added there again."""
        for ticks in (0, 1, 2, 50, 100000):
            e, _ = budget_engine(UNIT, exit_slippage_ticks=ticks)
            self.assertTrue(e.simulate(buy(), quote_view(SYMBOL, "100.00")).accepted, ticks)                     # exactly the budget still fits
            plain = e.risk_report().committed
            marked = e.risk_report({SYMBOL: quote_view(SYMBOL, "40.00")}).committed
            self.assertEqual((plain, marked), (UNIT, UNIT), ticks)
            e2, _ = budget_engine(UNIT - CENT, exit_slippage_ticks=ticks)
            before = snapshot(e2)
            self.assertEqual(e2.simulate(buy(), quote_view(SYMBOL, "100.00")).code, "RISK_DAILY_LOSS", ticks)    # one paisa less does not
            self.assertEqual(snapshot(e2), before)

    def test_the_allowance_only_moves_loss_from_remaining_to_unrealised_and_never_below_zero_value(self):
        marks = {SYMBOL: quote_view(SYMBOL, "40.00")}                       # bid 40: quoted value 2,600.00
        seen = {}
        for ticks in (0, 2, 100000):
            e, _ = budget_engine(Decimal("50000"), exit_slippage_ticks=ticks)
            e.simulate(buy(), quote_view(SYMBOL, "100.00"))
            basis = e.positions[SYMBOL].basis
            r = e.risk_report(marks)
            seen[ticks] = r
            self.assertEqual(r.unrealized_loss + r.remaining_max_loss, basis, ticks)               # always the premium paid: nothing counted twice
            self.assertGreaterEqual(r.remaining_max_loss, 0)
            self.assertEqual(r.exit_slippage_ticks, ticks)
        self.assertEqual(seen[0].unrealized_loss, Decimal("6509.75") - Decimal("2600.00"))
        self.assertEqual(seen[2].unrealized_loss, Decimal("6509.75") - Decimal("2593.50"))           # (40.00 - 2 x 0.05) x 65
        self.assertEqual((seen[2].exit_slippage_allowance, seen[0].exit_slippage_allowance), (Decimal("6.50"), 0))
        self.assertEqual((seen[100000].unrealized_loss, seen[100000].remaining_max_loss), (Decimal("6509.75"), 0))
        self.assertEqual(seen[100000].exit_slippage_allowance, Decimal("2600.00"))                 # cannot take off more than the position is quoted at
        self.assertEqual(len({r.committed for r in seen.values()}), 1)

    def test_a_gain_is_not_cushioned_or_credited(self):
        e, _ = budget_engine(Decimal("50000"), exit_slippage_ticks=2)
        e.simulate(buy(), quote_view(SYMBOL, "100.00"))
        r = e.risk_report({SYMBOL: quote_view(SYMBOL, "150.00")})
        self.assertEqual((r.unrealized_loss, r.exit_slippage_allowance, r.remaining_max_loss), (0, 0, e.positions[SYMBOL].basis))

    def test_the_limit_reached_boundary_follows_the_allowance_exactly(self):
        def reached(ticks, budget):
            e, _ = budget_engine(Decimal("100000"), exit_slippage_ticks=ticks)
            e.simulate(buy(), quote_view(SYMBOL, "100.00"))
            incurred = e.risk_report({SYMBOL: quote_view(SYMBOL, "90.00")}).incurred
            e.limits = RiskLimits(max_lots_per_trade=5, max_open_positions=9, max_exposure_fraction=Decimal("1"),
                                  daily_loss_fraction=budget / Decimal("1000000") if budget is not None else Decimal("1"), exit_slippage_ticks=ticks)
            return e, incurred
        e0, incurred0 = reached(0, None)
        e2, incurred2 = reached(2, None)
        self.assertEqual(incurred2 - incurred0, Decimal("6.50"))                       # the allowance: 2 ticks x 0.05 x 65
        for ticks, incurred in ((0, incurred0), (2, incurred2)):
            for delta, expected in ((Decimal("0"), True), (CENT, False)):
                e, _ = reached(ticks, incurred + delta)
                e.limits = RiskLimits(max_lots_per_trade=5, max_open_positions=9, max_exposure_fraction=Decimal("1"),
                                      daily_loss_fraction=(incurred + delta) / Decimal("1000000"), exit_slippage_ticks=ticks)
                self.assertEqual(e.risk_report({SYMBOL: quote_view(SYMBOL, "90.00")}).limit_reached, expected, (ticks, delta))
        # between the two incurred figures the allowance decides: reached with 2 ticks, not with 0
        budget = incurred0 + Decimal("3.00")
        for ticks, expected in ((0, False), (2, True)):
            e, _ = reached(ticks, budget)
            self.assertEqual(e.risk_report({SYMBOL: quote_view(SYMBOL, "90.00")}).limit_reached, expected, ticks)
        self.assertEqual(e.simulate(buy(1, "NEW"), quote_view("NEW", "10.00"), marks={SYMBOL: quote_view(SYMBOL, "90.00")}).code, "RISK_DAILY_LIMIT_REACHED")

    def test_the_allowance_must_be_a_whole_number_of_ticks(self):
        for bad in (-1, 1.5, True, "2", None):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    RiskLimits(exit_slippage_ticks=bad)
        self.assertEqual(RiskLimits(exit_slippage_ticks=0).exit_slippage_ticks, 0)
        self.assertEqual(RiskLimits().exit_slippage_ticks, 2)


class TestExitFeesCannotExceedTheEstimate(unittest.TestCase):
    """Audit finding: the exit-fee estimate once assumed a single exit fee per position, but a position can be sold a lot at a time, each trade paying the flat fee."""

    def test_the_estimate_is_one_flat_fee_per_lot(self):
        self.assertEqual(COSTS.exit_estimate(Decimal("6509.75"), 1), COSTS.cost(Decimal("6509.75")))
        self.assertEqual(COSTS.exit_estimate(Decimal("13019.50"), 2), Decimal("40") + Decimal("6.51"))             # 2 x 20 + 0.05% of 13,019.50 (6.50975 -> 6.51)

    def test_the_budget_boundary_for_a_two_lot_buy_includes_two_exit_fees(self):
        turnover = Decimal("13019.50")                                          # 2 lots (130) at fill 100.15
        total = turnover + COSTS.cost(turnover) + COSTS.exit_estimate(turnover, 2)
        self.assertEqual(total, Decimal("13092.52"))                            # 13,019.50 + buy fee 26.51 + exit fees 2 x 20 + 6.51
        e, _ = budget_engine(total)
        self.assertTrue(e.simulate(buy(2), quote_view(SYMBOL, "100.00")).accepted)       # exactly the budget fits
        self.assertEqual(e.risk_report().committed, total)
        e2, _ = budget_engine(total - CENT)
        before = snapshot(e2)
        self.assertEqual(e2.simulate(buy(2), quote_view(SYMBOL, "100.00")).code, "RISK_DAILY_LOSS")
        self.assertEqual(snapshot(e2), before)
        e3, _ = budget_engine(total - Decimal("20.00"))                          # the old single-exit-fee estimate would have let this through
        self.assertEqual(e3.simulate(buy(2), quote_view(SYMBOL, "100.00")).code, "RISK_DAILY_LOSS")

    def test_exiting_lot_by_lot_never_loses_more_than_the_committed_estimate(self):
        for seed in range(150):
            rnd = random.Random(seed)
            e, _ = budget_engine(Decimal("1000000"))
            lots = rnd.randint(1, 5)
            e.simulate(buy(lots), quote_view(SYMBOL, Decimal(rnd.randrange(60, 4000)) / 20))
            committed = e.risk_report().committed
            while e.positions:
                bid = Decimal(rnd.choice([10, 11, 20, 400, 2000, 6000])) / 20           # from almost nothing up to a profit
                e.simulate(sell(1), quote_view(SYMBOL, bid))
                self.assertLessEqual(-e.realized_today(), committed, seed)
                self.assertLessEqual(e.risk_report().committed, committed, seed)         # and an exit never raises the committed worst case
                committed = e.risk_report().committed

    def test_exits_never_raise_the_committed_total_with_several_positions(self):
        for seed in range(100):
            rnd = random.Random(seed)
            e, _ = budget_engine(Decimal("1000000"))
            for symbol in "ABC":
                e.simulate(buy(rnd.randint(1, 3), symbol), quote_view(symbol, Decimal(rnd.randrange(60, 2000)) / 20))
            while e.positions:
                symbol = rnd.choice(sorted(e.positions))
                before = e.risk_report().committed
                result = e.simulate(sell(1, symbol), quote_view(symbol, Decimal(rnd.choice([10, 20, 400, 3000])) / 20))
                self.assertTrue(result.accepted, (seed, result.code))
                self.assertLessEqual(e.risk_report().committed, before, seed)


def worker_env():
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(SRC), str(ROOT)])
    return env


WORKER = r'''
import json, sys, time
from decimal import Decimal
from niftypaper.paper.engine import PaperEngine, RiskLimits
from niftypaper.paper.store import JsonlStore
from tests.test_paper_engine import Clock, buy, quote_view
path, n, hold = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
limits = RiskLimits(max_lots_per_trade=5, max_open_positions=9, max_exposure_fraction=Decimal("1"), daily_loss_fraction=Decimal("1"))
e = PaperEngine(Decimal("1000000"), limits=limits, clock=Clock(), origin="SYNTHETIC", store=JsonlStore(path, 1000000, account="synthetic"))
print(json.dumps({"locked": e.lock_reason, "restored": e.restored}), flush=True)
accepted = 0
if not e.lock_reason:
    for i in range(n):
        symbol = "S%d" % (i % 3)
        accepted += e.simulate(buy(1, symbol), quote_view(symbol, "100.00")).accepted
    print(json.dumps({"accepted": accepted}), flush=True)
    time.sleep(hold)
e.close()
'''


def spawn(path, n, hold):
    return subprocess.Popen([sys.executable, "-B", "-c", WORKER, str(path), str(n), str(hold)], cwd=str(ROOT), env=worker_env(),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def first_lines(proc, count):
    return [json.loads(proc.stdout.readline()) for _ in range(count)]


def finish(proc):
    out, err = proc.communicate(timeout=60)
    return [json.loads(line) for line in out.splitlines() if line.strip()], err


def replay_cleanly(path):
    """Open the file in this process with a fresh engine: it must load without locking, with a continuous sequence."""
    e = PaperEngine(Decimal("1000000"), origin="SYNTHETIC", clock=Clock(), store=JsonlStore(path, 1000000, account="synthetic"))
    try:
        return e.lock_reason, [r.seq for r in e.ledger], e.restored
    finally:
        e.close()


class TestSingleWriterLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "paper_synthetic.jsonl"

    def store(self, **kw):
        s = JsonlStore(self.path, 1_000_000, **kw)
        self.addCleanup(s.close)
        return s

    def test_a_second_store_on_the_same_file_is_refused_until_the_first_lets_go(self):
        first, second = self.store(), self.store()
        first.acquire()
        with self.assertRaises(StoreError) as caught:
            second.acquire()
        self.assertEqual(caught.exception.code, "IN_USE")
        first.close()
        first.close()                                                           # closing twice is harmless
        second.acquire()                                                        # now it is free
        with self.assertRaises(StoreError):
            self.store().acquire()

    def test_an_engine_that_finds_the_file_in_use_locks_and_does_not_touch_it(self):
        owner = PaperEngine(Decimal("1000000"), clock=Clock(), store=self.store())
        self.assertTrue(owner.simulate(buy(), view()).accepted)
        saved = file_bytes(self.path)
        intruder = PaperEngine(Decimal("1000000"), clock=Clock(), store=self.store())
        self.assertIn("IN_USE", intruder.lock_reason)
        self.assertEqual(intruder.simulate(buy(), view()).code, "STATE_UNAVAILABLE")
        self.assertEqual(file_bytes(self.path), saved)
        self.assertTrue(owner.simulate(buy(), view()).accepted)                 # the owner is unaffected
        self.assertEqual(replay_cleanly_after(owner, self.path), (None, [1, 2], 2))

    def test_appending_without_the_lock_is_refused(self):
        with self.assertRaises(StoreError) as caught:
            self.store().append({"seq": 1})
        self.assertEqual(caught.exception.code, "NOT_LOCKED")
        self.assertFalse(self.path.exists())

    def test_a_file_changed_by_something_else_stops_trading_and_nothing_more_is_written(self):
        e = PaperEngine(Decimal("1000000"), clock=Clock(), store=self.store())
        e.simulate(buy(), view())
        with open(self.path, "ab") as handle:                                   # someone else appends a line behind the dashboard's back
            handle.write(b'{"kind": "trade"}\n')
        tampered = file_bytes(self.path)
        before = snapshot(e)
        result = e.simulate(buy(), view())
        self.assertEqual(result.code, "PERSISTENCE_FAILED")
        self.assertIn("CHANGED", result.message)
        self.assertEqual((e.cash, len(e.ledger)), (before[0], 1))               # the refused trade was not applied
        self.assertIn("changed by something else", e.lock_reason)
        self.assertEqual(e.simulate(buy(), view()).code, "STATE_UNAVAILABLE")  # and from now on every trade is refused
        self.assertEqual(file_bytes(self.path), tampered)

    def test_a_truncated_file_is_also_noticed(self):
        e = PaperEngine(Decimal("1000000"), clock=Clock(), store=self.store())
        e.simulate(buy(), view())
        self.path.write_bytes(self.path.read_bytes()[:-5])
        self.assertEqual(e.simulate(buy(), view()).code, "PERSISTENCE_FAILED")
        self.assertTrue(e.lock_reason)

    def test_two_dashboards_started_on_one_state_directory_fail_closed(self):
        from niftypaper.dashboard.sample_data import SyntheticProvider as P
        clock = Clock()

        def app():
            return App(P(), clock=clock, mode="synthetic", provider_id="synthetic", factories={"synthetic": lambda: P()},
                       engine_factory=lambda key: PaperEngine(Decimal("1000000"), clock=clock, origin="SYNTHETIC",
                                                              store=JsonlStore(Path(self.tmp.name) / f"paper_{key}.jsonl", 1_000_000, account=key)))
        first, second = app(), app()
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        first.handle_trade({"symbol": "SYN-CE-24000", "side": "BUY", "lots": "1"})
        second.handle_trade({"symbol": "SYN-CE-24000", "side": "BUY", "lots": "1"})
        self.assertEqual(len(first.engine.ledger), 1)
        self.assertEqual(second.engine.ledger, [])
        self.assertIn("locked", second.notice.lower())
        with Served(second) as s:
            page = s.request("GET", "/")[2]
        self.assertIn("IN_USE", page)
        self.assertIn("LOCKED", page)
        self.assertNotIn('action="/paper/trade"', page)


def replay_cleanly_after(owner, path):
    """Close the owner (as a stopping process would) and check the file loads cleanly in a fresh engine."""
    owner.close()
    return replay_cleanly(path)


class TestSeparateProcesses(unittest.TestCase):
    """Real operating-system processes, because that is what the lock is for."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "paper_synthetic.jsonl"
        self.procs = []
        self.addCleanup(lambda: [p.kill() for p in self.procs if p.poll() is None])

    def run_worker(self, n, hold):
        proc = spawn(self.path, n, hold)
        self.procs.append(proc)
        return proc

    def test_a_second_process_cannot_write_while_the_first_holds_the_file(self):
        owner = self.run_worker(3, 5)
        opened, traded = first_lines(owner, 2)
        self.assertIsNone(opened["locked"])
        self.assertEqual(traded["accepted"], 3)
        saved = file_bytes(self.path)
        intruder = self.run_worker(3, 0)
        reports, _ = finish(intruder)
        self.assertIn("IN_USE", reports[0]["locked"])
        self.assertEqual(len(reports), 1)                                       # it never traded
        self.assertEqual(file_bytes(self.path), saved)
        finish(owner)
        third = self.run_worker(2, 0)                                           # once the owner has gone the file is free and fully restored
        reports, _ = finish(third)
        self.assertEqual((reports[0]["locked"], reports[0]["restored"], reports[1]["accepted"]), (None, 3, 2))
        self.assertEqual(replay_cleanly(self.path), (None, [1, 2, 3, 4, 5], 5))

    def test_a_killed_process_leaves_no_stale_lock(self):
        owner = self.run_worker(4, 60)
        first_lines(owner, 2)
        owner.kill()                                                            # no chance to clean up
        owner.communicate(timeout=30)
        successor = self.run_worker(1, 0)
        reports, _ = finish(successor)
        self.assertEqual((reports[0]["locked"], reports[0]["restored"], reports[1]["accepted"]), (None, 4, 1))
        self.assertEqual(replay_cleanly(self.path), (None, [1, 2, 3, 4, 5], 5))

    def test_several_processes_started_together_never_corrupt_the_file(self):
        procs = [self.run_worker(5, 0.4) for _ in range(6)]
        results = [finish(p) for p in procs]
        owners = [r for r, _ in results if r and r[0]["locked"] is None]
        locked_out = [r for r, _ in results if r and r[0]["locked"]]
        self.assertTrue(owners)
        self.assertEqual(len(owners) + len(locked_out), 6, [e for _, e in results])
        self.assertTrue(all("IN_USE" in r[0]["locked"] for r in locked_out))
        accepted = sum(r[1]["accepted"] for r in owners)
        lock_reason, seqs, restored = replay_cleanly(self.path)
        self.assertIsNone(lock_reason)                                          # the file replays cleanly
        self.assertEqual(seqs, list(range(1, accepted + 1)))                     # no lost, duplicated or interleaved trade
        self.assertEqual(restored, accepted)
        raw = self.path.read_bytes().decode().splitlines()
        self.assertEqual(sum(1 for line in raw if json.loads(line)["kind"] == "header"), 1)


class TestAccountIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.apps = []

    def new_app(self, ids=("live-test",)):
        clock, tmp = self.clock, self.tmp.name
        providers = {i: TestProvider(clock) for i in ids}
        factories = {"synthetic": lambda: SyntheticProvider(), **{i: (lambda p=p: p) for i, p in providers.items()}}

        def engine_for(key):
            return PaperEngine(Decimal("1000000"), clock=clock, origin="SYNTHETIC" if key == "synthetic" else "PROVIDER",
                               store=JsonlStore(Path(tmp) / f"paper_{key}.jsonl", 1_000_000, account=key))
        app = App(SyntheticProvider(), clock=clock, mode="synthetic", provider_id="synthetic", factories=factories, engine_factory=engine_for,
                  permissions={i: (True, "test record") for i in ids})
        self.apps.append(app)
        self.addCleanup(app.close)
        return app

    def close_all(self):
        for app in self.apps:
            app.close()

    def test_each_data_source_has_its_own_file_cash_positions_ledger_and_risk_budget(self):
        app = self.new_app(("live-test", "live-test-b"))
        app.handle_trade({"symbol": "SYN-CE-23800", "side": "BUY", "lots": "1"})                   # uses most of the synthetic budget
        synthetic = app.engine
        app.switch("realtime", "live-test")
        a = app.engine
        self.assertIsNot(a, synthetic)
        self.assertEqual((a.ledger, a.positions, a.cash, a.risk_report().committed), ([], {}, Decimal("1000000"), 0))     # nothing came across
        app.handle_trade({"symbol": "TEST-OPT-1", "side": "BUY", "lots": "1"})
        self.assertEqual(len(a.ledger), 1)
        self.assertEqual(len(synthetic.ledger), 1)
        self.assertEqual({r.data_origin for r in a.ledger}, {"PROVIDER"})
        self.assertEqual({r.data_origin for r in synthetic.ledger}, {"SYNTHETIC"})
        app.switch("realtime", "live-test-b")
        b = app.engine
        self.assertEqual((b.ledger, b.positions, b.cash), ([], {}, Decimal("1000000")))              # a second provider is a separate account
        self.assertEqual(len({id(synthetic), id(a), id(b)}), 3)
        app.switch("realtime", "live-test")
        self.assertIs(app.engine, a)                                                                # switching back finds the same account, not a new one
        app.switch("synthetic")
        self.assertIs(app.engine, synthetic)
        files = sorted(p.name for p in Path(self.tmp.name).iterdir() if not p.name.endswith(".lock"))
        self.assertEqual(files, ["paper_live-test.jsonl", "paper_synthetic.jsonl"])                  # b never traded, so it has no file
        self.assertEqual(synthetic.risk_report().committed, Decimal("18267.95"))                     # the synthetic budget was used; the provider one was not
        self.assertEqual(a.risk_report().budget - a.risk_report().committed, a.risk_report().headroom)
        self.assertNotEqual(a.risk_report().committed, synthetic.risk_report().committed)

    def test_the_ledger_endpoint_and_page_show_only_the_current_account(self):
        app = self.new_app()
        app.handle_trade({"symbol": "SYN-CE-23800", "side": "BUY", "lots": "1"})
        app.handle_mode({"mode": "realtime", "provider": "live-test"})                 # the same path the page's button takes
        with Served(app) as s:
            body = json.loads(s.request("GET", "/api/ledger")[2])
            page = s.request("GET", "/")[2]
        self.assertEqual((body["mode"], body["trades"]), ("provider", []))
        self.assertNotIn("SYN-CE-23800", page)

    def test_a_restart_restores_every_account_separately(self):
        app = self.new_app()
        app.handle_trade({"symbol": "SYN-CE-23800", "side": "BUY", "lots": "1"})
        app.switch("realtime", "live-test")
        app.handle_trade({"symbol": "TEST-OPT-1", "side": "BUY", "lots": "1"})
        before = {k: (e.cash, [r.seq for r in e.ledger], e.risk_report().committed) for k, e in app._engines.items()}
        self.close_all()
        again = self.new_app()
        again.switch("realtime", "live-test")
        after = {"live-test": (again.engine.cash, [r.seq for r in again.engine.ledger], again.engine.risk_report().committed)}
        again.switch("synthetic")
        after["synthetic"] = (again.engine.cash, [r.seq for r in again.engine.ledger], again.engine.risk_report().committed)
        self.assertEqual(after, before)
        self.assertEqual((again.engine.restored, again.engine.origin), (1, "SYNTHETIC"))

    def test_a_form_left_open_in_one_mode_cannot_trade_in_another(self):
        app = self.new_app()
        app.switch("realtime", "live-test")
        before = snapshot(app.engine)
        app.handle_trade({"symbol": "SYN-CE-23800", "side": "BUY", "lots": "1"})                  # a synthetic instrument, sent while in provider mode
        self.assertIn("not in the current market data", app.notice)
        self.assertEqual(snapshot(app.engine), before)
        app.switch("synthetic")
        app.handle_trade({"symbol": "TEST-OPT-1", "side": "BUY", "lots": "1"})                    # and the other way round
        self.assertIn("not in the current market data", app.notice)
        self.assertEqual(app.engine.ledger, [])

    def test_an_account_refuses_prices_from_the_other_kind_of_data(self):
        synthetic_account = PaperEngine(Decimal("1000000"), clock=Clock(), origin="SYNTHETIC")
        provider_account = PaperEngine(Decimal("1000000"), clock=Clock(), origin="PROVIDER")
        for engine, foreign in ((synthetic_account, live_view()), (provider_account, view())):
            before = snapshot(engine)
            result = engine.simulate(buy(), foreign)
            self.assertEqual(result.code, "ACCOUNT_MISMATCH")
            self.assertEqual(snapshot(engine), before)
        with self.assertRaises(ValueError):
            PaperEngine(Decimal("1"), origin="REAL")

    def test_a_file_copied_to_another_account_is_not_accepted(self):
        src = Path(self.tmp.name) / "paper_synthetic.jsonl"
        e = PaperEngine(Decimal("1000000"), clock=self.clock, origin="SYNTHETIC", store=JsonlStore(src, 1_000_000, account="synthetic"))
        e.simulate(buy(), view())
        e.close()
        raw = src.read_bytes()
        # 1. copied under another account's name: the header says whose it is
        target = Path(self.tmp.name) / "paper_live-test.jsonl"
        target.write_bytes(raw)
        wrong = PaperEngine(Decimal("1000000"), clock=self.clock, origin="PROVIDER", store=JsonlStore(target, 1_000_000, account="live-test"))
        self.addCleanup(wrong.close)
        self.assertIn("DIFFERENT_ACCOUNT", wrong.lock_reason)
        self.assertEqual(file_bytes(target), raw)
        # 2. an older file with no account in its header: the records themselves say they are synthetic
        lines = raw.decode().splitlines()
        header = json.loads(lines[0])
        header.pop("account")
        legacy = Path(self.tmp.name) / "legacy.jsonl"
        legacy.write_bytes(("\n".join([json.dumps(header, sort_keys=True)] + lines[1:]) + "\n").encode())
        again = PaperEngine(Decimal("1000000"), clock=self.clock, origin="PROVIDER", store=JsonlStore(legacy, 1_000_000, account="live-test"))
        self.assertIn("DIFFERENT_ACCOUNT", again.lock_reason)
        again.close()                                                                            # (a locked engine keeps the file's lock until it is closed)
        # the right account still opens it
        right = PaperEngine(Decimal("1000000"), clock=self.clock, origin="SYNTHETIC", store=JsonlStore(legacy, 1_000_000, account="synthetic"))
        self.addCleanup(right.close)
        self.assertEqual((right.lock_reason, right.restored), (None, 1))

    def test_the_command_line_builds_one_account_per_source_and_rejects_unknown_ones(self):
        from niftypaper.dashboard import __main__ as cli
        a = cli._engine_for("synthetic", Decimal("1000000"), Path(self.tmp.name))
        b = cli._engine_for("fyers", Decimal("1000000"), Path(self.tmp.name))
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        self.assertEqual((a.origin, b.origin), ("SYNTHETIC", "PROVIDER"))
        self.assertNotEqual(a._store.path, b._store.path)
        self.assertEqual((a._store.path.name, b._store.path.name), ("paper_synthetic.jsonl", "paper_fyers.jsonl"))
        for bad in ("../x", "provider", "", "unknown"):
            with self.assertRaises(ValueError):
                cli._engine_for(bad, Decimal("1000000"), Path(self.tmp.name))


SYMBOL = "SYN-CE-24000"

if __name__ == "__main__":
    unittest.main()
