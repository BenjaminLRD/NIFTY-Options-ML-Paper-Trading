"""Tests for the V6-0.2 local dashboard. Synthetic data only; the only network traffic is to a throwaway loopback server.
These tests show properties of this code as written. They do not establish that the dashboard is secure."""
import ast
import contextlib
import http.client
import io
import json
import re
import threading
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html.parser import HTMLParser

from niftypaper import EligibilityPolicy, Quote, QuoteType, Reason, Side, check_fill_eligibility, fill_price, require_fillable
from niftypaper.dashboard import __main__ as cli
from niftypaper.dashboard.render import BANNER, render_page, snapshot_json
from niftypaper.dashboard.sample_data import SOURCE, SyntheticProvider
from niftypaper.dashboard.server import App, create_server
from niftypaper.dashboard.status import DataState, PaperAccount, Snapshot, assess
from tests import SRC

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 1, 5, 9, 30, 0, tzinfo=IST)
MAX_AGE = timedelta(seconds=30)
INDEX_TEXT = "24,000.00"


def view(provider, now=NOW):
    app = App(provider, clock=lambda: now)
    health, snapshot, _ = app.view()
    return health, snapshot, app.account


def page_for(provider):
    health, snapshot, account = view(provider)
    return health, render_page(health, snapshot, account, NOW), snapshot_json(health, snapshot, account, NOW)


class Cells(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.numeric, self.tags, self.text = [], [], [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags.append(tag)
        if a.get("class") == "num":
            self.numeric.append({"synthetic": a.get("data-synthetic"), "text": ""})
            self.stack.append(self.numeric[-1])
        else:
            self.stack.append(None)

    def handle_endtag(self, tag):
        if self.stack:
            self.stack.pop()

    def handle_data(self, data):
        self.text.append(data)
        for cell in self.stack:
            if cell is not None:
                cell["text"] += data


def parse_page(html):
    cells = Cells()
    cells.feed(html)
    return cells


class TestSyntheticIsIdentified(unittest.TestCase):
    def test_banner_is_in_every_state(self):
        for scenario in ("simulated", "stale", "unavailable"):
            with self.subTest(scenario=scenario):
                self.assertEqual(BANNER, "SIMULATED DATA — NOT LIVE MARKET DATA")
                _, html, doc = page_for(SyntheticProvider(scenario))
                self.assertIn(BANNER, "".join(parse_page(html).text))
                self.assertEqual(doc["banner"], BANNER)

    def test_every_shown_value_is_tagged_in_the_page(self):
        health, html, _ = page_for(SyntheticProvider())
        self.assertIs(health.state, DataState.SIMULATED)
        cells = parse_page(html).numeric
        self.assertEqual(len(cells), 2 + 10 * 3)             # index, change, then 5 strikes x CE/PE x (price, bid, ask)
        self.assertTrue(all(c["synthetic"] == "true" and "SYN" in c["text"] for c in cells))

    def test_every_shown_value_is_flagged_in_the_json(self):
        _, _, doc = page_for(SyntheticProvider())
        self.assertIs(doc["synthetic"], True)
        self.assertEqual(doc["source"], SOURCE)
        self.assertIs(doc["market"]["synthetic"], True)
        self.assertTrue(doc["chain"] and all(row["synthetic"] is True for row in doc["chain"]))

    def test_labels_never_claim_to_be_real(self):
        _, html, _ = page_for(SyntheticProvider())
        text = "".join(parse_page(html).text)
        self.assertIn("Not NSE quotes", text)
        self.assertIn("simulated", text.lower())
        symbols = re.findall(r"SYN-(?:CE|PE)-\d+", text)
        self.assertEqual(len(symbols), 10)
        self.assertTrue(symbols)
        self.assertNotRegex(text, r"NIFTY\d{2}[A-Z]{3}")      # no real-looking exchange symbols

    def test_page_shows_no_predictions_or_signals(self):
        _, html, _ = page_for(SyntheticProvider())
        text = "".join(parse_page(html).text).lower()
        for word in ("buy signal", "sell signal", "predict", "forecast", "probability"):
            self.assertNotIn(word, text.replace("no predictions or trading signals", ""))


class TestFailClosedStates(unittest.TestCase):
    def assertNoValues(self, provider_or_snapshot, state):
        provider = provider_or_snapshot if callable(provider_or_snapshot) else (lambda _now: provider_or_snapshot)
        health, html, doc = page_for(provider)
        self.assertIs(health.state, state)
        self.assertEqual(parse_page(html).numeric, [])
        self.assertNotIn(INDEX_TEXT, html)
        self.assertNotIn("24000", html)
        self.assertIs(doc["synthetic"], False)
        self.assertIsNone(doc["market"])
        self.assertEqual(doc["chain"], [])
        self.assertIn(state.value, html)

    def test_stale_data_withholds_values(self):
        self.assertNoValues(SyntheticProvider("stale"), DataState.STALE)

    def test_unavailable_data_withholds_values(self):
        self.assertNoValues(SyntheticProvider("unavailable"), DataState.UNAVAILABLE)

    def test_failing_provider_is_unavailable_and_never_falls_back_to_synthetic(self):
        def broken(now):
            raise RuntimeError("feed down")
        self.assertNoValues(broken, DataState.UNAVAILABLE)

    def test_bad_snapshots_are_unavailable(self):
        good = SyntheticProvider()(NOW)
        cases = {
            "not a snapshot": "24000", "naive timestamp": Snapshot(**{**good.__dict__, "as_of": NOW.replace(tzinfo=None)}),
            "string timestamp": Snapshot(**{**good.__dict__, "as_of": "2026-01-05"}),
            "future timestamp": Snapshot(**{**good.__dict__, "as_of": NOW + timedelta(minutes=5)}),
            "non-synthetic": Snapshot(**{**good.__dict__, "synthetic": False}),
            "synthetic flag not True": Snapshot(**{**good.__dict__, "synthetic": 1}),
        }
        for name, snapshot in cases.items():
            with self.subTest(case=name):
                self.assertNoValues(snapshot, DataState.UNAVAILABLE)

    def test_age_boundary(self):
        good = SyntheticProvider()(NOW)
        edge = Snapshot(**{**good.__dict__, "as_of": NOW - MAX_AGE})
        over = Snapshot(**{**good.__dict__, "as_of": NOW - MAX_AGE - timedelta(microseconds=1)})
        self.assertIs(assess(edge, NOW, MAX_AGE).state, DataState.SIMULATED)
        self.assertIs(assess(over, NOW, MAX_AGE).state, DataState.STALE)

    def test_missing_clock_is_unavailable(self):
        good = SyntheticProvider()(NOW)
        self.assertIs(assess(good, NOW.replace(tzinfo=None), MAX_AGE).state, DataState.UNAVAILABLE)
        self.assertIs(assess(good, None, MAX_AGE).state, DataState.UNAVAILABLE)


class TestPaperSummary(unittest.TestCase):
    def test_defaults_claim_no_trades(self):
        account = PaperAccount()
        self.assertEqual((account.positions, account.pnl, account.orders_executed), ((), Decimal("0"), 0))
        _, html, doc = page_for(SyntheticProvider())
        text = "".join(parse_page(html).text)
        self.assertIn("configurable example", text)
        self.assertIn("none", text)
        self.assertIn("No trade or fill has occurred", text)
        self.assertEqual((doc["paper_account"]["orders_executed"], doc["paper_account"]["pnl"], doc["paper_account"]["positions"]), (0, "0", 0))
        self.assertIs(doc["paper_account"]["starting_balance_is_example"], True)


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = create_server(App(SyntheticProvider(), clock=lambda: NOW), port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, method, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_binds_to_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        for host in ("0.0.0.0", "", "::", "192.168.1.10", "example.com", "127.0.0.2"):
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    create_server(App(SyntheticProvider()), host=host, port=0)
        other = create_server(App(SyntheticProvider()), host="localhost", port=0)
        try:
            self.assertEqual(other.server_address[0], "127.0.0.1")
        finally:
            other.server_close()

    def test_no_command_line_option_changes_the_host(self):
        flags = {opt for action in cli.build_parser()._actions for opt in action.option_strings}
        self.assertEqual(flags, {"-h", "--help", "--port", "--mode", "--realtime-provider", "--scenario", "--option-expiry", "--option-strike", "--option-type", "--starting-balance", "--permissions-file", "--state-dir", "--ml-dir"})

    def test_serves_the_page(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(headers["X-Data-Synthetic"], "true")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn(BANNER, body.decode("utf-8"))
        self.assertIn(INDEX_TEXT, body.decode("utf-8"))

    def test_serves_json_and_head(self):
        status, headers, body = self.request("GET", "/api/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data_state"], "SIMULATED")
        status, headers, body = self.request("HEAD", "/")
        self.assertEqual((status, body), (200, b""))

    def test_page_cannot_submit_anything(self):
        _, _, body = self.request("GET", "/")
        tags = set(parse_page(body.decode("utf-8")).tags)
        self.assertEqual(tags & {"form", "input", "button", "script", "textarea", "select", "iframe", "a", "link"}, set())

    def test_write_methods_and_order_paths_are_refused(self):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            for path in ("/", "/order", "/orders", "/api/order", "/api/orders", "/api/snapshot"):
                with self.subTest(method=method, path=path):
                    self.assertEqual(self.request(method, path)[0], 405)
        for path in ("/order", "/orders", "/api/order", "/api/orders", "/api/v2/orders", "/admin", "/../README.md"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)

    def test_foreign_host_header_is_refused(self):
        status, _, body = self.request("GET", "/", headers={"Host": "attacker.example"})
        self.assertEqual(status, 403)
        self.assertNotIn(INDEX_TEXT, body.decode("utf-8"))

    def test_stale_provider_over_http_shows_no_price(self):
        server = create_server(App(SyntheticProvider("stale"), clock=lambda: NOW), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            conn.request("GET", "/")
            body = conn.getresponse().read().decode("utf-8")
            conn.close()
            self.assertIn("STALE", body)
            self.assertNotIn(INDEX_TEXT, body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_cli_rejects_a_bad_balance(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["--starting-balance", "abc"]), 2)
            self.assertEqual(cli.main(["--starting-balance", "-5"]), 2)


class TestSeparationAndSafety(unittest.TestCase):
    def dashboard_files(self):
        return sorted((SRC / "niftypaper" / "dashboard").glob("*.py"))

    def imports(self, path):
        found = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                found.add(("." * node.level) + (node.module or ""))
        return found

    def test_dashboard_does_not_use_the_quote_model_or_eligibility_boundary(self):
        for path in self.dashboard_files():
            with self.subTest(file=path.name):
                for name in self.imports(path):
                    self.assertNotRegex(name, r"(^|\.)(quotes|eligibility|contract_specs)$|^niftypaper$")

    def test_only_the_cli_entry_point_imports_the_synthetic_provider(self):
        users = {p.name for p in self.dashboard_files() if any(n.endswith("sample_data") for n in self.imports(p))}
        self.assertEqual(users, {"__main__.py"})

    def test_synthetic_rows_are_not_quotes_and_cannot_be_filled(self):
        snapshot = SyntheticProvider()(NOW)
        self.assertTrue(snapshot.chain)
        self.assertFalse(any(isinstance(row, Quote) for row in snapshot.chain))
        self.assertNotIn("LIVE_QUOTE", "".join(p.read_text(encoding="utf-8") for p in self.dashboard_files()))

    def test_no_credentials_or_order_code_in_the_dashboard_source(self):
        text = "\n".join(p.read_text(encoding="utf-8") for p in self.dashboard_files()).lower()
        for word in ("password", "api_key", "apikey", "secret", "token", "authorization", "os.environ", "getenv", "requests", "urllib", "socket"):
            self.assertNotIn(word, text)
        self.assertNotRegex(text, r"(place|modify|cancel|submit|send)_?\w*order")
        self.assertNotRegex(text, r"https?://(?!\{default_host\}|\{h\})")      # the only URL is the printed loopback address


class TestV601BoundaryStillWorks(unittest.TestCase):
    """Smoke test only; the full boundary tests are in test_eligibility.py and are part of the same run."""
    POLICY = EligibilityPolicy(max_age=timedelta(seconds=5), max_future_skew=timedelta(seconds=1), tick_size=Decimal("0.05"))

    def quote(self, quote_type):
        return Quote(symbol="TESTSYM", quote_type=quote_type, bid=Decimal("100.00"), ask=Decimal("100.10"), bid_qty=65, ask_qty=65,
                     ts=NOW - timedelta(seconds=1), source="synthetic-test-feed")

    def test_live_quote_passes_and_settlement_quote_is_refused(self):
        executable = require_fillable(self.quote(QuoteType.LIVE_QUOTE), now=NOW, policy=self.POLICY)
        self.assertEqual(fill_price(executable, Side.BUY), Decimal("100.10"))
        result = check_fill_eligibility(self.quote(QuoteType.EOD_SETTLEMENT), now=NOW, policy=self.POLICY)
        self.assertIs(result.eligible, False)
        self.assertIn(Reason.QUOTE_TYPE_NOT_EXECUTABLE, result.reasons)


if __name__ == "__main__":
    unittest.main()
