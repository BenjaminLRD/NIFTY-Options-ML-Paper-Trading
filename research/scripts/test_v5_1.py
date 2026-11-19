"""Automated tests for the V5.1 acquisition audit and protocol freeze (stdlib unittest). Run: python -m unittest test_v5_1 -v"""
import ast
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import pandas as pd

import numpy as np
import v5_1_acquisition_audit as A
import v5_1_evaluation as E
import v5_1_freeze_protocol as F


def bhav_zip(path, date, rows):
    cols = ["TradDt", "FinInstrmTp", "TckrSymb", "XpryDt", "StrkPric", "OptnTp", "OpnPric", "HghPric", "LwPric", "ClsPric", "LastPric",
            "PrvsClsgPric", "UndrlygPric", "SttlmPric", "OpnIntrst", "ChngInOpnIntrst", "TtlTradgVol", "TtlNbOfTxsExctd"]
    recs = [dict(zip(cols, [date, "IDO", "NIFTY", "2026-09-29", k, o, 10, 12, 9, 11, 11, 10, 24000, s, 100, 0, v, 5])) for k, o, s, v in rows]
    recs.append(dict(zip(cols, [date, "IDF", "NIFTY", "2026-09-29", "", "", 1, 1, 1, 1, 1, 1, 24000, 1, 1, 1, 1, 1])))  # futures row must be ignored
    buf = io.StringIO()
    pd.DataFrame(recs).to_csv(buf, index=False)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(Path(path).name[:-4], buf.getvalue())


class TestLoad(unittest.TestCase):
    def test_only_nifty_index_options_are_kept(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "BhavCopy_NSE_FO_0_0_0_20260630_F_0000.csv.zip"
            bhav_zip(p, "2026-06-30", [(24000, "CE", 50, 10), (24000, "PE", 40, 0)])
            d, facts = A.load_zip(p)
            self.assertEqual(len(d), 2)
            self.assertEqual(set(d.option_type), {"CE", "PE"})
            self.assertEqual(facts["missing_columns"], "")

    def test_missing_column_is_reported_not_guessed(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "BhavCopy_NSE_FO_0_0_0_20260630_F_0000.csv.zip"
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("x.csv", "TradDt,TckrSymb\n2026-06-30,NIFTY\n")
            d, facts = A.load_zip(p)
            self.assertTrue(d.empty)
            self.assertIn("SttlmPric", facts["missing_columns"])


class TestIntegrity(unittest.TestCase):
    def frame(self, rows):
        df = pd.DataFrame(rows, columns=["date", "expiry", "option_type", "strike", "open", "high", "low", "close", "last", "prev_close",
                                         "underlying", "settle", "open_interest", "change_in_oi", "volume", "transactions"])
        df["date"], df["expiry"] = pd.to_datetime(df["date"]), pd.to_datetime(df["expiry"])
        return df

    def manifest(self):
        return pd.DataFrame([{"file": "BhavCopy_NSE_FO_0_0_0_20260630_F_0000.csv.zip", "trade_date": "2026-06-30", "on_disk": True,
                              "hash_matches_log": True, "missing_required_columns": "", "first_trade_date": "2026-06-30",
                              "trade_dates_in_file": 1, "retrieval_status": "downloaded"}])

    def row(self, **kw):
        base = dict(date="2026-06-30", expiry="2026-09-29", option_type="CE", strike=24000, open=10, high=12, low=9, close=11, last=11,
                    prev_close=10, underlying=24000.0, settle=11, open_interest=100, change_in_oi=0, volume=10, transactions=5)
        base.update(kw)
        return list(base.values())

    def result(self, checks, text):
        return checks[checks["check"].str.contains(text, regex=False)].iloc[0]

    def test_clean_data_has_no_review_items(self):
        df = self.frame([self.row(), self.row(option_type="PE")])
        c = A.integrity_checks(df, self.manifest(), [24000.0], pd.bdate_range("2026-06-30", "2026-06-30"))
        self.assertEqual(int((c.result == "REVIEW").sum()), 0)

    def test_duplicate_key_is_flagged(self):
        df = self.frame([self.row(), self.row()])
        c = A.integrity_checks(df, self.manifest(), [24000.0], pd.bdate_range("2026-06-30", "2026-06-30"))
        self.assertEqual(self.result(c, "duplicate contract-date keys").result, "REVIEW")

    def test_zero_settlement_is_noted_never_repaired(self):
        df = self.frame([self.row(settle=0.0)])
        c = A.integrity_checks(df, self.manifest(), [24000.0], pd.bdate_range("2026-06-30", "2026-06-30"))
        self.assertEqual(self.result(c, "settlement <= 0").result, "NOTE")
        self.assertEqual(float(df.settle.iloc[0]), 0.0)

    def test_two_underlying_values_on_one_date_are_flagged(self):
        df = self.frame([self.row(), self.row(option_type="PE", underlying=25000.0)])
        c = A.integrity_checks(df, self.manifest(), [24000.0], pd.bdate_range("2026-06-30", "2026-06-30"))
        self.assertEqual(self.result(c, "more than one underlying").result, "REVIEW")

    def test_date_outside_window_is_flagged(self):
        df = self.frame([self.row(date="2026-06-29")])
        c = A.integrity_checks(df, self.manifest(), [24000.0], pd.bdate_range("2026-06-30", "2026-06-30"))
        self.assertEqual(self.result(c, "outside the declared window").result, "REVIEW")

    def test_hash_mismatch_is_flagged(self):
        m = self.manifest()
        m["hash_matches_log"] = False
        c = A.integrity_checks(self.frame([self.row()]), m, [24000.0], pd.bdate_range("2026-06-30", "2026-06-30"))
        self.assertEqual(self.result(c, "download hash").result, "REVIEW")


class TestContamination(unittest.TestCase):
    def test_new_window_dates_in_an_old_artifact_are_detected(self):
        with tempfile.TemporaryDirectory() as t:
            pd.DataFrame({"date": ["2026-06-29", "2026-07-01"]}).to_csv(Path(t) / "V4_8_Dataset.csv", index=False)
            pd.DataFrame({"date": ["2026-06-25"]}).to_csv(Path(t) / "V5_0_Study_Dataset.csv", index=False)
            c = A.contamination_audit(t).set_index("artifact")
            self.assertTrue(c.loc["V4_8_Dataset.csv", "uses_dates_in_new_window"])
            self.assertFalse(c.loc["V5_0_Study_Dataset.csv", "uses_dates_in_new_window"])

    def test_expiry_date_is_not_a_trade_date(self):
        with tempfile.TemporaryDirectory() as t:
            pd.DataFrame({"date": ["2026-06-29"], "expiry": ["2026-09-29"]}).to_csv(Path(t) / "V4_8_Dataset.csv", index=False)
            c = A.contamination_audit(t)
            self.assertFalse(bool(c["uses_dates_in_new_window"].any()))


def make_project(t):
    """A minimal project: every manifest file with junk bytes (the freeze must never parse them), two raw zips and a matching download log."""
    root = Path(t)
    for n in F.CODE:
        (root / n).write_bytes(b"\x00\xffnot a csv or python file " + n.encode())
    (root / F.RAW_DIR).mkdir()
    for n in ("a.csv.zip", "b.csv.zip"):
        (root / F.RAW_DIR / n).write_bytes(b"PK" + n.encode())
    (root / F.DOWNLOAD_LOG).write_text("file,status\na.csv.zip,downloaded\nb.csv.zip,downloaded\n", encoding="utf-8")
    return root


def edit_record(root, fn):
    p = root / F.RECORD
    rec = json.loads(p.read_text(encoding="utf-8"))
    fn(rec)
    p.write_text(json.dumps(rec), encoding="utf-8")


class TestFreeze(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = make_project(self._t.name)
        self.rec = F.freeze(self.root)
        self.good = F.verify(self.root)

    def tearDown(self):
        self._t.cleanup()

    def assertBroken(self, text=None):
        with self.assertRaises(RuntimeError) as cm:
            F.verify(self.root)
        if text:
            self.assertIn(text, str(cm.exception))

    def test_intact_project_verifies_and_hash_is_recomputed(self):
        self.assertEqual(self.good, self.rec["combined_sha256"])
        edit_record(self.root, lambda r: r.update(combined_sha256="0" * 64))
        self.assertBroken("combined hash")          # a stored hash is never simply returned

    def test_changed_evaluation_script_fails(self):
        (self.root / "v5_1_evaluation.py").write_text("changed", encoding="utf-8")
        self.assertBroken("changed after freeze: v5_1_evaluation.py")

    def test_every_imported_module_and_the_verifier_are_covered(self):
        for n in ("v4_7_model_dataset.py", "v4_8_feature_research.py", "v5_0_stability_study.py", "v5_1_freeze_protocol.py", "test_v5_1.py"):
            with self.subTest(n):
                self.assertIn(n, F.CODE)
                orig = (self.root / n).read_bytes()
                (self.root / n).write_bytes(b"x")
                self.assertBroken(f"changed after freeze: {n}")
                (self.root / n).write_bytes(orig)
                self.assertEqual(F.verify(self.root), self.good)

    def test_changed_raw_zip_fails(self):
        (self.root / F.RAW_DIR / "a.csv.zip").write_bytes(b"PKchanged")
        self.assertBroken("changed after freeze: V5_1_Acquired_Raw/a.csv.zip")

    def test_changed_training_data_fails(self):
        (self.root / "V5_0_Study_Dataset.csv").write_bytes(b"other")
        self.assertBroken("changed after freeze: V5_0_Study_Dataset.csv")

    def test_changed_protocol_text_in_record_fails(self):
        edit_record(self.root, lambda r: r["protocol"].update(metric="something else"))
        self.assertBroken("protocol")

    def test_protocol_changed_in_code_fails(self):
        old = dict(F.PROTOCOL)
        try:
            F.PROTOCOL["metric"] = "edited after freeze"
            self.assertBroken("protocol")
        finally:
            F.PROTOCOL.clear()
            F.PROTOCOL.update(old)

    def test_changed_individual_recorded_hash_fails(self):
        edit_record(self.root, lambda r: r["file_sha256"].update({"v5_1_evaluation.py": "f" * 64}))
        self.assertBroken("v5_1_evaluation.py")

    def test_consistent_tampering_of_file_and_recorded_hash_still_fails_on_combined(self):
        (self.root / "v5_1_evaluation.py").write_text("changed", encoding="utf-8")
        new = F.sha256(self.root / "v5_1_evaluation.py")
        edit_record(self.root, lambda r: r["file_sha256"].update({"v5_1_evaluation.py": new}))
        self.assertBroken("combined hash")

    def test_changed_combined_hash_fails(self):
        edit_record(self.root, lambda r: r.update(combined_sha256="a" * 64))
        self.assertBroken()

    def test_external_expected_hash_must_match(self):
        self.assertEqual(F.verify(self.root, self.good), self.good)
        with self.assertRaises(RuntimeError):
            F.verify(self.root, "b" * 64)

    def test_missing_evaluation_script_fails(self):
        (self.root / "v5_1_evaluation.py").unlink()
        self.assertBroken("missing on disk: v5_1_evaluation.py")

    def test_missing_required_configuration_fails(self):
        (self.root / "V5_0_Hypotheses.csv").unlink()
        self.assertBroken("V5_0_Hypotheses.csv")

    def test_added_raw_zip_fails(self):
        (self.root / F.RAW_DIR / "c.csv.zip").write_bytes(b"PKc")
        self.assertBroken("added after freeze: V5_1_Acquired_Raw/c.csv.zip")

    def test_removed_raw_zip_fails(self):
        (self.root / F.RAW_DIR / "b.csv.zip").unlink()
        self.assertBroken("removed after freeze: V5_1_Acquired_Raw/b.csv.zip")

    def test_required_file_dropped_from_record_fails(self):
        edit_record(self.root, lambda r: r["file_sha256"].pop("v5_1_evaluation.py"))
        self.assertBroken("not in the frozen record: v5_1_evaluation.py")

    def test_download_log_must_match_zips(self):
        (self.root / F.DOWNLOAD_LOG).write_text("file,status\na.csv.zip,downloaded\n", encoding="utf-8")
        edit_record(self.root, lambda r: r["file_sha256"].update({F.DOWNLOAD_LOG: F.sha256(self.root / F.DOWNLOAD_LOG)}))
        self.assertBroken("download log")

    def test_freeze_refuses_when_a_manifest_file_is_missing(self):
        (self.root / "v5_1_evaluation.py").unlink()
        with self.assertRaises(FileNotFoundError):
            F.freeze(self.root)


class TestFreezeComputesNothing(unittest.TestCase):
    def test_module_imports_no_data_library(self):
        tree = ast.parse(Path(F.__file__).read_text(encoding="utf-8"))
        mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertFalse(mods & {"pandas", "numpy", "scipy", "sklearn"}, mods)

    def test_freeze_and_verify_run_on_unparseable_bytes(self):
        with tempfile.TemporaryDirectory() as t:
            root = make_project(t)
            F.freeze(root)
            F.verify(root)          # junk bytes everywhere: nothing was parsed, so nothing was computed from the data


class TestEvaluationUnits(unittest.TestCase):
    """Synthetic data only. The evaluation itself is never run here."""

    def test_split_purges_labels_crossing_the_boundary(self):
        d = pd.to_datetime
        el = pd.DataFrame({"date": d(["2026-06-26", "2026-06-29", "2026-06-30", "2026-07-01"]),
                           "target_date": d(["2026-06-29", "2026-06-30", "2026-07-01", "2026-07-02"])})
        tr, te = E.split(el)
        self.assertEqual(list(tr["date"].dt.strftime("%Y-%m-%d")), ["2026-06-26"])
        self.assertEqual(list(te["date"].dt.strftime("%Y-%m-%d")), ["2026-06-30", "2026-07-01"])
        self.assertEqual(len(set(tr.index) & set(te.index)), 0)

    def test_blocks_keep_v50_layout_and_merge_the_remainder(self):
        r = pd.DataFrame({"cal_idx": np.arange(245)})        # 180 old dates + 65 new
        b = E.assign_blocks(r, 180)["block"]
        self.assertEqual(b[:180].tolist(), (np.arange(180) // 30).tolist())
        self.assertEqual(b[180:210].unique().tolist(), [6])
        self.assertEqual(b[210:].unique().tolist(), [7])
        self.assertEqual(int((b == 7).sum()), 35)

    def test_bootstrap_is_deterministic_and_degenerate_for_constants(self):
        dates = np.repeat(pd.date_range("2026-07-01", periods=12).to_numpy(), 3)
        dse = np.random.default_rng(1).normal(size=len(dates))
        a, b = E.block_bootstrap_diff(dates, dse, 200), E.block_bootstrap_diff(dates, dse, 200)
        self.assertTrue(np.array_equal(a, b))
        lo, hi = E.interval(a)
        self.assertLess(lo, dse.mean())
        self.assertGreater(hi, dse.mean())
        c = E.block_bootstrap_diff(dates, np.full(len(dates), 0.25), 50)
        self.assertTrue(np.allclose(c, 0.25))

    def test_run_refuses_when_the_freeze_is_broken_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as t:
            root = make_project(t)
            good = F.freeze(root)["combined_sha256"]
            (root / "v5_0_stability_study.py").write_bytes(b"edited after freeze")
            with self.assertRaises(RuntimeError):
                E.run(root, expected_combined=good, verbose=False)
            self.assertEqual([p.name for p in root.glob("V5_1_Eval_*")], [])

    def test_run_refuses_without_the_externally_kept_hash(self):
        with tempfile.TemporaryDirectory() as t:
            root = make_project(t)
            F.freeze(root)
            with self.assertRaises(RuntimeError):
                E.run(root, expected_combined="0" * 64, verbose=False)

    def test_run_is_one_shot(self):
        with tempfile.TemporaryDirectory() as t:
            root = make_project(t)
            good = F.freeze(root)["combined_sha256"]
            (root / "V5_1_Eval_Run_Record.csv").write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "already run"):
                E.run(root, expected_combined=good, verbose=False)


class TestEvaluationSmoke(unittest.TestCase):
    """End-to-end run of the evaluation glue on a fabricated random-walk project in a temp folder (never on the real window)."""

    STRIKES = (23500.0, 24000.0, 24500.0)

    def build(self, root):
        rng = np.random.default_rng(7)
        old_dates = pd.bdate_range(end="2026-06-29", periods=60)
        new_dates = pd.bdate_range("2026-06-30", periods=65)
        alld = old_dates.append(new_dates)
        und = pd.Series(24000 * np.exp(np.cumsum(rng.normal(0, 0.004, len(alld)))), index=alld)
        rows = []
        for ot in ("CE", "PE"):
            for k in self.STRIKES:
                s = 100 * np.exp(np.cumsum(rng.normal(0, 0.05, len(alld))))
                for d, v in zip(alld, s):
                    rows.append({"date": d, "symbol": "NIFTY", "expiry": pd.Timestamp("2026-09-29"), "option_type": ot, "strike": k,
                                 "settle": round(float(v), 2), "underlying": round(float(und[d]), 2), "origin": "contract_wise_csv",
                                 "source_file": "synthetic.csv", "settle_conflict": False, "close_conflict": False})
        full = pd.DataFrame(rows)
        old = full[full["date"] <= pd.Timestamp("2026-06-29")]
        old.to_csv(root / "V5_0_Normalized_Study_Universe.csv", index=False)
        import v5_0_stability_study as S
        r, _, _, _ = S.prepare(old)
        ds = r[["option_type", "strike", "date", "target_date", "eligible"]].copy()
        ds["date"], ds["target_date"] = ds["date"].dt.strftime("%Y-%m-%d"), ds["target_date"].dt.strftime("%Y-%m-%d")
        ds.to_csv(root / "V5_0_Study_Dataset.csv", index=False)
        pd.DataFrame([{"scope": "x", "check": "y", "issues": 0, "result": "PASS", "detail": ""}]).to_csv(root / "V5_1_Raw_Integrity_Report.csv", index=False)
        cols = ["TradDt", "FinInstrmTp", "TckrSymb", "XpryDt", "StrkPric", "OptnTp", "OpnPric", "HghPric", "LwPric", "ClsPric", "LastPric",
                "PrvsClsgPric", "UndrlygPric", "SttlmPric", "OpnIntrst", "ChngInOpnIntrst", "TtlTradgVol", "TtlNbOfTxsExctd"]
        log = ["file,status"]
        for d in new_dates:
            day = full[full["date"] == d]
            recs = [dict(zip(cols, [d.strftime("%Y-%m-%d"), "IDO", "NIFTY", "2026-09-29", x.strike, x.option_type, 1, 1, 1, x.settle, x.settle, x.settle,
                                    x.underlying, x.settle, 10, 0, 5, 2])) for x in day.itertuples()]
            name = f"BhavCopy_NSE_FO_0_0_0_{d.strftime('%Y%m%d')}_F_0000.csv.zip"
            buf = io.StringIO()
            pd.DataFrame(recs).to_csv(buf, index=False)
            with zipfile.ZipFile(root / F.RAW_DIR / name, "w") as z:
                z.writestr(name[:-4], buf.getvalue())
            log.append(f"{name},downloaded")
        (root / F.DOWNLOAD_LOG).write_text("\n".join(log) + "\n", encoding="utf-8")

    def test_full_run_on_fabricated_data_then_one_shot(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            for n in F.CODE:
                (root / n).write_bytes(b"junk " + n.encode())
            (root / F.RAW_DIR).mkdir()
            self.build(root)
            good = F.freeze(root)["combined_sha256"]
            files = E.run(root, expected_combined=good, verbose=False)
            self.assertEqual(set(files), {"Sample_Sizes", "Block_Relationships", "Heterogeneity_Tests", "Brier_Results", "Hypotheses",
                                          "Split_Integrity", "Validation_Checks", "Run_Record"})
            self.assertEqual(int((files["Validation_Checks"].result == "REVIEW").sum()), 0)
            sizes = files["Sample_Sizes"]
            ev = sizes[sizes.set == "evaluation"]
            self.assertTrue((ev.distinct_dates == 64).all(), ev)       # last window date has no next-day label
            self.assertEqual(len(files["Hypotheses"]), 8)
            self.assertEqual(files["Split_Integrity"].iloc[0]["purged_training_rows_label_in_window"], 6)
            self.assertTrue((root / "V5_1_Evaluation_Reports.zip").exists())
            with self.assertRaisesRegex(RuntimeError, "already run"):
                E.run(root, expected_combined=good, verbose=False)


if __name__ == "__main__":
    unittest.main()
