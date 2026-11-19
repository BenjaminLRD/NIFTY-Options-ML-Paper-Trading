"""V6-0.2 integration: fail-closed matrix at dashboard level, an end-to-end flow over HTTP (with the page script run under Node), isolation of the synthetic chart
from the engine and the ledger, honest labels, and the freshness display. Synthetic and test-provider data only; every state folder is temporary.

The end-to-end flow here is NOT driven by a real browser (a unit test cannot start one): it uses real HTTP requests to the real server and executes chart.js under
Node with an in-memory DOM. A real-browser pass was done by hand and is described in the milestone report, not asserted here."""
import ast
import json
import re
import subprocess
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from niftypaper.dashboard import chart
from niftypaper.dashboard.sample_data import SyntheticProvider
from niftypaper.dashboard.server import App
from niftypaper.dashboard.status import (LATENCY_DELAYED_CONFIRMED, LATENCY_REALTIME_DOCUMENTED, LATENCY_UNKNOWN, TIME_LAST_TRADE, TIME_QUOTE, TIME_UNKNOWN, DataState,
                                         OptionRow, Snapshot)
from niftypaper.paper.engine import PaperEngine
from niftypaper.paper.store import JsonlStore
from tests import SRC
from tests.test_dashboard_ui import Base, SYMBOL, text_of
from tests.test_dashboard_workspace import data_of
from tests.test_dashboard_workspace import CHART_JS, HARNESS, NODE
from tests.test_modes import Clock, Served

PERMITTED = {"live-test": (True, "test record")}
ROOT = SRC / "niftypaper"


class FixtureProvider:
    """A TEST provider (source TEST_PROVIDER, clearly not a real feed) whose evidence and quote can be set, to probe the gates. Never presented as live data."""
    last_error = None

    def __init__(self, clock, latency=LATENCY_REALTIME_DOCUMENTED, semantics=TIME_QUOTE, sizes=True, bid="100.95", ask="101.05", missing=False):
        self.clock, self.latency, self.semantics, self.sizes, self.bid, self.ask, self.missing = clock, latency, semantics, sizes, bid, ask, missing
        self.observed = clock()                                   # the response is received once; it ages as the clock moves on

    def __call__(self, now=None):
        if self.missing:
            return None
        t = self.observed
        row = OptionRow("TEST-OPT-1", "CE", 24000, Decimal("101.00"), Decimal(self.bid), Decimal(self.ask), 650 if self.sizes else None, 650 if self.sizes else None, 65, Decimal("0.05"))
        return Snapshot(source="TEST_PROVIDER", synthetic=False, as_of=t, index_value=Decimal("24000"), pct_change=None, chain=(row,), received_at=t, provider_time_raw="raw",
                        provider_time=t - timedelta(seconds=1), time_semantics=self.semantics, latency_class=self.latency)


def run_js(data, actions):
    """chart.js under Node with an in-memory DOM; the page's own data block goes in, what the page would show after each action comes out."""
    done = subprocess.run([NODE, str(HARNESS), str(CHART_JS)], input=json.dumps({"data": data, "actions": actions}), capture_output=True, text=True, encoding="utf-8", timeout=60)
    if done.returncode:
        raise AssertionError(done.stderr)
    return json.loads(done.stdout)


def provider_app(provider, clock, permitted=True):
    return App(provider, clock=clock, mode="provider", provider_id="live-test", factories={"synthetic": lambda: SyntheticProvider(), "live-test": lambda: provider},
               engine_factory=lambda key: PaperEngine(Decimal("1000000"), clock=clock, origin="PROVIDER"), permissions=PERMITTED if permitted else {})


class TestFailClosedMatrix(unittest.TestCase):
    """Every way an execution price can be unfit: nothing is recorded, the ledger stays empty and the form either disappears or the order is refused."""
    CASES = (
        # name, provider kwargs, permission, expected data state, trade form shown, order accepted
        ("verified_and_permitted (control)", {}, True, DataState.LIVE, True, True),
        ("provider says nothing about latency", {"latency": LATENCY_UNKNOWN}, True, DataState.UNVERIFIED, False, False),
        ("provider confirms the data is delayed", {"latency": LATENCY_DELAYED_CONFIRMED}, True, DataState.DELAYED, False, False),
        ("a last-trade time is not a quote time", {"semantics": TIME_LAST_TRADE}, True, DataState.UNVERIFIED, False, False),
        ("quote time meaning unknown", {"semantics": TIME_UNKNOWN}, True, DataState.UNVERIFIED, False, False),
        ("verified data but no recorded terms permission", {}, False, DataState.LIVE, False, False),
        ("no displayed sizes", {"sizes": False}, True, DataState.LIVE, True, False),
        ("crossed market", {"bid": "101.10", "ask": "101.05"}, True, DataState.LIVE, True, False),
        ("locked market", {"bid": "101.05", "ask": "101.05"}, True, DataState.LIVE, True, False),
        ("off-tick bid", {"bid": "100.93"}, True, DataState.LIVE, True, False),
        ("zero bid", {"bid": "0"}, True, DataState.LIVE, True, False),
        ("no data at all", {"missing": True}, True, DataState.UNAVAILABLE, False, False),
    )

    def test_each_case_trades_only_when_every_condition_holds(self):
        for name, kwargs, permitted, state, form, accepted in self.CASES:
            with self.subTest(name):
                clock = Clock()
                app = provider_app(FixtureProvider(clock, **kwargs), clock, permitted)
                self.addCleanup(app.close)
                health, _, _ = app.view()
                self.assertEqual(health.state, state)
                with Served(app) as s:
                    page = s.request("GET", "/")[2]
                self.assertEqual('action="/paper/trade"' in page, form)
                app.handle_trade({"symbol": "TEST-OPT-1", "side": "BUY", "lots": "1"})
                self.assertEqual(len(app.engine.ledger), 1 if accepted else 0)
                self.assertEqual(app.notice_kind, "trade_ok" if accepted else "trade_refused")

    def test_a_response_that_has_become_stale_blocks_a_new_entry_and_an_exit_alike(self):
        clock = Clock()
        app = provider_app(FixtureProvider(clock), clock)
        self.addCleanup(app.close)
        app.handle_trade({"symbol": "TEST-OPT-1", "side": "BUY", "lots": "1"})
        self.assertEqual(len(app.engine.ledger), 1)
        clock.advance(31)                                                                      # older than the 30 s limit
        self.assertEqual(app.view()[0].state, DataState.STALE)
        for side in ("BUY", "SELL"):
            app.handle_trade({"symbol": "TEST-OPT-1", "side": side, "lots": "1"})
            self.assertEqual((app.notice_kind, len(app.engine.ledger)), ("trade_refused", 1))
        with Served(app) as s:
            page = s.request("GET", "/")[2]
            snapshot = json.loads(s.request("GET", "/api/snapshot")[2])
        self.assertNotIn('action="/paper/trade"', page)
        self.assertEqual((snapshot["data_state"], snapshot["market"], snapshot["chain"]), ("STALE", None, []))

    def test_synthetic_stale_and_unavailable_scenarios_block_every_order(self):
        for scenario in ("stale", "unavailable"):
            with self.subTest(scenario):
                clock = Clock()
                app = App(SyntheticProvider(scenario), clock=clock, mode="synthetic", provider_id="synthetic", factories={"synthetic": lambda s=scenario: SyntheticProvider(s)},
                          engine_factory=lambda key: PaperEngine(Decimal("1000000"), clock=clock, origin="SYNTHETIC"))
                for side in ("BUY", "SELL"):
                    app.handle_trade({"symbol": SYMBOL, "side": side, "lots": "1"})
                    self.assertEqual((app.notice_kind, app.engine.ledger), ("trade_refused", []))


class TestEndToEndOverHttp(Base):
    """One account, one directory: select, chart windows, accepted BUY, risk refusal, SELL validation, a valid exit, a restart, then stale data."""
    def apps(self, provider_factory=SyntheticProvider):
        clock = Clock()
        app = App(provider_factory(), clock=clock, mode="synthetic", provider_id="synthetic", factories={"synthetic": lambda: provider_factory()},
                  engine_factory=lambda key: PaperEngine(Decimal("1000000"), clock=clock, origin="SYNTHETIC",
                                                         store=JsonlStore(Path(self._tmp.name) / f"paper_{key}.jsonl", 1_000_000, account=key)))
        self.addCleanup(app.close)
        return app

    def trade(self, s, side, lots="1", symbol=SYMBOL):
        status = s.post("/paper/trade", {"symbol": symbol, "side": side, "lots": lots})[0]
        self.assertEqual(status, 303)
        return s.request("GET", "/")[2]

    def notice(self, page):
        m = re.search(r'<p class="notice ([a-z]*)" role="status"><span>(.*?)</span>(.*?)</p>', page, re.S)
        return (m.group(1), m.group(2), m.group(3)) if m else None

    def test_the_whole_workflow(self):
        state_file = Path(self._tmp.name) / "paper_synthetic.jsonl"
        app = self.apps()
        with Served(app) as s:
            page = s.request("GET", "/")[2]
            data = data_of(page)
            # 1. contract selection keeps the chart, the details and the order summary together; 2. every timeframe redraws (script steps need Node)
            if NODE:
                steps = run_js(data, [{"do": "select", "value": "SYN-PE-23900"}, {"do": "window", "value": "30m"}, {"do": "window", "value": "1h"}, {"do": "window", "value": "2h"},
                                      {"do": "window", "value": "4h"}])
                self.assertEqual((steps[1]["kind"], steps[1]["strike"], steps[1]["select"], steps[1]["watch"]), ("Put option (PE)", "23,900", "SYN-PE-23900", ["SYN-PE-23900"]))
                self.assertIn("Put option (PE), strike 23,900", " ".join(steps[1]["summary"]))
                self.assertEqual([st["candles"] for st in steps[2:]], [30, 12, 24, 48])
                self.assertEqual([st["current"] for st in steps[2:]], [["30m"], ["1h"], ["2h"], ["4h"]])
            self.assertEqual(set(data["history"][SYMBOL]), {"30m", "1h", "2h", "4h"})
            # 3. a BUY the simulator accepts
            page = self.trade(s, "BUY")
            self.assertEqual(self.notice(page)[:2], ("ok", "&#10003; Recorded: "))
            self.assertEqual(len(app.engine.ledger), 1)
            # 4. a BUY the risk limit refuses (nothing recorded, no success copy)
            page = self.trade(s, "BUY")
            kind, heading, _ = self.notice(page)
            self.assertEqual((kind, heading), ("refused", "&#10007; Refused by the daily risk limit: "))
            self.assertEqual(len(app.engine.ledger), 1)
            self.assertNotIn("Recorded", page)
            # 5. SELL validation: the page says what is held and refuses more; the engine agrees
            held = data_of(s.request("GET", "/")[2])
            self.assertEqual(held["order"]["symbols"][SYMBOL]["held_lots"], 1)
            if NODE:
                steps = run_js(held, [{"do": "side", "value": "SELL"}, {"do": "lots", "value": "2"}])
                self.assertIn("Available to sell | 1 lot (65 units)", " ".join(steps[1]["summary"]))
                self.assertIn("selling 2 would be refused", " ".join(steps[2]["summary"]))
            page = self.trade(s, "SELL", "2")
            self.assertEqual((self.notice(page)[0], len(app.engine.ledger)), ("refused", 1))
            self.assertIn("short selling is not simulated", self.notice(page)[2])                                   # the engine's own reason for SIZE_EXCEEDS_POSITION
            # 6. a valid simulated exit, then a new entry that stays open for the restart
            page = self.trade(s, "SELL", "1")
            self.assertEqual((self.notice(page)[0], len(app.engine.ledger), app.engine.positions), ("ok", 2, {}))
            page = self.trade(s, "BUY")
            self.assertEqual((self.notice(page)[0], len(app.engine.ledger)), ("ok", 3))
            ledger_before = json.loads(s.request("GET", "/api/ledger")[2])["trades"]
        app.close()
        bytes_before = state_file.read_bytes()
        # 7. persistence after a restart: same trades, same position, nothing rewritten
        second = self.apps()
        with Served(second) as s:
            page = s.request("GET", "/")[2]
            ledger_after = json.loads(s.request("GET", "/api/ledger")[2])["trades"]
        self.assertEqual(ledger_after, ledger_before)
        self.assertIn("Trades restored when the session opened</dt><dd>3 (counted once", page)
        self.assertRegex(page, rf'<td class="l">{SYMBOL}</td><td>65</td><td>1</td>')
        self.assertEqual(state_file.read_bytes(), bytes_before)
        second.close()
        # 8. stale data on the same account: no form, no new entry and no exit, nothing written
        stale = self.apps(lambda: SyntheticProvider("stale"))
        with Served(stale) as s:
            page = s.request("GET", "/")[2]
            self.assertNotIn('action="/paper/trade"', page)
            self.assertIn("Virtual trading is blocked", page)
            for side in ("BUY", "SELL"):
                page = self.trade(s, side)
                self.assertEqual(self.notice(page)[0], "refused")
            snapshot = json.loads(s.request("GET", "/api/snapshot")[2])
        self.assertEqual((snapshot["data_state"], snapshot["market"]), ("STALE", None))
        self.assertEqual(len(stale.engine.ledger), 3)
        self.assertEqual(state_file.read_bytes(), bytes_before)


class TestChartIsolation(Base):
    def py_files(self, *parts):
        return sorted((ROOT.joinpath(*parts)).rglob("*.py")) if parts else sorted(ROOT.rglob("*.py"))

    def imports(self, path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                base = ("." * node.level) + (node.module or "")
                found |= {base} | {f"{base}.{a.name}".replace("..", ".") for a in node.names}
        return found

    def test_nothing_outside_the_dashboard_can_import_the_chart_and_the_engines_cannot_import_the_dashboard(self):
        for path in self.py_files():
            names = self.imports(path)
            relative = path.relative_to(ROOT).parts
            if relative[0] != "dashboard":
                with self.subTest(file=str(path.relative_to(ROOT))):
                    self.assertFalse({n for n in names if n.endswith("dashboard.chart") or n.endswith("dashboard.render") or n.endswith("dashboard.trading")})
            if relative[0] in ("paper", "strategy", "historical", "evaluation"):
                with self.subTest(file=str(path.relative_to(ROOT))):
                    self.assertFalse({n for n in names if "dashboard" in n})
        users = [p.name for p in self.py_files("dashboard") if any(n.endswith("chart") for n in self.imports(p) if n not in ("hashlib", "decimal"))]
        self.assertEqual(sorted(set(users)), ["trading.py"])                                   # the only bridge: it asks chart.py for pictures and hands nothing back
        self.assertEqual({n for n in self.imports(ROOT / "dashboard" / "chart.py") if n}, {"hashlib", "decimal", "decimal.ROUND_HALF_UP", "decimal.Decimal"})

    def run_trades(self, patched=None):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            app = App(SyntheticProvider(), clock=clock, mode="synthetic", provider_id="synthetic", factories={"synthetic": lambda: SyntheticProvider()},
                      engine_factory=lambda key: PaperEngine(Decimal("1000000"), clock=clock, origin="SYNTHETIC", store=JsonlStore(Path(tmp) / "paper_synthetic.jsonl", 1_000_000, account=key)))
            try:
                with Served(app) as s:
                    s.request("GET", "/")
                    for side, lots in (("BUY", "1"), ("BUY", "1"), ("SELL", "1")):
                        s.post("/paper/trade", {"symbol": SYMBOL, "side": side, "lots": lots})
                    page = s.request("GET", "/")[2]
                    ledger = s.request("GET", "/api/ledger")[2]
                raw = (Path(tmp) / "paper_synthetic.jsonl").read_bytes()
            finally:
                app.close()
        return [r.to_dict() for r in app.engine.ledger], raw, ledger, page

    def test_replacing_or_breaking_the_chart_changes_no_fill_ledger_or_state_byte(self):
        baseline = self.run_trades()
        wild = lambda symbol, price, tick, minutes=chart.MINUTES: [[minutes - 1 - i, Decimal("999.00"), Decimal("1000.00"), Decimal("998.00"), Decimal("999.50")] for i in range(minutes)]
        with mock.patch.object(chart, "minute_candles", wild):
            poisoned = self.run_trades()
        self.assertEqual(poisoned[0], baseline[0])
        self.assertEqual(poisoned[1], baseline[1])                                                 # the saved state is byte-identical
        self.assertEqual(poisoned[2], baseline[2])
        self.assertNotEqual(poisoned[3], baseline[3])                                              # only the picture changed
        with mock.patch.object(chart, "minute_candles", side_effect=RuntimeError("chart broke")):
            broken = self.run_trades()
        self.assertEqual((broken[0], broken[1], broken[2]), baseline[:3])
        self.assertIn("No chart:", broken[3])                                                      # a failing chart degrades to a message; trading and the page keep working
        self.assertIn('action="/paper/trade"', broken[3])

    def test_the_saved_ledger_holds_quotes_and_fills_only_never_history(self):
        _, raw, _, _ = self.run_trades()
        text = raw.decode("utf-8")
        for forbidden in ("history", "candle", "ohlc", "SIMULATED HISTORY", "minutes_ago"):
            self.assertNotIn(forbidden, text.lower() if forbidden.islower() else text)
        for line in text.splitlines()[1:]:
            record = json.loads(line)
            self.assertEqual(record["quote_bid"] if record["side"] == "BUY" else record["quote_ask"], "279.90" if record["side"] == "BUY" else "280.10")
            self.assertEqual(record["data_origin"], "SYNTHETIC")

    def test_the_strategy_and_the_evaluator_read_no_dashboard_or_chart_data(self):
        for package in ("strategy", "evaluation"):
            for path in self.py_files(package):
                self.assertFalse({n for n in self.imports(path) if "dashboard" in n or n.endswith("chart")}, path.name)


class TestLabelsAndFreshness(Base):
    def test_synthetic_values_are_tagged_and_the_page_says_what_it_is(self):
        with Served(self.app()) as s:
            page = s.request("GET", "/")[2]
        cells = re.findall(r'<(?:td|span) class="num" data-synthetic="([a-z]+)">[^<]*<span class="tag">([A-Z]+)</span>', page)
        self.assertEqual(len(cells), 2 + 30)
        self.assertTrue(all(flag == "true" and tag == "SYN" for flag, tag in cells))
        text = text_of(page)
        self.assertIn("Data mode: Synthetic Mode — invented numbers from SYNTHETIC_SAMPLE_PROVIDER, state SIMULATED", __import__("html").unescape(text))
        self.assertIn("Quotes are not trading", text)
        self.assertIn("no broker connection, every trade is virtual and no real order can be sent", text)
        self.assertIn("SIMULATED HISTORY", page)
        self.assertNotIn("state-LIVE", page.split("</style>")[1])
        self.assertIn("s old since the data was produced; treated as stale after 30 s", text)

    def test_provider_values_are_never_labelled_synthetic_or_simulated(self):
        clock = Clock()
        app = provider_app(FixtureProvider(clock, latency=LATENCY_UNKNOWN), clock)
        self.addCleanup(app.close)
        with Served(app) as s:
            status, headers, page = s.request("GET", "/")
            doc = json.loads(s.request("GET", "/api/snapshot")[2])
        cells = re.findall(r'<(?:td|span) class="num" data-synthetic="([a-z]+)">[^<]*<span class="tag">([A-Z]+)</span>', page)
        self.assertTrue(cells)
        self.assertTrue(all(flag == "false" and tag == "UNVERIFIED" for flag, tag in cells))
        for banned in ("SIMULATED DATA", "SIMULATED HISTORY", "SYN-", 'data-synthetic="true"', "invented numbers"):
            self.assertNotIn(banned, page)
        self.assertEqual((headers["X-Data-Synthetic"], doc["synthetic"], doc["data_state"]), ("false", False, "UNVERIFIED"))
        text = __import__("html").unescape(text_of(page))
        self.assertIn("Data mode: Real-Time Mode — provider data from TEST_PROVIDER, state UNVERIFIED", text)
        self.assertIn("since the response was received; treated as stale after 30 s", text)
        self.assertIn("No chart:", text)

    def test_a_provider_fill_is_recorded_as_provider_data_in_the_ledger_table(self):
        clock = Clock()
        app = provider_app(FixtureProvider(clock), clock)
        self.addCleanup(app.close)
        app.handle_trade({"symbol": "TEST-OPT-1", "side": "BUY", "lots": "1"})
        with Served(app) as s:
            page = s.request("GET", "/")[2]
        self.assertIn("PROVIDER / TEST_PROVIDER", page)
        self.assertNotIn("SYNTHETIC /", page)

    def test_stale_data_shows_its_age_and_no_values(self):
        clock = Clock()
        app = App(SyntheticProvider("stale"), clock=clock, mode="synthetic", provider_id="synthetic", factories={"synthetic": lambda: SyntheticProvider("stale")},
                  engine_factory=lambda key: PaperEngine(Decimal("1000000"), clock=clock, origin="SYNTHETIC"))
        with Served(app) as s:
            page = s.request("GET", "/")[2]
        self.assertIn("900 s old since the data was produced", page)
        self.assertNotIn('class="num"', page)
        self.assertIn("STALE", page)


if __name__ == "__main__":
    unittest.main()
