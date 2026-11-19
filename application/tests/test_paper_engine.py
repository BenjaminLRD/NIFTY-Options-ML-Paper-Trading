"""Tests for the virtual paper-trading engine. Every trade here is simulated; nothing contacts a provider or the network."""
import ast
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from niftypaper.paper.engine import (BUY, ORIGIN_PROVIDER, ORIGIN_SYNTHETIC, SELL, CostModel, MarketView, PaperEngine, RiskLimits, TradeRequest)
from tests import SRC

IST = timezone(timedelta(hours=5, minutes=30))
T0 = datetime(2026, 11, 2, 11, 0, 0, tzinfo=IST)
SYMBOL = "SYN-CE-24000"


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


def view(**over):
    base = dict(origin=ORIGIN_SYNTHETIC, provider="SYNTHETIC_SAMPLE_PROVIDER", state="SIMULATED", connection="SIMULATED", symbol=SYMBOL,
                bid=Decimal("100.00"), ask=Decimal("100.10"), last=Decimal("100.05"), bid_qty=None, ask_qty=None, tick=Decimal("0.05"), lot_size=65,
                received_at=T0, provider_time_raw=None, provider_time=None, verified=True, permitted=True, reason="")
    base.update(over)
    return MarketView(**base)


def live_view(**over):
    """Provider data that has all the evidence the V6-0.1 boundary needs: documented quote time and displayed sizes."""
    fields = dict(origin=ORIGIN_PROVIDER, provider="TEST_PROVIDER", state="LIVE", connection="CONNECTED", bid_qty=650, ask_qty=650,
                  provider_time=T0 - timedelta(seconds=1), provider_time_raw="raw-value")
    fields.update(over)
    return view(**fields)


def engine(**over):
    clock = over.pop("clock", Clock())
    return PaperEngine(Decimal("1000000"), clock=clock, **over), clock


def buy(lots=1, symbol=SYMBOL):
    return TradeRequest(symbol, BUY, lots)


def sell(lots=1, symbol=SYMBOL):
    return TradeRequest(symbol, SELL, lots)


class TestFills(unittest.TestCase):
    def test_buy_fills_at_the_ask_plus_slippage_and_is_not_a_market_quote(self):
        e, _ = engine()
        result = e.simulate(buy(), view())
        self.assertTrue(result.accepted)
        r = result.record
        self.assertEqual((r.quote_bid, r.quote_ask, r.spread), (Decimal("100.00"), Decimal("100.10"), Decimal("0.10")))
        self.assertEqual(r.fill_price, Decimal("100.15"))                      # ask 100.10 + 1 tick (0.05)
        self.assertNotIn(r.fill_price, (r.quote_bid, r.quote_ask, r.quote_last))
        self.assertEqual((r.quantity, r.lots, r.lot_size), (65, 1, 65))
        self.assertEqual(r.turnover, Decimal("6509.75"))
        self.assertEqual(r.cost, Decimal("23.25"))                              # 20 + 0.05% of 6509.75
        self.assertEqual(e.cash, Decimal("1000000") - r.turnover - r.cost)

    def test_sell_fills_at_the_bid_minus_slippage(self):
        e, _ = engine()
        e.simulate(buy(), view())
        r = e.simulate(sell(), view()).record
        self.assertEqual(r.fill_price, Decimal("99.95"))
        self.assertEqual(e.positions, {})

    def test_round_trip_realised_pnl_includes_all_costs(self):
        e, _ = engine()
        bought = e.simulate(buy(), view()).record
        sold = e.simulate(sell(), view(bid=Decimal("110.00"), ask=Decimal("110.10"))).record
        gross = (sold.fill_price - bought.fill_price) * 65
        self.assertEqual(sold.realized_pnl, (gross - bought.cost - sold.cost).quantize(Decimal("0.01")))
        self.assertEqual(e.realized_pnl, sold.realized_pnl)
        self.assertEqual(e.cash, Decimal("1000000") + sold.realized_pnl)

    def test_every_record_keeps_provider_instrument_times_state_and_assumptions(self):
        e, _ = engine()
        r = e.simulate(buy(), view(received_at=T0 - timedelta(seconds=3), provider_time_raw="1691020800")).record
        d = r.to_dict()
        self.assertEqual((d["provider"], d["symbol"], d["data_origin"], d["data_state"], d["connection"]),
                         ("SYNTHETIC_SAMPLE_PROVIDER", SYMBOL, "SYNTHETIC", "SIMULATED", "SIMULATED"))
        self.assertEqual((d["received_at"], d["provider_time_raw"], d["simulated"]), ((T0 - timedelta(seconds=3)).isoformat(), "1691020800", True))
        self.assertIn("engine_time", d)
        self.assertIn("fill_basis", d)
        text = " ".join(d["assumptions"])
        for needle in ("virtual", "illustrative", "partial fills", "displayed size unknown", "limits"):
            self.assertIn(needle, text)
        with self.assertRaises(FrozenInstanceError):
            r.fill_price = Decimal("1")

    def test_displayed_size_is_checked_when_known_and_the_gap_is_recorded_when_not(self):
        e, _ = engine()
        refused = e.simulate(buy(), view(ask_qty=64))
        self.assertEqual(refused.code, "EXCEEDS_DISPLAYED_SIZE")
        self.assertTrue(e.simulate(buy(), view(ask_qty=65)).accepted)
        self.assertEqual(e.simulate(sell(), view(bid_qty=64)).code, "EXCEEDS_DISPLAYED_SIZE")


class TestRefusals(unittest.TestCase):
    def refused(self, request, market, code, **engine_over):
        e, _ = engine(**engine_over)
        result = e.simulate(request, market)
        self.assertFalse(result.accepted)
        self.assertEqual(result.code, code)
        self.assertIsNone(result.record)
        self.assertEqual((e.ledger, e.positions, e.cash), ([], {}, Decimal("1000000")))      # nothing changed
        return result

    def test_unverified_or_unpermitted_data_cannot_be_traded(self):
        self.refused(buy(), view(verified=False, state="UNVERIFIED", reason="timing not established"), "DATA_NOT_VERIFIED")
        self.refused(buy(), view(permitted=False), "TERMS_NOT_CONFIRMED")
        self.refused(buy(), view(verified=1), "DATA_NOT_VERIFIED")             # only a real True counts
        self.refused(buy(), view(permitted="yes"), "TERMS_NOT_CONFIRMED")

    def test_missing_empty_or_crossed_bid_ask_gives_no_fill(self):
        for name, over, code in (("no bid", dict(bid=None), "NO_BID_ASK"), ("no ask", dict(ask=None), "NO_BID_ASK"), ("zero bid", dict(bid=Decimal("0")), "NO_BID_ASK"),
                                 ("negative ask", dict(ask=Decimal("-1")), "NO_BID_ASK"), ("crossed", dict(bid=Decimal("101"), ask=Decimal("100")), "CROSSED_OR_LOCKED"),
                                 ("locked", dict(bid=Decimal("100"), ask=Decimal("100")), "CROSSED_OR_LOCKED")):
            with self.subTest(case=name):
                self.refused(buy(), view(**over), code)

    def test_unsupported_instruments_and_bad_requests(self):
        self.refused(buy(symbol="NSE:SOMETHING-ELSE"), view(), "UNSUPPORTED_INSTRUMENT")
        self.refused(buy(), None, "UNSUPPORTED_INSTRUMENT")
        self.refused(buy(), "not a view", "UNSUPPORTED_INSTRUMENT")
        self.refused(buy(), view(origin="MYSTERY"), "UNSUPPORTED_INSTRUMENT")
        for bad in (TradeRequest(SYMBOL, BUY, 0), TradeRequest(SYMBOL, BUY, -1), TradeRequest(SYMBOL, BUY, 2.5), TradeRequest(SYMBOL, BUY, True),
                    TradeRequest(SYMBOL, "HOLD", 1), TradeRequest(SYMBOL, "buy", 1), "BUY", None):
            with self.subTest(request=bad):
                self.refused(bad, view(), "BAD_REQUEST")
        self.refused(buy(), view(lot_size=None), "LOT_SIZE_UNKNOWN")
        self.refused(buy(), view(lot_size=0), "LOT_SIZE_UNKNOWN")
        self.refused(buy(), view(tick=None), "LOT_SIZE_UNKNOWN")

    def test_a_non_positive_fill_price_is_refused(self):
        self.refused(sell(), view(bid=Decimal("0.05"), ask=Decimal("0.10")), "FILL_PRICE_NOT_POSITIVE")
        e, _ = engine(slippage_ticks=1)
        e.simulate(buy(), view(bid=Decimal("0.05"), ask=Decimal("0.10")))
        self.assertEqual(e.simulate(sell(), view(bid=Decimal("0.05"), ask=Decimal("0.10"))).code, "FILL_PRICE_NOT_POSITIVE")

    def test_selling_what_is_not_held_is_refused(self):
        self.refused(sell(), view(), "NO_POSITION")
        e, _ = engine()
        e.simulate(buy(1), view())
        self.assertEqual(e.simulate(sell(2), view()).code, "SIZE_EXCEEDS_POSITION")             # no short selling
        self.assertEqual(e.positions[SYMBOL].quantity, 65)


class TestRiskLimits(unittest.TestCase):
    def test_per_trade_lots_open_positions_exposure_and_balance(self):
        e, _ = engine(limits=RiskLimits(max_lots_per_trade=2, max_open_positions=2))
        self.assertEqual(e.simulate(buy(3), view()).code, "RISK_MAX_LOTS")
        self.assertTrue(e.simulate(buy(1, "A"), view(symbol="A")).accepted)
        self.assertTrue(e.simulate(buy(1, "B"), view(symbol="B")).accepted)
        self.assertEqual(e.simulate(buy(1, "C"), view(symbol="C")).code, "RISK_MAX_POSITIONS")
        self.assertTrue(e.simulate(buy(1, "A"), view(symbol="A")).accepted)                    # adding to an open position is not a new position
        e2 = PaperEngine(Decimal("1000000"), clock=Clock())
        self.assertEqual(e2.simulate(buy(2), view(bid=Decimal("900"), ask=Decimal("900.05"))).code, "RISK_EXPOSURE")
        e3 = PaperEngine(Decimal("100"), clock=Clock(), limits=RiskLimits(max_exposure_fraction=Decimal("100"), daily_loss_fraction=Decimal("100")))
        self.assertEqual(e3.simulate(buy(1), view()).code, "INSUFFICIENT_BALANCE")

class TestProviderDataMustPassTheBoundary(unittest.TestCase):
    def test_verified_provider_quote_with_time_and_sizes_is_filled(self):
        e, _ = engine()
        r = e.simulate(buy(), live_view()).record
        self.assertEqual((r.data_origin, r.provider, r.data_state, r.connection), ("PROVIDER", "TEST_PROVIDER", "LIVE", "CONNECTED"))
        self.assertEqual(r.fill_price, Decimal("100.15"))
        self.assertEqual(r.provider_time_raw, "raw-value")
        self.assertIn("displayed size checked", " ".join(r.assumptions))

    def test_missing_sizes_or_quote_time_or_old_or_off_tick_quotes_are_refused(self):
        cases = {"no sizes": live_view(bid_qty=None, ask_qty=None), "no quote time": live_view(provider_time=None),
                 "old quote": live_view(provider_time=T0 - timedelta(seconds=30)), "future quote": live_view(provider_time=T0 + timedelta(minutes=5)),
                 "zero size": live_view(bid_qty=0)}
        for name, market in cases.items():
            with self.subTest(case=name):
                e, _ = engine()
                result = e.simulate(buy(), market)
                self.assertEqual(result.code, "BOUNDARY_REJECTED")
                self.assertEqual(e.ledger, [])

    def test_an_off_tick_provider_quote_is_refused_before_any_fill(self):
        e, _ = engine()
        self.assertEqual(e.simulate(buy(), live_view(bid=Decimal("100.02"))).code, "OFF_TICK")
        self.assertEqual(e.ledger, [])

    def test_provider_data_that_is_not_verified_never_reaches_the_boundary(self):
        for state in ("UNVERIFIED", "DELAYED", "STALE"):
            e, _ = engine()
            result = e.simulate(buy(), live_view(state=state, verified=False))
            self.assertEqual(result.code, "DATA_NOT_VERIFIED")

    def test_unrealised_pnl_is_unknown_unless_prices_are_verified(self):
        e, _ = engine()
        e.simulate(buy(), view())
        self.assertEqual(e.unrealized(SYMBOL, view(bid=Decimal("102.00"))), ((Decimal("102.00") - Decimal("100.15")) * 65).quantize(Decimal("0.01")))
        self.assertIsNone(e.unrealized(SYMBOL, view(verified=False)))
        self.assertIsNone(e.unrealized(SYMBOL, view(permitted=False)))
        self.assertIsNone(e.unrealized(SYMBOL, None))
        self.assertIsNone(e.unrealized("OTHER", view(symbol="OTHER")))


CENT = Decimal("0.01")


def state_of(e):
    """Everything an accepted trade may change; a rejected trade must leave all of it exactly as it was."""
    return (e.cash, e.realized_pnl, e.realized_today(), len(e.ledger), tuple((p.symbol, p.quantity, p.basis, p.cost_paid) for p in e.positions.values()))


def quote_view(symbol, bid, ask="0.10"):
    bid = Decimal(bid)
    return view(symbol=symbol, bid=bid, ask=bid + Decimal(ask))


class TestAccountingInvariants(unittest.TestCase):
    """Audit regression tests: cash, fees, positions, realised and unrealised P&L must reconcile with the ledger to the paisa."""

    def test_the_reported_dashboard_trade_reconciles(self):
        e, _ = engine()
        market = view(symbol="SYN-CE-23800", bid=Decimal("279.90"), ask=Decimal("280.10"))
        r = e.simulate(buy(symbol="SYN-CE-23800"), market).record
        self.assertEqual((r.quantity, r.fill_price, r.turnover, r.cost), (65, Decimal("280.15"), Decimal("18209.75"), Decimal("29.10")))
        self.assertEqual(r.cost, (Decimal("20") + Decimal("18209.75") * Decimal("0.0005")).quantize(CENT))   # 29.104875 -> 29.10
        self.assertEqual(e.cash, Decimal("981761.15"))
        self.assertEqual(Decimal("1000000") - e.cash, Decimal("18238.85"))                          # premium 18,209.75 + fee 29.10
        self.assertEqual(e.realized_pnl, 0)                                                         # nothing closed: no realised P&L
        self.assertEqual(e.unrealized("SYN-CE-23800", market), Decimal("-16.25"))                    # (279.90 - 280.15) x 65, before any fee
        marked = market.bid * 65
        self.assertEqual(e.cash + marked, Decimal("1000000") - r.cost + e.unrealized("SYN-CE-23800", market))   # equity identity

    def test_quantity_is_lots_times_lot_size_and_the_lot_limit_counts_lots(self):
        e, _ = engine()
        self.assertEqual(e.simulate(buy(2), view()).record.quantity, 130)
        self.assertEqual(e.simulate(buy(3), view()).code, "RISK_MAX_LOTS")
        self.assertTrue(all(r.quantity == r.lots * r.lot_size and r.lot_size == 65 for r in e.ledger))

    def test_each_fee_is_charged_once_and_cash_moves_by_exactly_turnover_and_fee(self):
        e, _ = engine()
        costs = CostModel()
        before = e.cash
        b = e.simulate(buy(2), view()).record
        self.assertEqual(b.cost, costs.cost(b.turnover))
        self.assertEqual(before - e.cash, b.turnover + b.cost)
        before = e.cash
        s = e.simulate(sell(2), view(bid=Decimal("110.00"), ask=Decimal("110.10"))).record
        self.assertEqual(s.cost, costs.cost(s.turnover))
        self.assertEqual(e.cash - before, s.turnover - s.cost)
        self.assertEqual(s.realized_pnl, s.turnover - b.turnover - b.cost - s.cost)               # each fee appears once in realised P&L
        self.assertEqual((e.positions, e.cash - Decimal("1000000")), ({}, s.realized_pnl))       # flat: all cash change is realised P&L

    def test_partial_closes_at_an_awkward_average_still_reconcile_exactly(self):
        e, _ = engine(limits=RiskLimits(max_lots_per_trade=5, max_exposure_fraction=Decimal("1"), daily_loss_fraction=Decimal("1")))
        e.simulate(buy(2), quote_view(SYMBOL, "100.00"))
        e.simulate(buy(1), quote_view(SYMBOL, "101.10"))                  # average = 19652.75 / 195, which does not terminate
        for bid in ("103.35", "99.20", "104.05"):
            e.simulate(sell(1), quote_view(SYMBOL, bid))
        self.assertEqual(e.positions, {})
        self.assertEqual(e.cash - Decimal("1000000"), e.realized_pnl)                                   # exactly, not to within a paisa
        self.assertEqual(sum(r.realized_pnl for r in e.ledger if r.realized_pnl is not None), e.realized_pnl)

    def test_random_trade_sequences_reconcile_with_the_ledger(self):
        import random
        for seed in range(150):
            rnd = random.Random(seed)
            e, _ = engine(limits=RiskLimits(max_exposure_fraction=Decimal("1"), daily_loss_fraction=Decimal("1")) if seed % 2 else RiskLimits())
            for _ in range(60):
                symbol = rnd.choice("ABC")
                market = quote_view(symbol, Decimal(rnd.randrange(400, 4000)) / 20)
                e.simulate(TradeRequest(symbol, BUY if rnd.random() < 0.55 else SELL, rnd.choice([1, 1, 2])), market)
                buys = sum(r.turnover + r.cost for r in e.ledger if r.side == BUY)
                sells = sum(r.turnover - r.cost for r in e.ledger if r.side == SELL)
                self.assertEqual(e.cash, Decimal("1000000") - buys + sells, seed)                                  # cash == ledger
                self.assertEqual(sum(r.realized_pnl for r in e.ledger if r.realized_pnl is not None), e.realized_pnl, seed)
                for p in e.positions.values():                                                                       # positions == ledger
                    self.assertEqual(p.quantity, sum(r.quantity if r.side == BUY else -r.quantity for r in e.ledger if r.symbol == p.symbol), seed)
                open_value = sum(p.basis + p.cost_paid for p in e.positions.values())
                self.assertEqual(e.cash, Decimal("1000000") + e.realized_pnl - open_value, seed)                    # cash = start + realised - open cost
                if not e.positions:
                    self.assertEqual(e.cash - Decimal("1000000"), e.realized_pnl, seed)
                self.assertTrue(all(v == v.quantize(CENT) for v in (e.cash, e.realized_pnl)), seed)                 # money stays in whole paise

    def test_every_rejection_leaves_cash_positions_fees_and_ledger_unchanged(self):
        e, _ = engine(limits=RiskLimits(max_open_positions=1, max_exposure_fraction=Decimal("0.015")))        # cap 15,000: one lot is 6,509.75
        e.simulate(buy(1, "A"), view(symbol="A"))
        before = state_of(e)
        refusals = [
            (buy(1, "B"), view(symbol="B")),                                                   # RISK_MAX_POSITIONS
            (buy(2, "A"), view(symbol="A")),                                                   # RISK_EXPOSURE
            (buy(3, "A"), view(symbol="A")),                                                   # RISK_MAX_LOTS
            (sell(2, "A"), view(symbol="A")),                                                  # SIZE_EXCEEDS_POSITION
            (sell(1, "B"), view(symbol="B")),                                                  # NO_POSITION
            (buy(1, "A"), view(symbol="A", verified=False)), (buy(1, "A"), view(symbol="A", permitted=False)),
            (buy(1, "A"), view(symbol="A", bid=None)), (buy(1, "A"), view(symbol="A", bid=Decimal("100"), ask=Decimal("100"))),
            (buy(1, "A"), view(symbol="A", lot_size=None)), (buy(1, "A"), view(symbol="A", ask_qty=1)),
            (buy(1, "A"), view(symbol="Z")), (buy(0, "A"), view(symbol="A")), (sell(1, "A"), view(symbol="A", bid=Decimal("0.05"), ask=Decimal("0.10"))),
            (buy(1, "A"), live_view(symbol="A", bid_qty=None)),
        ]
        codes = set()
        for request, market in refusals:
            result = e.simulate(request, market)
            self.assertFalse(result.accepted, request)
            codes.add(result.code)
            self.assertEqual(state_of(e), before, result.code)
        self.assertGreaterEqual(len(codes), 12)
        capped, _ = engine(limits=RiskLimits(daily_loss_fraction=Decimal("0.0075")))                  # cap 7,500: one lot (6,533.00) fits, two do not
        capped.simulate(buy(1, "A"), view(symbol="A"))
        before = state_of(capped)
        self.assertEqual(capped.simulate(buy(1, "B"), view(symbol="B")).code, "RISK_DAILY_LOSS")
        self.assertEqual(state_of(capped), before)
        tiny = PaperEngine(Decimal("100"), clock=Clock(), limits=RiskLimits(max_exposure_fraction=Decimal("100"), daily_loss_fraction=Decimal("100")))
        before = state_of(tiny)
        self.assertEqual(tiny.simulate(buy(), view()).code, "INSUFFICIENT_BALANCE")
        self.assertEqual(state_of(tiny), before)

    def test_the_exposure_limit_holds_however_the_buys_are_split(self):
        cap = Decimal("0.10") * Decimal("1000000")
        for prices in (("100.00",), ("5.00", "7.50"), ("2.00",)):
            e, _ = engine(limits=RiskLimits(max_lots_per_trade=1, max_open_positions=3, daily_loss_fraction=Decimal("1")))      # isolate the exposure limit
            for step in range(400):
                symbol = "ABC"[step % len(prices) if len(prices) > 1 else step % 3]
                e.simulate(buy(1, symbol), quote_view(symbol, prices[step % len(prices)]))
                self.assertLessEqual(sum(p.basis for p in e.positions.values()), cap)
            e.simulate(sell(1, "A"), quote_view("A", prices[0]))                                  # selling some does not let the cap be exceeded later
            for step in range(200):
                e.simulate(buy(1, "A"), quote_view("A", prices[0]))
                self.assertLessEqual(sum(p.basis for p in e.positions.values()), cap)
            self.assertLessEqual(len(e.positions), 3)

class TestSeparation(unittest.TestCase):
    def test_the_engine_package_does_not_import_providers_or_the_dashboard_or_the_network(self):
        banned = {"providers", "dashboard", "assembly", "urllib", "http", "socket", "ssl", "requests"}
        for path in sorted((SRC / "niftypaper" / "paper").glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [(node.module or "")] + [a.name for a in node.names]
                for name in names:
                    with self.subTest(file=path.name, module=name):
                        self.assertTrue(banned.isdisjoint(set(name.replace("..", ".").split("."))))

    def test_defaults_are_illustrative_and_say_so(self):
        self.assertIn("illustrative", CostModel().label)
        self.assertEqual(replace(CostModel(), fee_per_trade=Decimal("0")).cost(Decimal("1000")), Decimal("0.50"))


if __name__ == "__main__":
    unittest.main()
