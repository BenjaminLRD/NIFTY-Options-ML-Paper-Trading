"""Tests for the dual-mode dashboard: mode switching, provider states, gated virtual trading and the two POST endpoints.
Provider data is mocked everywhere; no test contacts a provider. A guard fails any non-loopback connection attempt."""
import contextlib
import http.client
import json
import threading
import unittest
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock
from urllib.parse import urlencode

from niftypaper import assembly
from niftypaper.assembly import default_factories
from niftypaper.dashboard.render import PROVIDER_BANNERS
from niftypaper.dashboard.sample_data import SyntheticProvider
from niftypaper.dashboard.server import POST_PATHS, App, create_server, parse_form
from niftypaper.dashboard.status import (LATENCY_DELAYED_CONFIRMED, LATENCY_REALTIME_DOCUMENTED, TIME_QUOTE, DataState, OptionRow, Snapshot,
                                         connection_status)
from niftypaper.paper.engine import PaperEngine
from niftypaper.providers.fyers_http import QuotesClient, TransportError
from niftypaper.providers.fyers_provider import FyersProvider, load_credentials
from niftypaper.providers.fyers_symbols import SymbolError, resolve_nifty_option
from niftypaper.providers.permissions import load_permissions
from niftypaper.providers.registry import PROVIDERS, credential_flags, real_provider_ids
from tests.test_fyers import (APP_ID, ENV, EXPIRY, INDEX_ENTRY, INDEX_SYMBOL, OPTION_ENTRY, OPTION_SYMBOL, SECRET, T0, Clock, Transport, entry, master_row,
                              master_text, ok)

PERMITTED = {"fyers": (True, "test record"), "live-test": (True, "test record")}
SYN_SYMBOL = "SYN-CE-24000"


class SpyEnv(dict):
    """An environment that records every read, to prove nothing reads credentials in Synthetic Mode."""
    reads = None

    def get(self, key, default=None):
        self.reads = (self.reads or []) + [key]
        return super().get(key, default)

    def __getitem__(self, key):
        self.reads = (self.reads or []) + [key]
        return super().__getitem__(key)


class TestProvider:
    """A provider that does carry the evidence LIVE needs (documented real-time, documented quote time, sizes), for testing the gate."""
    last_error = None

    def __init__(self, clock, latency=LATENCY_REALTIME_DOCUMENTED, semantics=TIME_QUOTE, sizes=True):
        self.clock, self.latency, self.semantics, self.sizes = clock, latency, semantics, sizes

    def __call__(self, now=None):
        t = self.clock()
        row = OptionRow("TEST-OPT-1", "CE", 24000, Decimal("101.00"), Decimal("100.95"), Decimal("101.05"),
                        650 if self.sizes else None, 650 if self.sizes else None, 65, Decimal("0.05"))
        return Snapshot(source="TEST_PROVIDER", synthetic=False, as_of=t, index_value=Decimal("24000"), pct_change=None, chain=(row,), received_at=t,
                        provider_time_raw="raw", provider_time=t - timedelta(seconds=1), time_semantics=self.semantics, latency_class=self.latency)


def fyers_provider(clock, *replies, option=True, **over):
    resolved = resolve_nifty_option(master_text(master_row()), expiry=EXPIRY, strike=24000, option_type="CE", today=date(2026, 11, 2)) if option else None
    transport = Transport(*replies)
    return FyersProvider(QuotesClient(load_credentials(ENV), transport), option=resolved, clock=clock, **over), transport


def make_app(clock, real=None, mode="synthetic", permissions=None, env=None, balance=1_000_000):
    factories = {"synthetic": lambda: SyntheticProvider(), "fyers": real or (lambda: fyers_provider(clock, ok(INDEX_ENTRY, OPTION_ENTRY))[0])}
    start = factories["synthetic" if mode == "synthetic" else "fyers"]()
    return App(start, clock=clock, mode=mode, provider_id="synthetic" if mode == "synthetic" else "fyers", factories=factories,
               engine_factory=lambda m: PaperEngine(Decimal(balance), clock=clock), permissions=PERMITTED if permissions is None else permissions, env=env)


class NoOutsideNetwork(unittest.TestCase):
    def setUp(self):
        real_connect = __import__("socket").socket.connect

        def only_loopback(sock, address, *a, **k):
            if address[0] not in ("127.0.0.1", "localhost"):
                raise AssertionError(f"outside network use in a test: {address[0]}")
            return real_connect(sock, address, *a, **k)

        guard = mock.patch("socket.socket.connect", only_loopback)
        guard.start()
        self.addCleanup(guard.stop)


class TestModeSwitching(NoOutsideNetwork):
    def test_switching_changes_the_provider_and_only_on_request(self):
        clock = Clock()
        app = make_app(clock)
        self.assertEqual((app.mode, app.provider_id), ("synthetic", "synthetic"))
        self.assertIn("Real-Time Mode with fyers", app.switch("realtime", "fyers"))
        self.assertEqual((app.mode, app.provider_id), ("provider", "fyers"))
        self.assertIsInstance(app.provider, FyersProvider)
        self.assertIn("Synthetic Mode", app.switch("synthetic"))
        self.assertEqual((app.mode, app.provider_id), ("synthetic", "synthetic"))
        self.assertIsInstance(app.provider, SyntheticProvider)

    def test_unsupported_choices_leave_the_mode_unchanged(self):
        app = make_app(Clock())
        for ui_mode, provider in (("realtime", "nope"), ("realtime", None), ("realtime", "synthetic"), ("turbo", "fyers"), (None, None), ("", "")):
            with self.subTest(mode=ui_mode, provider=provider):
                self.assertIn("not supported", app.switch(ui_mode, provider))
                self.assertEqual((app.mode, app.provider_id), ("synthetic", "synthetic"))
        self.assertIn("not configured", App(SyntheticProvider()).switch("realtime", "fyers"))

    def test_a_provider_that_cannot_start_is_unavailable_never_synthetic(self):
        def broken():
            raise RuntimeError("boom " + SECRET)
        app = make_app(Clock(), real=broken)
        app.switch("realtime", "fyers")
        health, snapshot, _ = app.view()
        self.assertEqual((app.mode, health.state, snapshot), ("provider", DataState.UNAVAILABLE, None))
        self.assertIn("could not be started", health.reason)
        self.assertNotIn(SECRET, health.reason)

    def test_synthetic_mode_makes_no_network_request_and_reads_no_credentials(self):
        clock, env, transport = Clock(), SpyEnv(ENV), Transport(ok(INDEX_ENTRY))
        factories = default_factories(env=env, transport=transport, clock=clock)
        app = App(factories["synthetic"](), clock=clock, mode="synthetic", provider_id="synthetic", factories=factories,
                  engine_factory=lambda m: PaperEngine(Decimal("1000000"), clock=clock), env=env)
        for _ in range(5):
            app.view()
        app.handle_trade({"symbol": SYN_SYMBOL, "side": "BUY", "lots": "1"})
        app.switch("synthetic")
        self.assertEqual(transport.calls, [])
        self.assertIsNone(env.reads)
        with mock.patch("socket.socket.connect", side_effect=AssertionError("network used in Synthetic Mode")):
            app.view()
            app.switch("synthetic")

    def test_the_real_provider_is_only_built_when_chosen(self):
        clock, env = Clock(), SpyEnv(ENV)
        transport = Transport(ok(INDEX_ENTRY))
        factories = default_factories(env=env, transport=transport, clock=clock)
        app = App(factories["synthetic"](), clock=clock, factories=factories, env=env)
        self.assertIsNone(env.reads)
        app.switch("realtime", "fyers")
        app.view()
        self.assertTrue(env.reads)
        self.assertEqual(len(transport.calls), 1)

    def test_each_mode_has_its_own_virtual_account(self):
        app = make_app(Clock())
        app.handle_trade({"symbol": SYN_SYMBOL, "side": "BUY", "lots": "1"})
        self.assertEqual(len(app.engine.ledger), 1)
        app.switch("realtime", "fyers")
        self.assertEqual(app.engine.ledger, [])
        app.switch("synthetic")
        self.assertEqual(len(app.engine.ledger), 1)


class TestRealTimeStates(NoOutsideNetwork):
    def app_with(self, *replies, mode="provider", option=True, **over):
        clock = Clock()
        provider, transport = fyers_provider(clock, *replies, option=option, **over)
        factories = {"synthetic": lambda: SyntheticProvider(), "fyers": lambda: provider}
        app = App(provider, clock=clock, mode="provider", provider_id="fyers", factories=factories,
                  engine_factory=lambda m: PaperEngine(Decimal("1000000"), clock=clock), permissions=PERMITTED, env=ENV)
        return app, clock, transport

    def assertUnavailableWithoutValues(self, app):
        health, snapshot, now = app.view()
        self.assertEqual(health.state, DataState.UNAVAILABLE)
        self.assertIsNone(snapshot)
        self.assertEqual(connection_status(health.state, True), "UNAVAILABLE")
        self.assertEqual((app.mode, app.provider_id), ("provider", "fyers"))               # no silent switch to synthetic
        return health

    def test_missing_credentials(self):
        clock = Clock()
        factories = default_factories(env={}, transport=Transport(ok(INDEX_ENTRY)), clock=clock)
        app = App(factories["fyers"](), clock=clock, mode="provider", provider_id="fyers", factories=factories, engine_factory=lambda m: PaperEngine(Decimal("1"), clock=clock))
        health = self.assertUnavailableWithoutValues(app)
        self.assertIn("not configured", health.reason)

    def test_failed_connections_and_provider_errors_are_unavailable_with_distinct_reasons(self):
        reasons = {}
        for name, reply in {"timeout": TransportError("TIMEOUT"), "network": TransportError("NETWORK_ERROR"), "auth": (401, b"{}"), "denied": (403, b"{}"),
                            "limit": (429, b"{}"), "html": (200, b"<html>"), "no index": ok(OPTION_ENTRY), "boom": RuntimeError(SECRET)}.items():
            app, _, _ = self.app_with(reply)
            reasons[name] = self.assertUnavailableWithoutValues(app).reason
            self.assertNotIn(SECRET, reasons[name])
        self.assertEqual(len(set(reasons.values())), len(reasons))                    # every kind of failure says what went wrong
        self.assertIn("denied", reasons["denied"])
        self.assertIn("rejected the credentials", reasons["auth"])

    def test_unavailable_real_data_blocks_trading_and_never_becomes_synthetic(self):
        app, _, _ = self.app_with(TransportError("TIMEOUT"))
        app.handle_trade({"symbol": SYN_SYMBOL, "side": "BUY", "lots": "1"})
        app.handle_trade({"symbol": OPTION_SYMBOL, "side": "BUY", "lots": "1"})
        self.assertEqual(app.engine.ledger, [])
        self.assertEqual(app.mode, "provider")

    def test_unverified_data_is_shown_but_cannot_be_traded_even_with_permission(self):
        app, clock, _ = self.app_with(ok(INDEX_ENTRY, OPTION_ENTRY))
        health, snapshot, _ = app.view()
        self.assertEqual((health.state, connection_status(health.state, True)), (DataState.UNVERIFIED, "UNVERIFIED"))
        self.assertTrue(snapshot.chain)
        app.handle_trade({"symbol": OPTION_SYMBOL, "side": "BUY", "lots": "1"})
        self.assertEqual(app.engine.ledger, [])
        self.assertIn("not verified", app.notice)
        self.assertIsNotNone(app.engine)

    def test_stale_data_withholds_values_and_blocks_trading(self):
        app, clock, _ = self.app_with(ok(INDEX_ENTRY, OPTION_ENTRY), min_interval=timedelta(seconds=300))
        self.assertEqual(app.view()[0].state, DataState.UNVERIFIED)
        clock.now += timedelta(seconds=40)
        health, snapshot, _ = app.view()
        self.assertEqual((health.state, connection_status(health.state, True)), (DataState.STALE, "STALE"))
        app.handle_trade({"symbol": OPTION_SYMBOL, "side": "BUY", "lots": "1"})
        self.assertEqual(app.engine.ledger, [])

    def test_invalid_option_data_is_dropped_and_unknown_instruments_are_refused(self):
        bad_option = entry(OPTION_SYMBOL, lp=1.0, bid=1.10, ask=1.05)
        app, _, _ = self.app_with(ok(INDEX_ENTRY, bad_option))
        health, snapshot, _ = app.view()
        self.assertEqual((health.state, snapshot.chain), (DataState.UNVERIFIED, ()))
        app.handle_trade({"symbol": OPTION_SYMBOL, "side": "BUY", "lots": "1"})
        self.assertIn("not in the current market data", app.notice)
        app.handle_trade({"symbol": "NSE:NOT-THERE", "side": "BUY", "lots": "1"})
        self.assertIn("not in the current market data", app.notice)

    def test_an_unresolvable_instrument_makes_real_time_unavailable_and_is_not_guessed(self):
        clock = Clock()
        with mock.patch.object(assembly, "resolve_option_online", mock.Mock(side_effect=SymbolError("AMBIGUOUS"))):
            factories = default_factories(option_request=(EXPIRY, 24000, "CE"), env=ENV, transport=Transport(ok(INDEX_ENTRY)), clock=clock)
            app = App(SyntheticProvider(), clock=clock, factories=factories)
            app.switch("realtime", "fyers")
        health = self.assertUnavailableWithoutValues(app)
        self.assertIn("AMBIGUOUS", health.reason)


class TestEvidenceGatedTrading(NoOutsideNetwork):
    def live_app(self, **over):
        clock = Clock()
        provider = TestProvider(clock, **over)
        factories = {"synthetic": lambda: SyntheticProvider(), "live-test": lambda: provider}
        app = App(provider, clock=clock, mode="provider", provider_id="live-test", factories=factories,
                  engine_factory=lambda m: PaperEngine(Decimal("1000000"), clock=clock), permissions=PERMITTED)
        return app

    def test_verified_data_with_recorded_permission_allows_a_virtual_trade_with_a_full_record(self):
        app = self.live_app()
        health, _, _ = app.view()
        self.assertEqual((health.state, connection_status(health.state, True)), (DataState.LIVE, "CONNECTED"))
        app.handle_trade({"symbol": "TEST-OPT-1", "side": "BUY", "lots": "1"})
        record, = app.engine.ledger
        d = record.to_dict()
        self.assertEqual((d["provider"], d["symbol"], d["data_state"], d["connection"], d["data_origin"]), ("TEST_PROVIDER", "TEST-OPT-1", "LIVE", "CONNECTED", "PROVIDER"))
        self.assertEqual((d["quote_bid"], d["quote_ask"], d["fill_price"]), ("100.95", "101.05", "101.10"))
        self.assertTrue(d["received_at"] and d["provider_time_raw"] == "raw" and d["engine_time"] and d["assumptions"])
        self.assertIn("simulated fill", app.notice.lower())

    def test_recorded_permission_is_required(self):
        app = self.live_app()
        app.permissions = {}
        app.handle_trade({"symbol": "TEST-OPT-1", "side": "BUY", "lots": "1"})
        self.assertEqual(app.engine.ledger, [])
        self.assertIn("terms of use have not been confirmed", app.notice)
        app.permissions = {"live-test": (False, "not recorded as permitted")}
        app.handle_trade({"symbol": "TEST-OPT-1", "side": "BUY", "lots": "1"})
        self.assertEqual(app.engine.ledger, [])

    def test_delayed_or_unknown_timing_or_missing_sizes_block_trading(self):
        cases = {"delayed": dict(latency=LATENCY_DELAYED_CONFIRMED), "unknown latency": dict(latency="UNKNOWN"),
                 "last trade time only": dict(semantics="LAST_TRADE_TIME"), "no sizes": dict(sizes=False)}
        for name, over in cases.items():
            with self.subTest(case=name):
                app = self.live_app(**over)
                health, _, _ = app.view()
                self.assertNotEqual(health.state, DataState.LIVE if name != "no sizes" else None)
                app.handle_trade({"symbol": "TEST-OPT-1", "side": "BUY", "lots": "1"})
                self.assertEqual(app.engine.ledger, [], name)

    def test_the_synthetic_demo_can_be_traded_and_is_labelled(self):
        app = make_app(Clock())
        app.handle_trade({"symbol": SYN_SYMBOL, "side": "BUY", "lots": "1"})
        d = app.engine.ledger[0].to_dict()
        self.assertEqual((d["data_origin"], d["provider"], d["data_state"]), ("SYNTHETIC", "SYNTHETIC_SAMPLE_PROVIDER", "SIMULATED"))
        app.handle_trade({"symbol": SYN_SYMBOL, "side": "SELL", "lots": "1"})
        self.assertEqual(len(app.engine.ledger), 2)
        app.handle_trade({"symbol": SYN_SYMBOL, "side": "BUY", "lots": "99"})
        self.assertEqual(len(app.engine.ledger), 2)
        self.assertIn("per-trade limit", app.notice)
        app.handle_trade({"symbol": SYN_SYMBOL, "side": "BUY", "lots": "abc"})
        self.assertIn("not valid", app.notice)


class TestDailyCapInTheDashboard(NoOutsideNetwork):
    def test_a_second_large_synthetic_buy_is_refused_with_the_reason_shown(self):
        app = make_app(Clock())
        app.handle_trade({"symbol": "SYN-CE-23800", "side": "BUY", "lots": "1"})                  # 18,238.85 of the 20,000 cap
        self.assertEqual(len(app.engine.ledger), 1)
        app.handle_trade({"symbol": "SYN-CE-23800", "side": "BUY", "lots": "1"})
        self.assertEqual(len(app.engine.ledger), 1)
        self.assertIn("daily loss limit", app.notice)
        self.assertIn("worst case", app.notice)
        cash = app.engine.cash
        app.handle_trade({"symbol": "SYN-CE-23800", "side": "SELL", "lots": "1"})                   # exiting is never blocked
        self.assertEqual(len(app.engine.ledger), 2)
        self.assertNotEqual(app.engine.cash, cash)


class Served:
    def __init__(self, app):
        self.app = app

    def __enter__(self):
        self.server = create_server(self.app, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            r = conn.getresponse()
            return r.status, dict(r.getheaders()), r.read().decode("utf-8")
        finally:
            conn.close()

    def post(self, path, fields, *, csrf=True, headers=None, raw=None):
        data = dict(fields)
        if csrf is True:
            data["csrf"] = self.app.csrf
        elif csrf:
            data["csrf"] = csrf
        base = {"Content-Type": "application/x-www-form-urlencoded", "Origin": f"http://127.0.0.1:{self.port}"}
        base.update(headers or {})
        return self.request("POST", path, raw if raw is not None else urlencode(data), base)


class TestHttpInterface(unittest.TestCase):
    def test_the_page_offers_both_modes_and_explains_requirements_without_secrets(self):
        app = make_app(Clock(), env=dict(ENV))
        with Served(app) as s:
            status, headers, page = s.request("GET", "/")
        self.assertEqual(status, 200)
        for text in ("Synthetic Mode (Free)", "Real-Time Mode (requires provider access)", 'action="/mode"', 'action="/paper/trade"', app.csrf,
                     "FYERS_APP_ID", "FYERS_ACCESS_TOKEN", "value never shown", "Subscription:", "Permissions:", "Terms:", "Setup steps",
                     "SIMULATED DATA", "Connection status", "SIMULATED"):
            self.assertIn(text, page)
        self.assertNotIn(SECRET, page)
        self.assertNotIn(APP_ID, page)
        self.assertEqual(page.count('aria-pressed="true"'), 1)

    def test_switching_by_post_shows_a_one_time_notice_and_provider_status(self):
        app = make_app(Clock(), env=dict(ENV))
        with Served(app) as s:
            status, headers, _ = s.post("/mode", {"mode": "realtime", "provider": "fyers"})
            self.assertEqual((status, headers["Location"]), (303, "/"))
            _, headers, page = s.request("GET", "/")
            self.assertIn("Switched to Real-Time Mode with fyers", page)
            self.assertIn(PROVIDER_BANNERS[DataState.UNVERIFIED], page)
            self.assertIn("UNVERIFIED", page)
            self.assertIn("FYERS (read-only quotes)", page)
            self.assertIn("set (value never shown)", page)
            self.assertEqual(headers["X-Data-Synthetic"], "false")
            self.assertIn("blocked", page)
            self.assertNotIn("Switched to Real-Time Mode", s.request("GET", "/")[2])           # shown once
            s.post("/mode", {"mode": "synthetic"})
            self.assertIn("SIMULATED DATA", s.request("GET", "/")[2])
        self.assertEqual(app.mode, "synthetic")

    def test_a_failing_real_provider_page_never_shows_synthetic_values(self):
        clock = Clock()
        app = make_app(clock, real=lambda: fyers_provider(clock, TransportError("TIMEOUT"))[0])
        with Served(app) as s:
            s.post("/mode", {"mode": "realtime", "provider": "fyers"})
            _, _, page = s.request("GET", "/")
            _, _, doc = s.request("GET", "/api/snapshot")
        self.assertIn(PROVIDER_BANNERS[DataState.UNAVAILABLE], page)
        self.assertNotIn("SYN-", page)
        self.assertNotIn("SIMULATED DATA", page)
        data = json.loads(doc)
        self.assertEqual((data["data_state"], data["synthetic"], data["chain"], data["market"]), ("UNAVAILABLE", False, [], None))

    def test_post_requests_without_the_form_key_or_from_elsewhere_are_refused(self):
        app = make_app(Clock())
        with Served(app) as s:
            good = {"mode": "realtime", "provider": "fyers"}
            self.assertEqual(s.post("/mode", good, csrf=False)[0], 403)
            self.assertEqual(s.post("/mode", good, csrf="wrong")[0], 403)
            self.assertEqual(s.post("/mode", good, headers={"Origin": "http://evil.example"})[0], 403)
            self.assertEqual(s.post("/mode", good, headers={"Host": "evil.example"})[0], 403)
            self.assertEqual(s.post("/mode", good, headers={"Content-Type": "application/json"})[0], 400)
            self.assertEqual(s.post("/mode", good, raw="x" * 3000)[0], 400)
            self.assertEqual(s.post("/mode", good, raw="mode=realtime&mode=synthetic")[0], 400)
            self.assertEqual(s.post("/mode", good, raw="möde=1".encode("utf-8").decode("latin-1"))[0], 400)
            self.assertEqual(s.post("/mode", good, raw="")[0], 400)
            self.assertEqual(app.mode, "synthetic")                       # none of them had any effect
            self.assertEqual(app.engine.ledger, [])
            self.assertEqual(s.post("/paper/trade", {"symbol": SYN_SYMBOL, "side": "BUY", "lots": "1"}, csrf=False)[0], 403)
            self.assertEqual(app.engine.ledger, [])

    def test_virtual_trade_by_post_is_recorded_and_listed(self):
        app = make_app(Clock())
        with Served(app) as s:
            self.assertEqual(s.post("/paper/trade", {"symbol": SYN_SYMBOL, "side": "BUY", "lots": "1"})[0], 303)
            _, _, page = s.request("GET", "/")
            _, headers, ledger = s.request("GET", "/api/ledger")
        self.assertIn("Virtual trade recorded", page)
        self.assertIn("Simulated trade ledger", page)
        self.assertIn("no real order exists", page)
        data = json.loads(ledger)
        self.assertEqual((data["simulated"], data["mode"], len(data["trades"]), data["trades"][0]["symbol"]), (True, "synthetic", 1, SYN_SYMBOL))
        self.assertIn("quote_vs_fill", data["trades"][0])
        self.assertEqual(headers["X-Data-Synthetic"], "true")

    def test_trading_form_is_hidden_and_the_reason_shown_when_prices_are_not_verified(self):
        clock = Clock()
        app = make_app(clock, mode="provider", real=lambda: fyers_provider(clock, ok(INDEX_ENTRY, OPTION_ENTRY))[0])
        with Served(app) as s:
            _, _, page = s.request("GET", "/")
        self.assertIn("Virtual trading is blocked", page)
        self.assertIn("not verified current prices", page)
        self.assertNotIn('action="/paper/trade"', page)

    def test_only_the_two_documented_posts_exist_and_nothing_else_is_writable(self):
        self.assertEqual(POST_PATHS, ("/mode", "/paper/trade"))
        app = make_app(Clock())
        with Served(app) as s:
            for path in ("/", "/api/snapshot", "/api/ledger", "/order", "/orders", "/api/order", "/api/orders", "/positions"):
                self.assertEqual(s.post(path, {"x": "1"})[0], 405, path)
            for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
                self.assertEqual(s.request(method, "/mode")[0], 405, method)
            for path in ("/order", "/orders", "/api/orders", "/paper/order"):
                self.assertEqual(s.request("GET", path)[0], 404, path)

    def test_a_dashboard_without_modes_or_trading_accepts_no_posts_at_all(self):
        with Served(App(SyntheticProvider(), clock=Clock())) as s:
            for path in POST_PATHS:
                self.assertEqual(s.post(path, {"mode": "realtime"}, csrf=False)[0], 405)
            self.assertNotIn("<form", s.request("GET", "/")[2])

    def test_refused_posts_still_get_their_answer_when_a_body_was_sent(self):
        """The server reads the body before refusing; answering with unread data can reset the connection on Windows (seen once during development)."""
        for app in (App(SyntheticProvider(), clock=Clock()), make_app(Clock())):
            with Served(app) as s:
                for _ in range(25):
                    self.assertEqual(s.post("/order", {"x": "y" * 1500}, csrf=False)[0], 405)
                    self.assertEqual(s.post("/mode", {"mode": "realtime"}, csrf=False)[0], 403 if app.interactive else 405)

    def test_the_form_parser_is_strict(self):
        self.assertEqual(parse_form(b"a=1&b=x+y%3Az"), {"a": "1", "b": "x y:z"})
        for bad in (b"", b"a", b"a=1&a=2", b"a=%ff", b"a=<script>", b"1a=2", b"a=" + b"x" * 201, b"a=1&&b=2"):
            with self.subTest(raw=bad):
                with self.assertRaises(ValueError):
                    parse_form(bad)


class TestPermissionsAndRegistry(unittest.TestCase):
    def check(self, content, today=date(2026, 11, 2)):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            if content is not None:
                path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
            return load_permissions(path, today)

    def entry(self, **over):
        e = {"virtual_trading_permitted": True, "evidence": "FYERS support e-mail of 2026-11-01", "confirmed_on": "2026-11-01"}
        e.update(over)
        return {"schema_version": 1, "providers": {"fyers": e}}

    def test_only_a_complete_recorded_permission_counts(self):
        self.assertTrue(self.check(self.entry())[0]["fyers"][0])
        self.assertEqual(self.check(None)[0], {})
        for name, content in {"not json": "{oops", "wrong schema": {"schema_version": 2, "providers": {}}, "no providers": {"schema_version": 1}}.items():
            self.assertEqual(self.check(content)[0], {}, name)
        for name, over in {"false": dict(virtual_trading_permitted=False), "string true": dict(virtual_trading_permitted="true"),
                           "no evidence": dict(evidence=""), "short evidence": dict(evidence="ok"), "no date": dict(confirmed_on=None),
                           "bad date": dict(confirmed_on="1/11/2026"), "future date": dict(confirmed_on="2026-12-01")}.items():
            with self.subTest(case=name):
                self.assertFalse(self.check(self.entry(**over))[0]["fyers"][0])
        self.assertFalse(self.check({"schema_version": 1, "providers": {"fyers": "yes"}})[0]["fyers"][0])

    def test_registry_describes_requirements_and_reports_only_whether_variables_are_set(self):
        self.assertEqual(real_provider_ids(), ("fyers",))
        info = PROVIDERS["fyers"]
        self.assertTrue(info.needs_access and info.credential_env and info.terms_note and info.setup_steps)
        self.assertFalse(PROVIDERS["synthetic"].needs_access or PROVIDERS["synthetic"].credential_env)
        self.assertEqual(credential_flags(info, {"FYERS_APP_ID": SECRET}), [("FYERS_APP_ID", True), ("FYERS_ACCESS_TOKEN", False)])
        self.assertEqual(credential_flags(info, {}), [("FYERS_APP_ID", False), ("FYERS_ACCESS_TOKEN", False)])
        self.assertTrue(all(isinstance(flag, bool) for _, flag in credential_flags(info, ENV)))

    def test_connection_status_vocabulary(self):
        mapping = {DataState.LIVE: "CONNECTED", DataState.DELAYED: "CONNECTED", DataState.UNVERIFIED: "UNVERIFIED", DataState.STALE: "STALE",
                   DataState.UNAVAILABLE: "UNAVAILABLE", DataState.SIMULATED: "UNAVAILABLE"}
        for state, expected in mapping.items():
            self.assertEqual(connection_status(state, True), expected, state)
        self.assertEqual(connection_status(DataState.SIMULATED, False), "SIMULATED")


if __name__ == "__main__":
    unittest.main()
