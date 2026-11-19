"""ML-to-paper-trading integration (V6-0.3): feature availability, chronological processing, leakage, missing data, the decision gates, risk refusals,
persistence, reproducibility and the /ml page. Everything runs on INVENTED fixture data in temporary folders; no provider, no network, no market data."""
import json
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from niftypaper.dashboard.ml_page import load_replay, render_ml_page
from niftypaper.dashboard.server import App
from niftypaper.historical import ingest as ingest_module
from niftypaper.historical import manifest as mf
from niftypaper.historical.gate import PointInTimeView, ReplayClock
from niftypaper.ml import features, fixtures as fx, predict as pred, session
from niftypaper.ml.models import PARAMS_SCHEMA, Params, build_registry, load_params_file
from niftypaper.ml.strategy import BUY, NO_TRADE, SELL
from tests.test_modes import Served

DAYS = 40
NO_PARAMS = Path(tempfile.gettempdir()) / "niftypaper_no_such_params_dir"


def view_for(folder, series, index):
    """A point-in-time view at the 10:15:02 decision time of day `index`, over the records ingested from `folder`."""
    entries = mf.load_manifest(Path(folder) / mf.MANIFEST_NAME)
    records = ingest_module.ingest(Path(folder), entries, as_of=fx.at(series[-1].day + timedelta(days=1), 12, 0)).records
    return PointInTimeView(records, ReplayClock(fx.decision_time(series[index].day)))


class Replayed(unittest.TestCase):
    """One demonstration replay shared by the tests that only read it."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / "run"
        cls.summary = session.run_replay(cls.out, "demo_synthetic_ur1", DAYS, NO_PARAMS)
        cls.decisions = [json.loads(line) for line in (cls.out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class TestFeatures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.series = fx.series(12)
        cls.folder = fx.write_fixture_set(Path(cls.tmp.name) / "f", cls.series)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_ur1_uses_the_two_newest_closes_available_before_the_decision(self):
        feature = features.underlying_return(view_for(self.folder, self.series, 5))
        self.assertEqual(feature.code, "OK")
        expected = float(self.series[4].close / self.series[3].close - 1)       # closes of days 4 and 3: today's (day 5) close is not yet available
        self.assertAlmostEqual(feature.value, expected, places=12)
        self.assertEqual([i[0] for i in feature.inputs], [self.series[3].day.isoformat(), self.series[4].day.isoformat()])
        self.assertLessEqual(feature.observed_through, fx.decision_time(self.series[5].day))

    def test_lr1_uses_contract_settlements(self):
        feature = features.settlement_log_return(view_for(self.folder, self.series, 6), fx.CE)
        import math
        self.assertAlmostEqual(feature.value, math.log(self.series[5].settle_ce / self.series[4].settle_ce), places=12)

    def test_insufficient_history_is_reported_not_filled(self):
        for index in (0, 1):                                    # day 0 sees no close, day 1 sees one
            feature = features.underlying_return(view_for(self.folder, self.series, index))
            self.assertIsNone(feature.value)
            self.assertEqual(feature.code, "INSUFFICIENT_HISTORY")
        self.assertIsNotNone(features.underlying_return(view_for(self.folder, self.series, 2)).value)

    def test_a_stale_series_is_not_used(self):
        view = view_for(self.folder, self.series, 11)
        late = PointInTimeView(view.visible(), ReplayClock(view.now + timedelta(days=20)))
        self.assertEqual(features.underlying_return(late).code, "STALE_INPUT")

    def test_settlement_below_the_research_price_floor_is_not_used(self):
        low = [fx.SeriesDay(d.day, d.close, Decimal("0.50") if i == 4 else d.settle_ce, d.settle_pe, d.quote_mid_ce, d.quote_mid_pe) for i, d in enumerate(self.series)]
        with tempfile.TemporaryDirectory() as tmp:
            folder = fx.write_fixture_set(Path(tmp) / "f", low)
            feature = features.settlement_log_return(view_for(folder, low, 5), fx.CE)
        self.assertEqual((feature.value, feature.code), (None, "BELOW_PRICE_FLOOR"))


class TestLeakage(Replayed):
    def test_changing_the_future_changes_no_earlier_prediction(self):
        series = fx.series(15)
        altered = series[:8] + [fx.SeriesDay(d.day, d.close * 3, d.settle_ce + 400, d.settle_pe + 400, d.quote_mid_ce + 400, d.quote_mid_pe + 400) for d in series[8:]]
        registry = build_registry(NO_PARAMS)
        with tempfile.TemporaryDirectory() as tmp:
            a, b = fx.write_fixture_set(Path(tmp) / "a", series), fx.write_fixture_set(Path(tmp) / "b", altered)
            for index in range(8):                              # every decision up to and including day 7 must be identical
                for contract in (fx.CE, fx.PE):
                    one = pred.predict(registry["demo_synthetic_ur1"], view_for(a, series, index), contract).to_dict()
                    two = pred.predict(registry["demo_synthetic_ur1"], view_for(b, altered, index), contract).to_dict()
                    self.assertEqual(one, two, f"day {index} {contract}")

    def test_a_shorter_replay_reproduces_the_first_decisions_of_a_longer_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            session.run_replay(Path(tmp) / "short", "demo_synthetic_ur1", 20, NO_PARAMS)
            short = [json.loads(line) for line in (Path(tmp) / "short" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(short, self.decisions[:20])

    def test_every_prediction_rests_on_data_available_before_its_decision_time(self):
        for d in self.decisions:
            for p in d["inputs"]["predictions"].values():
                if p["data_observed_through"] is not None:
                    self.assertLessEqual(p["data_observed_through"], d["decision_time"])
                    self.assertLess(max(i[0] for i in p["provenance"]["inputs"]), d["decision_time"][:10])     # input trading dates are strictly earlier days


class TestChronology(Replayed):
    def test_decisions_are_in_strictly_increasing_time(self):
        times = [d["decision_time"] for d in self.decisions]
        self.assertEqual(times, sorted(set(times)))
        self.assertEqual(len(times), DAYS)

    def test_equity_history_follows_the_decision_times(self):
        self.assertEqual([e["at"] for e in self.summary["equity"]], [d["decision_time"] for d in self.decisions])

    def test_fills_are_in_time_and_sequence_order(self):
        fills = self.summary["fills"]
        self.assertEqual([f["seq"] for f in fills], list(range(1, len(fills) + 1)))
        self.assertEqual([f["engine_time"] for f in fills], sorted(f["engine_time"] for f in fills))

    def test_a_scoring_label_is_never_an_input_of_a_decision(self):
        text = json.dumps([d["inputs"] for d in self.decisions])
        self.assertNotIn("next_up", text)
        self.assertNotIn("settle_", text)


class TestMissingData(Replayed):
    def test_the_first_two_days_have_no_prediction_and_no_trade(self):
        for d in self.decisions[:2]:
            self.assertEqual((d["label"], d["reason_code"]), (NO_TRADE, "PREDICTION_UNAVAILABLE"))
            self.assertIn("INSUFFICIENT_HISTORY", d["explanation"])
            self.assertTrue(all(p["probability"] is None for p in d["inputs"]["predictions"].values()))

    def test_a_research_model_without_parameters_predicts_nothing_and_never_trades(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = session.run_replay(Path(tmp) / "r", "b2_ur1_logistic", 12, NO_PARAMS)
            self.assertEqual(summary["decisions"], {"BUY": 0, "SELL": 0, "NO TRADE": 12})
            self.assertEqual(summary["reasons"], {"PREDICTION_UNAVAILABLE": 12})
            self.assertEqual(summary["health"]["fills"], 0)
            self.assertEqual(summary["performance"]["scored"] if "scored" in summary["performance"] else 0, 0)

    def test_an_unknown_model_is_refused_before_anything_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(session.SessionError) as caught:
                session.run_replay(Path(tmp) / "r", "nope", 5, NO_PARAMS)
            self.assertEqual(caught.exception.code, "UNKNOWN_MODEL")
            self.assertFalse((Path(tmp) / "r").exists())

    def test_parameter_files_are_validated(self):
        good = {"schema": PARAMS_SCHEMA, "model_id": "b2_ur1_logistic", "trained_through": "2026-06-29", "source_note": "test",
                "by_option_type": {"CE": {"lo": -0.05, "hi": 0.05, "mean": 0.0, "sd": 0.01, "intercept": -0.2, "coef": 0.1}}}
        with tempfile.TemporaryDirectory() as tmp:
            def attempt(body):
                path = Path(tmp) / "p.json"
                path.write_text(json.dumps(body), encoding="utf-8")
                return load_params_file(path, "b2_ur1_logistic")[0]
            self.assertIsNotNone(attempt(good))
            for bad in ({**good, "model_id": "other"}, {**good, "extra": 1}, {**good, "trained_through": "yesterday"},
                        {**good, "by_option_type": {"CE": {**good["by_option_type"]["CE"], "sd": 0}}},
                        {**good, "by_option_type": {"CE": {**good["by_option_type"]["CE"], "coef": float("nan")}}},
                        {**good, "by_option_type": {}}):
                self.assertIsNone(attempt(bad))
            Path(tmp, "p.json").write_text("{not json", encoding="utf-8")
            self.assertIsNone(load_params_file(Path(tmp) / "p.json", "b2_ur1_logistic")[0])

    def test_logistic_probability_matches_the_research_transform(self):
        p = Params(-0.05, 0.05, 0.0, 0.01, 0.0, 1.0)
        self.assertAlmostEqual(p.probability(0.0), 0.5)
        self.assertAlmostEqual(p.probability(0.01), 1 / (1 + 2.718281828459045 ** -1), places=9)
        self.assertEqual(p.probability(0.5), p.probability(0.05))             # winsorized at the stored bounds


class TestDecisionGates(Replayed):
    def test_the_demonstration_model_can_produce_buy_sell_and_no_trade(self):
        self.assertTrue(all(self.summary["decisions"][k] > 0 for k in (BUY, SELL, NO_TRADE)))
        self.assertEqual(sum(self.summary["decisions"].values()), DAYS)

    def test_every_decision_records_a_nonempty_explanation_and_its_predictions(self):
        for d in self.decisions:
            self.assertTrue(d["explanation"])
            self.assertIn("predictions", d["inputs"])
            self.assertIn(d["label"], (BUY, SELL, NO_TRADE))

    def test_buy_and_sell_rest_on_an_eligible_prediction_and_the_stated_threshold(self):
        for d in self.decisions:
            preds = d["inputs"]["predictions"]
            if d["label"] == BUY:
                chosen = [p for p in preds.values() if p["status"] == "ELIGIBLE" and float(p["probability"]) >= float(d["inputs"]["buy_threshold"])]
                self.assertTrue(chosen, d["explanation"])
            if d["label"] == SELL:
                self.assertTrue(all(p["status"] == "ELIGIBLE" and float(p["probability"]) < float(d["inputs"]["exit_threshold"]) for p in preds.values()))

    def test_research_models_with_standing_none_never_produce_a_trade_even_with_parameters(self):
        body = {"schema": PARAMS_SCHEMA, "model_id": "b2_ur1_logistic", "trained_through": "2026-06-29", "source_note": "test constants",
                "by_option_type": {k: {"lo": -0.05, "hi": 0.05, "mean": 0.0, "sd": 0.01, "intercept": 0.0, "coef": 5.0 * s} for k, s in (("CE", 1), ("PE", -1))}}
        with tempfile.TemporaryDirectory() as tmp:
            params = Path(tmp) / "params"
            params.mkdir()
            (params / "b2_ur1_logistic.json").write_text(json.dumps(body), encoding="utf-8")
            for model_id in ("b2_ur1_logistic", "b0_training_prior"):
                with self.subTest(model=model_id):
                    summary = session.run_replay(Path(tmp) / model_id, model_id, 25, params)
                    self.assertEqual((summary["decisions"]["BUY"], summary["decisions"]["SELL"], summary["health"]["fills"]), (0, 0, 0))
                    self.assertIn("NO_DEMONSTRATED_EDGE", summary["reasons"])
            self.assertGreater(summary["performance"].get("scored", 0), 0)            # the probabilities are still shown and scored: display only

    def test_the_demonstration_model_is_confined_to_synthetic_fixture_data(self):
        card = build_registry(NO_PARAMS)["demo_synthetic_ur1"].card
        self.assertEqual(card.allowed_origins, ("SYNTHETIC_FIXTURE",))
        self.assertEqual(card.standing, "DEMONSTRATION_ONLY")
        for p in (p for d in self.decisions for p in d["inputs"]["predictions"].values() if p["probability"] is not None):
            self.assertEqual(p["provenance"]["origin_classes"], ["SYNTHETIC_FIXTURE"])
            self.assertIn("INVENTED", p["provenance"]["label"].upper())


class TestRiskAndEngine(Replayed):
    def test_the_engine_refuses_some_decisions_and_a_refused_decision_makes_no_fill(self):
        refused = [d for d in self.decisions if (d["engine_result"] or {}).get("code")]
        self.assertTrue(refused, "the demonstration is expected to meet at least one engine refusal")
        self.assertTrue(any(d["engine_result"]["stage"] == "ENGINE" and d["engine_result"]["code"].startswith("RISK_") for d in refused))
        submitted = [d for d in self.decisions if d["label"] in (BUY, SELL)]
        self.assertEqual(len(submitted) - len(refused), self.summary["health"]["fills"])
        self.assertEqual(len(self.summary["refused"]), len(refused))

    def test_fills_come_from_the_paper_engine_with_its_cost_model_on_the_historical_origin(self):
        for f in self.summary["fills"]:
            self.assertEqual(f["data_origin"], "HISTORICAL")
            self.assertTrue(f["simulated"])
            self.assertGreater(Decimal(f["cost"]), 0)
            self.assertIn("virtual trade: no real order exists", f["assumptions"])


class TestPersistenceAndReproducibility(Replayed):
    def test_the_saved_account_matches_the_summary_and_the_audit_is_consistent_and_full(self):
        lines = [json.loads(x) for x in (self.out / "state" / f"replay_{fx.SOURCE_ID}.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(lines[0]["kind"], "header")
        self.assertEqual(len(lines) - 1, self.summary["health"]["fills"])
        self.assertEqual((self.summary["health"]["audit_verdict"], self.summary["health"]["audit_coverage"]), ("CONSISTENT", "FULL"))

    def test_the_same_inputs_give_the_same_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            again = session.run_replay(Path(tmp) / "again", "demo_synthetic_ur1", DAYS, NO_PARAMS)
            for key in ("decisions_sha256", "event_log_head", "event_log_sha256", "plan_digest", "identity_digest"):
                self.assertEqual(again["health"][key], self.summary["health"][key], key)

    def test_an_existing_replay_folder_is_never_overwritten(self):
        before = (self.out / "summary.json").read_bytes()
        with self.assertRaises(session.SessionError) as caught:
            session.run_replay(self.out, "demo_synthetic_ur1", DAYS, NO_PARAMS)
        self.assertEqual(caught.exception.code, "DESTINATION_EXISTS")
        self.assertEqual((self.out / "summary.json").read_bytes(), before)

    def test_the_summary_labels_the_data_and_claims_no_provider_or_profit(self):
        text = json.dumps(self.summary).lower()
        self.assertIn("invented", text)
        self.assertFalse(self.summary["health"]["provider_connected"])
        self.assertEqual(self.summary["health"]["data_origin"], "SYNTHETIC_FIXTURE")
        for phrase in ("provider_response", "live data"):
            self.assertNotIn(phrase, text)
        for sentence in text.replace("\\n", " ").split("."):
            if "profitab" in sentence:
                self.assertTrue(any(w in sentence for w in ("no ", "nothing", "not ", "cannot")), sentence)
        self.assertTrue(all(not r["supported"] for r in self.summary["research"]["rows"]))


class TestMlPage(Replayed):
    def test_the_page_shows_every_section_and_labels_the_data(self):
        page = render_ml_page(self.out)[1]
        for needle in ("INVENTED FIXTURE DATA", "NOT CONNECTED", "System health", "Predictions and decisions", "Model performance", "Portfolio history",
                       "Simulated trades", "Limitations", "NOT SUPPORTED", "DEMONSTRATION"):
            self.assertIn(needle, page)
        for banned in ("<script", "http://", "https://", "src=", "<form", "<input", "<button"):
            self.assertNotIn(banned, page)

    def test_without_a_replay_the_page_says_how_to_make_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = render_ml_page(Path(tmp) / "none")[1]
        self.assertIn("python run_ml_demo.py", page)
        self.assertIn("No replay to show", page)
        self.assertEqual(load_replay(None)[0], None)

    def test_a_damaged_replay_is_reported_not_rendered(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "summary.json").write_text("{broken", encoding="utf-8")
            (Path(tmp) / "decisions.jsonl").write_text("", encoding="utf-8")
            self.assertIn("could not be read", render_ml_page(tmp)[1])

    def test_the_server_serves_the_page_and_json_read_only(self):
        app = App(lambda now=None: None, ml_dir=self.out)
        with Served(app) as s:
            status, headers, body = s.request("GET", "/ml")
            self.assertEqual(status, 200)
            self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
            self.assertIn("INVENTED FIXTURE DATA", body)
            status, _, body = s.request("GET", "/api/ml")
            self.assertEqual((status, json.loads(body)["health"]["data_origin"]), (200, "SYNTHETIC_FIXTURE"))
            self.assertEqual(s.request("POST", "/ml")[0], 405)
            self.assertEqual(s.request("GET", "/api/ml/../x")[0], 404)


if __name__ == "__main__":
    unittest.main()
