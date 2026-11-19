"""Chart, contract details and order-entry estimates. Synthetic data only; every state folder is temporary. The script (chart.js) is also executed under Node with a
small in-memory DOM when Node is installed (skipped otherwise): no browser and no network are involved."""
import ast
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from niftypaper.dashboard import chart
from niftypaper.dashboard.sample_data import SyntheticProvider
from niftypaper.dashboard.server import App
from niftypaper.dashboard.trading import build_views
from niftypaper.paper.engine import PaperEngine
from niftypaper.paper.store import JsonlStore
from tests import SRC
from tests.test_dashboard_ui import Base, SYMBOL, text_of
from tests.test_modes import Clock, Served, fyers_provider, make_app
from tests.test_fyers import INDEX_ENTRY, OPTION_ENTRY, ok

CENT = Decimal("0.01")
NODE = shutil.which("node")
HARNESS = Path(__file__).with_name("dashboard_js_harness.js")
CHART_JS = SRC / "niftypaper" / "dashboard" / "chart.js"


def data_of(page):
    return json.loads(re.search(r'<script type="application/json" id="workspace-data">(.*?)</script>', page, re.S).group(1).replace("<\\/", "</"))


def rows():
    return {r.symbol: r for r in SyntheticProvider()(Clock()()).chain}


class TestSyntheticSeries(unittest.TestCase):
    def test_the_series_is_deterministic_and_pinned(self):
        a = chart.minute_candles(SYMBOL, Decimal("280.00"), Decimal("0.05"))
        b = chart.minute_candles(SYMBOL, Decimal("280.00"), Decimal("0.05"))
        self.assertEqual(a, b)
        self.assertNotEqual(a, chart.minute_candles("SYN-PE-23800", Decimal("280.00"), Decimal("0.05")))                 # each contract has its own series
        digest = hashlib.sha256(repr([[c[0]] + [str(v) for v in c[1:]] for c in a]).encode()).hexdigest()
        self.assertEqual(digest, "f6fdce589d2fc64495db6857fd0cc250c4fdd1b0a1cd669df1856790922aab63")                      # the same on every run and machine (integer arithmetic)
        self.assertEqual((a[0][0], a[-1][0], len(a)), (239, 0, 240))

    def test_every_candle_is_valid_ohlc_on_the_tick_grid_and_ends_at_the_current_premium(self):
        for symbol, row in rows().items():
            series = chart.minute_candles(symbol, row.price, row.tick)
            self.assertEqual(series[-1][4], row.price)
            for label, candles in chart.windows(series).items():
                minutes, step = chart.WINDOWS[label]
                with self.subTest(symbol=symbol, window=label):
                    self.assertEqual(len(candles), minutes // step)
                    self.assertTrue(all(chart.valid_ohlc(c) for c in candles))
                    self.assertTrue(all((v / row.tick) == (v / row.tick).to_integral_value() for c in candles for v in c[1:]))
                    self.assertEqual(candles[-1][0], 0)

    def test_larger_candles_are_built_from_the_same_minute_values(self):
        series = chart.minute_candles(SYMBOL, Decimal("280.00"), Decimal("0.05"))
        five = chart.aggregate(series[-60:], 5)
        for i, candle in enumerate(five):
            group = series[-60:][i * 5:(i + 1) * 5]
            self.assertEqual(candle[1:], [group[0][1], max(c[2] for c in group), min(c[3] for c in group), group[-1][4]])
        with self.assertRaises(ValueError):
            chart.aggregate(series[:7], 5)
        with self.assertRaises(ValueError):
            chart.minute_candles("X", Decimal("0"), Decimal("0.05"))

    def test_the_chart_module_cannot_reach_the_engine_or_the_network(self):
        tree = ast.parse((SRC / "niftypaper" / "dashboard" / "chart.py").read_text(encoding="utf-8"))
        imported = {n.module if isinstance(n, ast.ImportFrom) else a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in getattr(n, "names", [None])}
        self.assertEqual({i.split(".")[0] for i in imported if i}, {"hashlib", "decimal"})
        self.assertIn("never market history", chart.__doc__.lower().replace("is never ", "never ").replace("history, never a quote", "history, never a quote"))


class TestContractDetails(Base):
    def test_calls_and_puts_are_described_in_words_with_the_right_numbers(self):
        r = rows()
        ce, pe = chart.contract_details(r["SYN-CE-23800"]), chart.contract_details(r["SYN-PE-23800"])
        self.assertEqual((ce["kind_text"], ce["strike_text"], ce["premium"], ce["bid"], ce["ask"], ce["lot_size"]), ("Call option (CE)", "23,800", "280.00", "279.90", "280.10", 65))
        self.assertEqual((pe["kind_text"], pe["title"]), ("Put option (PE)", "Put option (PE), strike 23,800"))
        for row in r.values():
            d = chart.contract_details(row)
            self.assertLess(Decimal(d["bid"]), Decimal(d["premium"]))
            self.assertLess(Decimal(d["premium"]), Decimal(d["ask"]))

    def test_the_page_explains_the_terms_without_decoding_a_symbol(self):
        with Served(self.app()) as s:
            page = s.request("GET", "/")[2]
        text = __import__("html").unescape(text_of(page))
        for needle in ("SYN-CE-23800 — Call option (CE) · strike 23,800 · premium", "SYN-PE-23800 — Put option (PE) · strike 23,800 · premium",
                       "CE means Call option; PE means Put option.", "The contract's reference level. It is not the price you pay.", "The price per unit of the option (synthetic here).",
                       "The simulated price a seller receives: what you get if you sell.", "The simulated price a buyer pays: what you pay if you buy.",
                       "Synthetic examples: not actual NSE contracts or quotes.", "1 synthetic lot = 65 simulator units", "not a verified exchange lot size",
                       "Latest simulated quote", "separate from the chart history"):
            self.assertIn(needle, text)
        self.assertIn(">23,800<", page)
        self.assertIn("&#8377;280.00", page)
        self.assertIn("&#8377;279.90", page)
        self.assertIn("&#8377;280.10", page)


class TestOrderNumbersComeFromTheEngine(Base):
    def setup_engine(self):
        app = self.app()
        health, snapshot, _ = app.view()
        return app, health, snapshot

    def table(self, app, symbol=SYMBOL):
        health, snapshot, _ = app.view()
        views = build_views(health, snapshot, True)
        report = app.engine.risk_report(views)
        held = {p.symbol: p.quantity // p.lot_size for p in app.engine.positions.values()}
        return chart.order_table(views[symbol], app.engine, report, held.get(symbol, 0), sum((p.basis for p in app.engine.positions.values()), Decimal("0")),
                                 len(app.engine.positions)), report

    def test_a_buy_that_fits_is_marked_as_fitting_and_the_engine_accepts_it(self):
        app, _, _ = self.setup_engine()
        table, report = self.table(app)
        one = table["buy"]["1"]
        self.assertTrue(one["fits"] and one["exposure_ok"] and one["cash_ok"])
        self.assertEqual((one["units"], one["value"], one["fee"], one["exit_fee"], one["risk"], one["excess"]), (65, "18209.75", "29.10", "29.10", "18267.95", "0.00"))
        self.assertEqual(Decimal(one["risk"]), Decimal("18209.75") + Decimal("29.10") * 2)
        result = app.engine.simulate(__import__("niftypaper.paper.engine", fromlist=["TradeRequest"]).TradeRequest(SYMBOL, "BUY", 1), build_views(*app.view()[:2], True)[SYMBOL])
        self.assertTrue(result.accepted)
        self.assertEqual(str(result.record.turnover), one["value"])                                                # the estimate equals the real fill value
        self.assertEqual(str(result.record.cost), one["fee"])

    def test_a_buy_that_exceeds_the_budget_shows_the_excess_the_engine_then_uses(self):
        app, _, _ = self.setup_engine()
        app.handle_trade({"symbol": SYMBOL, "side": "BUY", "lots": "1"})
        table, report = self.table(app)
        one = table["buy"]["1"]
        self.assertFalse(one["fits"])
        self.assertEqual(Decimal(one["excess"]), Decimal(one["risk"]) - report.headroom)
        app.handle_trade({"symbol": SYMBOL, "side": "BUY", "lots": "1"})
        self.assertEqual(app.notice_code, "RISK_DAILY_LOSS")
        projected = Decimal(re.search(r"projected worst-case daily loss ([0-9,.]+)", app.notice).group(1).replace(",", ""))
        self.assertEqual(projected - report.budget, Decimal(one["excess"]))                                      # the excess shown before submission is the engine's own
        self.assertEqual(len(app.engine.ledger), 1)

    def test_quantity_changes_the_estimate_and_two_lots_do_not_fit_an_empty_budget(self):
        app, _, _ = self.setup_engine()
        table, _ = self.table(app)
        one, two = table["buy"]["1"], table["buy"]["2"]
        self.assertEqual(two["units"], 130)
        self.assertGreater(Decimal(two["risk"]), Decimal(one["risk"]))
        self.assertFalse(two["fits"])
        app.handle_trade({"symbol": SYMBOL, "side": "BUY", "lots": "2"})
        self.assertEqual((app.notice_kind, app.notice_code), ("trade_refused", "RISK_DAILY_LOSS"))
        self.assertEqual(set(table["buy"]), {"1", "2"})                                                         # the per-trade limit is 2 lots: nothing is estimated beyond it

    def test_sell_is_offered_only_for_what_is_held_and_matches_the_engine(self):
        app, _, _ = self.setup_engine()
        table, _ = self.table(app)
        self.assertEqual((table["held_lots"], table["sell"]), (0, {}))
        app.handle_trade({"symbol": SYMBOL, "side": "SELL", "lots": "1"})
        self.assertEqual(app.notice_code, "NO_POSITION")                                                         # nothing to sell: refused, so the page does not promise it
        app.handle_trade({"symbol": SYMBOL, "side": "BUY", "lots": "1"})
        table, _ = self.table(app)
        self.assertEqual((table["held_lots"], table["held_units"], set(table["sell"])), (1, 65, {"1"}))
        app.handle_trade({"symbol": SYMBOL, "side": "SELL", "lots": "2"})
        self.assertEqual(app.notice_code, "SIZE_EXCEEDS_POSITION")                                               # more than held: no estimate and a refusal
        before = len(app.engine.ledger)
        app.handle_trade({"symbol": SYMBOL, "side": "SELL", "lots": "1"})
        self.assertEqual((app.notice_kind, len(app.engine.ledger)), ("trade_ok", before + 1))
        self.assertEqual(str(app.engine.ledger[-1].turnover), table["sell"]["1"]["proceeds"])                    # proceeds estimate = the real sale value
        self.assertEqual(str(app.engine.ledger[-1].cost), table["sell"]["1"]["fee"])

    def test_an_exit_is_not_blocked_by_the_daily_budget_that_blocks_new_buys(self):
        app, _, _ = self.setup_engine()
        app.handle_trade({"symbol": SYMBOL, "side": "BUY", "lots": "1"})
        table, report = self.table(app)
        self.assertFalse(table["buy"]["1"]["fits"])
        self.assertTrue(table["sell"]["1"])
        app.handle_trade({"symbol": SYMBOL, "side": "SELL", "lots": "1"})
        self.assertEqual(app.notice_kind, "trade_ok")


class TestWorkspacePage(Base):
    def test_the_page_carries_one_consistent_data_block_for_every_contract(self):
        app = self.app()
        with Served(app) as s:
            page = s.request("GET", "/")[2]
        data = data_of(page)
        options = re.findall(r'<option value="(SYN-[^"]+)"', page)
        self.assertEqual(data["symbols"], options)
        self.assertEqual(data["default"], options[0])
        self.assertEqual(set(data["details"]), set(options))
        self.assertEqual(set(data["history"]), set(options))
        self.assertEqual(set(data["order"]["symbols"]), set(options))
        for symbol in options:
            for label in data["windows"]:
                last = data["history"][symbol][label][-1]
                self.assertEqual((last[0], last[4]), (0, data["details"][symbol]["premium"]))                    # the history ends at that contract's current synthetic premium
        health, snapshot, _ = app.view()
        self.assertEqual(data["order"]["headroom"], format(app.engine.risk_report(build_views(health, snapshot, True)).headroom, "f"))
        self.assertIn("SIMULATED HISTORY", page)
        self.assertLess(page.index('id="chart-svg"'), page.index('id="order-summary"'))                          # chart first, execution kept apart

    def test_history_is_identical_on_every_refresh_and_never_changes_a_fill(self):
        app = self.app()
        with Served(app) as s:
            first = data_of(s.request("GET", "/")[2])["history"]
            second = data_of(s.request("GET", "/")[2])["history"]
            s.post("/paper/trade", {"symbol": SYMBOL, "side": "BUY", "lots": "1"})
        self.assertEqual(first, second)
        self.assertEqual(str(app.engine.ledger[0].fill_price), "280.15")                                         # the quote's ask plus a tick, not a chart price

    def test_stale_unavailable_and_provider_data_get_no_chart_and_nothing_synthetic(self):
        for scenario in ("stale", "unavailable"):
            with self.subTest(scenario=scenario):
                app = App(SyntheticProvider(scenario), clock=Clock(), mode="synthetic", provider_id="synthetic", factories={"synthetic": lambda: SyntheticProvider()},
                          engine_factory=lambda key: PaperEngine(Decimal("1000000"), clock=Clock(), origin="SYNTHETIC"))
                with Served(app) as s:
                    page = s.request("GET", "/")[2]
                self.assertNotIn('id="chart-svg"', page)
                self.assertNotIn("workspace-data", page)
                self.assertNotIn("<script", page)
                self.assertIn("No chart:", page)
        clock = Clock()
        for real in (lambda: fyers_provider(clock, ok(INDEX_ENTRY, OPTION_ENTRY))[0],):
            app = make_app(clock, mode="provider", real=real)
            with Served(app) as s:
                page = s.request("GET", "/")[2]
            self.assertNotIn('id="chart-svg"', page)
            self.assertNotIn("SIMULATED HISTORY", page)
            self.assertNotIn("<script", page)
            self.assertIn("never charted", page)


@unittest.skipUnless(NODE, "Node.js is not installed")
class TestScriptInteractions(Base):
    def run_js(self, data, actions):
        done = subprocess.run([NODE, str(HARNESS), str(CHART_JS)], input=json.dumps({"data": data, "actions": actions}), capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def page_data(self, app):
        with Served(app) as s:
            return data_of(s.request("GET", "/")[2])

    def test_choosing_an_instrument_updates_the_chart_details_and_order_together(self):
        data = self.page_data(self.app())
        steps = self.run_js(data, [{"do": "select", "value": "SYN-PE-23800"}, {"do": "watch", "value": "SYN-CE-24100"}])
        start, put, watch = steps
        self.assertTrue(start["js_class"])
        self.assertEqual((start["title"], start["kind"], start["strike"], start["premium"]), ("SYN-CE-23800 — Call option (CE), strike 23,800", "Call option (CE)", "23,800", "₹280.00"))
        self.assertEqual((put["title"], put["kind"], put["premium"], put["bid"], put["ask"], put["select"]),
                         ("SYN-PE-23800 — Put option (PE), strike 23,800", "Put option (PE)", "₹80.00", "₹79.90", "₹80.10", "SYN-PE-23800"))
        self.assertIn("Put option (PE)", " ".join(put["summary"]))
        self.assertIn("SYN-PE-23800", put["readout"])
        self.assertEqual(put["watch"], ["SYN-PE-23800"])
        self.assertEqual((watch["select"], watch["kind"], watch["strike"], watch["watch"]), ("SYN-CE-24100", "Call option (CE)", "24,100", ["SYN-CE-24100"]))
        self.assertEqual(watch["lot"], "1 synthetic lot = 65 simulator units")
        self.assertGreater(put["candles"], 0)

    def test_time_windows_redraw_the_candles(self):
        steps = self.run_js(self.page_data(self.app()), [{"do": "window", "value": w} for w in ("30m", "2h", "4h", "1h")])
        self.assertEqual([s["candles"] for s in steps], [12, 30, 24, 48, 12])
        self.assertEqual([s["current"] for s in steps], [["1h"], ["30m"], ["2h"], ["4h"], ["1h"]])
        self.assertTrue(all(s["grid"] == 5 for s in steps))

    def test_hover_and_keyboard_move_a_crosshair_and_the_readout(self):
        steps = self.run_js(self.page_data(self.app()), [{"do": "pointer", "x": 5}, {"do": "key", "value": "ArrowRight"}, {"do": "key", "value": "Home"}, {"do": "key", "value": "End"}])
        start, hover, right, home, end = steps
        self.assertEqual((start["crosshair"], hover["crosshair"]), ("hidden", "visible"))
        self.assertIn("SIMULATED HISTORY", hover["readout"])
        self.assertIn("min ago", hover["readout"])
        self.assertIn("min ago", home["readout"])
        self.assertIn("latest candle", end["readout"])
        self.assertNotEqual(hover["readout"], right["readout"])

    def test_quantity_and_side_change_the_estimate_with_the_backends_figures(self):
        app = self.app()
        data = self.page_data(app)
        steps = self.run_js(data, [{"do": "lots", "value": "2"}, {"do": "lots", "value": "3"}, {"do": "lots", "value": "x"}, {"do": "lots", "value": "1"}])
        start, two, three, bad, one = steps
        text = lambda s: " ".join(s["summary"])
        self.assertIn("BUY 1 lot | 65 units of Call option (CE), strike 23,800", text(start))
        self.assertIn("Counts against the risk budget | ₹18,267.95", text(start))
        self.assertIn("Appears to fit the daily risk budget", text(start))
        self.assertIn("BUY 2 lots | 130 units", text(two))
        self.assertIn("Over the daily risk budget by", text(two))
        excess = data["order"]["symbols"][SYMBOL]["buy"]["2"]["excess"]
        self.assertIn(f"₹{Decimal(excess):,.2f}", text(two))                                                  # the excess is the server's number
        self.assertIn("More than the per-trade limit of 2 lots", text(three))
        self.assertIn("Enter a whole number of lots", text(bad))
        self.assertEqual(text(one), text(start))

    def test_a_buy_that_no_longer_fits_and_sell_validation_follow_the_backend(self):
        app = self.app()
        app.handle_trade({"symbol": SYMBOL, "side": "BUY", "lots": "1"})
        data = self.page_data(app)
        steps = self.run_js(data, [{"do": "side", "value": "SELL"}, {"do": "lots", "value": "2"}, {"do": "select", "value": "SYN-PE-23800"}, {"do": "side", "value": "BUY"}])
        start, sell1, sell2, no_position, buy = steps
        text = lambda s: " ".join(s["summary"])
        self.assertIn("Over the daily risk budget by", text(start))
        self.assertIn("will refuse this order", text(start))
        self.assertIn("Available to sell | 1 lot (65 units)", text(sell1))
        self.assertIn("Exits are not blocked by the daily risk budget", text(sell1))
        self.assertIn("Estimated proceeds (before fee)", text(sell1))
        self.assertIn("You hold 1 lot: selling 2 would be refused", text(sell2))
        self.assertIn("You hold none of this contract", text(no_position))
        self.assertIn("short selling is not simulated", text(no_position))
        self.assertIn("Over the daily risk budget by", text(buy))


if __name__ == "__main__":
    unittest.main()
