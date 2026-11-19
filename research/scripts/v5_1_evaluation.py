"""ML Options Paper Trading V5.1 - one-shot evaluation on the window 2026-06-30 to 2026-09-29 (paper research only; no orders).

Everything here follows the protocol frozen in v5_1_freeze_protocol.PROTOCOL. The script refuses to start unless
v5_1_freeze_protocol.verify() passes for the exact files on disk, and refuses to run twice (so the window cannot be re-used after a
revision made from its results). A settlement-to-settlement relationship is not an executable strategy: no spreads, fees or slippage.

Usage: python v5_1_evaluation.py PROJECT_DIR --expected-combined HASH
"""
import platform
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import v4_7_model_dataset as M
import v4_7_source_audit as A
import v4_8_feature_research as V
import v5_0_stability_study as S
import v5_1_acquisition_audit as Q
import v5_1_freeze_protocol as F

PREFIX = "V5_1_Eval_"
BOUNDARY = Q.BOUNDARY                    # 2026-06-29: last trade date of every earlier experiment
WINDOW_START, WINDOW_END = Q.WINDOW_START, Q.WINDOW_END
BLOCK_LEN = S.BLOCK_LEN
N_BOOT, BOOT_BLOCK, BOOT_SEED = 2000, 5, 20260630
ALPHA, N_MAIN = 0.05, 2
LEVEL = 1 - ALPHA / N_MAIN
FEATURES = ("lr1", "ur1")
OPTION_TYPES = ("CE", "PE")


# ----------------------------------------------------------------------------- data
def build_series(project):
    """Old study universe (to 2026-06-29) + new official rows for the same contracts. Lags are computed later on the whole series."""
    p = Path(project)
    old = pd.read_csv(p / "V5_0_Normalized_Study_Universe.csv", parse_dates=["date", "expiry"])
    grid = set(old["strike"].unique())
    new = Q.load_all(p / F.RAW_DIR)
    new = new[(new["expiry"] == Q.STUDY_EXPIRY) & new["strike"].isin(grid) & (new["date"] >= WINDOW_START)].copy()
    new["symbol"] = "NIFTY"
    new["origin"] = "official_bhavcopy_udiff"
    new["settle_conflict"] = False          # single origin: nothing to disagree with
    new["close_conflict"] = False
    cols = [c for c in old.columns if c in new.columns]
    return pd.concat([old, new[cols]], ignore_index=True), len(old["date"].unique())


def assign_blocks(r, n_old):
    """Old calendar keeps the V5.0 blocks (cal_idx // 30); new dates form 30-date blocks, remainder merged into the last one."""
    r = r.copy()
    old = r["cal_idx"] < n_old
    r.loc[old, "block"] = r.loc[old, "cal_idx"] // BLOCK_LEN
    first = int(r.loc[old, "block"].max()) + 1
    n_new = int(r["cal_idx"].max()) + 1 - n_old
    n_blocks = max(1, n_new // BLOCK_LEN)
    r.loc[~old, "block"] = first + np.minimum((r.loc[~old, "cal_idx"] - n_old) // BLOCK_LEN, n_blocks - 1)
    r["block"] = r["block"].astype(int)
    return r


def split(el):
    """Training rows: feature date and label date on or before the boundary. Evaluation rows: feature date on or after the window start."""
    train = el[(el["date"] <= BOUNDARY) & (el["target_date"] <= BOUNDARY)]
    test = el[el["date"] >= WINDOW_START]
    return train, test


# ----------------------------------------------------------------------------- uncertainty
def block_bootstrap_diff(dates, dse, n_boot=N_BOOT, block=BOOT_BLOCK, seed=BOOT_SEED):
    """Moving-block bootstrap over distinct dates of the pooled mean of dse (row-weighted). Returns the array of resampled means."""
    ud, inv = np.unique(np.asarray(dates), return_inverse=True)
    d = len(ud)
    s, n = np.bincount(inv, np.asarray(dse, float), d), np.bincount(inv, minlength=d).astype(float)
    b = min(block, d)
    k = int(np.ceil(d / b))
    rng = np.random.default_rng(seed)
    out = np.empty(n_boot)
    for i in range(n_boot):
        starts = rng.integers(0, d - b + 1, k)
        idx = (starts[:, None] + np.arange(b)).ravel()[:d]
        out[i] = s[idx].sum() / n[idx].sum()
    return out


def interval(vals, level=LEVEL):
    tail = (1 - level) / 2
    lo, hi = np.quantile(vals, [tail, 1 - tail])
    return round(float(lo), 5), round(float(hi), 5)


# ----------------------------------------------------------------------------- models
def evaluate_model(train, test, ot, feature):
    model = S.MODELS[feature]
    kind, cols = V.SPECS[model]
    tr, te = train[train.option_type == ot], test[test.option_type == ot]
    base = {"option_type": ot, "feature": feature, "model": model, "train_rows": len(tr), "train_dates": tr["date"].nunique(),
            "eval_rows": len(te), "eval_dates": te["date"].nunique()}
    if te.empty or tr.target_next_up.nunique() < 2:
        return {**base, "note": "not evaluable: no evaluation rows or one training class"}, None
    y_tr, y_te = tr.target_next_up.to_numpy(float), te.target_next_up.to_numpy(float)
    p0 = pd.Series(V.prior_prob(y_tr), index=te.index)
    p1 = pd.Series(V.predict_model(V.fit_model(kind, cols, V.arrays(tr), y_tr), V.arrays(te)), index=te.index)
    V.assert_same_rows({"B0": p0, model: p1})
    dse = (p1.to_numpy() - y_te) ** 2 - (p0.to_numpy() - y_te) ** 2
    boots = block_bootstrap_diff(te["date"].to_numpy(), dse)
    lo, hi = interval(boots)
    row = {**base, "training_up_rate": round(float(y_tr.mean()), 4), "evaluation_up_rate": round(float(y_te.mean()), 4),
           "brier_prior": round(V.brier(p0, y_te), 5), "brier_model": round(V.brier(p1, y_te), 5), "brier_diff": round(float(dse.mean()), 5),
           "log_loss_prior": round(V.logloss(p0, y_te), 5), "log_loss_model": round(V.logloss(p1, y_te), 5),
           f"ci{100 * LEVEL:.1f}_lo": lo, f"ci{100 * LEVEL:.1f}_hi": hi, "interval_entirely_below_0": bool(hi < 0),
           "note": f"moving-block bootstrap over dates, block {BOOT_BLOCK}, {N_BOOT} resamples; fitted parameters held fixed"}
    return row, pd.DataFrame({"date": te["date"].to_numpy(), "dse": dse})


# ----------------------------------------------------------------------------- orchestration
def run(project, out_dir=None, expected_combined=None, verbose=True):
    project = Path(project)
    combined_hash = F.verify(project, expected_combined)              # 1. fail closed before anything is read or computed
    out = Path(out_dir) if out_dir else project
    record = out / f"{PREFIX}Run_Record.csv"
    if record.exists():
        raise RuntimeError(f"{record.name} exists: the evaluation was already run. Rerunning after a revision would void the window.")
    integrity = pd.read_csv(project / "V5_1_Raw_Integrity_Report.csv")
    if (integrity["result"] == "REVIEW").any():
        raise RuntimeError("V5_1_Raw_Integrity_Report.csv has unresolved REVIEW items")

    study, n_old = build_series(project)
    r, cal, n_und, rules = S.prepare(study)
    if pd.Timestamp(cal[n_old - 1]) != BOUNDARY:
        raise RuntimeError("calendar boundary mismatch: old calendar does not end on 2026-06-29")
    r = assign_blocks(r, n_old)
    el = r[r.eligible]
    train, test = split(el)
    el_all = pd.concat([train, test])          # boundary-crossing labels purged from the stability set as well

    v50 = pd.read_csv(project / "V5_0_Study_Dataset.csv")
    key = lambda d: set(map(tuple, d[["option_type", "strike", "date"]].astype(str).to_numpy()))
    v50_keys = key(v50[(v50["eligible"]) & (pd.to_datetime(v50["target_date"]) <= BOUNDARY)])
    train_keys = key(train.assign(date=train["date"].dt.strftime("%Y-%m-%d")))

    brier, parts = [], {}
    for f in FEATURES:
        for ot in OPTION_TYPES:
            row, dse = evaluate_model(train, test, ot, f)
            brier.append(row)
            parts[(ot, f)] = dse
    brier = pd.DataFrame(brier)
    blocks = pd.concat([S.block_table(el_all, f) for f in FEATURES], ignore_index=True)
    het = pd.DataFrame([S.heterogeneity(el_all, ot, f) for f in FEATURES for ot in OPTION_TYPES])

    sizes = []
    for ot in OPTION_TYPES:
        for tag, d in (("training", train), ("evaluation", test)):
            g = d[d.option_type == ot]
            sizes.append({"option_type": ot, "set": tag, "eligible_rows": len(g), "distinct_dates": g["date"].nunique(),
                          "first_date": g["date"].min().date() if len(g) else "", "last_date": g["date"].max().date() if len(g) else ""})
    sizes = pd.DataFrame(sizes)

    verdicts = []
    thr = ALPHA / N_MAIN
    for ot in OPTION_TYPES:
        for f in FEATURES:
            h = het[(het.option_type == ot) & (het.feature == f)].iloc[0]
            p = h.get("p_value")
            verdicts.append({"id": f"STAB_{f}_{ot}", "option_type": ot, "feature": f, "test": f"date-permutation heterogeneity, reject if p < {thr}",
                             "result": f"p={p}; block rho range [{h.get('block_rho_min')}, {h.get('block_rho_max')}] over {h.get('blocks_included')} blocks",
                             "verdict": "NOT EVALUABLE" if pd.isna(p) else ("INSTABILITY SUPPORTED" if p < thr else "HOMOGENEITY NOT REJECTED")})
            b = brier[(brier.option_type == ot) & (brier.feature == f)].iloc[0]
            ok = bool(b.get("interval_entirely_below_0") == True)  # noqa: E712  (NaN when not evaluable)
            verdicts.append({"id": f"BRIER_{f}_{ot}", "option_type": ot, "feature": f, "test": f"{100 * LEVEL:.1f}% block-bootstrap interval of Brier difference entirely below 0",
                             "result": f"diff={b.get('brier_diff')}; {b.get('eval_rows')} rows, {b.get('eval_dates')} dates",
                             "verdict": "NOT EVALUABLE" if pd.isna(b.get("brier_diff")) else ("SUPPORTED" if ok else "NOT SUPPORTED")})
    verdicts = pd.DataFrame(verdicts)
    verdicts["caveat"] = "settlement-to-settlement direction; not an executable return; no spreads, fees or slippage"

    checks = A.Checks()
    checks.add("split", "training rows with a label date after 2026-06-29", int((train["target_date"] > BOUNDARY).sum()))
    checks.add("split", "evaluation rows dated before 2026-06-30", int((test["date"] < WINDOW_START).sum()))
    checks.add("split", "rows in both training and evaluation", int(len(set(train.index) & set(test.index))))
    checks.add("split", "evaluation rows with a label date after 2026-09-29", int((test["target_date"] > WINDOW_END).sum()))
    checks.add("split", "training set differs from the V5.0 eligible rows with label on or before 2026-06-29", len(train_keys ^ v50_keys))
    checks.add("data", "dates with more than one underlying value", n_und)
    checks.add("data", "eligible rows below the price floor at t", int((el["settle_t"] < M.PRICE_FLOOR).sum()))
    checks.add("data", "duplicate contract-date keys among eligible rows", int(el.duplicated(["symbol", "expiry", "option_type", "strike", "date"]).sum()))
    n_zips = len(list((project / F.RAW_DIR).glob("*.zip")))
    checks.add("data", "trading dates in the window differ from the number of downloaded files", abs(int((pd.DatetimeIndex(cal) >= WINDOW_START).sum()) - n_zips))
    split_int = pd.DataFrame([{"max_training_feature_date": train["date"].max().date(), "max_training_label_date": train["target_date"].max().date(),
                               "min_evaluation_feature_date": test["date"].min().date(), "max_evaluation_label_date": test["target_date"].max().date(),
                               "purged_training_rows_label_in_window": int(((el["date"] <= BOUNDARY) & (el["target_date"] > BOUNDARY)).sum())}])
    run_record = pd.DataFrame([{"utc_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "combined_sha256_verified": combined_hash,
                                "python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__}])

    out.mkdir(parents=True, exist_ok=True)
    files = {"Sample_Sizes": sizes, "Block_Relationships": blocks, "Heterogeneity_Tests": het, "Brier_Results": brier, "Hypotheses": verdicts,
             "Split_Integrity": split_int, "Validation_Checks": checks.frame(), "Run_Record": run_record}
    for k, v in files.items():
        v.to_csv(out / f"{PREFIX}{k}.csv", index=False)
    zp = out / "V5_1_Evaluation_Reports.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for k in files:
            z.writestr(zipfile.ZipInfo(f"{PREFIX}{k}.csv", date_time=(2026, 1, 1, 0, 0, 0)), (out / f"{PREFIX}{k}.csv").read_bytes())
    bad = checks.frame()
    bad = bad[bad["result"] == "REVIEW"]
    if verbose:
        print(sizes.to_string(index=False))
        print(brier.to_string(index=False))
        print(het[["option_type", "feature", "blocks_included", "p_value"]].to_string(index=False))
        print(verdicts[["id", "verdict"]].to_string(index=False))
    if len(bad):
        raise AssertionError("unresolved validation items:\n" + bad[["scope", "check", "issues"]].to_string(index=False))
    return files


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    exp = sys.argv[sys.argv.index("--expected-combined") + 1] if "--expected-combined" in sys.argv else None
    if exp is None:
        sys.exit("refusing to run: pass --expected-combined <hash printed when the protocol was frozen>")
    if exp in a:
        a.remove(exp)
    run(a[0] if a else ".", expected_combined=exp)
