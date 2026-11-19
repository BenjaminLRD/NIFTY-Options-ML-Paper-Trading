"""The redesigned dashboard page: structure, labelling, accessibility hooks, fail-closed Real-Time Mode, and the arithmetic shown in the risk card.
Synthetic data only; every state folder is temporary."""
import json
import re
import tempfile
import unittest
from decimal import ROUND_HALF_UP, Decimal
from html.parser import HTMLParser
from pathlib import Path

from niftypaper.dashboard.render import BANNER
from niftypaper.dashboard.sample_data import SyntheticProvider
from niftypaper.dashboard.server import App
from niftypaper.paper.engine import PaperEngine
from niftypaper.paper.store import JsonlStore
from niftypaper.providers.fyers_http import TransportError
from tests.test_modes import APP_ID, ENV, SECRET, Clock, Served, fyers_provider, make_app

CENT = Decimal("0.01")
SYMBOL = "SYN-CE-23800"


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found, self.depth_scroll, self.tables_in_scroll, self.tables = [], 0, 0, 0
        self.open_stack = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.found.append((tag, a))
        if tag == "div" and a.get("class") == "scroll":
            self.depth_scroll += 1
            self.open_stack.append("scroll")
        elif tag == "div":
            self.open_stack.append("div")
        if tag == "table":
            self.tables += 1
            self.tables_in_scroll += 1 if self.depth_scroll else 0

    def handle_endtag(self, tag):
        if tag == "div" and self.open_stack:
            if self.open_stack.pop() == "scroll":
                self.depth_scroll -= 1


def parse(html):
    p = Tags()
    p.feed(html)
    return p


def text_of(html):
    html = re.sub(r"<style.*?</style>", "", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clock = Clock()

    def app(self):
        clock = self.clock
        app = App(SyntheticProvider(), clock=clock, mode="synthetic", provider_id="synthetic", factories={"synthetic": lambda: SyntheticProvider()},
                  engine_factory=lambda key: PaperEngine(Decimal("1000000"), clock=clock, origin="SYNTHETIC",
                                                         store=JsonlStore(Path(self._tmp.name) / f"paper_{key}.jsonl", 1_000_000, account=key)))
        self.addCleanup(app.close)
        return app

    def page(self, s):
        return s.request("GET", "/")[2]


class TestLayout(Base):
    def test_the_page_is_a_compact_dashboard_with_the_expected_regions(self):
        with Served(self.app()) as s:
            page = self.page(s)
        text = text_of(page)
        for needle in ("NIFTY Paper Trading", "All trades are virtual", "Mode: Synthetic", "Synthetic Mode (Free)", "Real-Time Mode (requires provider access)", "Virtual cash",
                       "NIFTY index (SIMULATED)", "Change (SIMULATED)", "Data source", "Last update", "Paper-trading workspace", "Daily risk budget", "Open virtual positions",
                       "Sample option chain", "SYNTHETIC", "Recent activity", "Data health and eligibility", "Provider connection and setup", "Risk details and assumptions",
                       "Account and persistence"):
            self.assertIn(needle, text)
        self.assertIn(BANNER, text)
        self.assertEqual(page.count('aria-pressed="true"'), 1)
        self.assertLess(len(text_of(page.split("<main>")[1].split("<details>")[0])), 6000)       # the main screen is cards and tables, not paragraphs

    def test_nothing_is_loaded_from_outside_and_the_only_script_is_the_local_hashed_one(self):
        import base64
        import hashlib
        with Served(self.app()) as s:
            status, headers, page = s.request("GET", "/")
        for banned in ("<link", "<img", "<iframe", "src=", "@import", "url(", "http://", "https://", "onclick", "onload", "onerror"):
            self.assertNotIn(banned, page)
        self.assertEqual(re.findall(r'href="([^"]*)"', page), ["/ml"])                       # the one link: same-origin, read-only
        scripts = re.findall(r"<script([^>]*)>(.*?)</script>", page, re.S)
        self.assertEqual([a.strip() for a, _ in scripts], ['type="application/json" id="workspace-data"', ""])             # a data block and one local script
        code = scripts[1][1]
        digest = "'sha256-" + base64.b64encode(hashlib.sha256(code.encode("utf-8")).digest()).decode("ascii") + "'"
        self.assertIn(f"script-src {digest};", headers["Content-Security-Policy"])                                         # allowed by its hash alone
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        for network in ("fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon", "localStorage", "sessionStorage", "document.cookie", "eval(", "import(", "http:", "https:"):
            self.assertNotIn(network, code)
        self.assertIn('<meta name="viewport"', page)

    def test_long_explanations_live_in_collapsed_details(self):
        with Served(self.app()) as s:
            page = self.page(s)
        self.assertGreaterEqual(page.count("<details>"), 5)
        self.assertNotIn("<details open", page)
        main = page.split("<main>")[1]
        before_details = main.split("<details>")[0]
        for long_text in ("does not guarantee", "Setup steps", "Exit slippage allowance", "UNVERIFIED is well-formed provider data"):
            self.assertNotIn(long_text, before_details)                                          # still in the page, but not on the main screen
            self.assertIn(long_text, page)

    def test_tables_scroll_sideways_on_narrow_screens_and_inputs_are_labelled(self):
        app = self.app()
        with Served(app) as s:
            page = self.page(s)
        tags = parse(page)
        self.assertEqual(tags.tables, tags.tables_in_scroll)
        ids = {a.get("id") for t, a in tags.found if a.get("id")}
        for tag, a in tags.found:
            if tag in ("select", "input") and a.get("type") not in ("hidden", "radio"):
                self.assertTrue(a.get("aria-label") or a.get("id") in {x[1].get("for") for x in tags.found if x[0] == "label"}, a)
        self.assertEqual({"symbol", "lots", "side-buy", "side-sell"} - ids, set())
        self.assertIn(":focus-visible", page)
        self.assertIn("@media (max-width:860px)", page)
        self.assertEqual(len([a for t, a in tags.found if t == "progress" and a.get("aria-label")]), 1)

    def test_state_is_never_shown_by_colour_alone(self):
        with Served(self.app()) as s:
            page = self.page(s)
        self.assertIn("◆ SIMULATED", text_of(page))
        self.assertIn("✓ OPEN: room for new risk", text_of(page).replace("&#10003;", "✓"))


class TestTradingWorkspace(Base):
    def test_quantity_is_labelled_in_lots_with_the_unverified_lot_size_stated(self):
        with Served(self.app()) as s:
            page = self.page(s)
        text = text_of(page)
        self.assertIn("Quantity (lots)", text)
        self.assertIn("1 lot = 65 units in this simulator", text)
        self.assertIn("not a verified exchange lot size", text)
        self.assertIn("Qty (units)", text)
        self.assertNotIn("Qty (lots)", text)

    def test_the_form_still_posts_the_same_fields_with_the_form_key(self):
        app = self.app()
        with Served(app) as s:
            page = self.page(s)
        self.assertIn('action="/paper/trade"', page)
        self.assertIn(f'name="csrf" value="{app.csrf}"', page)
        self.assertRegex(page, r'<input type="radio" id="side-buy" name="side" value="BUY" checked>')
        self.assertRegex(page, r'<input type="radio" id="side-sell" name="side" value="SELL">')
        self.assertIn('name="lots"', page)

    def test_per_lot_estimates_are_calculated_by_the_backend_not_typed_in(self):
        app = self.app()
        with Served(app) as s:
            page = self.page(s)
        row = next(r for r in SyntheticProvider()(self.clock()).chain if r.symbol == SYMBOL)
        unit_slippage = row.tick * 1
        premium = (row.ask + unit_slippage) * row.lot_size
        fee = (Decimal("20") + premium * Decimal("0.0005")).quantize(CENT, ROUND_HALF_UP)
        self.assertIn(f"{SYMBOL} &mdash; Call option (CE) &middot; strike 23,800 &middot; premium &#8377;{row.price:,.2f}", page)
        self.assertRegex(page, rf"<td class=\"l\">{SYMBOL}</td><td>{row.lot_size}</td><td>{premium:,.2f}</td><td>{fee:,.2f}</td><td>{unit_slippage * row.lot_size:,.2f}</td>")

    def test_a_recorded_trade_and_a_refusal_each_get_a_clear_result(self):
        with Served(self.app()) as s:
            s.post("/paper/trade", {"symbol": SYMBOL, "side": "BUY", "lots": "1"})
            page = self.page(s)
            self.assertIn("&#10003; Recorded", page)
            self.assertIn("Virtual trade recorded", page)
            self.assertNotIn("Virtual trade recorded", self.page(s))                              # shown once
            s.post("/paper/trade", {"symbol": SYMBOL, "side": "BUY", "lots": "99"})
            page = self.page(s)
            self.assertIn("&#10007; Order refused (order rule", page)
            self.assertIn("More lots than the per-trade limit", page)
            self.assertIn('class="notice refused"', page)

    def test_positions_show_lots_the_current_bid_and_a_pnl_marked_with_a_symbol(self):
        with Served(self.app()) as s:
            s.post("/paper/trade", {"symbol": SYMBOL, "side": "BUY", "lots": "1"})
            page = self.page(s)
        row = next(r for r in SyntheticProvider()(self.clock()).chain if r.symbol == SYMBOL)
        positions = re.search(r"Open virtual positions</h2>.*?<tbody>(.*?)</tbody>", page, re.S).group(1)
        self.assertIn(f"<td class=\"l\">{SYMBOL}</td><td>65</td><td>1</td>", positions)
        self.assertIn(f"<td>{row.bid:,.2f}</td>", positions)
        self.assertRegex(positions, r"&#9660; -&#8377;[0-9,.]+")                                  # a loss carries an arrow and a sign, not just a colour

    def test_recent_activity_lists_the_latest_trades_first(self):
        with Served(self.app()) as s:
            s.post("/paper/trade", {"symbol": SYMBOL, "side": "BUY", "lots": "1"})
            s.post("/paper/trade", {"symbol": SYMBOL, "side": "SELL", "lots": "1"})
            page = self.page(s)
        recent = re.search(r"Recent activity</h2>.*?<tbody>(.*?)</tbody>", page, re.S).group(1)
        self.assertLess(recent.index(">SELL<"), recent.index(">BUY<"))
        self.assertIn("Simulated trade ledger (latest 20; full list at /api/ledger)", page)
        self.assertIn("Quote bid / ask", page)
        self.assertIn("Simulated fill", page)


class TestRiskCardArithmetic(Base):
    def numbers(self, page):
        value = re.search(r'<progress class="[a-z]*" max="([0-9.]+)" value="([0-9.]+)"', page)
        remaining = re.search(r"Remaining budget for new risk</span><span class=\"big\">&#8377;([0-9,.]+)<", page)
        committed = re.search(r"Committed worst case <strong>&#8377;([0-9,.]+)</strong> of &#8377;([0-9,.]+)", page)
        return value, remaining, committed

    def test_progress_bar_figures_and_trade_validation_use_the_same_backend_numbers(self):
        app = self.app()
        with Served(app) as s:
            value, remaining, committed = self.numbers(self.page(s))
            self.assertEqual((value.group(1), value.group(2), remaining.group(1)), ("20000.00", "0.00", "20,000.00"))                  # empty account: nothing committed
            s.post("/paper/trade", {"symbol": SYMBOL, "side": "BUY", "lots": "1"})
            page = self.page(s)
            value, remaining, committed = self.numbers(page)
            report = app.engine.risk_report()
            risk = json.loads(s.request("GET", "/api/ledger")[2])["risk"]
        budget, shown = Decimal(value.group(1)), Decimal(value.group(2))
        self.assertEqual((budget, shown), (report.budget, report.committed))                                                           # the bar IS the backend's figure
        self.assertEqual(Decimal(remaining.group(1).replace(",", "")), report.headroom)
        self.assertEqual(report.headroom, report.budget - report.committed)
        self.assertEqual((risk["budget"], risk["committed"], risk["headroom"]), (str(report.budget), str(report.committed), str(report.headroom)))
        self.assertEqual((committed.group(1), committed.group(2), remaining.group(1)), ("18,267.95", "20,000.00", "1,732.05"))
        # the same arithmetic decides the next trade: a second identical BUY would take the committed figure to twice the first
        with Served(app) as s:
            s.post("/paper/trade", {"symbol": SYMBOL, "side": "BUY", "lots": "1"})
            page = self.page(s)
        self.assertIn(f"projected worst-case daily loss {report.committed * 2:,.2f}", page)
        self.assertIn(f"budget of {report.budget:,.2f}", page)
        self.assertGreater(report.committed * 2, report.budget)

    def test_the_bar_never_exceeds_its_maximum_and_the_status_has_words(self):
        app = self.app()
        with Served(app) as s:
            s.post("/paper/trade", {"symbol": SYMBOL, "side": "BUY", "lots": "1"})
            page = self.page(s)
        value = re.search(r'<progress class="([a-z]*)" max="([0-9.]+)" value="([0-9.]+)"', page)
        self.assertLessEqual(Decimal(value.group(3)), Decimal(value.group(2)))
        self.assertEqual(value.group(1), "hot")                                                       # over 80% committed
        self.assertIn("OPEN: little room left", page)

    def test_the_card_does_not_promise_a_loss_cap_and_the_details_explain_the_limits(self):
        with Served(self.app()) as s:
            page = self.page(s)
        card = re.search(r'<section class="panel" aria-labelledby="risk-h">.*?</section>', page, re.S).group(0)
        self.assertIn("not a guarantee: losses can exceed the budget", card)
        self.assertNotIn("guarantees", text_of(card))
        details = re.search(r"<details><summary>Risk details and assumptions</summary>.*?</details>", page, re.S).group(0)
        for text in ("does not guarantee", "nothing is ever sold automatically", "Exit slippage allowance", "00:00 IST"):
            self.assertIn(text, details)


class TestResultMessages(Base):
    """The outcome of an order is stated by what happened to the order; the quote check is a separate, differently worded line."""
    def notice_of(self, page):
        m = re.search(r'<p class="notice ([a-z]*)" role="status"><span>(.*?)</span>(.*?)</p>', page, re.S)
        return (m.group(1), m.group(2), m.group(3)) if m else None

    def test_success_is_shown_only_for_a_recorded_trade(self):
        app = self.app()
        with Served(app) as s:
            s.post("/paper/trade", {"symbol": SYMBOL, "side": "BUY", "lots": "1"})
            page = self.page(s)
        kind, heading, message = self.notice_of(page)
        self.assertEqual((kind, heading), ("ok", "&#10003; Recorded: "))
        self.assertEqual(len(app.engine.ledger), 1)
        self.assertNotIn("virtual trades are allowed", page)

    def test_a_daily_risk_limit_refusal_says_so_and_is_not_followed_by_a_success_message(self):
        app = self.app()
        with Served(app) as s:
            s.post("/paper/trade", {"symbol": SYMBOL, "side": "BUY", "lots": "1"})
            self.page(s)
            s.post("/paper/trade", {"symbol": SYMBOL, "side": "BUY", "lots": "1"})
            page = self.page(s)
        kind, heading, message = self.notice_of(page)
        self.assertEqual((kind, heading), ("refused", "&#10007; Refused by the daily risk limit: "))
        self.assertIn("daily budget", message)
        self.assertIn("projected worst-case daily loss", message)
        self.assertEqual(len(app.engine.ledger), 1)                                                   # nothing was recorded
        for contradictory in ("virtual trades are allowed", "Recorded", "Prices passed the checks"):
            self.assertNotIn(contradictory, page)
        self.assertIn("This is not an order result: an order can still be refused by a risk or order rule", page)   # the quote line says what it covers

    def test_an_order_rule_refusal_is_told_apart_from_quote_eligibility(self):
        with Served(self.app()) as s:
            s.post("/paper/trade", {"symbol": SYMBOL, "side": "BUY", "lots": "99"})
            page = self.page(s)
        kind, heading, message = self.notice_of(page)
        self.assertEqual(kind, "refused")
        self.assertIn("Order refused (order rule; the quotes were not the problem)", heading)
        self.assertIn("More lots than the per-trade limit", message)
        self.assertIn("Quote status:", page)

    def test_a_quote_or_data_rejection_is_labelled_as_such(self):
        app = self.app()
        with Served(app) as s:
            s.post("/paper/trade", {"symbol": "NOT-AN-INSTRUMENT", "side": "BUY", "lots": "1"})
            page = self.page(s)
        kind, heading, message = self.notice_of(page)
        self.assertEqual((kind, heading), ("refused", "&#10007; Refused: quote or data check failed: "))
        self.assertIn("not in the current market data", message)
        self.assertEqual(app.engine.ledger, [])

    def test_a_refusal_for_unverified_data_shows_no_success_copy(self):
        clock = Clock()
        app = make_app(clock, mode="provider", real=lambda: fyers_provider(clock, TransportError("TIMEOUT"))[0])
        app.handle_trade({"symbol": "X", "side": "BUY", "lots": "1"})
        with Served(app) as s:
            page = self.page(s)
        self.assertNotIn("virtual trades are allowed", page)
        self.assertNotIn("Recorded", page)
        self.assertIn("Virtual trading is blocked", page)

    def test_the_notice_is_shown_once(self):
        with Served(self.app()) as s:
            s.post("/paper/trade", {"symbol": SYMBOL, "side": "BUY", "lots": "99"})
            self.assertIn("Order refused", self.page(s))
            self.assertNotIn("Order refused", self.page(s))


class TestLossCountedSoFar(Base):
    def test_the_label_and_the_figure_reconcile_with_the_backend(self):
        from niftypaper.dashboard.trading import build_views
        app = self.app()
        with Served(app) as s:
            s.post("/paper/trade", {"symbol": SYMBOL, "side": "BUY", "lots": "1"})
            page = self.page(s)
            risk = json.loads(s.request("GET", "/api/ledger")[2])["risk"]
        health, snapshot, _ = app.view()
        report = app.engine.risk_report(build_views(health, snapshot, True))
        card = re.search(r'<section class="panel" aria-labelledby="risk-h">.*?</section>', page, re.S).group(0)
        self.assertIn("Loss counted so far (estimate, not realised)", card)
        self.assertNotIn("Loss incurred so far today", page)
        self.assertIn(f"&#8377;{report.incurred:,.2f}", card)
        # the backend's definition, spelled out: -realised today + marked loss on open positions (losses only, with the exit allowance) + buy fees on open positions
        self.assertEqual(report.incurred, -report.realized_today + report.unrealized_loss + report.open_buy_fees)
        self.assertEqual((report.unrealized_loss, report.open_buy_fees, report.incurred), (Decimal("22.75"), Decimal("29.10"), Decimal("51.85")))
        self.assertEqual(risk["incurred"], "51.85")
        self.assertEqual(report.exit_slippage_allowance, Decimal("6.50"))                              # 2 ticks x 0.05 x 65 units, inside the 22.75
        details = re.search(r"<details><summary>Risk details and assumptions</summary>.*?</details>", page, re.S).group(0)
        self.assertIn("&#8377;51.85 = realised loss today (&#8377;0.00) + marked loss on open positions (&#8377;22.75, including the exit-slippage allowance, gains never offset) "
                      "+ buy fees on open positions (&#8377;29.10)", details)
        self.assertIn("It is not a realised loss", card)

    def test_with_no_open_positions_the_figure_is_just_the_realised_loss(self):
        app = self.app()
        app.handle_trade({"symbol": SYMBOL, "side": "BUY", "lots": "1"})
        app.handle_trade({"symbol": SYMBOL, "side": "SELL", "lots": "1"})                              # a round trip at the touch: a small realised loss
        report = app.engine.risk_report()
        self.assertEqual((report.unrealized_loss, report.open_buy_fees), (Decimal("0"), Decimal("0")))
        self.assertEqual(report.incurred, -report.realized_today)
        self.assertGreater(report.incurred, 0)


class TestCollapsedByDefault(Base):
    def test_every_details_panel_starts_closed_and_the_main_view_holds_the_essentials(self):
        with Served(self.app()) as s:
            page = self.page(s)
        tags = [a for t, a in parse(page).found if t == "details"]
        self.assertGreaterEqual(len(tags), 5)
        self.assertTrue(all("open" not in a for a in tags))
        main = page.split("<body>")[1]                                                              # the top bar and the main view
        visible = main
        for panel in re.findall(r"<details>.*?</details>", main, re.S):
            visible = visible.replace(panel, "")
        text = __import__("html").unescape(text_of(visible))
        for essential in ("Mode: Synthetic", "Virtual cash", "Realised P&L", "Unrealised P&L", "Daily risk budget", "Paper-trading workspace", "Open virtual positions",
                          "Sample option chain", "Recent activity"):
            self.assertIn(essential, text)
        for technical in ("Setup steps", "Exit slippage allowance", "UNVERIFIED is well-formed", "Subscription:", "FYERS_APP_ID"):
            self.assertNotIn(technical, text)


class TestRestoredCount(Base):
    def test_restored_and_current_counts_are_shown_apart(self):
        first = self.app()
        first.handle_trade({"symbol": SYMBOL, "side": "BUY", "lots": "1"})
        first.close()
        second = self.app()
        second.handle_trade({"symbol": SYMBOL, "side": "SELL", "lots": "1"})
        with Served(second) as s:
            page = self.page(s)
        self.assertIn("Trades restored when the session opened</dt><dd>1 (counted once, when the saved file was read; not the current count)", page)
        self.assertIn("Trades in this account now</dt><dd>2</dd>", page)


class TestRealTimeFailsClosed(unittest.TestCase):
    def provider_page(self, make):
        with Served(make) as s:
            return s.request("GET", "/")[2]

    def test_unavailable_provider_shows_a_short_reason_no_prices_and_no_trade_form(self):
        clock = Clock()
        app = make_app(clock, mode="provider", real=lambda: fyers_provider(clock, TransportError("TIMEOUT"))[0])
        page = self.provider_page(app)
        text = text_of(page)
        self.assertIn("UNAVAILABLE", text)
        self.assertIn("Virtual trading is blocked", text)
        self.assertIn("choose Synthetic Mode (Free) in the top bar", text)
        self.assertNotIn('action="/paper/trade"', page)
        self.assertNotIn("SYN-", page)
        self.assertNotIn("SIMULATED DATA", page)
        self.assertNotIn('class="num"', page)

    def test_the_fyers_steps_are_written_out_complete_and_hold_no_secret(self):
        app = make_app(Clock(), env=dict(ENV))
        with Served(app) as s:
            page = s.request("GET", "/")[2]
        steps = re.search(r'<ol class="steps">(.*?)</ol>', page, re.S).group(1)
        items = re.findall(r"<li>(.*?)</li>", steps, re.S)
        self.assertEqual(len(items), 5)
        self.assertTrue(all(len(i.strip()) > 60 for i in items))
        joined = " ".join(items)
        for needle in ("docs/provider_terms/FYERS_TERMS_AND_CONFIRMATION.md", "FYERS_APP_ID", "FYERS_ACCESS_TOKEN", "config/provider_permissions.example.json",
                       "config/local/provider_permissions.json", "virtual_trading_permitted", "Synthetic Mode (Free)", "UNVERIFIED", "BEFORE starting it"):
            self.assertIn(needle, joined)
        self.assertNotIn(SECRET, page)
        self.assertNotIn(APP_ID, page)

    def test_switching_to_real_time_keeps_the_mode_bar_and_a_way_back(self):
        app = make_app(Clock(), env=dict(ENV))
        with Served(app) as s:
            s.post("/mode", {"mode": "realtime", "provider": "fyers"})
            page = s.request("GET", "/")[2]
        self.assertIn("Mode: Real-Time", text_of(page))
        self.assertIn("Switched to Real-Time Mode with fyers", page)
        self.assertIn('class="notice "', page)
        self.assertIn("Virtual trading is blocked", page)
        self.assertEqual(page.count('aria-pressed="true"'), 1)
        self.assertNotIn("SIMULATED DATA", page)


if __name__ == "__main__":
    unittest.main()


class TestPolishPass(Base):
    def test_the_estimate_that_counts_against_the_budget_matches_the_engines_refusal_amount(self):
        app = self.app()
        with Served(app) as s:
            before = self.page(s)
            s.post("/paper/trade", {"symbol": SYMBOL, "side": "BUY", "lots": "1"})
            self.page(s)
            s.post("/paper/trade", {"symbol": SYMBOL, "side": "BUY", "lots": "1"})
            page = self.page(s)
        shown = re.search(rf'<td class="l">{SYMBOL}</td><td>65</td><td>([0-9,.]+)</td><td>([0-9,.]+)</td><td>([0-9,.]+)</td><td>([0-9,.]+)</td>', before).groups()
        premium, fee, risk_added = (Decimal(shown[0].replace(",", "")), Decimal(shown[1].replace(",", "")), Decimal(shown[3].replace(",", "")))
        self.assertEqual(risk_added, premium + fee + fee)                                          # order value + buy fee + estimated exit fee (one lot)
        refused = re.search(r"this trade adds ([0-9,.]+)\)", page).group(1)
        self.assertEqual(Decimal(refused.replace(",", "")), risk_added)                             # the same amount the daily risk check used for the submitted request
        self.assertIn("Counts against risk budget", before)

    def test_the_risk_bar_has_a_percentage_line_taken_from_the_backend_figures(self):
        app = self.app()
        with Served(app) as s:
            empty = self.page(s)
            s.post("/paper/trade", {"symbol": SYMBOL, "side": "BUY", "lots": "1"})
            page = self.page(s)
        self.assertIn("0% of the daily budget committed", empty)
        report = app.engine.risk_report()
        self.assertIn(f"{report.committed / report.budget * 100:.0f}% of the daily budget committed", page)

    def test_empty_states_say_what_to_do_next(self):
        with Served(self.app()) as s:
            page = self.page(s)
        self.assertIn("No open positions yet. Pick an instrument and press Simulate virtual trade.", page)
        self.assertIn("No activity yet: no virtual trades yet.", page)

    def test_buy_and_sell_stay_labelled_in_words_whatever_their_colour(self):
        with Served(self.app()) as s:
            page = self.page(s)
        self.assertIn('<label for="side-buy">BUY</label>', page)
        self.assertIn('<label for="side-sell">SELL</label>', page)
        self.assertIn("#side-buy:checked+label", page)
        self.assertIn("#side-sell:checked+label", page)

    def test_the_account_cards_carry_a_tone_only_alongside_a_sign_and_an_arrow(self):
        app = self.app()
        with Served(app) as s:
            s.post("/paper/trade", {"symbol": SYMBOL, "side": "BUY", "lots": "1"})
            page = self.page(s)
        self.assertIn("card tone-neg", page)
        self.assertRegex(page, r"&#9660; -&#8377;16\.25")
