"""Provider provenance, timestamps, fail-closed contract metadata, and the unpriced-position state with its recovery behaviour.
No network, no credentials: every provider here is a clearly labelled fixture or a fake transport. Nothing in this file shows that any FYERS data is real, live or permitted."""
import io
import json
import re
import unittest
from datetime import timedelta
from decimal import Decimal

from niftypaper.dashboard.status import (EVIDENCE_FIXTURE_TRANSPORT, EVIDENCE_PROVIDER_RESPONSE, EVIDENCE_SYNTHETIC, DataState, OptionRow, Snapshot)
from niftypaper.providers import fyers_http
from niftypaper.providers.fyers_http import TransportError, urllib_transport
from niftypaper.providers.fyers_provider import build_provider
from niftypaper.providers.fyers_smoke import main as smoke
from tests.test_dashboard_ui import text_of
from tests.test_fyers import ENV, INDEX_ENTRY, OPTION_ENTRY, SECRET, APP_ID, Clock, Transport, ok
from tests.test_integration_v602 import FixtureProvider, provider_app
from tests.test_modes import Served, fyers_provider
from tests.test_dashboard_ui import SYMBOL, Base
from niftypaper.dashboard.sample_data import SyntheticProvider


class TestProvenance(unittest.TestCase):
    def test_a_fake_transport_is_labelled_as_a_fixture_and_only_the_real_client_can_claim_a_provider_response(self):
        clock = Clock()
        fixture = build_provider(None, env=ENV, transport=Transport(ok(INDEX_ENTRY)), clock=clock)
        health_snapshot = fixture(clock())
        self.assertEqual(health_snapshot.evidence, EVIDENCE_FIXTURE_TRANSPORT)
        real = build_provider(None, env=ENV, transport=urllib_transport, clock=clock)                     # built, never called: no request is made here
        self.assertEqual(real._evidence, EVIDENCE_PROVIDER_RESPONSE)
        self.assertEqual(SyntheticProvider()(clock()).evidence, EVIDENCE_SYNTHETIC)

    def test_the_page_json_and_smoke_output_show_the_evidence(self):
        clock = Clock()
        app = provider_app(FixtureProvider(clock, latency="UNKNOWN"), clock)
        self.addCleanup(app.close)
        with Served(app) as s:
            page = s.request("GET", "/")[2]
            doc = json.loads(s.request("GET", "/api/snapshot")[2])
        self.assertEqual(doc["evidence"], "UNSPECIFIED")                                                 # a hand-built test provider claims nothing
        self.assertIn("Evidence: UNSPECIFIED", page)
        provider, _ = fyers_provider(clock, ok(INDEX_ENTRY, OPTION_ENTRY))
        snapshot = provider(clock())
        self.assertEqual(snapshot.evidence, EVIDENCE_FIXTURE_TRANSPORT)
        lines = []
        code = smoke([], env=ENV, transport=Transport(ok(INDEX_ENTRY)), clock=clock, out=lines.append)
        text = "\n".join(lines)
        self.assertEqual(code, 0)
        self.assertIn("evidence: FIXTURE_TRANSPORT", text)
        self.assertIn("exchange time: NOT ESTABLISHED", text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(APP_ID, text)


class TestTimestamps(unittest.TestCase):
    def test_local_receive_time_and_the_providers_own_field_are_kept_apart_and_no_exchange_time_is_invented(self):
        clock = Clock()
        provider, _ = fyers_provider(clock, ok(INDEX_ENTRY, OPTION_ENTRY))
        snapshot = provider(clock())
        self.assertEqual(snapshot.received_at, clock())
        self.assertIsNotNone(snapshot.provider_time_raw)                                                 # exactly as received
        self.assertIsNone(snapshot.provider_time)                                                        # not interpreted: no documented meaning
        self.assertEqual((snapshot.time_semantics, snapshot.latency_class), ("UNKNOWN", "UNKNOWN"))

    def test_the_page_and_the_json_say_the_exchange_time_is_not_established(self):
        clock = Clock()
        provider, _ = fyers_provider(clock, ok(INDEX_ENTRY, OPTION_ENTRY))
        app = provider_app(provider, clock)
        self.addCleanup(app.close)
        with Served(app) as s:
            page = s.request("GET", "/")[2]
            doc = json.loads(s.request("GET", "/api/snapshot")[2])
        self.assertEqual((doc["exchange_time"], doc["exchange_time_status"], doc["data_state"]), (None, "NOT_ESTABLISHED", "UNVERIFIED"))
        self.assertIsNotNone(doc["received_at"])
        self.assertIsNotNone(doc["provider_time_raw"])
        text = text_of(page)
        self.assertIn("Exchange time: not established", text)
        self.assertIn("(local clock); provider time field", text)

    def test_a_documented_quote_time_is_shown_only_when_the_snapshot_declares_it(self):
        clock = Clock()
        app = provider_app(FixtureProvider(clock), clock)                                                # this TEST provider declares a quote time and real-time latency
        self.addCleanup(app.close)
        with Served(app) as s:
            doc = json.loads(s.request("GET", "/api/snapshot")[2])
        self.assertEqual((doc["exchange_time_status"], doc["data_state"]), ("ESTABLISHED", "LIVE"))


class SpecProvider(FixtureProvider):
    """A TEST provider whose contract metadata can be missing."""
    def __init__(self, clock, lot=65, tick="0.05", **kw):
        super().__init__(clock, **kw)
        self.lot, self.tick = lot, None if tick is None else Decimal(tick)

    def __call__(self, now=None):
        base = super().__call__(now)
        row = base.chain[0]
        return Snapshot(**{**base.__dict__, "chain": (OptionRow(row.symbol, row.kind, row.strike, row.price, row.bid, row.ask, row.bid_qty, row.ask_qty, self.lot, self.tick),)})


class TestFailClosedMetadata(unittest.TestCase):
    def test_unknown_contract_specifications_never_trade(self):
        for name, kw in (("no lot size", {"lot": None}), ("no tick size", {"tick": None}), ("zero lot size", {"lot": 0})):
            with self.subTest(name):
                clock = Clock()
                app = provider_app(SpecProvider(clock, **kw), clock)
                self.addCleanup(app.close)
                app.handle_trade({"symbol": "TEST-OPT-1", "side": "BUY", "lots": "1"})
                self.assertEqual((app.notice_kind, len(app.engine.ledger)), ("trade_refused", 0))

    def test_the_adapter_rejects_bad_quotes_and_never_passes_them_on(self):
        clock = Clock()
        for name, bid, ask in (("crossed", "101.10", "101.00"), ("locked", "101.00", "101.00"), ("zero", "0", "101.00"), ("off tick", "100.93", "101.00")):
            with self.subTest(name):
                entry = dict(OPTION_ENTRY)
                entry["v"] = {**entry["v"], "bid": float(bid), "ask": float(ask)}
                provider, _ = fyers_provider(clock, ok(INDEX_ENTRY, entry))
                snapshot = provider(clock())
                self.assertEqual(snapshot.chain, ())                                                     # the option is dropped; the reason is in the notes
                self.assertTrue(any("rejected" in n for n in snapshot.notes))

    def test_a_missing_option_quote_and_a_failed_response_give_no_values(self):
        clock = Clock()
        provider, _ = fyers_provider(clock, ok(INDEX_ENTRY))                                             # the option was asked for but is not in the answer
        self.assertEqual(provider(clock()).chain, ())
        failing, _ = fyers_provider(clock, TransportError("TIMEOUT"))
        self.assertIsNone(failing(clock()))
        self.assertIn("timed out", failing.last_error)

    def test_no_credentials_means_no_request_and_no_data(self):
        transport = Transport(ok(INDEX_ENTRY))
        provider = build_provider(None, env={}, transport=transport)
        self.assertIsNone(provider(Clock()()))
        self.assertEqual(transport.calls if hasattr(transport, "calls") else [], [])

    def test_only_two_read_only_endpoints_exist_and_no_other_path_or_host_can_be_named(self):
        self.assertEqual(len(fyers_http.ALLOWED), 2)
        host, path = fyers_http.DATA_HOST, fyers_http.QUOTES_PATH
        for url in (f"https://{host}/other/endpoint", f"https://{host}{path}/extra", f"https://other.example.org{path}", f"http://{host}{path}", f"https://{host}:8443{path}",
                    f"https://{host}{path}#x", f"https://user@{host}{path}"):
            with self.subTest(url=url), self.assertRaises(TransportError):
                fyers_http.check_url(url)
        fyers_http.check_url(f"https://{host}{path}?symbols=NSE:NIFTY50-INDEX")                              # the one allowed quotes path


class TestUnpricedPositions(Base):
    def positions_app(self):
        clock = Clock()
        provider = FixtureProvider(clock)
        app = provider_app(provider, clock)
        self.addCleanup(app.close)
        app.handle_trade({"symbol": "TEST-OPT-1", "side": "BUY", "lots": "1"})
        self.assertEqual(len(app.engine.ledger), 1)
        return app, clock, provider

    def test_stale_data_shows_a_visible_unpriced_state_and_invents_nothing(self):
        app, clock, provider = self.positions_app()
        with Served(app) as s:
            fresh = s.request("GET", "/")[2]
            self.assertNotIn("UNPRICED POSITION", fresh)
            ledger_fresh = json.loads(s.request("GET", "/api/ledger")[2])
            self.assertEqual((ledger_fresh["unpriced_positions"], ledger_fresh["positions"][0]["priced"]), (0, True))
            clock.advance(31)
            before = [r.to_dict() for r in app.engine.ledger]
            page = s.request("GET", "/")[2]
            ledger = json.loads(s.request("GET", "/api/ledger")[2])
        text = text_of(page)
        self.assertIn("UNPRICED POSITION: 1 open position without a verified price", text.replace("&#9888; ", ""))
        self.assertIn("Data state: STALE", text)
        self.assertIn("Nothing has been priced, filled or closed on their behalf", text)
        self.assertIn("no price or exit is invented", text)
        self.assertIn("UNPRICED", page.split("Open virtual positions")[1])
        self.assertIn("Recovery", text)
        self.assertIn("docs/STALE_DATA_RECOVERY.md", text)
        self.assertEqual((ledger["unpriced_positions"], ledger["positions"][0]["priced"], ledger["positions"][0]["unrealized"], ledger["data_state"]), (1, False, None, "STALE"))
        self.assertEqual([r.to_dict() for r in app.engine.ledger], before)                                # viewing the incident changed nothing
        for side in ("BUY", "SELL"):
            app.handle_trade({"symbol": "TEST-OPT-1", "side": side, "lots": "1"})
            self.assertEqual((app.notice_kind, len(app.engine.ledger)), ("trade_refused", 1))              # no entry and no exit without a verified price
        self.assertEqual(len(app.engine.positions), 1)                                                    # and nothing was closed for the user

    def test_when_a_fresh_verified_price_returns_the_position_reprices_and_a_real_exit_is_possible(self):
        app, clock, provider = self.positions_app()
        clock.advance(31)
        self.assertEqual(app.view()[0].state, DataState.STALE)
        provider.observed = clock()                                                                       # a new response arrives
        provider.bid, provider.ask = "100.40", "100.50"                                                   # at a different, verified quote
        self.assertEqual(app.view()[0].state, DataState.LIVE)
        with Served(app) as s:
            page = s.request("GET", "/")[2]
        self.assertNotIn("UNPRICED POSITION", page)
        app.handle_trade({"symbol": "TEST-OPT-1", "side": "SELL", "lots": "1"})
        self.assertEqual(app.notice_kind, "trade_ok")
        exit_fill = app.engine.ledger[-1]
        self.assertEqual(str(exit_fill.fill_price), "100.35")                                             # the new bid less a tick: never the old quote
        self.assertEqual(app.engine.positions, {})

    def test_the_risk_budget_still_counts_an_unpriced_position(self):
        app, clock, provider = self.positions_app()
        committed_fresh = app.engine.risk_report().committed
        clock.advance(31)
        report = app.engine.risk_report({})                                                               # no verified marks at all
        self.assertFalse(report.marks_known)
        self.assertGreaterEqual(report.committed, committed_fresh - Decimal("1"))                         # the whole premium is still committed, so budget is not released
        self.assertGreater(report.committed, Decimal("0"))

    def test_the_unpriced_alert_is_absent_without_positions_and_the_synthetic_stale_case_shows_it_too(self):
        empty = self.app()
        with Served(empty) as s:
            self.assertNotIn("UNPRICED POSITION", s.request("GET", "/")[2])
        empty.close()                                                                                      # release the account's lock before the next session opens it
        app = self.app()
        app.handle_trade({"symbol": SYMBOL, "side": "BUY", "lots": "1"})
        self.assertEqual(len(app.engine.ledger), 1)
        app.close()
        from niftypaper.dashboard.server import App
        from niftypaper.paper.engine import PaperEngine
        from niftypaper.paper.store import JsonlStore
        from pathlib import Path
        stale = App(SyntheticProvider("stale"), clock=Clock(), mode="synthetic", provider_id="synthetic", factories={"synthetic": lambda: SyntheticProvider("stale")},
                    engine_factory=lambda key: PaperEngine(Decimal("1000000"), clock=Clock(), origin="SYNTHETIC",
                                                           store=JsonlStore(Path(self._tmp.name) / f"paper_{key}.jsonl", 1_000_000, account=key)))
        self.addCleanup(stale.close)
        with Served(stale) as s:
            page = s.request("GET", "/")[2]
        self.assertIn("UNPRICED POSITION", text_of(page).replace("&#9888; ", ""))
        self.assertEqual(len(stale.engine.ledger), 1)


class TestExecutionPriceEligibilityIsStated(unittest.TestCase):
    def test_the_strip_says_whether_prices_are_eligible_and_why(self):
        clock = Clock()
        for kwargs, word in (({}, "ELIGIBLE"), ({"latency": "UNKNOWN"}, "NOT ELIGIBLE")):
            app = provider_app(FixtureProvider(clock, **kwargs), clock)
            self.addCleanup(app.close)
            with Served(app) as s:
                text = text_of(s.request("GET", "/")[2])
            self.assertIn(f"Execution prices: {word}", text)
        self.assertNotIn("Execution prices: ELIGIBLE", text)


if __name__ == "__main__":
    unittest.main()
