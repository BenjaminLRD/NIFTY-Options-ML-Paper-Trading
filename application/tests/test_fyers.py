"""Tests for the V6-0.5 read-only FYERS adapter. Every response is a made-up fixture in the documented shape; no credentials are needed and no
test sends a request to FYERS (a guard makes any socket connection in the adapter tests fail). They show properties of this code as written
and do not establish that the adapter is secure, that FYERS behaves as the fixtures do, or that any price is live."""
import ast
import contextlib
import csv
import io
import json
import pickle
import threading
import unittest
import http.client
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

from niftypaper.dashboard import __main__ as dashboard_cli
from niftypaper.dashboard.render import PROVIDER_BANNERS, render_page, snapshot_json
from niftypaper.dashboard.sample_data import SyntheticProvider
from niftypaper.dashboard.server import App, create_server
from niftypaper.dashboard.status import (LATENCY_DELAYED_CONFIRMED, LATENCY_REALTIME_DOCUMENTED, TIME_LAST_TRADE, TIME_QUOTE, DataState,
                                         PaperAccount, Snapshot, assess)
from niftypaper.providers import fyers_http, fyers_smoke
from niftypaper.providers.fyers_http import ALLOWED, DATA_HOST, MASTER_HOST, QUOTES_PATH, QuotesClient, TransportError, check_url
from niftypaper.providers.fyers_provider import (ConfigError, Credentials, FyersProvider, build_provider, credentials_summary, load_credentials)
from niftypaper.providers.fyers_quotes import DESCRIPTIONS, INDEX_SYMBOL, QuoteError, QuoteSpec, code_for_http_status, parse_quotes
from niftypaper.providers.fyers_symbols import IST, SymbolError, resolve_nifty_option
from tests import SRC
from tests.test_dashboard import parse_page

APP_ID = "TESTAPP-100"
SECRET = "-".join(["fake", "value", "for", "tests", "only", "0123456789"])      # not a real credential
ENV = {"FYERS_APP_ID": APP_ID, "FYERS_ACCESS_TOKEN": SECRET}
T0 = datetime(2026, 11, 2, 10, 0, 0, tzinfo=IST)
EXPIRY = date(2026, 11, 3)
OPTION_SYMBOL = "NSE:NIFTY26N0324000CE"
TT = "1691020800"                       # the shape of the documented examples: a 10-digit string


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def entry(symbol, **values):
    v = {"lp": 100.5, "symbol": symbol, "tt": TT}
    v.update(values)
    return {"n": symbol, "s": "ok", "v": v}


def body(*entries):
    return {"s": "ok", "code": 200, "message": "", "d": list(entries)}


INDEX_ENTRY = entry(INDEX_SYMBOL, lp=24012.35, chp=0.12)
OPTION_ENTRY = entry(OPTION_SYMBOL, lp=151.2, bid=151.15, ask=151.25)


def master_row(underlying="NIFTY", expiry=EXPIRY, strike=24000, kind="CE", ticker=OPTION_SYMBOL, details=None):
    when = datetime(expiry.year, expiry.month, expiry.day, 15, 40, tzinfo=IST)
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    details = details or f"{underlying} {expiry.day:02d} {months[expiry.month - 1]} {expiry.year % 100:02d} {strike} {kind}"
    return ["1011261103" + str(strike), details, "14", "65", "0.05", "", "0915-1540|1815-1915:", "2026-10-23", str(int(when.timestamp())),
            ticker, "10", "11", "35040", underlying, "26000", f"{strike}.0", kind, "101000000026000", "None", "0", "0.0"]


def master_text(*rows):
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    return out.getvalue()


class Transport:
    """Records every request and replays queued replies (a (status, bytes) pair, or an exception to raise)."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, url, headers, timeout, max_bytes):
        self.calls.append((url, dict(headers)))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def ok(*entries):
    return 200, json.dumps(body(*entries)).encode()


class NoNetwork(unittest.TestCase):
    def setUp(self):
        guard = mock.patch("socket.socket.connect", side_effect=AssertionError("real network use in a test"))
        guard.start()
        self.addCleanup(guard.stop)


class TestParse(NoNetwork):
    SPECS = {INDEX_SYMBOL: QuoteSpec(False), OPTION_SYMBOL: QuoteSpec(True, Decimal("0.05"))}

    def parse(self, *entries):
        return parse_quotes(body(*entries), self.SPECS)

    def test_valid_quotes_are_accepted_and_the_time_field_is_kept_exactly(self):
        result = self.parse(INDEX_ENTRY, OPTION_ENTRY)
        index, option = result[INDEX_SYMBOL].quote, result[OPTION_SYMBOL].quote
        self.assertEqual((index.last_price, index.pct_change, index.bid), (Decimal("24012.35"), Decimal("0.12"), None))
        self.assertEqual((option.bid, option.ask, option.last_price), (Decimal("151.15"), Decimal("151.25"), Decimal("151.2")))
        self.assertEqual(index.provider_time_raw, TT)                      # still the string it arrived as
        self.assertEqual(parse_quotes(body(entry(INDEX_SYMBOL, tt=1691020800)), {INDEX_SYMBOL: QuoteSpec(False)})[INDEX_SYMBOL].quote.provider_time_raw, 1691020800)

    def test_per_symbol_rejections(self):
        def option(**v):
            return entry(OPTION_SYMBOL, lp=151.2, bid=151.15, ask=151.25, **v)
        bad_index = {"missing lp": ({k: v for k, v in INDEX_ENTRY["v"].items() if k != "lp"}, "MISSING_FIELD"),
                     "string lp": ({**INDEX_ENTRY["v"], "lp": "24012.35"}, "INVALID_PRICE"), "bool lp": ({**INDEX_ENTRY["v"], "lp": True}, "INVALID_PRICE"),
                     "null lp": ({**INDEX_ENTRY["v"], "lp": None}, "INVALID_PRICE"), "zero lp": ({**INDEX_ENTRY["v"], "lp": 0}, "INVALID_PRICE"),
                     "negative lp": ({**INDEX_ENTRY["v"], "lp": -5}, "INVALID_PRICE"), "nan lp": ({**INDEX_ENTRY["v"], "lp": float("nan")}, "INVALID_PRICE"),
                     "inf lp": ({**INDEX_ENTRY["v"], "lp": float("inf")}, "INVALID_PRICE"), "bad chp": ({**INDEX_ENTRY["v"], "chp": "up"}, "INVALID_PRICE"),
                     "other symbol": ({**INDEX_ENTRY["v"], "symbol": "NSE:BANKNIFTY"}, "SYMBOL_MISMATCH"), "values not a dict": ("lp", "MALFORMED_RESPONSE")}
        for name, (values, code) in bad_index.items():
            with self.subTest(case=name):
                result = self.parse({"n": INDEX_SYMBOL, "s": "ok", "v": values}, OPTION_ENTRY)
                self.assertIsNone(result[INDEX_SYMBOL].quote)
                self.assertEqual(result[INDEX_SYMBOL].error, code)
        bad_option = {"missing bid": (dict(lp=1.0, ask=1.05), "MISSING_FIELD"), "missing ask": (dict(lp=1.0, bid=1.0), "MISSING_FIELD"),
                      "zero bid": (dict(bid=0, ask=1.05), "EMPTY_QUOTE"), "zero ask": (dict(bid=1.0, ask=0), "EMPTY_QUOTE"),
                      "crossed": (dict(bid=1.10, ask=1.05), "CROSSED_OR_LOCKED"), "locked": (dict(bid=1.05, ask=1.05), "CROSSED_OR_LOCKED"),
                      "off tick bid": (dict(bid=1.02, ask=1.05), "OFF_TICK"), "off tick ask": (dict(bid=1.0, ask=1.07), "OFF_TICK"),
                      "string bid": (dict(bid="1.0", ask=1.05), "INVALID_PRICE"), "negative ask": (dict(bid=1.0, ask=-1.05), "EMPTY_QUOTE")}
        for name, (values, code) in bad_option.items():
            with self.subTest(case=name):
                result = self.parse(INDEX_ENTRY, {"n": OPTION_SYMBOL, "s": "ok", "v": {"lp": 1.0, "symbol": OPTION_SYMBOL, **values}})
                self.assertIsNone(result[OPTION_SYMBOL].quote)
                self.assertEqual(result[OPTION_SYMBOL].error, code)
                self.assertIsNotNone(result[INDEX_SYMBOL].quote)                 # a bad option never poisons or replaces the index

    def test_symbol_level_errors_and_missing_symbols(self):
        result = self.parse(INDEX_ENTRY, {"n": OPTION_SYMBOL, "s": "error", "v": {"code": -300}})
        self.assertEqual(result[OPTION_SYMBOL].error, "INVALID_SYMBOL")
        self.assertEqual(self.parse({"n": OPTION_SYMBOL, "s": "error", "v": {}}, INDEX_ENTRY)[OPTION_SYMBOL].error, "API_ERROR")
        self.assertEqual(self.parse(INDEX_ENTRY)[OPTION_SYMBOL].error, "SYMBOL_MISMATCH")        # asked for, not returned

    def test_whole_response_failures_raise(self):
        cases = {"not a dict": ["x"], "status not ok": {"s": "ok", "code": 201, "d": []}, "no status": {"code": 200, "d": []},
                 "d not a list": {"s": "ok", "code": 200, "d": {}}, "no d": {"s": "ok", "code": 200},
                 "entry not a dict": body("x"), "entry without name": body({"s": "ok", "v": {}}), "duplicate": body(INDEX_ENTRY, INDEX_ENTRY)}
        for name, payload in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(QuoteError) as ctx:
                    parse_quotes(payload, self.SPECS)
                self.assertEqual(ctx.exception.code, "MALFORMED_RESPONSE")
        for api_code, expected in ((-8, "AUTH_FAILED"), (-15, "AUTH_FAILED"), (-16, "AUTH_FAILED"), (-17, "AUTH_FAILED"), (-300, "INVALID_SYMBOL"),
                                   (-429, "RATE_LIMITED"), (-360, "RATE_LIMITED"), (-50, "API_ERROR")):
            with self.subTest(api_code=api_code):
                with self.assertRaises(QuoteError) as ctx:
                    parse_quotes({"s": "error", "code": api_code, "message": "text that is never copied"}, self.SPECS)
                self.assertEqual(ctx.exception.code, expected)
                self.assertNotIn("never copied", str(ctx.exception))

    def test_http_status_mapping(self):
        self.assertEqual([code_for_http_status(s) for s in (200, 401, 403, 429, 400, 500)],
                         [None, "AUTH_FAILED", "ENTITLEMENT_DENIED", "RATE_LIMITED", "API_ERROR", "API_ERROR"])
        self.assertTrue(all(code in DESCRIPTIONS for code in ("AUTH_FAILED", "ENTITLEMENT_DENIED", "RATE_LIMITED")))


class TestCredentials(NoNetwork):
    def test_loading_and_redaction(self):
        credentials = load_credentials(ENV)
        self.assertEqual(credentials.header_value(), f"{APP_ID}:{SECRET}")
        for text in (repr(credentials), str(credentials), f"{credentials}", "%s" % credentials, format(credentials)):
            self.assertNotIn(SECRET, text)
            self.assertNotIn(APP_ID, text)
        with self.assertRaises(TypeError):
            pickle.dumps(credentials)
        self.assertNotIn(SECRET, credentials_summary(ENV))

    def test_missing_and_malformed_credentials(self):
        for name, env in {"empty": {}, "no secret": {"FYERS_APP_ID": APP_ID}, "blank": {"FYERS_APP_ID": APP_ID, "FYERS_ACCESS_TOKEN": ""}}.items():
            with self.subTest(case=name):
                with self.assertRaises(ConfigError) as ctx:
                    load_credentials(env)
                self.assertEqual(ctx.exception.code, "CREDENTIALS_MISSING")
        bad = {"space": SECRET + " x", "newline header injection": SECRET + "\r\nX-Evil: 1", "too short": "short", "quote": SECRET + '"'}
        for name, value in bad.items():
            with self.subTest(case=name):
                with self.assertRaises(ConfigError) as ctx:
                    Credentials(APP_ID, value)
                self.assertEqual(ctx.exception.code, "CREDENTIALS_INVALID")
                self.assertNotIn(value, str(ctx.exception))
                self.assertNotIn(value.strip(), credentials_summary({"FYERS_APP_ID": APP_ID, "FYERS_ACCESS_TOKEN": value}))
        self.assertIsNotNone(credentials_summary({}))


class TestClientAndNetworkGuards(NoNetwork):
    def client(self, *replies):
        transport = Transport(*replies)
        return QuotesClient(load_credentials(ENV), transport), transport

    def test_request_shape_and_where_the_credentials_go(self):
        client, transport = self.client(ok(INDEX_ENTRY))
        client.get_quotes((INDEX_SYMBOL,))
        (url, headers), = transport.calls
        self.assertTrue(url.startswith(f"https://{DATA_HOST}{QUOTES_PATH}?symbols="))
        self.assertIn("NSE%3ANIFTY50-INDEX", url)
        self.assertNotIn(SECRET, url)
        self.assertNotIn(APP_ID, url)
        self.assertEqual(headers["Authorization"], f"{APP_ID}:{SECRET}")

    def test_http_failures_map_to_short_codes(self):
        cases = {401: "AUTH_FAILED", 403: "ENTITLEMENT_DENIED", 429: "RATE_LIMITED", 500: "API_ERROR"}
        for status, code in cases.items():
            with self.subTest(status=status):
                client, _ = self.client((status, b'{"s":"error","code":-1,"message":"details"}'))
                with self.assertRaises(QuoteError) as ctx:
                    client.get_quotes((INDEX_SYMBOL,))
                self.assertEqual(ctx.exception.code, code)
        client, _ = self.client((400, b'{"s":"error","code":-300,"message":"Invalid symbol"}'))
        with self.assertRaises(QuoteError) as ctx:
            client.get_quotes((INDEX_SYMBOL,))
        self.assertEqual(ctx.exception.code, "INVALID_SYMBOL")
        client, _ = self.client((401, b"<html>not json</html>"))
        with self.assertRaises(QuoteError):
            client.get_quotes((INDEX_SYMBOL,))

    def test_undecodable_success_bodies_are_malformed(self):
        for name, raw in {"html": b"<html>", "empty": b"", "nan": b'{"s":"ok","code":NaN,"d":[]}', "bad utf8": b"\xff\xfe"}.items():
            with self.subTest(case=name):
                client, _ = self.client((200, raw))
                with self.assertRaises(QuoteError) as ctx:
                    client.get_quotes((INDEX_SYMBOL,))
                self.assertEqual(ctx.exception.code, "MALFORMED_RESPONSE")

    def test_transport_errors_propagate_as_codes(self):
        for code in ("TIMEOUT", "NETWORK_ERROR", "REDIRECT_REFUSED"):
            client, _ = self.client(TransportError(code))
            with self.assertRaises(TransportError) as ctx:
                client.get_quotes((INDEX_SYMBOL,))
            self.assertEqual(ctx.exception.code, code)

    def test_symbol_arguments_are_restricted(self):
        client, transport = self.client(ok(INDEX_ENTRY))
        bad = [(), ("NSE:A",) * 51, ("BSE:SENSEX",), ("NSE:ABC\r\nX-Evil: 1",), ("NSE:A B",), ("NSE:AB&x=1/../",), (INDEX_SYMBOL + ",NSE:SBIN-EQ",), ("",)]
        for symbols in bad:
            with self.subTest(symbols=symbols):
                with self.assertRaises(QuoteError):
                    client.get_quotes(symbols)
        self.assertEqual(transport.calls, [])

    def test_only_two_hosts_and_paths_can_be_requested(self):
        self.assertEqual(ALLOWED, {(DATA_HOST, QUOTES_PATH), (MASTER_HOST, "/sym_details/NSE_FO.csv")})
        for ok_url in (f"https://{DATA_HOST}{QUOTES_PATH}?symbols=NSE%3AX", f"https://{MASTER_HOST}/sym_details/NSE_FO.csv"):
            check_url(ok_url)
        bad = [f"http://{DATA_HOST}{QUOTES_PATH}", f"https://{DATA_HOST}/api/v3/" + "orders", f"https://{DATA_HOST}/api/v3/" + "positions",
               f"https://{DATA_HOST}{QUOTES_PATH}/extra", "https://example.com/data/quotes", f"https://{DATA_HOST}:8443{QUOTES_PATH}",
               f"https://user:pw@{DATA_HOST}{QUOTES_PATH}", f"https://{DATA_HOST}{QUOTES_PATH}#frag", f"https://{DATA_HOST}.evil.example{QUOTES_PATH}",
               "file:///etc/passwd", f"ftp://{DATA_HOST}{QUOTES_PATH}", ""]
        for url in bad:
            with self.subTest(url=url):
                with self.assertRaises(TransportError):
                    check_url(url)
                with self.assertRaises(TransportError):
                    fyers_http.urllib_transport(url, {}, 1, 10)       # refused before any connection is attempted

    def test_redirects_are_refused_and_the_client_is_get_only(self):
        self.assertIsNone(fyers_http._NoRedirect().redirect_request(mock.Mock(), mock.Mock(), 302, "Found", {}, "https://elsewhere.example/"))
        source = (SRC / "niftypaper" / "providers" / "fyers_http.py").read_text(encoding="utf-8")
        self.assertIn('method="GET"', source)
        self.assertEqual([m for m in ("POST", "PUT", "DELETE", "PATCH", "data=") if m in source], [])
        self.assertEqual([n for n in dir(QuotesClient) if not n.startswith("_")], ["get_quotes"])

    def test_the_real_transport_is_never_reached_by_these_tests(self):
        import socket
        with socket.socket() as probe:
            with self.assertRaises(AssertionError):
                probe.connect(("127.0.0.1", 9))


class TestSymbolResolution(NoNetwork):
    def resolve(self, text, **over):
        args = dict(expiry=EXPIRY, strike=24000, option_type="CE", today=date(2026, 11, 2))
        args.update(over)
        return resolve_nifty_option(text, **args)

    def test_unique_match_is_resolved_from_the_file(self):
        text = master_text(master_row(underlying="BANKNIFTY", ticker="NSE:BANKNIFTY26N0324000CE"), master_row(kind="PE", ticker="NSE:NIFTY26N0324000PE"),
                           master_row(strike=24100, ticker="NSE:NIFTY26N0324100CE"), master_row(), ["short", "row"])
        found = self.resolve(text)
        self.assertEqual((found.symbol, found.lot_size, found.tick_size, found.strike, found.option_type), (OPTION_SYMBOL, 65, Decimal("0.05"), 24000, "CE"))
        self.assertEqual(self.resolve(text, option_type="PE").symbol, "NSE:NIFTY26N0324000PE")

    def test_nothing_is_guessed(self):
        good = master_row()
        cases = {
            "not found (strike)": (master_text(good), dict(strike=24050), "NOT_FOUND"),
            "not found (expiry)": (master_text(good), dict(expiry=date(2026, 11, 10)), "NOT_FOUND"),
            "expired": (master_text(good), dict(today=date(2026, 11, 4)), "EXPIRED"),
            "ambiguous": (master_text(good, master_row(ticker="NSE:NIFTY26N0324000CE")), {}, "AMBIGUOUS"),
            "ticker disagrees": (master_text(master_row(ticker="NSE:NIFTY26N0324500CE")), {}, "INCONSISTENT"),
            "details disagree": (master_text(master_row(details="NIFTY 10 Nov 26 24000 CE")), {}, "INCONSISTENT"),
            "wrong prefix": (master_text(master_row(ticker="BSE:NIFTY26N0324000CE")), {}, "INCONSISTENT"),
            "bad lot": (master_text([*good[:3], "0", *good[4:]]), {}, "INCONSISTENT"),
            "bad tick": (master_text([*good[:4], "x", *good[5:]]), {}, "INCONSISTENT"),
            "empty file": ("", {}, "MASTER_UNREADABLE"), "garbage": ("<html>error</html>", {}, "MASTER_UNREADABLE"),
            "no NIFTY rows": (master_text(master_row(underlying="BANKNIFTY", ticker="NSE:BANKNIFTY26N0324000CE")), {}, "MASTER_UNREADABLE"),
            "bad type": (master_text(good), dict(option_type="XX"), "BAD_REQUEST"), "float strike": (master_text(good), dict(strike=24000.5), "BAD_REQUEST"),
            "bool strike": (master_text(good), dict(strike=True), "BAD_REQUEST"), "zero strike": (master_text(good), dict(strike=0), "BAD_REQUEST"),
            "string expiry": (master_text(good), dict(expiry="2026-11-03"), "BAD_REQUEST"),
        }
        for name, (text, over, code) in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(SymbolError) as ctx:
                    self.resolve(text, **over)
                self.assertEqual(ctx.exception.code, code)


class TestProvider(NoNetwork):
    def make(self, *replies, option=True, clock=None, **over):
        clock = clock or Clock()
        transport = Transport(*replies)
        resolved = resolve_nifty_option(master_text(master_row()), expiry=EXPIRY, strike=24000, option_type="CE", today=date(2026, 11, 2)) if option else None
        provider = FyersProvider(QuotesClient(load_credentials(ENV), transport), option=resolved, clock=clock, **over)
        return provider, transport, clock

    def view(self, provider, clock):
        return App(provider, clock=clock, mode="provider").view()

    def test_success_is_unverified_never_live(self):
        provider, transport, clock = self.make(ok(INDEX_ENTRY, OPTION_ENTRY))
        health, snapshot, _ = self.view(provider, clock)
        self.assertIs(health.state, DataState.UNVERIFIED)
        self.assertFalse(snapshot.synthetic)
        self.assertEqual((snapshot.received_at, snapshot.as_of), (T0, T0))
        self.assertEqual((snapshot.provider_time_raw, snapshot.provider_time), (TT, None))
        self.assertEqual((snapshot.time_semantics, snapshot.latency_class), ("UNKNOWN", "UNKNOWN"))
        self.assertEqual(snapshot.index_value, Decimal("24012.35"))
        row, = snapshot.chain
        self.assertEqual((row.symbol, row.bid, row.ask, row.price), (OPTION_SYMBOL, Decimal("151.15"), Decimal("151.25"), Decimal("151.2")))
        self.assertEqual(len(transport.calls), 1)
        self.assertIn(f"{INDEX_SYMBOL.replace(':', '%3A')},{OPTION_SYMBOL.replace(':', '%3A')}", transport.calls[0][0])

    def test_no_response_can_make_it_live_or_delayed(self):
        """Even a perfect response, with a time field equal to 'now', is only UNVERIFIED: success and a fresh-looking time prove nothing."""
        now_epoch = int(T0.timestamp())
        for tt in (TT, str(now_epoch), now_epoch, None, 0, "garbage", [1]):
            with self.subTest(tt=tt):
                provider, _, clock = self.make(ok(entry(INDEX_SYMBOL, tt=tt, lp=24000, chp=0.1)), option=False)
                health, snapshot, _ = self.view(provider, clock)
                self.assertIs(health.state, DataState.UNVERIFIED)
                self.assertEqual(snapshot.provider_time_raw, tt)

    def test_failures_show_unavailable_with_no_values_and_no_synthetic_data(self):
        cases = {"timeout": TransportError("TIMEOUT"), "network": TransportError("NETWORK_ERROR"), "401": (401, b"{}"), "403": (403, b"{}"),
                 "429": (429, b"{}"), "html": (200, b"<html>"), "token expired code": (200, json.dumps({"s": "error", "code": -8}).encode()),
                 "no index": ok(OPTION_ENTRY), "bad index": ok(entry(INDEX_SYMBOL, lp=0), OPTION_ENTRY), "boom": RuntimeError("secret detail " + SECRET)}
        for name, reply in cases.items():
            with self.subTest(case=name):
                provider, _, clock = self.make(reply)
                health, snapshot, now = self.view(provider, clock)
                self.assertIs(health.state, DataState.UNAVAILABLE)
                self.assertIsNone(snapshot)
                self.assertTrue(provider.last_error)
                self.assertEqual(health.reason, provider.last_error)
                app = App(provider, clock=clock, mode="provider")
                html = render_page(health, snapshot, app.account, now, "provider")
                doc = snapshot_json(health, snapshot, app.account, now, "provider")
                self.assertEqual(parse_page(html).numeric, [])
                self.assertNotIn("SYN", html)
                self.assertIs(doc["synthetic"], False)
                self.assertEqual(doc["chain"], [])
                self.assertIn(PROVIDER_BANNERS[DataState.UNAVAILABLE], html)
                for text in (html, json.dumps(doc), provider.last_error, repr(provider)):
                    self.assertNotIn(SECRET, text)
                    self.assertNotIn(APP_ID, text)

    def test_missing_credentials_never_reach_the_network(self):
        transport = Transport(ok(INDEX_ENTRY))
        provider = build_provider(None, env={}, transport=transport)
        self.assertIsNone(provider(T0))
        self.assertIn("not configured", provider.last_error)
        self.assertEqual(transport.calls, [])
        bad = build_provider(None, env={"FYERS_APP_ID": APP_ID, "FYERS_ACCESS_TOKEN": "x y"}, transport=transport)
        self.assertIsNone(bad(T0))
        self.assertEqual(transport.calls, [])

    def test_a_bad_option_quote_never_hides_or_replaces_the_index(self):
        provider, _, clock = self.make(ok(INDEX_ENTRY, entry(OPTION_SYMBOL, lp=1.0, bid=1.10, ask=1.05)))
        health, snapshot, _ = self.view(provider, clock)
        self.assertIs(health.state, DataState.UNVERIFIED)
        self.assertEqual(snapshot.chain, ())
        self.assertTrue(any("rejected" in n for n in snapshot.notes))

    def test_requests_are_rate_limited_and_cached(self):
        provider, transport, clock = self.make(ok(INDEX_ENTRY, OPTION_ENTRY))
        first = provider(clock())
        clock.advance(2)
        self.assertIs(provider(clock()), first)
        self.assertEqual(len(transport.calls), 1)
        clock.advance(4)
        provider(clock())
        self.assertEqual(len(transport.calls), 2)

    def test_failures_are_not_retried_in_a_tight_loop_and_429_backs_off(self):
        provider, transport, clock = self.make((429, b"{}"), ok(INDEX_ENTRY, OPTION_ENTRY))
        self.assertIsNone(provider(clock()))
        for _ in range(5):
            clock.advance(10)                         # past the normal interval, still inside the 60 s back-off
            if clock.now - T0 < timedelta(seconds=60):
                self.assertIsNone(provider(clock()))
        self.assertEqual(len(transport.calls), 1)
        clock.advance(20)
        self.assertIsNotNone(provider(clock()))       # recovered after the back-off
        self.assertEqual(len(transport.calls), 2)

    def test_recovery_after_an_error(self):
        provider, _, clock = self.make(TransportError("NETWORK_ERROR"), ok(INDEX_ENTRY, OPTION_ENTRY))
        self.assertIs(self.view(provider, clock)[0].state, DataState.UNAVAILABLE)
        clock.advance(6)
        self.assertIs(self.view(provider, clock)[0].state, DataState.UNVERIFIED)
        self.assertIsNone(provider.last_error)

    def test_an_old_cached_response_becomes_stale_and_withholds_values(self):
        provider, _, clock = self.make(ok(INDEX_ENTRY, OPTION_ENTRY), min_interval=timedelta(seconds=300))
        self.assertIs(self.view(provider, clock)[0].state, DataState.UNVERIFIED)
        clock.advance(31)
        health, snapshot, now = self.view(provider, clock)
        self.assertIs(health.state, DataState.STALE)
        html = render_page(health, snapshot, PaperAccount(), now, "provider")
        self.assertEqual(parse_page(html).numeric, [])
        self.assertNotIn("24,012.35", html)

    def test_credentials_never_appear_anywhere_the_user_can_see(self):
        provider, transport, clock = self.make(ok(INDEX_ENTRY, OPTION_ENTRY))
        health, snapshot, now = self.view(provider, clock)
        account = PaperAccount()
        shown = [render_page(health, snapshot, account, now, "provider"), json.dumps(snapshot_json(health, snapshot, account, now, "provider")),
                 repr(snapshot), repr(health), repr(provider), repr(provider._client), str(provider.last_error)]
        for text in shown:
            self.assertNotIn(SECRET, text)
            self.assertNotIn(APP_ID, text)
        self.assertEqual(transport.calls[0][1]["Authorization"], f"{APP_ID}:{SECRET}")      # positive control: it was sent, in the header only


class TestStatesNeedEvidence(unittest.TestCase):
    def snap(self, **over):
        base = dict(source="TEST_PROVIDER", synthetic=False, as_of=T0, index_value=Decimal("1"), pct_change=None, chain=(), received_at=T0)
        base.update(over)
        return Snapshot(**base)

    def state(self, now=T0, **over):
        return assess(self.snap(**over), now, timedelta(seconds=30)).state

    def test_live_needs_documented_realtime_and_a_fresh_quote_time(self):
        evidence = dict(time_semantics=TIME_QUOTE, provider_time=T0 - timedelta(seconds=2), latency_class=LATENCY_REALTIME_DOCUMENTED)
        self.assertIs(self.state(**evidence), DataState.LIVE)
        self.assertIs(self.state(**{**evidence, "latency_class": "UNKNOWN"}), DataState.UNVERIFIED)
        self.assertIs(self.state(**{**evidence, "time_semantics": TIME_LAST_TRADE}), DataState.UNVERIFIED)
        self.assertIs(self.state(**{**evidence, "time_semantics": "UNKNOWN"}), DataState.UNVERIFIED)
        self.assertIs(self.state(**{**evidence, "provider_time": T0 - timedelta(seconds=31)}), DataState.STALE)
        self.assertIs(self.state(**{**evidence, "provider_time": T0 + timedelta(seconds=30)}), DataState.UNAVAILABLE)
        self.assertIs(self.state(**{**evidence, "provider_time": None}), DataState.UNAVAILABLE)
        self.assertIs(self.state(**{**evidence, "provider_time": T0.replace(tzinfo=None)}), DataState.UNAVAILABLE)

    def test_delayed_only_when_the_provider_confirms_it(self):
        self.assertIs(self.state(latency_class=LATENCY_DELAYED_CONFIRMED), DataState.DELAYED)
        self.assertIs(self.state(), DataState.UNVERIFIED)

    def test_local_receive_time_rules(self):
        self.assertIs(self.state(now=T0 + timedelta(seconds=30)), DataState.UNVERIFIED)
        self.assertIs(self.state(now=T0 + timedelta(seconds=31)), DataState.STALE)
        self.assertIs(self.state(now=T0 - timedelta(seconds=5)), DataState.UNAVAILABLE)
        self.assertIs(self.state(received_at=None), DataState.UNAVAILABLE)
        self.assertIs(self.state(received_at=T0.replace(tzinfo=None)), DataState.UNAVAILABLE)
        self.assertIs(self.state(as_of=T0.replace(tzinfo=None)), DataState.UNAVAILABLE)
        self.assertIs(assess(self.snap(), None, timedelta(seconds=30)).state, DataState.UNAVAILABLE)


class TestDashboardIntegration(unittest.TestCase):
    def test_provider_mode_refuses_synthetic_data_instead_of_showing_it(self):
        app = App(SyntheticProvider(), clock=Clock(), mode="provider")
        health, snapshot, now = app.view()
        self.assertIs(health.state, DataState.UNAVAILABLE)
        self.assertIsNone(snapshot)
        html = render_page(health, snapshot, app.account, now, app.mode)
        self.assertNotIn("24,000.00", html)
        self.assertEqual(parse_page(html).numeric, [])
        with self.assertRaises(ValueError):
            App(SyntheticProvider(), mode="demo")

    def test_a_provider_snapshot_can_never_wear_the_simulated_banner(self):
        provider = FyersProvider(QuotesClient(load_credentials(ENV), Transport(ok(INDEX_ENTRY))), clock=Clock())
        health, snapshot, now = App(provider, clock=Clock(), mode="provider").view()
        for mode in ("synthetic", "provider"):           # even if the app were mislabelled
            html = render_page(health, snapshot, PaperAccount(), now, mode)
            page = "".join(parse_page(html).text)
            self.assertIn(PROVIDER_BANNERS[DataState.UNVERIFIED], page)
            self.assertNotIn("SIMULATED DATA", page)
            cells = parse_page(html).numeric
            self.assertTrue(cells and all(c["synthetic"] == "false" and "UNVERIFIED" in c["text"] and "SYN" not in c["text"] for c in cells))
            self.assertIs(snapshot_json(health, snapshot, PaperAccount(), now, mode)["synthetic"], False)
        self.assertIn("not established", html)

    def test_the_server_labels_provider_data_and_serves_loopback_only(self):
        clock = Clock()
        provider = FyersProvider(QuotesClient(load_credentials(ENV), Transport(ok(INDEX_ENTRY))), clock=clock)
        server = create_server(App(provider, clock=clock, mode="provider"), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.assertEqual(server.server_address[0], "127.0.0.1")
        thread.start()
        try:
            seen = {}
            for path in ("/", "/api/snapshot"):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
                conn.request("GET", path)
                response = conn.getresponse()
                seen[path] = (response.status, response.getheader("X-Data-Synthetic"), response.read().decode("utf-8"))
                conn.close()
            self.assertEqual([v[:2] for v in seen.values()], [(200, "false"), (200, "false")])
            self.assertEqual(json.loads(seen["/api/snapshot"][2])["data_state"], "UNVERIFIED")
            self.assertNotIn(SECRET, seen["/"][2] + seen["/api/snapshot"][2])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class TestCommandLines(NoNetwork):
    def run_cli(self, argv):
        err = io.StringIO()
        never_start = mock.patch.object(dashboard_cli, "create_server", side_effect=AssertionError("the server must not start"))
        with never_start, contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            return dashboard_cli.main(argv), err.getvalue()

    def test_starts_in_synthetic_mode_and_realtime_is_an_explicit_choice(self):
        args = dashboard_cli.build_parser().parse_args([])
        self.assertEqual((args.mode, args.realtime_provider, args.scenario), ("synthetic", "fyers", "simulated"))

    def test_inconsistent_arguments_are_refused_before_anything_starts(self):
        cases = [["--option-expiry", "2026-11-03"], ["--option-strike", "24000", "--option-type", "CE"],
                 ["--option-expiry", "03/11/2026", "--option-strike", "24000", "--option-type", "CE"], ["--starting-balance", "abc"],
                 ["--starting-balance", "-5"]]
        for argv in cases:
            with self.subTest(argv=argv):
                code, err = self.run_cli(argv)
                self.assertEqual(code, 2)
                self.assertIn("error:", err)

    def test_option_symbol_failure_makes_the_provider_unavailable_and_nothing_is_guessed(self):
        from niftypaper import assembly
        failing = mock.Mock(side_effect=SymbolError("NOT_FOUND"))
        with mock.patch.object(assembly, "resolve_option_online", failing):
            provider = assembly.default_factories(option_request=(EXPIRY, 24000, "CE"), env=ENV, transport=Transport(ok(INDEX_ENTRY)))["fyers"]()
        self.assertIsNone(provider(T0))
        self.assertIn("nothing was guessed", provider.last_error)
        self.assertIn("NOT_FOUND", provider.last_error)

    def smoke(self, env, transport, *argv):
        lines = []
        code = fyers_smoke.main(list(argv), env=env, transport=transport, clock=Clock(), out=lines.append)
        return code, "\n".join(lines)

    def test_smoke_test_reports_and_redacts(self):
        code, text = self.smoke(ENV, Transport(ok(INDEX_ENTRY)))
        self.assertEqual(code, 0)
        self.assertIn("state: UNVERIFIED", text)
        self.assertIn("provider time field, raw: '1691020800'", text)
        self.assertIn("does not prove the price is live", text)
        for secret in (SECRET, APP_ID, "Authorization"):
            self.assertNotIn(secret, text)

    def test_smoke_test_with_an_option_uses_the_symbol_master(self):
        master = (200, master_text(master_row()).encode())
        transport = Transport(master, ok(INDEX_ENTRY, OPTION_ENTRY))
        code, text = self.smoke(ENV, transport, "--option-expiry", "2026-11-03", "--option-strike", "24000", "--option-type", "CE")
        self.assertEqual(code, 0)
        self.assertIn(f"option {OPTION_SYMBOL}: last 151.2  bid 151.15  ask 151.25", text)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual("Authorization" in transport.calls[0][1], False)        # the public file is fetched without credentials
        self.assertNotIn(SECRET, text)

    def test_smoke_test_failure_modes(self):
        code, text = self.smoke({}, Transport(ok(INDEX_ENTRY)))
        self.assertEqual(code, 2)
        self.assertNotIn(SECRET, text)
        code, text = self.smoke(ENV, Transport((401, b"{}")))
        self.assertEqual((code, "state: UNAVAILABLE" in text), (1, True))
        code, _ = self.smoke(ENV, Transport((200, master_text(master_row()).encode())), "--option-expiry", "2026-11-03", "--option-strike", "99999", "--option-type", "CE")
        self.assertEqual(code, 3)
        self.assertEqual(self.smoke(ENV, Transport(ok(INDEX_ENTRY)), "--option-expiry", "2026-11-03")[0], 2)
        self.assertEqual(self.smoke(ENV, Transport(ok(INDEX_ENTRY)), "--option-expiry", "03/11/2026", "--option-strike", "24000", "--option-type", "CE")[0], 2)


class TestNoOrderCapability(unittest.TestCase):
    def test_provider_sources_contain_no_order_or_account_endpoints(self):
        for path in sorted((SRC / "niftypaper" / "providers").glob("*.py")):
            text = path.read_text(encoding="utf-8").lower()
            with self.subTest(file=path.name):
                for word in ("positions", "holdings", "funds", "tradebook", "orderbook", "/orders", "order_id", "place" "order", "cancel" "order"):
                    self.assertNotIn(word, text)
                tree = ast.parse(text)
                strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and "/" in n.value and "." not in n.value[:1]]
                self.assertEqual([s for s in strings if "order" in s and len(s) < 40], [])


if __name__ == "__main__":
    unittest.main()
