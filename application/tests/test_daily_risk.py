"""Tests for the daily risk-entry limit: the projected worst-case loss gate, day rollover, atomic refusals and the durable state that survives a restart.
All trades are virtual and all quotes are made up. These tests show the code behaves as specified; they do not show the limit protects a real account."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from niftypaper.dashboard.sample_data import SyntheticProvider
from niftypaper.dashboard.server import App
from niftypaper.paper.engine import BUY, SELL, CostModel, PaperEngine, RiskLimits, TradeRecord, TradeRequest, trading_day
from niftypaper.paper.store import JsonlStore, StoreError
from tests.test_modes import Served
from tests.test_paper_engine import IST, SYMBOL, T0, Clock, buy, quote_view, sell, view

CENT = Decimal("0.01")
UNIT = Decimal("6556.25")      # a 1-lot BUY at bid 100.00 / ask 100.10: premium 6,509.75 + buy fee 23.25 + estimated exit cost 23.25
COSTS = CostModel()


def budget_engine(budget, *, clock=None, store=None, **over):
    """An engine whose daily loss budget is exactly `budget` rupees (starting balance 1,000,000) and whose other limits are out of the way."""
    fields = dict(max_lots_per_trade=5, max_open_positions=9, max_exposure_fraction=Decimal("1"), daily_loss_fraction=Decimal(budget) / Decimal("1000000"))
    fields.update(over)
    clock = clock or Clock()
    return PaperEngine(Decimal("1000000"), limits=RiskLimits(**fields), clock=clock, store=store), clock


def snapshot(e):
    """Everything a refused trade must leave exactly as it was, including the day's figures and the saved file."""
    return (e.cash, e.realized_pnl, e.realized_today(), len(e.ledger), e.lock_reason, e.restored,
            tuple((p.symbol, p.quantity, p.basis, p.cost_paid, p.lot_size) for p in e.positions.values()), e.risk_report().committed)


def file_bytes(path):
    return path.read_bytes() if path.exists() else None


def independent_committed(e):
    """The committed worst case recomputed from the ledger alone: -realised today + premium + buy fee + estimated exit fees (one flat fee per lot) of what is still open."""
    open_ = {}
    realized = Decimal("0")
    day = trading_day(e._clock())
    for r in e.ledger:
        if r.side == BUY:
            o = open_.setdefault(r.symbol, [0, Decimal("0"), Decimal("0"), r.lot_size])
            o[0] += r.quantity
            o[1] += r.turnover
            o[2] += r.cost
        else:
            o = open_[r.symbol]
            share = Decimal(r.quantity) / Decimal(o[0])
            basis_out = o[1] if r.quantity == o[0] else (o[1] * share).quantize(CENT)
            fee_out = o[2] if r.quantity == o[0] else (o[2] * share).quantize(CENT)
            o[0] -= r.quantity
            o[1] -= basis_out
            o[2] -= fee_out
            if trading_day(r.engine_time) == day:
                realized += r.turnover - basis_out - fee_out - r.cost
    return -realized + sum(o[1] + o[2] + COSTS.exit_estimate(o[1], o[0] // o[3]) for o in open_.values() if o[0] > 0)


class TestTheBudgetAndTheGate(unittest.TestCase):
    def test_the_budget_is_two_percent_of_the_configured_starting_balance_and_does_not_move(self):
        e = PaperEngine(Decimal("1000000"), clock=Clock())
        self.assertEqual((e.budget, e.risk_report().budget), (Decimal("20000.00"), Decimal("20000.00")))
        self.assertEqual(PaperEngine(Decimal("500000"), clock=Clock()).budget, Decimal("10000.00"))
        e.simulate(buy(), view())
        e._clock = Clock(T0 + timedelta(days=3))
        self.assertEqual(e.risk_report().budget, Decimal("20000.00"))                  # not re-based by trades, equity or a new day

    def test_the_audited_trade_fits_and_a_second_lot_is_refused_with_the_numbers_shown(self):
        e = PaperEngine(Decimal("1000000"), clock=Clock())
        market = view(symbol="SYN-CE-23800", bid=Decimal("279.90"), ask=Decimal("280.10"))
        self.assertTrue(e.simulate(buy(symbol="SYN-CE-23800"), market).accepted)
        report = e.risk_report()
        self.assertEqual((report.committed, report.headroom, report.realized_today), (Decimal("18267.95"), Decimal("1732.05"), 0))   # 18,209.75 + 29.10 + 29.10
        before = snapshot(e)
        refused = e.simulate(buy(symbol="SYN-CE-23800"), market)
        self.assertEqual(refused.code, "RISK_DAILY_LOSS")
        for text in ("worst case", "projected worst-case daily loss 36,535.90", "budget of 20,000.00", "already committed 18,267.95", "this trade adds 18,267.95"):
            self.assertIn(text, refused.message)
        self.assertEqual(snapshot(e), before)

    def test_the_boundary_is_exact(self):
        e, _ = budget_engine(UNIT)
        self.assertTrue(e.simulate(buy(), quote_view(SYMBOL, "100.00")).accepted)             # exactly the budget is allowed
        e2, _ = budget_engine(UNIT - CENT)
        before = snapshot(e2)
        self.assertEqual(e2.simulate(buy(), quote_view(SYMBOL, "100.00")).code, "RISK_DAILY_LOSS")
        self.assertEqual(snapshot(e2), before)

    def test_existing_unrealised_losses_are_counted_once_not_twice(self):
        e, _ = budget_engine(Decimal("50000"), exit_slippage_ticks=0)             # no allowance here, so the quoted bid is the whole story
        e.simulate(buy(), quote_view(SYMBOL, "100.00"))
        basis = e.positions[SYMBOL].basis
        plain = e.risk_report()
        down = e.risk_report({SYMBOL: quote_view(SYMBOL, "40.00")})                         # bid 40: position worth 2,600.00
        self.assertEqual((down.unrealized_loss, down.remaining_max_loss), (basis - Decimal("2600.00"), Decimal("2600.00")))
        self.assertEqual(down.unrealized_loss + down.remaining_max_loss, basis)             # the two parts always add up to the premium paid
        self.assertEqual((down.committed, down.headroom), (plain.committed, plain.headroom))   # so a mark never changes the committed total
        self.assertTrue(down.marks_known and not plain.marks_known)
        self.assertEqual(down.incurred, down.unrealized_loss + down.open_buy_fees)
        up = e.risk_report({SYMBOL: quote_view(SYMBOL, "150.00")})                         # an unrealised gain is not credited
        self.assertEqual((up.unrealized_loss, up.remaining_max_loss), (0, basis))
        self.assertEqual(up.committed - plain.committed, COSTS.cost(Decimal("9750.00")) - COSTS.cost(basis))   # only the exit-cost estimate follows the higher value

    def test_unverified_marks_are_ignored_and_never_help(self):
        e, _ = budget_engine(Decimal("50000"))
        e.simulate(buy(), quote_view(SYMBOL, "100.00"))
        plain = e.risk_report()
        for bad in (view(symbol=SYMBOL, bid=Decimal("1.00"), ask=Decimal("1.10"), verified=False), view(symbol=SYMBOL, bid=Decimal("1.00"), ask=Decimal("1.10"), permitted=False),
                    view(symbol="OTHER", bid=Decimal("1.00"), ask=Decimal("1.10")), None, "junk"):
            report = e.risk_report({SYMBOL: bad})
            self.assertEqual((report.unrealized_loss, report.marks_known, report.committed), (0, False, plain.committed))

    def test_a_limit_that_is_already_reached_blocks_new_risk_with_its_own_reason_but_not_exits(self):
        e, _ = budget_engine(Decimal("20000"))
        e.simulate(buy(), quote_view(SYMBOL, "100.00"))
        e.limits = RiskLimits(max_lots_per_trade=5, max_open_positions=9, max_exposure_fraction=Decimal("1"), daily_loss_fraction=Decimal("0.0002"))     # budget now 200
        crashed = {SYMBOL: quote_view(SYMBOL, "1.00")}
        report = e.risk_report(crashed)
        self.assertTrue(report.limit_reached)
        self.assertGreaterEqual(report.incurred, report.budget)
        before = snapshot(e)
        self.assertEqual(e.simulate(buy(1, "NEW"), quote_view("NEW", "10.00"), marks=crashed).code, "RISK_DAILY_LIMIT_REACHED")
        self.assertEqual(e.simulate(buy(1, "NEW"), quote_view("NEW", "10.00")).code, "RISK_DAILY_LOSS")           # without marks the committed total still says no
        self.assertEqual(snapshot(e), before)
        self.assertTrue(e.simulate(sell(), quote_view(SYMBOL, "1.00")).accepted)                                  # the exit is always allowed
        self.assertEqual(e.positions, {})

    def test_exits_are_always_valid_and_nothing_is_ever_sold_by_itself(self):
        e, clock = budget_engine(UNIT * 2)
        e.simulate(buy(1, "A"), quote_view("A", "100.00"))
        e.simulate(buy(1, "B"), quote_view("B", "100.00"))
        before = snapshot(e)
        clock.now += timedelta(days=2)
        e.risk_report({"A": quote_view("A", "0.10"), "B": quote_view("B", "0.10")})
        e.summary()
        e.unrealized("A", quote_view("A", "0.10"))
        self.assertEqual(snapshot(e)[1:4] + snapshot(e)[6:7], before[1:4] + before[6:7])                           # looking at risk changes nothing; no auto-liquidation
        self.assertEqual(e.simulate(buy(1, "C"), quote_view("C", "100.00")).code, "RISK_DAILY_LOSS")
        self.assertTrue(e.simulate(sell(1, "A"), quote_view("A", "0.10")).accepted)
        self.assertTrue(e.simulate(sell(1, "B"), quote_view("B", "0.10")).accepted)

    def test_multiple_positions_and_fees_are_counted_once_each(self):
        e, _ = budget_engine(Decimal("100000"))
        e.simulate(buy(1, "A"), quote_view("A", "100.00"))
        e.simulate(buy(1, "B"), quote_view("B", "50.00"))
        e.simulate(buy(2, "C"), quote_view("C", "20.00"))
        expected = sum(r.turnover + r.cost + COSTS.exit_estimate(r.turnover, r.lots) for r in e.ledger)
        self.assertEqual(e.risk_report().committed, expected)
        self.assertEqual(independent_committed(e), expected)
        a_buy = e.ledger[0]
        sold = e.simulate(sell(1, "A"), quote_view("A", "90.00")).record
        loss_a = (a_buy.turnover + a_buy.cost) - (sold.turnover - sold.cost)               # premium and both fees, less what came back
        self.assertEqual(-sold.realized_pnl, loss_a)
        rest = sum(r.turnover + r.cost + COSTS.exit_estimate(r.turnover, r.lots) for r in e.ledger[1:3])
        self.assertEqual(e.risk_report().committed, loss_a + rest)                         # A's fees are now in realised P&L once, no longer in the open fees
        self.assertEqual(independent_committed(e), loss_a + rest)

    def test_partial_closes_move_premium_fees_and_realised_pnl_exactly(self):
        e, _ = budget_engine(Decimal("100000"))
        e.simulate(buy(2), quote_view(SYMBOL, "100.00"))                                   # 130 at 100.15 = 13,019.50 + fee 26.51
        bought = e.ledger[0]
        first = e.simulate(sell(1), quote_view(SYMBOL, "95.00")).record                    # 65 at 94.95 = 6,171.75, sell fee 23.09
        self.assertEqual(first.realized_pnl, first.turnover - Decimal("6509.75") - Decimal("13.26") - first.cost)    # half the premium, half the buy fee (13.255 -> 13.26)
        p = e.positions[SYMBOL]
        self.assertEqual((p.quantity, p.basis, p.cost_paid), (65, Decimal("6509.75"), bought.cost - Decimal("13.26")))
        report = e.risk_report()
        self.assertEqual(report.committed, -first.realized_pnl + p.basis + p.cost_paid + COSTS.cost(p.basis))
        self.assertEqual(independent_committed(e), report.committed)
        last = e.simulate(sell(1), quote_view(SYMBOL, "105.00")).record
        self.assertEqual(e.positions, {})
        self.assertEqual(e.risk_report().committed, -(first.realized_pnl + last.realized_pnl))       # flat: just the day's net result, fees once
        self.assertEqual(e.cash - Decimal("1000000"), e.realized_pnl)

    def test_a_round_trip_loss_is_exactly_the_spread_slippage_and_two_fees(self):
        e, _ = budget_engine(Decimal("100000"))
        b = e.simulate(buy(), quote_view(SYMBOL, "100.00")).record
        s = e.simulate(sell(), quote_view(SYMBOL, "100.00")).record
        self.assertEqual(e.risk_report().committed, (b.turnover + b.cost) - (s.turnover - s.cost))
        self.assertEqual(e.risk_report().committed, -e.realized_today())

    def test_realised_profit_raises_the_room_and_realised_loss_uses_it_up(self):
        e, _ = budget_engine(Decimal("20000"))
        e.simulate(buy(), quote_view(SYMBOL, "100.00"))
        e.simulate(sell(), quote_view(SYMBOL, "110.00"))
        profit = e.realized_today()
        self.assertGreater(profit, 0)
        self.assertEqual(e.risk_report().committed, -profit)
        self.assertEqual(e.risk_report().headroom, Decimal("20000") + profit)
        e2, _ = budget_engine(UNIT + 100)
        e2.simulate(buy(), quote_view(SYMBOL, "100.00"))
        e2.simulate(sell(), quote_view(SYMBOL, "50.00"))
        self.assertEqual(e2.simulate(buy(), quote_view(SYMBOL, "100.00")).code, "RISK_DAILY_LOSS")

    def test_whatever_the_sequence_the_committed_total_never_passes_the_budget_after_a_buy(self):
        import random
        accepted = 0
        for seed in range(100):
            rnd = random.Random(seed)
            budget = Decimal(rnd.choice([7000, 15000, 20000, 60000]))
            e, _ = budget_engine(budget)
            for _ in range(70):
                symbol = rnd.choice("ABCD")
                result = e.simulate(TradeRequest(symbol, BUY if rnd.random() < 0.6 else SELL, rnd.choice([1, 2])), quote_view(symbol, Decimal(rnd.randrange(100, 1500)) / 20))
                if result.accepted and result.record.side == BUY:
                    accepted += 1
                    self.assertLessEqual(e.risk_report().committed, budget, (seed, result.record.seq))
                self.assertEqual(e.risk_report().committed, independent_committed(e), seed)       # and the gate's number matches an independent recomputation
        self.assertGreater(accepted, 150)


class TestInstrumentSpecifications(unittest.TestCase):
    def test_lot_size_and_tick_size_come_from_the_instrument_and_are_enforced(self):
        e, _ = budget_engine(Decimal("100000"))
        r = e.simulate(buy(2), view(lot_size=75, tick=Decimal("0.05"))).record
        self.assertEqual((r.lot_size, r.quantity), (75, 150))
        self.assertEqual(e.positions[SYMBOL].lot_size, 75)
        before = snapshot(e)
        self.assertEqual(e.simulate(buy(1), view(lot_size=65)).code, "LOT_SIZE_CHANGED")              # a changed specification is not guessed around
        self.assertEqual(e.simulate(sell(1), view(lot_size=65)).code, "LOT_SIZE_CHANGED")
        self.assertEqual(e.simulate(buy(1), view(bid=Decimal("100.02"), ask=Decimal("100.12"))).code, "OFF_TICK")
        self.assertEqual(e.simulate(buy(1), view(tick=Decimal("0.10"), bid=Decimal("100.05"), ask=Decimal("100.15"))).code, "OFF_TICK")
        self.assertEqual(snapshot(e), before)

    def test_the_synthetic_demo_uses_the_configured_65_lot_and_005_tick(self):
        from niftypaper.dashboard.sample_data import SYNTHETIC_LOT_SIZE, TICK
        row = SyntheticProvider()(T0).chain[0]
        self.assertEqual((row.lot_size, row.tick, SYNTHETIC_LOT_SIZE, TICK), (65, Decimal("0.05"), 65, Decimal("0.05")))


class TestTradingDayRollover(unittest.TestCase):
    def test_the_day_is_the_india_time_calendar_date_and_rolls_at_midnight_ist(self):
        utc = timezone.utc
        self.assertEqual(trading_day(datetime(2026, 11, 2, 23, 59, 59, tzinfo=IST)), trading_day(datetime(2026, 11, 2, 18, 29, 59, tzinfo=utc)))
        self.assertNotEqual(trading_day(datetime(2026, 11, 2, 23, 59, 59, tzinfo=IST)), trading_day(datetime(2026, 11, 3, 0, 0, 0, tzinfo=IST)))
        self.assertEqual(trading_day(datetime(2026, 11, 3, 0, 0, 0, tzinfo=IST)), trading_day(datetime(2026, 11, 2, 18, 30, 0, tzinfo=utc)))

    def test_yesterdays_realised_result_is_dropped_at_midnight_but_open_positions_carry_over(self):
        e, clock = budget_engine(UNIT + 100)
        e.simulate(buy(1, "A"), quote_view("A", "100.00"))
        e.simulate(sell(1, "A"), quote_view("A", "50.00"))
        loss = -e.realized_today()
        self.assertGreater(loss, 0)
        clock.now = datetime(2026, 11, 2, 23, 59, 59, tzinfo=IST)
        self.assertEqual(e.realized_today(), -loss)
        self.assertEqual(e.simulate(buy(1, "B"), quote_view("B", "100.00")).code, "RISK_DAILY_LOSS")
        clock.now = datetime(2026, 11, 3, 0, 0, 0, tzinfo=IST)
        self.assertEqual((e.realized_today(), e.realized_pnl), (0, -loss))                        # the day's figure resets; the account's total does not
        self.assertTrue(e.simulate(buy(1, "B"), quote_view("B", "100.00")).accepted)
        clock.now = datetime(2026, 11, 4, 9, 0, 0, tzinfo=IST)
        self.assertEqual(e.simulate(buy(1, "C"), quote_view("C", "100.00")).code, "RISK_DAILY_LOSS")    # B (premium and fees) is still open and still counts

    def test_a_refused_trade_on_a_new_day_does_not_touch_any_state(self):
        e, clock = budget_engine(UNIT)
        e.simulate(buy(1, "A"), quote_view("A", "100.00"))
        clock.now += timedelta(days=1, hours=3)
        before = snapshot(e)
        self.assertEqual(e.simulate(buy(1, "B"), quote_view("B", "100.00")).code, "RISK_DAILY_LOSS")
        self.assertEqual(snapshot(e), before)


class TestRefusalsAreAtomic(unittest.TestCase):
    def test_every_risk_refusal_changes_nothing_in_memory_or_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            e, _ = budget_engine(UNIT * 2, store=JsonlStore(path, 1_000_000))
            try:
                e.simulate(buy(1, "A"), quote_view("A", "100.00"))
                e.simulate(buy(1, "B"), quote_view("B", "100.00"))
                before, saved = snapshot(e), file_bytes(path)
                for request, market in ((buy(1, "C"), quote_view("C", "100.00")), (buy(1, "A"), quote_view("A", "100.00")), (buy(6, "A"), quote_view("A", "100.00")),
                                        (buy(1, "A"), quote_view("A", "100.02")), (sell(2, "A"), quote_view("A", "100.00")), (sell(1, "Z"), quote_view("Z", "100.00"))):
                    result = e.simulate(request, market)
                    self.assertFalse(result.accepted)
                    self.assertEqual(snapshot(e), before, result.code)
                    self.assertEqual(file_bytes(path), saved, result.code)
            finally:
                e.close()
    def test_a_trade_that_cannot_be_saved_is_not_made(self):
        class Failing:
            path = Path("x.jsonl")
            calls = []

            def load(self):
                return []

            def append(self, record):
                self.calls.append((record["seq"], e.cash, len(e.ledger)))
                raise StoreError("WRITE_FAILED")
        e = PaperEngine(Decimal("1000000"), clock=Clock(), store=Failing())
        before = snapshot(e)
        result = e.simulate(buy(), view())
        self.assertEqual(result.code, "PERSISTENCE_FAILED")
        self.assertEqual(snapshot(e), before)
        self.assertEqual(Failing.calls, [(1, Decimal("1000000"), 0)])                       # it was offered to the store before anything was applied

    def test_an_unwritable_location_locks_trading_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "afile"
            blocker.write_text("x")
            e = PaperEngine(Decimal("1000000"), clock=Clock(), store=JsonlStore(blocker / "sub" / "s.jsonl", 1_000_000))
            self.assertIn("LOCK_UNAVAILABLE", e.lock_reason)                                   # no lock, no trading
            self.assertEqual(e.simulate(buy(), view()).code, "STATE_UNAVAILABLE")
            self.assertEqual((e.cash, e.positions, e.ledger), (Decimal("1000000"), {}, []))
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["afile"])
class TestRestart(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "paper_synthetic.jsonl"
        self.opened = []

    def start(self, budget=UNIT * 2, clock=None, balance=1_000_000, **over):
        """Open the account. Any engine opened earlier in the same test is closed first: only one may hold the file."""
        for earlier in self.opened:
            earlier.close()
        e, clock = budget_engine(budget, clock=clock, store=JsonlStore(self.path, balance, account="synthetic"), **over)
        self.opened.append(e)
        self.addCleanup(e.close)
        return e, clock

    def test_a_restart_keeps_positions_cash_pnl_and_the_remaining_budget(self):
        e1, clock = self.start()
        e1.simulate(buy(1, "A"), quote_view("A", "100.00"))
        e1.simulate(buy(1, "B"), quote_view("B", "100.00"))
        e1.simulate(sell(1, "B"), quote_view("B", "60.00"))
        e2, _ = self.start(clock=clock)
        self.assertIsNone(e2.lock_reason)
        self.assertEqual(e2.restored, 3)
        self.assertEqual(snapshot(e2)[:5] + snapshot(e2)[6:], snapshot(e1)[:5] + snapshot(e1)[6:])
        self.assertEqual([r.to_dict() for r in e2.ledger], [r.to_dict() for r in e1.ledger])
        self.assertEqual(e2.risk_report().headroom, e1.risk_report().headroom)
        self.assertEqual(e1.simulate(buy(1, "C"), quote_view("C", "100.00")).code, e2.simulate(buy(1, "C"), quote_view("C", "100.00")).code)
        self.assertEqual(e2.simulate(buy(1, "C"), quote_view("C", "100.00")).code, "RISK_DAILY_LOSS")

    def test_a_restart_does_not_hand_back_a_budget_that_was_used_up(self):
        e1, clock = self.start(UNIT + 2000)
        e1.simulate(buy(), quote_view(SYMBOL, "100.00"))
        e1.simulate(sell(), quote_view(SYMBOL, "70.00"))                                  # a realised loss uses up most of the budget
        self.assertEqual(e1.simulate(buy(), quote_view(SYMBOL, "100.00")).code, "RISK_DAILY_LOSS")
        e2, _ = self.start(UNIT + 2000, clock=clock)
        self.assertEqual(e2.simulate(buy(), quote_view(SYMBOL, "100.00")).code, "RISK_DAILY_LOSS")
        self.assertEqual(e2.realized_today(), e1.realized_today())

    def test_after_midnight_a_restart_gets_a_fresh_day_but_keeps_the_open_positions(self):
        e1, clock = self.start(UNIT * 2)
        e1.simulate(buy(1, "A"), quote_view("A", "100.00"))
        e1.simulate(buy(1, "B"), quote_view("B", "100.00"))
        e1.simulate(sell(1, "B"), quote_view("B", "50.00"))
        next_day = Clock(datetime(2026, 11, 3, 9, 20, tzinfo=IST))
        e2, _ = self.start(UNIT * 2, clock=next_day)
        self.assertEqual((e2.realized_today(), e2.realized_pnl), (0, e1.realized_pnl))
        self.assertEqual(set(e2.positions), {"A"})
        self.assertTrue(e2.simulate(buy(1, "C"), quote_view("C", "100.00")).accepted)       # A + C fit; yesterday's loss no longer counts

    def test_the_file_holds_one_header_and_one_line_per_accepted_trade(self):
        e, _ = self.start(Decimal("100000"))
        e.simulate(buy(), quote_view(SYMBOL, "100.00"))
        e.simulate(buy(6, "X"), quote_view("X", "100.00"))                                 # refused: too many lots, never written
        e.simulate(sell(), quote_view(SYMBOL, "100.00"))
        raw = self.path.read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        lines = [json.loads(line) for line in raw.decode().splitlines()]
        self.assertEqual([x["kind"] for x in lines], ["header", "trade", "trade"])
        self.assertEqual((lines[0]["schema"], lines[0]["starting_balance"]), (1, "1000000"))
        self.assertEqual([x["seq"] for x in lines[1:]], [1, 2])
        self.assertNotIn("simulated", lines[1])

    def corrupt(self, name, edit):
        e, clock = self.start(Decimal("100000"))
        e.simulate(buy(), quote_view(SYMBOL, "100.00"))
        e.simulate(sell(), quote_view(SYMBOL, "90.00"))
        raw = self.path.read_bytes()
        lines = raw.decode().splitlines()
        edited = edit(lines, raw)
        self.path.write_bytes(edited if isinstance(edited, bytes) else ("\n".join(edited) + "\n").encode())
        return clock

    def test_a_state_file_that_cannot_be_trusted_locks_trading_and_is_left_untouched(self):
        def tamper(index, **changes):
            def edit(lines, raw):
                row = json.loads(lines[index])
                row.update(changes)
                return lines[:index] + [json.dumps(row, sort_keys=True)] + lines[index + 1:]
            return edit
        cases = {
            "truncated last line": lambda lines, raw: raw[:-1],
            "invalid json": lambda lines, raw: lines[:-1] + ["{oops"],
            "binary garbage": lambda lines, raw: b"\xff\xfe\x00\n",
            "wrong header": lambda lines, raw: ['{"kind": "header", "schema": 2, "starting_balance": "1000000"}'] + lines[1:],
            "no header": lambda lines, raw: lines[1:],
            "a line that is not a trade": lambda lines, raw: lines + ['{"kind": "note"}'],
            "sequence gap": lambda lines, raw: [lines[0], lines[2]],
            "tampered turnover": tamper(1, turnover="1.00"),
            "tampered realised P&L": tamper(2, realized_pnl="9999.00"),
            "a sale with no purchase": lambda lines, raw: [lines[0], json.dumps({**json.loads(lines[2]), "seq": 1}, sort_keys=True)],
            "a BUY with a realised P&L": tamper(1, realized_pnl="5.00"),
            "quantity not lots x lot size": tamper(1, quantity=64),
            "not a number": lambda lines, raw: lines[:1] + [lines[1].replace('"turnover": "6509.75"', '"turnover": NaN')] + lines[2:],
            "time going backwards": tamper(2, engine_time="2020-01-01T00:00:00+05:30"),
            "missing field": lambda lines, raw: lines[:1] + [json.dumps({k: v for k, v in json.loads(lines[1]).items() if k != "cost"})] + lines[2:],
        }
        for name, edit in cases.items():
            with self.subTest(case=name):
                if self.path.exists():
                    self.path.unlink()
                clock = self.corrupt(name, edit)
                saved = file_bytes(self.path)
                e, _ = self.start(Decimal("100000"), clock=clock)
                self.assertTrue(e.lock_reason, name)
                self.assertEqual((e.cash, e.positions, e.ledger, e.restored), (Decimal("1000000"), {}, [], 0), name)
                for request, market in ((buy(), quote_view(SYMBOL, "100.00")), (sell(), quote_view(SYMBOL, "100.00"))):
                    result = e.simulate(request, market)
                    self.assertEqual(result.code, "STATE_UNAVAILABLE", name)
                self.assertEqual(file_bytes(self.path), saved, name)
                self.assertTrue(e.risk_report().locked)

    def test_a_state_file_for_a_different_account_locks_instead_of_being_reused(self):
        e, clock = self.start(Decimal("100000"), balance=1_000_000)
        e.simulate(buy(), quote_view(SYMBOL, "100.00"))
        saved = file_bytes(self.path)
        e.close()
        other, _ = budget_engine(Decimal("100000"), store=JsonlStore(self.path, 500_000))
        self.addCleanup(other.close)
        self.assertIn("DIFFERENT_ACCOUNT", other.lock_reason)
        self.assertEqual(other.simulate(buy(), quote_view(SYMBOL, "100.00")).code, "STATE_UNAVAILABLE")
        self.assertEqual(file_bytes(self.path), saved)

    def test_an_empty_or_missing_file_is_a_fresh_account_not_a_lock(self):
        e, _ = self.start()
        self.assertIsNone(e.lock_reason)
        self.assertFalse(self.path.exists())                                                  # nothing is written until the first accepted trade
        self.path.write_bytes(b"")
        self.assertIsNone(self.start()[0].lock_reason)

    def test_records_round_trip(self):
        e, _ = self.start(Decimal("100000"))
        r = e.simulate(buy(), view(received_at=T0 - timedelta(seconds=2), provider_time_raw="1691020800")).record
        again = TradeRecord.from_dict(json.loads(json.dumps(r.to_dict())))
        self.assertEqual(again, r)


class TestDashboardShowsTheBudget(unittest.TestCase):
    def make_app(self, tmp, clock=None):
        clock = clock or Clock()
        from niftypaper.dashboard.sample_data import SyntheticProvider as P
        return App(P(), clock=clock, mode="synthetic", provider_id="synthetic", factories={"synthetic": lambda: P()},
                   engine_factory=lambda key: PaperEngine(Decimal("1000000"), clock=clock, origin="SYNTHETIC",
                                                          store=JsonlStore(Path(tmp) / f"paper_{key}.jsonl", 1_000_000, account=key))), clock

    def test_the_page_shows_the_budget_the_remaining_room_and_why_a_trade_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, _ = self.make_app(tmp)
            try:
                with Served(app) as s:
                    page = s.request("GET", "/")[2]
                    for text in ("Daily risk budget", "20,000.00", "Remaining budget for new risk", "OPEN", "00:00 IST", "does not guarantee", "nothing is ever sold automatically",
                                 "paper_synthetic.jsonl", "Trades restored when the session opened", "(counted once, when the saved file was read; not the current count)", "Exit slippage allowance"):
                        self.assertIn(text, page)
                    s.post("/paper/trade", {"symbol": "SYN-CE-23800", "side": "BUY", "lots": "1"})
                    page = s.request("GET", "/")[2]
                    self.assertIn("18,267.95", page)                                              # committed
                    self.assertIn("1,732.05", page)                                               # what is left
                    s.post("/paper/trade", {"symbol": "SYN-CE-23800", "side": "BUY", "lots": "1"})
                    page = s.request("GET", "/")[2]
                    self.assertIn("projected worst-case daily loss 36,535.90", page)
                    self.assertIn("budget of 20,000.00", page)
                    risk = json.loads(s.request("GET", "/api/ledger")[2])["risk"]
                    self.assertEqual((risk["budget"], risk["committed"], risk["headroom"], risk["limit_reached"]), ("20000.00", "18267.95", "1732.05", False))
                    self.assertIn("not a guarantee", risk["note"])
            finally:
                app.close()
    def test_a_new_dashboard_run_restores_the_trades_and_the_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, clock = self.make_app(tmp)
            first.handle_trade({"symbol": "SYN-CE-23800", "side": "BUY", "lots": "1"})
            first.close()                                                                  # the first run ends (a killed run releases the lock too)
            second, _ = self.make_app(tmp, clock)
            try:
                self.assertEqual(second.engine.restored, 1)
                second.handle_trade({"symbol": "SYN-CE-23800", "side": "BUY", "lots": "1"})
                self.assertIn("projected worst-case daily loss", second.notice)
                with Served(second) as s:
                    page = s.request("GET", "/")[2]
                self.assertIn("Trades restored when the session opened</dt><dd>1 (counted once", page)
                self.assertIn("Trades in this account now</dt><dd>1</dd>", page)
                self.assertIn("1,732.05", page)
                second.handle_trade({"symbol": "SYN-CE-23800", "side": "SELL", "lots": "1"})                  # an exit is always allowed: the count now differs from the restored one
                with Served(second) as s:
                    page = s.request("GET", "/")[2]
                self.assertIn("Trades restored when the session opened</dt><dd>1 (counted once", page)
                self.assertIn("Trades in this account now</dt><dd>2</dd>", page)
            finally:
                second.close()
    def test_a_locked_state_is_shown_with_its_reason_and_no_trade_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "paper_synthetic.jsonl").write_bytes(b"{oops\n")
            app, _ = self.make_app(tmp)
            try:
                with Served(app) as s:
                    page = s.request("GET", "/")[2]
                    self.assertIn("saved paper-trading state could not be trusted", page)
                    self.assertIn("LOCKED", page)
                    self.assertNotIn('action="/paper/trade"', page)
                    s.post("/paper/trade", {"symbol": "SYN-CE-23800", "side": "BUY", "lots": "1"})
                    self.assertIn("locked", s.request("GET", "/")[2].lower())
            finally:
                app.close()
            self.assertEqual((Path(tmp) / "paper_synthetic.jsonl").read_bytes(), b"{oops\n")
    def test_the_command_line_saves_each_account_under_the_state_directory(self):
        from niftypaper.dashboard import __main__ as cli
        with tempfile.TemporaryDirectory() as tmp:
            args = cli.build_parser().parse_args(["--state-dir", tmp])
            app, _ = cli._build_app(args, Decimal("1000000"))
            try:
                self.assertEqual(app.engine._store.path, Path(tmp) / "paper_synthetic.jsonl")
                self.assertEqual((app.engine.origin, app.engine._store.account), ("SYNTHETIC", "synthetic"))
                default = Path(cli.build_parser().parse_args([]).state_dir)
                self.assertTrue(default.is_absolute())                                          # not relative to wherever the app is started from
                self.assertEqual(default, Path(cli.__file__).resolve().parents[3] / "data" / "paper_state")
            finally:
                app.close()
if __name__ == "__main__":
    unittest.main()
