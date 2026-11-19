"""V6-0.4 hardening tests: independent accounting checks, interrupted and repeated replays, saved-run handling, duplicate submission, restart and persistence, dashboard error
handling, and the separation of the research models and the synthetic demo from the research evaluation window. Invented data in temporary folders only."""
import json
import re
import tempfile
import unittest
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from unittest import mock

from niftypaper.dashboard.ml_page import render_ml_page, resolve
from niftypaper.dashboard.render import BANNER
from niftypaper.dashboard.sample_data import SyntheticProvider
from niftypaper.dashboard.server import App
from niftypaper.historical import ingest as ingest_module
from niftypaper.historical import manifest as mf
from niftypaper.historical.gate import PointInTimeView, ReplayClock
from niftypaper.ml import fixtures as fx, predict as pred, runs, session
from niftypaper.ml.models import build_registry
from niftypaper.paper.engine import ORIGIN_SYNTHETIC, PaperEngine
from niftypaper.paper.store import JsonlStore
from tests.test_modes import Served

NO_PARAMS = Path(tempfile.gettempdir()) / "niftypaper_no_such_params_dir"
SYMBOL = "SYN-CE-23800"
CENT = Decimal("0.01")


def synthetic_app(state_dir, ml_dir=None):
    """The real App with a synthetic provider and a saved virtual account in `state_dir` (always a temporary folder, never data/paper_state)."""
    balance = Decimal(1_000_000)
    return App(SyntheticProvider(), mode="synthetic", provider_id="synthetic", factories={"synthetic": lambda: SyntheticProvider()},
               engine_factory=lambda key: PaperEngine(balance, origin=ORIGIN_SYNTHETIC, store=JsonlStore(Path(state_dir) / f"paper_{key}.jsonl", balance, account=key)), ml_dir=ml_dir)


class Replayed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "runs"
        cls.out = cls.root / "run1"
        cls.summary = session.run_replay(cls.out, "demo_synthetic_ur1", 40, NO_PARAMS)
        cls.decisions = [json.loads(x) for x in (cls.out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class TestAccounting(Replayed):
    """Recomputed here from the saved fills with plain Decimal arithmetic, independently of the engine's own bookkeeping."""

    def test_cash_equals_start_minus_buys_plus_sells_less_every_fee(self):
        cash = Decimal(self.summary["health"]["starting_balance"])
        for f in self.summary["fills"]:
            turnover, cost = Decimal(f["turnover"]), Decimal(f["cost"])
            cash += (-turnover - cost) if f["side"] == "BUY" else (turnover - cost)
        self.assertEqual(cash.quantize(CENT), Decimal(self.summary["health"]["final_cash"]).quantize(CENT))

    def test_every_fee_follows_the_stated_cost_model_and_every_price_the_touch_plus_slippage(self):
        sim = self.summary["simulator"]
        for f in self.summary["fills"]:
            turnover = Decimal(f["fill_price"]) * f["quantity"]
            self.assertEqual(Decimal(f["turnover"]), turnover)
            self.assertEqual(Decimal(f["cost"]), (Decimal(sim["fee_per_trade"]) + turnover * Decimal(sim["turnover_fraction"])).quantize(CENT, ROUND_HALF_UP))
            slip = Decimal(sim["tick_size"]) * sim["slippage_ticks"]
            expected = Decimal(f["quote_ask"]) + slip if f["side"] == "BUY" else Decimal(f["quote_bid"]) - slip
            self.assertEqual(Decimal(f["fill_price"]), expected)
            self.assertEqual(f["quantity"] % sim["lot_size"], 0)
            self.assertLessEqual(f["quantity"] // sim["lot_size"], sim["max_lots_per_trade"])

    def test_when_the_account_ends_flat_cash_change_equals_the_sum_of_realised_pnl(self):
        self.assertEqual(self.summary["health"]["open_positions"], {})
        realised = sum((Decimal(f["realized_pnl"]) for f in self.summary["fills"] if f["side"] == "SELL"), Decimal(0))
        change = Decimal(self.summary["health"]["final_cash"]) - Decimal(self.summary["health"]["starting_balance"])
        self.assertEqual(change.quantize(CENT), realised.quantize(CENT))

    def test_positions_are_the_net_of_the_fills_and_never_negative(self):
        held = {}
        for f in self.summary["fills"]:
            held[f["symbol"]] = held.get(f["symbol"], 0) + (f["quantity"] if f["side"] == "BUY" else -f["quantity"])
            self.assertGreaterEqual(held[f["symbol"]], 0)
        self.assertEqual({k: v for k, v in held.items() if v}, self.summary["health"]["open_positions"])

    def test_a_refused_attempt_never_appears_as_a_fill(self):
        refused = [d for d in self.decisions if (d["engine_result"] or {}).get("code")]
        self.assertTrue(refused)
        fill_times = [f["engine_time"] for f in self.summary["fills"]]
        for d in refused:
            self.assertNotIn(d["decision_time"], fill_times)
            self.assertNotEqual(d["engine_result"].get("event"), "FILL")
        accepted = [d for d in self.decisions if (d["engine_result"] or {}).get("event") == "FILL"]
        self.assertEqual(len(accepted), len(self.summary["fills"]))

    def test_the_engine_limits_are_never_exceeded_by_any_accepted_trade(self):
        sim = self.summary["simulator"]
        held = {}
        for f in self.summary["fills"]:
            held[f["symbol"]] = held.get(f["symbol"], 0) + (f["quantity"] if f["side"] == "BUY" else -f["quantity"])
            if f["side"] == "BUY":
                self.assertLessEqual(len([v for v in held.values() if v]), sim["max_open_positions"])
                self.assertLessEqual(Decimal(f["turnover"]), Decimal(sim["starting_balance"]) * Decimal(sim["max_exposure_fraction"]))


class TestInterruptedAndRepeatedRuns(unittest.TestCase):
    def test_a_failed_replay_leaves_no_result_and_blocks_the_next_run_until_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run1"
            with mock.patch.object(session, "run_strategy", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    session.run_replay(out, "demo_synthetic_ur1", 6, NO_PARAMS)
            self.assertFalse(out.exists())
            partial = Path(tmp) / "run1.partial"
            self.assertTrue(partial.exists())
            self.assertFalse((partial / "summary.json").exists())                  # summary.json is written last: a partial folder never looks finished
            with self.assertRaises(session.SessionError) as caught:
                session.run_replay(out, "demo_synthetic_ur1", 6, NO_PARAMS)
            self.assertEqual(caught.exception.code, "PARTIAL_EXISTS")
            self.assertEqual(runs.list_runs(tmp), [])                              # a .partial folder is not a run
            self.assertEqual(runs.next_run_dir(tmp).name, "run2")                   # and its name is not reused

    def test_a_keyboard_interrupt_during_the_replay_also_saves_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(session, "run_strategy", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    session.run_replay(Path(tmp) / "run1", "demo_synthetic_ur1", 6, NO_PARAMS)
            self.assertFalse((Path(tmp) / "run1").exists())

    def test_bad_day_counts_are_refused_before_anything_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            for bad in (0, 4, 251, -3, True, "40", 7.5):
                with self.subTest(days=bad):
                    with self.assertRaises(session.SessionError) as caught:
                        session.run_replay(Path(tmp) / "r", "demo_synthetic_ur1", bad, NO_PARAMS)
                    self.assertEqual(caught.exception.code, "BAD_DAYS")
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_runs_accumulate_are_listed_in_order_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = session.run_replay(runs.next_run_dir(tmp), "demo_synthetic_ur1", 8, NO_PARAMS)
            before = (Path(tmp) / "run1" / "summary.json").read_bytes()
            session.run_replay(runs.next_run_dir(tmp), "b0_training_prior", 8, NO_PARAMS)
            found = runs.list_runs(tmp)
            self.assertEqual([r["id"] for r in found], ["run1", "run2"])
            self.assertEqual([r["summary"]["model_id"] for r in found], ["demo_synthetic_ur1", "b0_training_prior"])
            self.assertEqual((Path(tmp) / "run1" / "summary.json").read_bytes(), before)
            self.assertIn("run1: model demo_synthetic_ur1", runs.describe(found[0]))
            self.assertEqual(first["decisions"], found[0]["summary"]["decisions"])
            (Path(tmp) / "notes").mkdir()
            (Path(tmp) / "run7x").mkdir()
            self.assertEqual([r["id"] for r in runs.list_runs(tmp)], ["run1", "run2"])      # folders with other names are not runs

    def test_a_summary_not_labelled_as_invented_data_is_not_shown(self):
        with tempfile.TemporaryDirectory() as tmp:
            session.run_replay(Path(tmp) / "run1", "demo_synthetic_ur1", 6, NO_PARAMS)
            path = Path(tmp) / "run1" / "summary.json"
            body = json.loads(path.read_text(encoding="utf-8"))
            for change in ({"data_origin": "PROVIDER"}, {"provider_connected": True}):
                body["health"].update(change)
                path.write_text(json.dumps(body), encoding="utf-8")
                status, page = render_ml_page(tmp)
                self.assertIn("not labelled as invented fixture data", page)
                self.assertNotIn("Predictions and decisions", page)


class TestMlPageErrors(Replayed):
    def test_unknown_runs_are_404_and_runs_are_selectable(self):
        app = synthetic_app(self.tmp.name + "/state", self.root)
        with Served(app) as s:
            self.assertEqual(s.request("GET", "/ml?run=run1")[0], 200)
            status, _, body = s.request("GET", "/ml?run=run9")
            self.assertEqual(status, 404)
            self.assertIn("There is no replay named run9", body)
            self.assertEqual(s.request("GET", "/api/ml?run=run9")[0], 404)
            status, _, body = s.request("GET", "/ml?run=..%2F..%2Fetc")                 # not a run id: ignored, the newest run is shown
            self.assertEqual(status, 200)
            self.assertIn("replay run1", body)
        app.close()

    def test_the_ml_routes_are_read_only_and_host_checked(self):
        app = synthetic_app(self.tmp.name + "/state2", self.root)
        with Served(app) as s:
            for method in ("POST", "PUT", "DELETE", "PATCH"):
                self.assertEqual(s.request(method, "/ml")[0], 405)
            self.assertEqual(s.request("GET", "/ml", headers={"Host": "evil.example"})[0], 403)
            status, headers, _ = s.request("GET", "/ml")
            self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
            self.assertIn("script-src 'sha256-", headers["Content-Security-Policy"])
        app.close()

    def test_damaged_or_missing_files_give_a_message_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in (("summary.json", "{broken"), ("decisions.jsonl", "")):
                (Path(tmp) / name).write_text(text, encoding="utf-8")
            self.assertIn("could not be read", render_ml_page(tmp)[1])
            (Path(tmp) / "summary.json").write_text("[]", encoding="utf-8")
            self.assertIn("not in the expected format", render_ml_page(tmp)[1])
            self.assertEqual(resolve(Path(tmp) / "absent")[4], "no replay has been run yet")

    def test_data_kinds_cannot_be_confused_between_the_pages(self):
        ml = render_ml_page(self.root)[1]
        self.assertIn("HISTORICAL REPLAY OF INVENTED FIXTURE DATA", ml)
        for state_class in ("state-LIVE", "state-DELAYED", "state-SIMULATED"):
            self.assertNotIn(f'class="{state_class}', ml)                                        # no live, delayed or simulated-quote status chip on a replay page
        self.assertNotIn(BANNER, ml)
        app = synthetic_app(self.tmp.name + "/state3", self.root)
        with Served(app) as s:
            main = s.request("GET", "/")[2]
        app.close()
        self.assertIn(BANNER, main)
        self.assertNotIn("HISTORICAL REPLAY OF INVENTED", main)
        self.assertNotIn("P(up)", main)                                              # the main page shows no model predictions


class TestOlderAndBrokenRuns(Replayed):
    def test_a_run_saved_by_another_format_is_listed_not_shown_and_does_not_crash_the_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "run1"
            old.mkdir()
            body = dict(self.summary)
            body["summary_version"] = 1
            body.pop("simulator")
            (old / "summary.json").write_text(json.dumps(body), encoding="utf-8")
            (old / "decisions.jsonl").write_text("", encoding="utf-8")
            status, page = render_ml_page(tmp)
            self.assertEqual(status, 200)
            self.assertIn("another version of the program", page)
            self.assertNotIn("Predictions and decisions", page)
            session.run_replay(runs.next_run_dir(tmp), "demo_synthetic_ur1", 6, NO_PARAMS)
            self.assertIn("Predictions and decisions", render_ml_page(tmp)[1])             # the newest readable run is shown by default
            self.assertEqual(runs.next_run_dir(tmp).name, "run3")

    def test_a_missing_required_field_is_a_message_not_an_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = dict(self.summary)
            body.pop("refused")
            (Path(tmp) / "summary.json").write_text(json.dumps(body), encoding="utf-8")
            (Path(tmp) / "decisions.jsonl").write_text("", encoding="utf-8")
            self.assertIn("not in the expected format", render_ml_page(tmp)[1])

    def test_an_unexpected_failure_while_building_a_page_is_a_500_with_no_details(self):
        app = synthetic_app(self.tmp.name + "/state9", self.root)
        with mock.patch("niftypaper.dashboard.server.render_ml_page", side_effect=RuntimeError("secret detail C:/path")):
            with Served(app) as s:
                status, _, body = s.request("GET", "/ml")
                self.assertEqual(status, 500)
                self.assertIn("internal error", body)
                self.assertNotIn("secret detail", body)
                self.assertEqual(s.request("GET", "/api/snapshot")[0], 200)              # the server keeps serving
        app.close()


class TestDuplicateSubmissionAndRestart(unittest.TestCase):
    def ledger(self, s):
        return json.loads(s.request("GET", "/api/ledger")[2])["trades"]

    def test_the_same_form_submitted_twice_trades_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = synthetic_app(tmp)
            with Served(app) as s:
                nonce = re.search(r'name="nonce" value="([0-9a-f]+)"', s.request("GET", "/")[2]).group(1)
                form = {"symbol": SYMBOL, "side": "BUY", "lots": "1", "nonce": nonce}
                s.post("/paper/trade", form)
                self.assertEqual(len(self.ledger(s)), 1)
                s.post("/paper/trade", form)                                          # a double click, a back-button resubmit
                self.assertEqual(len(self.ledger(s)), 1)
            app.close()

    def test_the_duplicate_is_reported_to_the_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = synthetic_app(tmp)
            with Served(app) as s:
                nonce = re.search(r'name="nonce" value="([0-9a-f]+)"', s.request("GET", "/")[2]).group(1)
                form = {"symbol": SYMBOL, "side": "BUY", "lots": "1", "nonce": nonce}
                s.post("/paper/trade", form)
                s.request("GET", "/")                                                  # consume the first notice
                s.post("/paper/trade", form)
                self.assertIn("Not submitted: this form was already used", s.request("GET", "/")[2])
            app.close()

    def test_a_made_up_form_id_is_refused_and_two_page_loads_have_two_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = synthetic_app(tmp)
            with Served(app) as s:
                a = re.search(r'name="nonce" value="([0-9a-f]+)"', s.request("GET", "/")[2]).group(1)
                b = re.search(r'name="nonce" value="([0-9a-f]+)"', s.request("GET", "/")[2]).group(1)
                self.assertNotEqual(a, b)
                s.post("/paper/trade", {"symbol": SYMBOL, "side": "BUY", "lots": "1", "nonce": "0" * 24})
                self.assertEqual(self.ledger(s), [])
                s.post("/paper/trade", {"symbol": SYMBOL, "side": "BUY", "lots": "1", "nonce": a})
                self.assertEqual(len(self.ledger(s)), 1)
            app.close()

    def test_a_restart_keeps_cash_positions_and_the_ledger_and_does_not_replay_trades(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = synthetic_app(tmp)
            with Served(app) as s:
                s.post("/paper/trade", {"symbol": SYMBOL, "side": "BUY", "lots": "1"})
                s.post("/paper/trade", {"symbol": SYMBOL, "side": "SELL", "lots": "1"})
                s.post("/paper/trade", {"symbol": SYMBOL, "side": "BUY", "lots": "1"})
                before = app.engine.summary()
                held = {k: (p.quantity, p.basis, p.cost_paid) for k, p in app.engine.positions.items()}
                ledger_before = self.ledger(s)
            app.close()
            again = synthetic_app(tmp)
            engine = again.engine
            after = engine.summary()
            self.assertEqual((after["cash"], after["realized_pnl"], after["trades"], after["restored_trades"]), (before["cash"], before["realized_pnl"], 3, 3))
            self.assertEqual({k: (p.quantity, p.basis, p.cost_paid) for k, p in engine.positions.items()}, held)
            with Served(again) as s:
                self.assertEqual(self.ledger(s), ledger_before)
                self.assertEqual(len(self.ledger(s)), 3)
            again.close()

    def test_a_damaged_saved_account_locks_trading_instead_of_starting_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = synthetic_app(tmp)
            with Served(app) as s:
                s.post("/paper/trade", {"symbol": SYMBOL, "side": "BUY", "lots": "1"})
            app.close()
            path = Path(tmp) / "paper_synthetic.jsonl"
            path.write_text(path.read_text(encoding="utf-8")[:-40], encoding="utf-8")          # cut the last record in half
            broken = synthetic_app(tmp)
            self.assertTrue(broken.engine.lock_reason)
            with Served(broken) as s:
                s.post("/paper/trade", {"symbol": SYMBOL, "side": "BUY", "lots": "1"})
                self.assertEqual(self.ledger(s), [])
            broken.close()


class TestSeparationFromResearchData(Replayed):
    def test_the_fixtures_are_dated_2031_and_never_touch_the_research_evaluation_window(self):
        for path in (self.out / "fixtures").glob("SYNTHETIC_*.jsonl"):
            dates = set(re.findall(r"\d{4}-\d{2}-\d{2}", path.read_text(encoding="utf-8")))
            self.assertTrue(dates)
            self.assertTrue(all(d.startswith("2031-") for d in dates), path.name)
        manifest = (self.out / "fixtures" / mf.MANIFEST_NAME).read_text(encoding="utf-8")
        self.assertIn("SYNTHETIC_FIXTURE", manifest)
        self.assertNotIn("nse", manifest.lower().replace("invented", ""))

    def test_the_ml_package_reads_no_research_or_market_files(self):
        src = Path(session.__file__).parent
        for path in src.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for banned in ("Desktop", "..\\\\ML", "Bhavcopy.csv", "nsearchives", "read_csv", "import pandas", "import numpy"):
                self.assertNotIn(banned, text, f"{path.name}: {banned}")

    def test_the_demo_model_is_refused_on_data_of_any_other_origin_and_research_models_never_trade(self):
        registry = build_registry(NO_PARAMS)
        series = fx.series(8)
        with tempfile.TemporaryDirectory() as tmp:
            folder = fx.write_fixture_set(Path(tmp) / "f", series)
            entries = mf.load_manifest(folder / mf.MANIFEST_NAME)
            records = ingest_module.ingest(folder, entries, as_of=fx.at(series[-1].day, 23, 0)).records
            from dataclasses import replace
            foreign = [replace(r, origin_class="UNVERIFIED_SOURCE") for r in records]
            view = PointInTimeView(foreign, ReplayClock(fx.decision_time(series[5].day)))
            p = pred.predict(registry["demo_synthetic_ur1"], view, fx.CE)
            self.assertEqual(p.status, pred.DISPLAY_ONLY)
            self.assertIn("ORIGIN_NOT_ALLOWED", [code for code, _ in p.reasons])
        for model_id, model in registry.items():
            if model_id != "demo_synthetic_ur1":
                self.assertEqual(model.card.standing, "NONE", model_id)
        self.assertEqual(sorted(m for m, v in registry.items() if v.card.standing != "NONE"), ["demo_synthetic_ur1"])


if __name__ == "__main__":
    unittest.main()
