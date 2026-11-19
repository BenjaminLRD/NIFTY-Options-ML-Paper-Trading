"""Automated tests for V4.9 diagnostics (stdlib unittest). Run: python -m unittest test_v4_9_feature_diagnostics -v"""
import math
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_v4_8_feature_research as T8      # reuse the synthetic-data builders
import v4_7_model_dataset as M
import v4_7_source_audit as A
import v4_8_feature_research as V
import v4_9_feature_diagnostics as D

D.N_BOOT = 60
V.N_BOOT = 60


def to_raw(df):
    return pd.DataFrame({"option_type": df.option_type, "strike": df.strike, "expiry": df.expiry, "date": df.date,
                         "settle": df.src_settle, "und": df.src_underlying}).reset_index(drop=True)


def tiny_raw():
    """One CE (strike 1000) and one PE (strike 1100) contract, 6 consecutive business days, hand-picked numbers."""
    dates = pd.bdate_range("2026-02-02", periods=6)
    und = [1000.0, 1010.0, 1020.1, 1000.0, 1100.0, 1150.0]
    ce = [10.0, 11.0, 12.1, 9.0, 18.0, 20.0]
    pe = [50.0, 45.0, 40.0, 60.0, 30.0, 25.0]
    rows = []
    for i, d in enumerate(dates):
        rows.append({"option_type": "CE", "strike": 1000.0, "expiry": pd.Timestamp("2026-09-29"), "date": d, "settle": ce[i], "und": und[i]})
        rows.append({"option_type": "PE", "strike": 1100.0, "expiry": pd.Timestamp("2026-09-29"), "date": d, "settle": pe[i], "und": und[i]})
    return pd.DataFrame(rows), dates


class TestIndependentRebuildHandWorked(unittest.TestCase):
    def test_hand_calculated_values(self):
        raw, dates = tiny_raw()
        ind, cal, dup = D.independent_features(raw)
        self.assertEqual(dup, 0)
        ce = ind[(ind.option_type == "CE") & (ind.date == dates[4])].iloc[0]
        # decision day = dates[4]: lr1 = ln(settle[3]/settle[2]) = ln(9/12.1) = -0.295981
        self.assertAlmostEqual(ce.lr1, -0.295981, places=5)
        # ur1 = underlying[3]/underlying[2]-1 = 1000/1020.1-1 = -0.019704
        self.assertAlmostEqual(ce.ur1, -0.019704, places=5)
        # call, S[3]=1000, K=1000 -> ln(1)=0 (ATM)
        self.assertAlmostEqual(ce.sm, 0.0, places=9)
        # target: settle[5]=20 > settle[4]=18
        self.assertEqual(ce.target, 1.0)
        self.assertEqual(ce.target_date, dates[5])
        self.assertEqual((ce.s2, ce.s1, ce.s0, ce.sn), (12.1, 9.0, 18.0, 20.0))
        pe = ind[(ind.option_type == "PE") & (ind.date == dates[4])].iloc[0]
        # put, S[3]=1000, K=1100 -> -ln(1000/1100) = +0.095310 (in the money)
        self.assertAlmostEqual(pe.sm, 0.095310, places=5)
        self.assertEqual(pe.target, 0.0)                                  # 25 > 30 is False
        self.assertAlmostEqual(pe.lr1, math.log(60 / 40), places=9)

    def test_edges_of_the_series_are_nan(self):
        raw, dates = tiny_raw()
        ind, _, _ = D.independent_features(raw)
        first = ind[(ind.option_type == "CE") & (ind.date == dates[1])].iloc[0]
        self.assertTrue(np.isnan(first.lr1) and np.isnan(first.ur1))     # needs t-2
        last = ind[(ind.option_type == "CE") & (ind.date == dates[5])].iloc[0]
        self.assertTrue(np.isnan(last.target))                            # no t+1


class TestAgreementWithV47Builder(unittest.TestCase):
    def _compare(self, src):
        raw = to_raw(src)
        ind, cal, _ = D.independent_features(raw)
        r, _, _ = M.build_features(src)
        r = V.add_v48_features(r)
        m = ind.merge(r, on=["option_type", "strike", "date"], validate="one_to_one")
        self.assertEqual(len(m), len(src))
        for a, b in (("lr1_x", "lr1_y"), ("ur1_x", "ur1_y"), ("sm_x", "sm_y"), ("target", "target_next_up")):
            np.testing.assert_allclose(m[a].to_numpy(float), m[b].to_numpy(float), rtol=0, atol=1e-12, equal_nan=True, err_msg=a)

    def test_clean_series(self):
        self._compare(T8.synth_frame()[0])

    def test_missing_dates_nonconsecutive(self):
        self._compare(T8.synth_frame(drop=("CE", 21000.0, 6))[0])
        self._compare(T8.synth_frame(drop=("PE", 25000.0, 3))[0])

    def test_input_order_does_not_matter(self):
        src, _ = T8.synth_frame(drop=("CE", 21000.0, 6))
        shuffled = src.sample(frac=1.0, random_state=1).reset_index(drop=True)
        a, _, _ = D.independent_features(to_raw(src))
        b, _, _ = D.independent_features(to_raw(shuffled))
        key = ["option_type", "strike", "date"]
        pd.testing.assert_frame_equal(a.sort_values(key).reset_index(drop=True), b.sort_values(key).reset_index(drop=True))
        r1, _, _ = M.build_features(src)
        r2, _, _ = M.build_features(shuffled)
        c = M.FEATURES + ["target_next_up"]
        pd.testing.assert_frame_equal(r1.sort_values(key).reset_index(drop=True)[c], r2.sort_values(key).reset_index(drop=True)[c])


class TestSplitBoundaries(unittest.TestCase):
    def test_independent_split_matches_v47_and_purges(self):
        src, _ = T8.synth_frame(n_dates=30)
        r, cal, _ = M.build_features(src)
        r, v0, t0 = M.assign_splits(r, cal)
        split, iv0, it0 = D.independent_split(r["cal_idx"].to_numpy(), len(cal))
        self.assertEqual((np.array(split) == r["split"].to_numpy()).all(), True)
        self.assertEqual((iv0, it0), (int(30 * 0.6), int(30 * 0.8)))
        for b in (iv0, it0):
            self.assertTrue((split[r["cal_idx"].to_numpy() == b - 1] == "purged").all())
        kept = np.isin(split, ["train", "validation"])
        nxt = np.where(r["cal_idx"].to_numpy() < iv0, iv0, it0)
        self.assertTrue(((r["cal_idx"].to_numpy() + 1 < nxt) | ~kept).all())


class TestLeakageAndPositiveControl(unittest.TestCase):
    def test_no_leak_in_real_builder(self):
        raw = to_raw(T8.synth_frame()[0])
        f, l, sd, _ = D.perturbation_leak_test(raw, 7)
        self.assertEqual((f, l, sd), (0, 0, 0))

    def test_detects_injected_leaks(self):
        raw = to_raw(T8.synth_frame()[0])
        real = D.independent_features

        def future_leak(r):                                     # feature built from the NEXT day's price
            out, cal, dup = real(r)
            out["lr1"] = np.log(out["sn"] / out["s0"])
            return out, cal, dup

        def same_day_leak(r):                                   # feature built from the decision-day price
            out, cal, dup = real(r)
            out["lr1"] = np.log(out["s0"] / out["s1"])
            return out, cal, dup
        with mock.patch.object(D, "independent_features", future_leak):
            f, _, sd, _ = D.perturbation_leak_test(raw, 7)
        self.assertGreater(f, 0)
        with mock.patch.object(D, "independent_features", same_day_leak):
            f2, _, sd2, _ = D.perturbation_leak_test(raw, 7)
        self.assertEqual(f2, 0)                                 # invisible to a pure-future perturbation ...
        self.assertGreater(sd2, 0)                              # ... but caught by the same-day perturbation

    def _ds(self, shift):
        rng = np.random.default_rng(2)
        dates = pd.bdate_range("2026-01-05", periods=60)
        und = pd.Series(23000 * np.exp(np.cumsum(rng.normal(0, 0.01, 60))), index=dates)
        ret = und.pct_change()
        rows = []
        for i, d in enumerate(dates[:-4]):
            nxt = ret.iloc[i + 1 + shift] if 0 <= i + 1 + shift < 60 else np.nan
            for k in range(6):
                rows.append({"option_type": "CE", "date": d, "eligible": True, "split": "train",
                             "target_next_up": float((nxt > 0) ^ (rng.random() < 0.1))})
        return pd.DataFrame(rows), list(dates), und.to_dict()

    def test_lead_lag_peaks_at_the_intended_window_and_detects_misalignment(self):
        ds, cal, und = self._ds(0)
        res = {r["shift_days"]: r["corr_with_date_up_rate"] for r in D.lead_lag(ds, cal, und, "CE")}
        self.assertEqual(max(res, key=lambda k: abs(res[k])), 0)
        self.assertGreater(res[0], 0.6)
        ds2, cal2, und2 = self._ds(1)                            # labels built one day late
        res2 = {r["shift_days"]: r["corr_with_date_up_rate"] for r in D.lead_lag(ds2, cal2, und2, "CE")}
        self.assertEqual(max(res2, key=lambda k: abs(res2[k])), 1)


class TestEligibilityCorrection(unittest.TestCase):
    def test_rule(self):
        ds = pd.DataFrame({"option_type": "CE", "strike": [1.0, 2.0, 3.0, 4.0, 5.0], "date": pd.Timestamp("2026-01-05"),
                           "eligible": [True, False, False, False, False],
                           "exclusion_reason": [np.nan, "R1_features", "R1_features|R3_price_floor", "R2_target", "R1_features"]})
        ind = pd.DataFrame({"option_type": "CE", "strike": [1.0, 2.0, 3.0, 4.0, 5.0], "date": pd.Timestamp("2026-01-05"),
                            "lr1": [0.1, 0.1, 0.1, 0.1, np.nan], "ur1": 0.1, "sm": 0.1})
        got = D.corrected_eligibility(ds, ind).tolist()
        # row1 stays; row2 (only unused-feature R1) is restored; row3 fails price floor; row4 fails target; row5 lacks a used feature
        self.assertEqual(got, [True, True, False, False, False])


class TestStatistics(unittest.TestCase):
    def test_cluster_boot_is_seeded(self):
        dates = np.repeat(np.arange(10), 5)
        y = np.random.default_rng(0).random(50)
        f = lambda w: (w * y).sum() / w.sum()
        np.testing.assert_array_equal(D.cluster_boot(f, dates, 50, 3), D.cluster_boot(f, dates, 50, 3))
        self.assertFalse(np.array_equal(D.cluster_boot(f, dates, 50, 3), D.cluster_boot(f, dates, 50, 4)))

    def test_wcorr_hand_value(self):
        self.assertAlmostEqual(D.wcorr(np.array([1., 2, 3]), np.array([2., 4, 6]), np.ones(3)), 1.0)
        self.assertAlmostEqual(D.wcorr(np.array([1., 2, 3]), np.array([6., 4, 2]), np.ones(3)), -1.0)
        self.assertAlmostEqual(D.wcorr(np.array([1., 2, 3, 100]), np.array([3., 2, 1, 0]), np.array([1, 1, 1, 0.])), -1.0)  # zero weight = absent

    def test_calibration_slope(self):
        rng = np.random.default_rng(1)
        n = 4000
        p = rng.uniform(0.2, 0.8, n)
        y = (rng.random(n) < p).astype(float)
        dates = np.repeat(np.arange(200), 20)
        s, (lo, hi) = D.calibration_slope(y, p, dates, n_boot=50)
        self.assertTrue(0.6 < s < 1.4)
        y_rev = (rng.random(n) < (1 - p)).astype(float)
        s2, _ = D.calibration_slope(y_rev, p, dates, n_boot=50)
        self.assertLess(s2, -0.5)

    def test_reversal_test_flags_a_flip_and_ignores_stable(self):
        rng = np.random.default_rng(5)

        def frame(sign_val):
            rows = []
            for sp, n_dates, sign in (("train", 80, 1), ("validation", 40, sign_val)):
                for k in range(n_dates):
                    x = rng.normal()
                    for _ in range(6):
                        rows.append({"option_type": "CE", "split": sp, "date": pd.Timestamp("2026-01-01") + pd.Timedelta(days=k + (0 if sp == "train" else 200)),
                                     "lr1": x, "ur1": x, "target_next_up": float(sign * x + rng.normal(0, 0.8) > 0)})
            return pd.DataFrame(rows)
        flip = D.reversal_test(frame(-1))
        self.assertTrue(flip.sign_flipped.all() and flip.delta_ci_excludes_0.all())
        stable = D.reversal_test(frame(+1))
        self.assertFalse(stable.sign_flipped.any())
        self.assertFalse(stable.delta_ci_excludes_0.any())

    def test_loss_concentration_hand_example(self):
        te = pd.DataFrame({"date": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]),
                           "target_next_up": [1.0, 1.0, 1.0, 1.0], "lr1": [0.0] * 4})
        idx = te.index
        preds = {"B0_training_prior": pd.Series(0.5, index=idx),                 # se = 0.25 everywhere
                 "B1_lag1_settle_logreturn": pd.Series([0.1, 0.4, 0.9, 1.0], index=idx)}   # se = .81,.36,.01,0
        out = D.loss_concentration("CE", te, preds, {"lr1": (-1, 1)})[0]
        self.assertEqual(out["dates_worse_than_B0"], 2)
        self.assertAlmostEqual(out["top1_date_share_of_loss_on_worse_dates"], round((0.81 - 0.25) / ((0.81 - 0.25) + (0.36 - 0.25)), 3))
        self.assertAlmostEqual(out["top5_dates_share_of_loss_on_worse_dates"], 1.0)

    def test_b3_driver_leave_one_out_hand_example(self):
        tr = pd.DataFrame({"bucket": ["OTM"] * 50 + ["ITM"] * 50, "target_next_up": [0.0] * 50 + [1.0] * 50})
        te = pd.DataFrame({"bucket": ["OTM", "OTM", "ITM", "ITM"], "target_next_up": [0.0, 0.0, 1.0, 0.0],
                           "date": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]), "strike": [1.0, 2.0, 1.0, 2.0]})
        rows = {(r["view"], r["item"]): r["value"] for r in D.b3_drivers("CE", tr, te)}
        rates = V.BucketRates().fit(tr["bucket"].to_numpy(), tr["target_next_up"].to_numpy(float))
        p3 = np.array([rates.rates["OTM"]] * 2 + [rates.rates["ITM"]] * 2)
        y = te["target_next_up"].to_numpy()
        dse = (p3 - y) ** 2 - (0.5 - y) ** 2
        self.assertAlmostEqual(rows[("overall", "mean_diff_B3_minus_B0")], round(dse.mean(), 5))
        loo = [(dse.sum() - dse[i]) / 3 for i in range(4)]
        self.assertAlmostEqual(rows[("leave-one-date-out", "min_mean_diff")], round(min(loo), 5))
        self.assertAlmostEqual(rows[("leave-one-date-out", "max_mean_diff")], round(max(loo), 5))


class TestMoneynessAndGuards(unittest.TestCase):
    def test_hand_worked_examples_pass_and_catch_a_wrong_formula(self):
        d = pd.DataFrame({"option_type": ["CE"], "split": ["train"], "bucket": ["ATM"], "date": pd.Timestamp("2026-01-05"),
                          "strike": [1.0], "target_next_up": [1.0]})
        rates = pd.DataFrame({"option_type": "CE", "bucket": V.LOGM_LABELS, "rate_shrunk": [0.5] * 5})
        ex, bt, chk = D.moneyness_tables(d, rates)
        self.assertTrue(ex["pass"].all())
        with mock.patch.object(V, "signed_log_moneyness", lambda s, k, o: np.log(np.asarray(s, float) / np.asarray(k, float))):
            ex2, _, _ = D.moneyness_tables(d, rates)
        self.assertFalse(ex2["pass"].all())                        # calls/puts not distinguished -> examples fail

    def test_bucket_rates_are_train_only(self):
        d = pd.DataFrame({"option_type": "CE", "split": ["train"] * 4 + ["validation"] * 2, "bucket": ["ATM"] * 6,
                          "date": pd.Timestamp("2026-01-05"), "strike": 1.0, "target_next_up": [1.0, 0, 1, 0, 1, 1]})
        prior = 0.5
        rep = pd.DataFrame({"option_type": "CE", "bucket": V.LOGM_LABELS, "rate_shrunk": [prior] * 5})
        _, _, chk = D.moneyness_tables(d, rep)
        self.assertAlmostEqual(chk.loc[chk.bucket == "ATM", "refit_train_only_v48_eligibility"].iloc[0], 0.5)   # validation 1s ignored
        d2 = d.copy()
        d2.loc[d2.split == "validation", "target_next_up"] = 0.0
        _, _, chk2 = D.moneyness_tables(d2, rep)
        pd.testing.assert_frame_equal(chk, chk2)

    def test_test_split_is_refused(self):
        bad = pd.DataFrame({"split": ["train", "test"], "option_type": "CE", "date": pd.Timestamp("2026-01-05"),
                            "lr1": 0.1, "ur1": 0.1, "sm": 0.1, "target_next_up": 1.0})
        for fn in (D.distributions, D.relationships, D.binned_rates, D.reversal_test):
            with self.assertRaises(AssertionError):
                fn(bad)


class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="v49test_"))
        cls.data = cls.root / "data"
        cls.data.mkdir()
        T8.write_source_csv(cls.data / "OPTIDX_NIFTY_CE_01-Jan-2026_TO_31-Mar-2026.csv", n_dates=80, seed=11)
        A.run_audit(cls.data, verbose=False)
        V.run(cls.data, cls.data, verbose=False)               # produces the V4.8 reports V4.9 reads
        cls.out1, cls.out2 = cls.root / "o1", cls.root / "o2"
        cls.res = D.run(cls.data, cls.out1, verbose=False)
        D.run(cls.data, cls.out2, verbose=False)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_alignment_checks_all_pass(self):
        chk = self.res["checks"]
        self.assertFalse((chk.result == "FAIL").any(), chk[chk.result == "FAIL"].to_string())

    def test_reports_and_zip(self):
        names = {p.name for p in self.out1.glob("V4_9_*")}
        for n in ("Temporal_Alignment_Checks", "Feature_Distributions", "Date_Level_Performance", "Calibration", "Diagnostic_Conclusions",
                  "Hand_Worked_Examples", "Sign_Reversal_Test", "B3_Driver_Analysis", "Eligibility_Correction_Effect"):
            self.assertIn(f"V4_9_{n}.csv", names)
        self.assertIn("V4_9_Reports.zip", names)

    def test_deterministic_rerun(self):
        for p in sorted(self.out1.glob("V4_9_*")):
            self.assertEqual(A.sha256(p), A.sha256(self.out2 / p.name), p.name)

    def test_inputs_unmodified(self):
        before = {p.name: A.sha256(p) for p in self.data.iterdir()}
        D.run(self.data, self.root / "o3", verbose=False)
        self.assertEqual(before, {p.name: A.sha256(p) for p in self.data.iterdir()})

    def test_conclusions_have_verdicts_and_no_skill_claim(self):
        c = self.res["conclusions"]
        self.assertTrue(c["verdict"].notna().all() and (c["evidence"].str.len() > 20).all())
        self.assertFalse(c["verdict"].str.contains("profit|skill", case=False).any())

    def test_fails_loudly_on_a_misaligned_dataset(self):
        d = self.root / "bad"
        shutil.copytree(self.data, d)
        ds = pd.read_csv(d / "V4_8_Dataset.csv")
        ds["target_next_up"] = ds.groupby(["option_type", "strike"])["target_next_up"].shift(1)   # labels pushed one day
        ds.to_csv(d / "V4_8_Dataset.csv", index=False)
        with self.assertRaises(AssertionError):
            D.run(d, self.root / "o_bad", verbose=False)

    def test_missing_v48_report_is_an_error(self):
        d = self.root / "missing"
        shutil.copytree(self.data, d)
        (d / "V4_8_Baseline_Comparison.csv").unlink()
        with self.assertRaises(FileNotFoundError):
            D.run(d, self.root / "o_missing", verbose=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
