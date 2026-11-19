"""Automated tests for V5.0 (stdlib unittest). Run: python -m unittest test_v5_0 -v"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_v4_8_feature_research as T8
import v4_7_source_audit as A
import v4_8_feature_research as V
import v5_0_data_expansion as X
import v5_0_stability_study as S

S.N_PERM, S.N_BOOT, V.N_BOOT = 150, 60, 60


def cw_csv(path, df):
    pd.DataFrame({"Symbol": "NIFTY", "Date": df.date.dt.strftime("%d-%b-%Y"), "Expiry": "29-Sep-2026", "Option type": df.option_type,
                  "Strike Price": df.strike, "Open": "-", "High": "-", "Low": "-", "Close": df.src_settle + 1.5, "LTP": "-", "Settle Price": df.src_settle,
                  "No. of contracts": 5, "Turnover * in  ₹ Lakhs": 1.0, "Premium Turnover ** in   ₹ Lakhs": 0.1, "Open Int": 100,
                  "Change in OI": 1, "Underlying Value": df.src_underlying}).to_csv(path, index=False)


def bhav_csv(path, rows, date, drop_col=None):
    out = []
    for r in rows:
        out.append({"TradDt": date, "BizDt": date, "Sgmt": "FO", "Src": "NSE", "FinInstrmTp": "IDO", "TckrSymb": "NIFTY", "XpryDt": "2026-09-29",
                    "StrkPric": f"{r['strike']:.2f}", "OptnTp": r["ot"], "OpnPric": "0", "HghPric": "0", "LwPric": "0", "ClsPric": f"{r['close']:.2f}",
                    "LastPric": "0", "PrvsClsgPric": "0", "UndrlygPric": f"{r['und']:.2f}", "SttlmPric": f"{r['settle']:.2f}", "OpnIntrst": "100",
                    "ChngInOpnIntrst": "1", "TtlTradgVol": "5", "TtlTrfVal": "1000", "TtlNbOfTxsExctd": "3"})
    df = pd.DataFrame(out)
    if drop_col:
        df = df.drop(columns=[drop_col])
    df.to_csv(path, index=False)


def make_dir(root, n_dates=100, pe_end=70, bhav_idx=(80, 81), conflict=False):
    """CE for all dates, PE only up to index pe_end; bhavcopy (official) for a few later dates, consistent with the same price series."""
    df, dates = T8.synth_frame(n_dates=n_dates, seed=4)
    d = Path(root)
    d.mkdir(parents=True, exist_ok=True)
    ce = df[df.option_type == "CE"]
    pe = df[(df.option_type == "PE") & (df.date <= dates[pe_end - 1])]
    cw_csv(d / f"OPTIDX_NIFTY_CE_{dates[0].strftime('%d-%b-%Y')}_TO_{dates[-1].strftime('%d-%b-%Y')}.csv", ce)
    cw_csv(d / f"OPTIDX_NIFTY_PE_{dates[0].strftime('%d-%b-%Y')}_TO_{dates[pe_end - 1].strftime('%d-%b-%Y')}.csv", pe)
    for i in bhav_idx:
        day = df[df.date == dates[i]]
        rows = [{"strike": r.strike, "ot": r.option_type, "close": r.src_settle + 1.5, "settle": r.src_settle, "und": r.src_underlying} for r in day.itertuples()]
        if conflict and i == bhav_idx[0]:     # make the CE rows of this date disagree with the contract-wise file
            for r in rows:
                if r["ot"] == "CE":
                    r["settle"] += 9.0
        bhav_csv(d / f"BhavCopy_NSE_FO_0_0_0_{dates[i].strftime('%Y%m%d')}_F_0000.csv", rows, dates[i].strftime("%Y-%m-%d"))
    return d, df, dates


class TestGapsAndManifest(unittest.TestCase):
    def test_gap_ranges_hand_example(self):
        av = pd.DataFrame({"date": [f"2026-01-{i:02d}" for i in range(1, 9)],
                           "PE_available": [True, False, False, False, True, False, True, True], "CE_available": True})
        g = X.gap_ranges(av, "PE")
        self.assertEqual(g.values.tolist(), [["PE", "2026-01-02", "2026-01-04", 3], ["PE", "2026-01-06", "2026-01-06", 1]])
        self.assertTrue(X.gap_ranges(av, "CE").empty)
        av.loc[7, "PE_available"] = False        # gap running to the end of the calendar
        self.assertEqual(X.gap_ranges(av, "PE").iloc[-1].tolist(), ["PE", "2026-01-08", "2026-01-08", 1])


class TestDataExpansion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="v50x_"))
        cls.data, cls.df, cls.dates = make_dir(cls.root / "d")
        cls.before = {p.name: A.sha256(p) for p in cls.data.iterdir()}
        cls.out = cls.root / "o1"
        cls.res = X.run(cls.data, cls.out, verbose=False)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_raw_files_untouched(self):
        self.assertEqual(self.before, {p.name: A.sha256(p) for p in self.data.iterdir()})
        chk = self.res["checks"]
        self.assertEqual(int(chk[chk.check.str.contains("raw input files unchanged")].issues.iloc[0]), 0)

    def test_nothing_fabricated_or_interpolated(self):
        st = self.res["study"]
        src_pairs = set(map(tuple, self.res["cw"][["date", "option_type"]].drop_duplicates().to_numpy().tolist())) | \
            set(map(tuple, self.res["bh"][["date", "option_type"]].drop_duplicates().to_numpy().tolist()))
        got = set(map(tuple, st[["date", "option_type"]].drop_duplicates().to_numpy().tolist()))
        self.assertEqual(got, src_pairs)                                   # no (date, type) appears that no source supplied
        pe_dates = set(st[st.option_type == "PE"].date)
        gap = set(self.dates[70:80]) | set(self.dates[82:])
        self.assertTrue(pe_dates.isdisjoint(gap))                          # PE gap dates stay empty
        self.assertEqual(len(st), len(self.res["cw"]) + int(((self.res["bh"].option_type == "PE") & self.res["bh"].date.isin([self.dates[80], self.dates[81]])).sum()))

    def test_bhavcopy_fills_only_missing_and_keeps_close_distinct(self):
        st = self.res["study"]
        new = st[(st.origin == "official_bhavcopy_udiff")]
        self.assertTrue((new.option_type == "PE").all())                   # CE dates were already in the contract-wise files
        self.assertTrue(set(new.date) == {self.dates[80], self.dates[81]})
        self.assertTrue(((new.close - new.settle).abs() > 1.0).all())      # close (settle+1.5) never replaced by or aliased to settlement

    def test_manifest_ranks_missing_and_does_not_rerequest_local(self):
        m = self.res["manifest"]
        pe_win = m[m.request.str.contains("contract-wise PE")]
        self.assertTrue((pe_win.status == "NOT ACQUIRED").all())
        daily = m[m.request.str.contains("alternative to the PE export")]
        requested = set(daily.expected_filename)
        for i in (80, 81):                                                 # present locally -> not requested again
            self.assertNotIn(f"BhavCopy_NSE_FO_0_0_0_{self.dates[i].strftime('%Y%m%d')}_F_0000.csv", requested)
        self.assertIn(f"BhavCopy_NSE_FO_0_0_0_{self.dates[75].strftime('%Y%m%d')}_F_0000.csv", requested)
        self.assertTrue((m[m.status == "NOT ACQUIRED"].sha256.fillna("") == "").all())   # never claims a hash for a file it does not have
        self.assertTrue((m[m.status == "present locally"].priority == 0).sum() >= 2)

    def test_gaps_report(self):
        g = self.res["gaps"]
        pe = g[g.option_type == "PE"]
        self.assertEqual(pe.missing_trading_dates.sum(), 100 - 70 - 2)
        self.assertTrue(g[g.option_type == "CE"].empty)

    def test_rerun_is_byte_identical(self):
        out2 = self.root / "o2"
        X.run(self.data, out2, verbose=False)
        for p in sorted(self.out.glob("V5_0_*")):
            self.assertEqual(A.sha256(p), A.sha256(out2 / p.name), p.name)

    def test_schema_and_date_checks_fire(self):
        d, _, dates = make_dir(self.root / "bad", n_dates=40, pe_end=30, bhav_idx=())
        bhav_csv(d / "BhavCopy_NSE_FO_0_0_0_20260401_F_0000.csv", [{"strike": 21000.0, "ot": "PE", "close": 5.0, "settle": 4.0, "und": 23000.0}], "2026-04-02")
        bhav_csv(d / "BhavCopy_NSE_FO_0_0_0_20260402_F_0000.csv", [{"strike": 21000.0, "ot": "PE", "close": 5.0, "settle": 4.0, "und": 23000.0}],
                 "2026-04-02", drop_col="SttlmPric")
        chk = X.run(d, self.root / "ob", verbose=False)["checks"]
        self.assertGreater(int(chk[chk.check == "required columns present"].issues.sum()), 0)
        self.assertGreater(int(chk[chk.check.str.contains("differs from filename date")].issues.sum()), 0)

    def test_source_conflicts_are_flagged_not_overwritten(self):
        d, df, dates = make_dir(self.root / "conf", conflict=True)
        r = X.run(d, self.root / "oc", verbose=False)
        st = r["study"]
        c = st[(st.date == dates[80]) & (st.option_type == "CE")]
        self.assertTrue(c.settle_conflict.all())
        raw = df[(df.date == dates[80]) & (df.option_type == "CE")].set_index("strike").src_settle
        np.testing.assert_allclose(c.set_index("strike").settle.sort_index().to_numpy(), raw.sort_index().to_numpy())   # contract-wise value kept
        self.assertEqual(int(r["checks"][r["checks"].check.str.contains("settlement conflicts")].issues.iloc[0]), len(c))
        chk = r["checks"]
        self.assertTrue((chk[chk.check == "settle: contract-wise vs official bhavcopy disagree"].issues > 0).all())


class TestStudy(unittest.TestCase):
    def _frame(self, flip_block=None, n_blocks=5, dates_per_block=30, rows_per_date=6, seed=0):
        rng = np.random.default_rng(seed)
        rows = []
        for b in range(n_blocks):
            sign = -1 if (flip_block is not None and b >= flip_block) else 1
            for k in range(dates_per_block):
                i = b * S.BLOCK_LEN + k
                x = rng.normal()
                for _ in range(rows_per_date):
                    rows.append({"option_type": "CE", "block": b, "cal_idx": i, "date": pd.Timestamp("2026-01-01") + pd.Timedelta(days=i),
                                 "target_date": pd.Timestamp("2026-01-01") + pd.Timedelta(days=i + 1), "lr1": x, "ur1": x, "sm": 0.0, "bucket": "ATM",
                                 "target_next_up": float(sign * x + rng.normal(0, 0.7) > 0)})
        return pd.DataFrame(rows)

    def test_heterogeneity_detects_a_flip_and_not_stability(self):
        flip = S.heterogeneity(self._frame(flip_block=3), "CE", "lr1", n_perm=300)
        stable = S.heterogeneity(self._frame(), "CE", "lr1", n_perm=300)
        self.assertLess(flip["p_value"], 0.01)
        self.assertGreater(stable["p_value"], 0.05)
        self.assertEqual(S.heterogeneity(self._frame(), "CE", "lr1", n_perm=300), stable)     # seeded -> deterministic

    def test_small_blocks_are_excluded_and_listed(self):
        f = self._frame(n_blocks=3)
        small = f[f.block == 2].copy()
        small = small[small.date <= small.date.min() + pd.Timedelta(days=4)]
        f2 = pd.concat([f[f.block < 2], small])
        t = S.block_table(f2.assign(eligible=True), "lr1")
        self.assertEqual(t[t.block == 2].included_in_tests.iloc[0], False)
        self.assertIn("sample-size", t[t.block == 2].exclusion_reason.iloc[0])
        h = S.heterogeneity(f2, "CE", "lr1", n_perm=50)
        self.assertEqual(h["blocks_included"], 2)
        self.assertTrue(np.isnan(S.heterogeneity(f2[f2.block == 0], "CE", "lr1", n_perm=50)["p_value"]))

    def test_walk_forward_purges_boundary_and_trains_only_on_the_past(self):
        f = self._frame()
        seen = []
        real = V.fit_model

        def spy(kind, cols, arrs, y):
            seen.append(len(y))
            return real(kind, cols, arrs, y)
        with mock.patch.object(V, "fit_model", spy):
            wf, integ, pool = S.walk_forward(f, "CE", "lr1")
        self.assertEqual(wf.test_block.tolist(), [2, 3, 4])               # needs >= 2 earlier blocks
        self.assertTrue((integ.train_label_reaches_test_block == 0).all())
        self.assertTrue((integ.purged_train_rows == 6).all())              # last date of the preceding block, 6 rows
        # the first fit for test block 2 used exactly blocks 0-1 minus the purged last day (2*30*6 - 6 rows)
        self.assertEqual(seen[0], 2 * 30 * 6 - 6)
        self.assertTrue((wf.train_rows == [2 * 180 - 6, 3 * 180 - 6, 4 * 180 - 6]).all())
        self.assertEqual(wf.test_rows.tolist(), [180, 180, 180])
        # test labels never influence the fit: flip all labels in block 4, the training sizes/predictions for block 4 are unchanged
        g = f.copy()
        g.loc[g.block == 4, "target_next_up"] = 1 - g.loc[g.block == 4, "target_next_up"]
        wf2, _, _ = S.walk_forward(g, "CE", "lr1")
        self.assertEqual(wf.brier_prior.iloc[:2].tolist(), wf2.brier_prior.iloc[:2].tolist())
        self.assertEqual(wf.train_rows.tolist(), wf2.train_rows.tolist())

    def test_pooled_interval_hand_check(self):
        t = pd.DataFrame({"date": pd.date_range("2026-01-01", periods=40), "dse": -0.01, "block": 2})
        p = S.pooled(t, "CE", "lr1", 0.975)
        self.assertAlmostEqual(p["pooled_brier_diff_vs_prior"], -0.01)
        self.assertTrue(p["interval_entirely_below_0"])
        t["dse"] = np.where(np.arange(40) % 2 == 0, 0.05, -0.05)
        self.assertFalse(S.pooled(t, "CE", "lr1", 0.975)["interval_entirely_below_0"])
        self.assertEqual(S.pooled(t.iloc[:0], "PE", "ur1", 0.975)["test_blocks"], 0)

    def test_lags_are_computed_before_filtering(self):
        df, dates = T8.synth_frame(n_dates=14, low_day=6)
        st = pd.DataFrame({"source_file": "x", "symbol": "NIFTY", "expiry": df.expiry, "option_type": df.option_type, "strike": df.strike,
                           "date": df.date, "settle": df.src_settle, "underlying": df.src_underlying, "close": df.src_settle, "origin": "contract_wise_csv",
                           "settle_conflict": False, "close_conflict": False})
        r, _, _, _ = S.prepare(st)
        c = r[(r.option_type == "CE") & (r.strike == 21000.0)].set_index("date")
        s = df[(df.option_type == "CE") & (df.strike == 21000.0)].set_index("date").src_settle
        self.assertAlmostEqual(c.loc[dates[8], "settle_return_lag1"], s[dates[7]] / s[dates[6]] - 1, places=12)
        self.assertFalse(c.loc[dates[8], "eligible"])
        self.assertTrue(c.loc[dates[8], "R1_features"])
        self.assertIn("R3_price_floor", c.loc[dates[8], "exclusion_reason"])
        self.assertTrue((r[~r.eligible].exclusion_reason != "").all() and len(r) == len(st))   # nothing deleted

    def test_hypotheses_are_predeclared_constants(self):
        self.assertEqual([h[0] for h in S.HYPOTHESES], ["H1", "H2", "H3", "H4", "H5"])
        self.assertEqual(S.N_MAIN, 2)
        self.assertEqual((S.BLOCK_LEN, S.MIN_DATES_BLOCK, S.MIN_TRAIN_BLOCKS), (30, 15, 2))
        self.assertIn("same in every included chronological block", S.HYPOTHESES[0][3])


class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="v50e_"))
        cls.data, cls.df, cls.dates = make_dir(cls.root / "d", n_dates=130, pe_end=100, bhav_idx=(110, 111, 112))
        A.run_audit(cls.data, verbose=False)
        cls.out1, cls.out2 = cls.root / "o1", cls.root / "o2"
        cls.res = S.run_all(cls.data, cls.out1, verbose=False)
        S.run_all(cls.data, cls.out2, verbose=False)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_deterministic_rerun_all_files_and_zip(self):
        files = sorted(self.out1.glob("V5_0_*"))
        self.assertGreaterEqual(len(files), 17)
        for p in files:
            self.assertEqual(A.sha256(p), A.sha256(self.out2 / p.name), p.name)

    def test_pe_sample_limitation_is_reported_not_hidden(self):
        lay = self.res["layout"]
        pe = lay[lay.option_type == "PE"]
        self.assertTrue((~pe[pe.eligible_dates < S.MIN_DATES_BLOCK].included_in_tests).all())
        cov = self.res["coverage"]
        a = cov[(cov.option_type == "PE") & cov.dataset.str.startswith("after")].iloc[0]
        b = cov[(cov.option_type == "PE") & cov.dataset.str.startswith("before")].iloc[0]
        self.assertGreater(a.raw_rows, b.raw_rows)                         # official rows added
        self.assertEqual(a.raw_dates - b.raw_dates, 3)

    def test_hypotheses_report_has_verdicts_and_no_skill_claim(self):
        h = self.res["hypotheses"]
        self.assertEqual(len(h), 5)
        self.assertFalse(h.verdict.str.contains("profit|skill", case=False).any())
        self.assertTrue(h.caveat.str.contains("exploratory").all())

    def test_gate_fails_closed(self):
        d = self.root / "g1"
        shutil.copytree(self.data, d)
        for p in d.glob("V4_7_Audit_*"):
            p.unlink()
        with self.assertRaises(V.GateError):
            S.run_all(d, self.root / "og1", verbose=False)
        d = self.root / "g2"
        shutil.copytree(self.data, d)
        f = next(d.glob("OPTIDX_NIFTY_CE_*.csv"))
        f.write_text(f.read_text().replace("21000.0", "21001.0", 1))
        with self.assertRaises(V.GateError):
            S.run_all(d, self.root / "og2", verbose=False)
        self.assertFalse((self.root / "og1").exists() and any((self.root / "og1").glob("V5_0_*")))

    def test_inputs_unmodified(self):
        before = {p.name: A.sha256(p) for p in self.data.iterdir()}
        S.run_all(self.data, self.root / "o3", verbose=False)
        self.assertEqual(before, {p.name: A.sha256(p) for p in self.data.iterdir()})


if __name__ == "__main__":
    unittest.main(verbosity=2)
