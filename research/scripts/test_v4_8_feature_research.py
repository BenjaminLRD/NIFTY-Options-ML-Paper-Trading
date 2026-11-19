"""Automated tests for V4.8 (stdlib unittest). Run: python -m unittest test_v4_8_feature_research -v"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import v4_7_model_dataset as M
import v4_7_source_audit as A
import v4_8_feature_research as V

V.N_BOOT = 60   # keep the suite fast; the production default (1000) is exercised by the real run


def synth_frame(n_dates=14, drop=None, seed=0, low_day=None):
    """Source-like frame as returned by A.load_source: 1 expiry, CE+PE, 3 strikes. drop=(type,strike,day_index)."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2026-01-05", periods=n_dates)
    rows = []
    for ot in ("CE", "PE"):
        for strike in (21000.0, 23000.0, 25000.0):
            px = 100.0
            for i, d in enumerate(dates):
                px = max(px * (1 + rng.normal(0, 0.06)), 3.0)
                if low_day is not None and (ot, strike) == ("CE", 21000.0) and i == low_day:
                    px = 0.5
                if drop and (ot, strike, i) == drop:
                    continue
                rows.append({"source_file": "x.csv", "symbol": "NIFTY", "expiry": pd.Timestamp("2026-09-29"), "option_type": ot,
                             "strike": strike, "date": d, "src_settle": round(px, 2), "src_underlying": 23000 + 40 * i + 15 * (i % 3)})
    return pd.DataFrame(rows), dates


def write_source_csv(path, n_dates=60, seed=3):
    df, _ = synth_frame(n_dates=n_dates, seed=seed)
    out = pd.DataFrame({"Symbol": df.symbol, "Date": df.date.dt.strftime("%d-%b-%Y"), "Expiry": "29-Sep-2026",
                        "Option type": df.option_type, "Strike Price": df.strike, "Close": df.src_settle,
                        "Settle Price": df.src_settle, "Underlying Value": df.src_underlying})
    out.to_csv(path, index=False)


def feats(src):
    r, cal, _ = M.build_features(src)
    return V.add_v48_features(r), cal


class TestMoneyness(unittest.TestCase):
    def test_hand_worked_call_put(self):
        # S=23000, K=24000: call is OTM (S<K), put is ITM. ln(23000/24000) = -0.0425596
        ce = V.signed_log_moneyness([23000], [24000], ["CE"])[0]
        pe = V.signed_log_moneyness([23000], [24000], ["PE"])[0]
        self.assertAlmostEqual(ce, -0.0425596, places=6)
        self.assertAlmostEqual(pe, +0.0425596, places=6)
        self.assertEqual(V.bucketize([ce, pe]).tolist(), ["OTM", "ITM"])
        # S=23000, K=15000: call deep ITM, put deep OTM. ln(23000/15000) = 0.427444
        self.assertEqual(V.bucketize(V.signed_log_moneyness([23000, 23000], [15000, 15000], ["CE", "PE"])).tolist(),
                         ["DEEP_ITM", "DEEP_OTM"])
        self.assertEqual(V.bucketize([0.0])[0], "ATM")

    def test_bucket_edges_are_left_closed(self):
        self.assertEqual(V.bucketize([-0.10, -0.03, 0.03, 0.10]).tolist(), ["OTM", "ATM", "ITM", "DEEP_ITM"])

    def test_same_ratio_opposite_meaning_by_type(self):
        r, _ = feats(synth_frame()[0])
        row = r.dropna(subset=["moneyness_lag1"])
        ce, pe = row[row.option_type == "CE"], row[row.option_type == "PE"]
        np.testing.assert_allclose(ce["sm"], np.log(ce["moneyness_lag1"]))
        np.testing.assert_allclose(pe["sm"], -np.log(pe["moneyness_lag1"]))

    def test_rejects_unknown_type_and_marks_nan(self):
        with self.assertRaises(ValueError):
            V.signed_log_moneyness([1], [1], ["XX"])
        self.assertEqual(V.bucketize([np.nan, np.inf])[0], V.MISSING)

    def test_lr1_is_log_of_price_ratio(self):
        src, dates = synth_frame()
        r, _ = feats(src)
        c = src[(src.option_type == "CE") & (src.strike == 23000.0)].set_index("date").src_settle
        row = r[(r.option_type == "CE") & (r.strike == 23000.0) & (r.date == dates[6])].iloc[0]
        self.assertAlmostEqual(row.lr1, np.log(c[dates[5]] / c[dates[4]]), places=12)


class TestNoLookahead(unittest.TestCase):
    def test_future_prices_do_not_change_past_features_or_labels(self):
        src, dates = synth_frame()
        T = dates[8]
        other = src.copy()
        fut = other["date"] > T
        other.loc[fut, "src_settle"] *= 3.7
        other.loc[fut, "src_underlying"] += 900
        a, b = feats(src)[0], feats(other)[0]
        cols = M.FEATURES + ["lr1", "ur1", "sm"]
        past = a["date"] <= T
        pd.testing.assert_frame_equal(a.loc[past, cols], b.loc[past, cols])
        lab = a["date"] < T                         # label at t uses t+1 <= T
        pd.testing.assert_series_equal(a.loc[lab, "target_next_up"], b.loc[lab, "target_next_up"])

    def test_features_at_t_ignore_price_at_t(self):
        src, dates = synth_frame()
        T = dates[8]
        other = src.copy()
        m = other["date"] == T
        other.loc[m, "src_settle"] *= 5
        other.loc[m, "src_underlying"] += 700
        a, b = feats(src)[0], feats(other)[0]
        cols = M.FEATURES + ["lr1", "ur1", "sm"]
        same = a["date"] <= T
        pd.testing.assert_frame_equal(a.loc[same, cols], b.loc[same, cols])
        self.assertFalse(a.loc[a.date == dates[9], cols].equals(b.loc[b.date == dates[9], cols]))  # it enters the NEXT day only


class TestGapsAndLagOrder(unittest.TestCase):
    def test_missing_date_breaks_target_and_lags(self):
        src, dates = synth_frame(drop=("CE", 21000.0, 6))
        r, _ = feats(src)
        c = r[(r.option_type == "CE") & (r.strike == 21000.0)].set_index("date")
        self.assertTrue(pd.isna(c.loc[dates[5], "target_next_up"]))
        self.assertTrue(pd.isna(c.loc[dates[7], "settle_return_lag1"]) and pd.isna(c.loc[dates[8], "settle_return_lag1"]))
        self.assertFalse(pd.isna(c.loc[dates[9], "settle_return_lag1"]))
        self.assertTrue(pd.isna(c.loc[dates[7], "lr1"]))

    def test_lags_are_computed_before_eligibility_filters(self):
        src, dates = synth_frame(low_day=6)          # CE 21000 settles at 0.5 (< floor) on day 6
        r, _ = feats(src)
        rl = r[["date", "option_type", "strike", "expiry"]].assign(audit_status=A.ST_NO_DATE)
        for c in ("date", "expiry"):
            rl[c] = rl[c].dt.strftime("%Y-%m-%d")
        r = M.apply_rules(r, rl)
        c = r[(r.option_type == "CE") & (r.strike == 21000.0)].set_index("date")
        # day 7's lag uses the real day-6 price although day 6 is filtered out by the floor
        s = src[(src.option_type == "CE") & (src.strike == 21000.0)].set_index("date").src_settle
        self.assertAlmostEqual(c.loc[dates[8], "settle_return_lag1"], s[dates[7]] / s[dates[6]] - 1, places=12)
        self.assertFalse(c.loc[dates[6], "R3_price_floor"])
        self.assertFalse(c.loc[dates[8], "R3_price_floor"])        # t-2 is the low price
        self.assertTrue(c.loc[dates[8], "R1_features"])             # feature exists; only the filter removes the row


class TestTrainOnlyAndPolicies(unittest.TestCase):
    def test_standardizer_params_come_from_train_only(self):
        tr, va = np.arange(100.0), np.array([1e6, -1e6, np.nan])
        s = V.Standardizer().fit(tr)
        before = (s.lo, s.hi, s.mu, s.sd)
        z = s.transform(va)
        self.assertEqual(before, (s.lo, s.hi, s.mu, s.sd))               # transform does not refit
        both = V.Standardizer().fit(np.r_[tr, va[:2]])
        self.assertNotEqual(before, (both.lo, both.hi, both.mu, both.sd))  # contamination would show
        self.assertTrue(np.isfinite(z).all())
        self.assertEqual(s.n_imputed, 1)
        self.assertEqual(z[2], 0.0)                                       # NaN -> training mean

    def test_bucket_rates_train_only_and_unseen_policy(self):
        lab = np.array(["OTM"] * 40 + ["ATM"] * 40, dtype=object)
        y = np.r_[np.ones(40), np.zeros(40)]
        t = V.BucketRates().fit(lab, y)
        self.assertAlmostEqual(t.prior, 0.5)
        self.assertAlmostEqual(t.rates["OTM"], (40 + V.SHRINK * 0.5) / (40 + V.SHRINK))
        p = t.transform(np.array(["OTM", "DEEP_ITM", V.MISSING, "never-seen"], dtype=object))
        self.assertEqual(p[1], t.prior)
        self.assertEqual(p[2], t.prior)
        self.assertEqual(p[3], t.prior)
        self.assertEqual(t.n_fallback, 3)
        self.assertNotIn("DEEP_ITM", t.rates)

    def test_predictions_never_read_evaluation_labels(self):
        src, _ = synth_frame(n_dates=40)
        r, cal = feats(src)
        r = r.dropna(subset=["lr1", "ur1", "sm", "target_next_up"])
        tr, te = r.iloc[: len(r) // 2], r.iloc[len(r) // 2:].copy()
        p1, _ = V.predict_all(tr, te)
        te2 = te.copy()
        te2["target_next_up"] = 1 - te2["target_next_up"]
        p2, _ = V.predict_all(tr, te2)
        for k in p1:
            pd.testing.assert_series_equal(p1[k], p2[k])
        tr2 = tr.copy()
        tr2["target_next_up"] = 1 - tr2["target_next_up"]
        p3, _ = V.predict_all(tr2, te)
        self.assertFalse(p1["B3_signed_moneyness_bucket"].equals(p3["B3_signed_moneyness_bucket"]))

    def test_missing_and_extreme_inputs_give_finite_predictions(self):
        src, _ = synth_frame(n_dates=40)
        r, _ = feats(src)
        r = r.dropna(subset=["lr1", "ur1", "sm", "target_next_up"])
        te = r.iloc[-20:].copy()
        te.iloc[0, te.columns.get_loc("lr1")] = np.nan
        te.iloc[1, te.columns.get_loc("ur1")] = 1e9
        te.iloc[2, te.columns.get_loc("bucket")] = "BRAND_NEW"
        p, _ = V.predict_all(r.iloc[:-20], te)
        for s in p.values():
            self.assertTrue(np.isfinite(s.to_numpy()).all())
            self.assertTrue(((s > 0) & (s < 1)).all())

    def test_b4_only_when_events_justify(self):
        ok, ev, nv = V.b4_justified(np.r_[np.ones(40), np.zeros(60)])
        self.assertTrue(ok)
        ok2, _, _ = V.b4_justified(np.r_[np.ones(20), np.zeros(200)])
        self.assertFalse(ok2)


class TestPairedAndMetrics(unittest.TestCase):
    def test_assert_same_rows_rejects_mismatch(self):
        a = pd.Series([0.5, 0.5], index=[1, 2])
        with self.assertRaises(AssertionError):
            V.assert_same_rows({"B0": a, "B1": pd.Series([0.4, 0.6], index=[1, 3])})

    def test_all_models_share_row_index(self):
        src, _ = synth_frame(n_dates=40)
        r, _ = feats(src)
        r = r.dropna(subset=["lr1", "ur1", "sm", "target_next_up"])
        p, _ = V.predict_all(r.iloc[:60], r.iloc[60:])
        idx = [s.index.tolist() for s in p.values()]
        self.assertTrue(all(i == idx[0] for i in idx))
        self.assertEqual(len(p), 5)

    def test_hand_metrics(self):
        self.assertAlmostEqual(V.brier([0.5, 0.5], [1, 0]), 0.25)
        self.assertAlmostEqual(V.logloss([0.5, 0.5], [1, 0]), np.log(2))
        self.assertAlmostEqual(V.auc([0, 1], [0.1, 0.9]), 1.0)
        self.assertAlmostEqual(V.auc([0, 1, 0, 1], [0.5, 0.5, 0.5, 0.5]), 0.5)
        self.assertTrue(np.isnan(V.auc([1, 1], [0.2, 0.3])))

    def test_logit_intercept_matches_log_odds(self):
        y = np.r_[np.ones(30), np.zeros(70)]
        w = V.fit_logit(np.zeros((100, 1)), y)
        self.assertAlmostEqual(w[0], np.log(0.3 / 0.7), places=5)

    def test_bootstrap_identical_model_has_zero_difference_and_is_seeded(self):
        src, _ = synth_frame(n_dates=40)
        r, _ = feats(src)
        r = r.dropna(subset=["lr1", "ur1", "sm", "target_next_up"])
        tr, te = r.iloc[:80], r.iloc[80:]
        p, _ = V.predict_all(tr, te)
        same = {"B0_training_prior": p["B0_training_prior"]}
        out = V.bootstrap(tr, te, p, seed=5, n_boot=30)
        again = V.bootstrap(tr, te, p, seed=5, n_boot=30)
        for m in out:
            np.testing.assert_array_equal(out[m]["eval_only"], again[m]["eval_only"])
            np.testing.assert_array_equal(out[m]["refit"], again[m]["refit"])
            self.assertTrue(np.isfinite(out[m]["refit"]).sum() > 20)
        p_dup = dict(p)
        p_dup["B1_lag1_settle_logreturn"] = p["B0_training_prior"]
        z = V.bootstrap(tr, te, {k: p_dup[k] for k in ("B0_training_prior", "B1_lag1_settle_logreturn")}, seed=5, n_boot=30)
        np.testing.assert_allclose(z["B1_lag1_settle_logreturn"]["eval_only"], 0.0, atol=1e-12)


class TestPipelineEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="v48test_"))
        cls.data = cls.root / "data"
        cls.data.mkdir()
        write_source_csv(cls.data / "OPTIDX_NIFTY_CE_01-Jan-2026_TO_31-Mar-2026.csv")
        A.run_audit(cls.data, verbose=False)
        cls.out = cls.root / "out1"
        cls.res = V.run(cls.data, cls.out, verbose=False)
        cls.ds = pd.read_csv(cls.out / "V4_8_Dataset.csv", parse_dates=["date", "target_date"])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_no_label_crosses_a_split_boundary(self):
        ds = self.ds
        e = ds[ds.eligible]
        val0 = ds.loc[ds.split == "validation", "date"].min()
        te0 = ds.loc[ds.split == "test", "date"].min()
        self.assertTrue((e.loc[e.split == "train", "target_date"] < val0).all())
        self.assertTrue((e.loc[e.split == "validation", "target_date"] < te0).all())
        self.assertTrue((e["target_date"] > e["date"]).all())

    def test_purge_at_every_boundary(self):
        ds = self.ds
        val0 = ds.loc[ds.split == "validation", "cal_idx"].min()
        te0 = ds.loc[ds.split == "test", "cal_idx"].min()
        for b, origin in ((val0, "train"), (te0, "validation")):
            at = ds[ds.cal_idx == b - 1]
            self.assertGreater(len(at), 0)
            self.assertTrue((at.split == "purged").all())
            self.assertTrue((at.split_if_not_purged == origin).all())
        kept = ds[ds.split.isin(["train", "validation"])]
        nxt = np.where(kept.cal_idx < val0, val0, te0)            # first date of the following split
        self.assertTrue((kept.cal_idx + 1 < nxt).all())

    def test_all_raw_rows_kept(self):
        self.assertEqual(len(self.ds), 360)                       # 2 types x 3 strikes x 60 dates, none deleted
        ex = self.ds["exclusion_reason"].fillna("")
        self.assertTrue((ex[self.ds.eligible] == "").all() and (ex[~self.ds.eligible] != "").all())

    def test_reports_exist_and_checks_pass(self):
        for n in ("Dataset", "Dataset_Eligibility", "Split_Integrity", "Feature_Definitions", "Baseline_Comparison",
                  "Uncertainty_Intervals", "Calibration", "Moneyness_Bucket_Rates", "Validation_Checks"):
            self.assertTrue((self.out / f"V4_8_{n}.csv").exists(), n)
        self.assertTrue((self.out / "V4_8_Reports.zip").exists())
        self.assertTrue((self.res["checks"]["issues"] == 0).all())

    def test_comparison_pairs_rows_and_labels_test_exploratory(self):
        c = self.res["comparison"]
        c = c[c.eval_rows > 0]
        for (ot, sp), g in c.groupby(["option_type", "split"]):
            self.assertEqual(g["eval_rows"].nunique(), 1)
            self.assertIn("B0_training_prior", set(g.model))
        self.assertTrue(c[c.split == "test"]["status"].str.startswith("EXPLORATORY").all())
        self.assertTrue((c[c.split == "test"]["selected_on_validation"] == "").all())

    def test_deterministic_rerun_byte_identical(self):
        out2 = self.root / "out2"
        V.run(self.data, out2, verbose=False)
        for p in sorted(self.out.glob("V4_8_*")):
            self.assertEqual(A.sha256(p), A.sha256(out2 / p.name), p.name)

    def _fresh(self, name):
        d = self.root / name
        shutil.copytree(self.data, d)
        return d

    def test_gate_fails_closed(self):
        d = self._fresh("g1")
        (d / "V4_7_Audit_Input_Manifest.csv").unlink()
        with self.assertRaises(V.GateError):
            V.run(d, self.root / "o_g1", verbose=False)
        d = self._fresh("g2")                                    # audit has unresolved REVIEW item
        vc = pd.read_csv(d / "V4_7_Audit_Validation_Checks.csv")
        vc.loc[0, ["issues", "result"]] = [3, "REVIEW"]
        vc.to_csv(d / "V4_7_Audit_Validation_Checks.csv", index=False)
        with self.assertRaises(V.GateError):
            V.run(d, self.root / "o_g2", verbose=False)
        d = self._fresh("g3")                                    # source changed after the audit
        f = next(d.glob("OPTIDX_NIFTY_*.csv"))
        f.write_text(f.read_text().replace("23000.0", "23001.0", 1))
        with self.assertRaises(V.GateError):
            V.run(d, self.root / "o_g3", verbose=False)
        d = self._fresh("g4")                                    # stale row-level report
        rl = pd.read_csv(d / "V4_7_Audit_Row_Level_Comparison.csv")
        rl.iloc[:-5].to_csv(d / "V4_7_Audit_Row_Level_Comparison.csv", index=False)
        with self.assertRaises(V.GateError):
            V.run(d, self.root / "o_g4", verbose=False)
        d = self._fresh("g5")                                    # new official file the audit never saw
        (d / "BhavCopy_NSE_FO_0_0_0_20260110_F_0000.csv").write_text("TradDt,FinInstrmTp,TckrSymb\n")
        with self.assertRaises(V.GateError):
            V.run(d, self.root / "o_g5", verbose=False)
        self.assertFalse((self.root / "o_g1" / "V4_8_Reports.zip").exists())   # nothing written when the gate fails

    def test_does_not_modify_inputs(self):
        before = {p.name: A.sha256(p) for p in self.data.iterdir()}
        V.run(self.data, self.root / "out3", verbose=False)
        self.assertEqual(before, {p.name: A.sha256(p) for p in self.data.iterdir()})


if __name__ == "__main__":
    unittest.main(verbosity=2)
