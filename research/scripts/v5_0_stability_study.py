"""ML Options Paper Trading V5.0 - temporal stability study of the lagged settlement-return (lr1) and lagged underlying-return (ur1)
relationships with next-day option direction (paper research only; no orders, no profitability claim).

PREDECLARED BEFORE ANY V5.0 RESULT WAS COMPUTED (see HYPOTHESES below; the same text is written to the report):
  Data     : V5.0 study universe (contract-wise files + local official bhavcopy rows; expiry 2026-09-29 and the original strike grid).
  Features : lr1 = ln(settle[t-1]/settle[t-2]); ur1 = underlying[t-1]/underlying[t-2]-1. Known at the close of t-1. Lags are computed on the
             full unfiltered series BEFORE eligibility filters. Label: settle[t+1] > settle[t], same contract, next consecutive trading day.
  Blocks   : consecutive chronological blocks of BLOCK_LEN=30 trading dates. A block enters a test only with >= MIN_DATES_BLOCK=15 eligible dates.
  Main hypotheses (CE): the block-wise rank correlation of lr1 (H1) and of ur1 (H2) with next-day direction is the same in every included block.
             Test: date-permutation heterogeneity test, rejected at 0.05/2 (two main tests). Anything else is secondary or exploratory.
  Secondary: expanding-window walk-forward (train = all earlier included blocks, >=2 of them, last day before the test block purged).
             B1/B2 single-feature logistic (V4.8 definition, L2 lambda=1, nothing tuned) vs the training-rate prior; pooled paired Brier
             difference with a date-clustered interval at Bonferroni level 1-0.05/2.
  Never: reverse a sign, pick a feature, or tune on any of these results. Every period has been seen in earlier experiments, so all results
  are exploratory; no untouched out-of-sample window exists in the local data.
Usage: python v5_0_stability_study.py [DATA_DIR] [OUT_DIR]   (DATA_DIR needs the raw files and fresh V4.7 audit reports)
"""
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import v4_7_model_dataset as M
import v4_7_source_audit as A
import v4_8_feature_research as V
import v4_9_feature_diagnostics as D
import v5_0_data_expansion as X

PREFIX = "V5_0_"
BLOCK_LEN = 30
MIN_DATES_BLOCK = 15
MIN_TRAIN_BLOCKS = 2
N_PERM = 2000
N_BOOT = 1000
ALPHA = 0.05
N_MAIN = 2
SEED = 500
MODELS = {"lr1": "B1_lag1_settle_logreturn", "ur1": "B2_lag1_underlying_return"}
HYPOTHESES = [
    ("H1", "main", "CE", "The rank correlation of lr1 with next-day option direction is the same in every included chronological block.",
     f"date-permutation heterogeneity test; reject if p < {ALPHA}/{N_MAIN}"),
    ("H2", "main", "CE", "The rank correlation of ur1 with next-day option direction is the same in every included chronological block.",
     f"date-permutation heterogeneity test; reject if p < {ALPHA}/{N_MAIN}"),
    ("H3", "secondary", "CE", "Walk-forward B1 (lr1) has a lower Brier score than the training-rate prior.",
     f"pooled paired difference, date-clustered {100 * (1 - ALPHA / N_MAIN):.1f}% interval entirely below 0"),
    ("H4", "secondary", "CE", "Walk-forward B2 (ur1) has a lower Brier score than the training-rate prior.",
     f"pooled paired difference, date-clustered {100 * (1 - ALPHA / N_MAIN):.1f}% interval entirely below 0"),
    ("H5", "exploratory", "PE", "Same four questions for PE; reported with its sample-size limitation, no confirmatory claim.",
     "descriptive; blocks with fewer than MIN_DATES_BLOCK eligible dates are excluded and listed"),
]


# ----------------------------------------------------------------------------- feature preparation (lags BEFORE filters)
def prepare(study):
    src = pd.DataFrame({"source_file": study["source_file"], "symbol": study["symbol"], "expiry": study["expiry"], "option_type": study["option_type"],
                        "strike": study["strike"], "date": study["date"], "src_settle": study["settle"], "src_underlying": study["underlying"]})
    r, cal, n_und = M.build_features(src)                       # shift/lag on the full series; no filter applied yet
    r = V.add_v48_features(r)
    flags = study[X.KEY + ["settle_conflict", "close_conflict"]]
    r = r.merge(flags, on=X.KEY, how="left", validate="one_to_one")
    r["R1_features"] = r[["lr1", "ur1", "sm"]].notna().all(axis=1)                 # only the features the study uses (V4.9 correction)
    r["R2_target"] = r["target_next_up"].notna()
    r["R3_price_floor"] = r[["settle_t", "settle_t1", "settle_t2"]].min(axis=1, skipna=False) >= M.PRICE_FLOOR
    r["R4_no_source_conflict"] = ~(r["settle_conflict"].fillna(False) | r["close_conflict"].fillna(False))
    r["R5_unique_key"] = ~r.duplicated(["symbol", "expiry", "option_type", "strike", "date"], keep=False)
    rules = ["R1_features", "R2_target", "R3_price_floor", "R4_no_source_conflict", "R5_unique_key"]
    r["eligible"] = r[rules].all(axis=1)
    r["exclusion_reason"] = r[rules].apply(lambda row: "|".join(k for k in rules if not row[k]), axis=1)
    r["block"] = r["cal_idx"] // BLOCK_LEN
    return r, cal, n_und, rules


# ----------------------------------------------------------------------------- Part A: block relationships + heterogeneity
def block_table(el, feature):
    rows = []
    for (ot, b), g in el.groupby(["option_type", "block"]):
        inc = g["date"].nunique() >= MIN_DATES_BLOCK
        y, x = g["target_next_up"].to_numpy(float), g[feature].to_numpy(float)
        rx, ry = pd.Series(x).rank().to_numpy(), pd.Series(y).rank().to_numpy()
        rho = D.wcorr(rx, ry, np.ones(len(y)))
        lo, hi = (D.ci(D.cluster_boot(lambda w: D.wcorr(rx, ry, w), g["date"].to_numpy(), N_BOOT, SEED))) if inc else (np.nan, np.nan)
        rows.append({"option_type": ot, "feature": feature, "block": int(b), "first_date": g["date"].min().date(), "last_date": g["date"].max().date(),
                     "eligible_rows": len(g), "eligible_dates": g["date"].nunique(), "up_rate": round(float(y.mean()), 4),
                     "spearman": round(float(rho), 4), "ci95_date_cluster_lo": lo, "ci95_date_cluster_hi": hi, "included_in_tests": bool(inc),
                     "exclusion_reason": "" if inc else f"fewer than {MIN_DATES_BLOCK} eligible dates (sample-size limitation)"})
    return pd.DataFrame(rows)


def heterogeneity(el, ot, feature, n_perm=None, seed=SEED):
    """Permutation test that block-wise correlations are equal. Dates (clusters) are permuted between blocks; block sizes are preserved.
    Assumes dates are exchangeable across blocks, so serial dependence and regime changes make it anti-conservative."""
    n_perm = N_PERM if n_perm is None else n_perm
    g0 = el[el.option_type == ot]
    keep = g0.groupby("block")["date"].nunique()
    keep = keep[keep >= MIN_DATES_BLOCK].index
    g = g0[g0.block.isin(keep)]
    if len(keep) < 2:
        return {"option_type": ot, "feature": feature, "blocks_included": int(len(keep)), "observed_H": np.nan, "p_value": np.nan,
                "note": "fewer than 2 included blocks: test not defined"}
    udates, inv = np.unique(g["date"].to_numpy(), return_inverse=True)
    blk_of_date = g.groupby(g["date"])["block"].first().reindex(udates).to_numpy()
    rx, ry = g[feature].rank().to_numpy(), g["target_next_up"].rank().to_numpy()
    blocks = np.unique(blk_of_date)
    nb = len(blocks)
    code = np.searchsorted(blocks, blk_of_date)
    ndates = np.bincount(code, minlength=nb).astype(float)

    def stat(assign):
        a = assign[inv]
        n = np.bincount(a, minlength=nb).astype(float)
        sx, sy = np.bincount(a, rx, nb), np.bincount(a, ry, nb)
        sxx, syy, sxy = np.bincount(a, rx * rx, nb), np.bincount(a, ry * ry, nb), np.bincount(a, rx * ry, nb)
        cov, vx, vy = sxy / n - sx * sy / n ** 2, sxx / n - (sx / n) ** 2, syy / n - (sy / n) ** 2
        rho = cov / np.sqrt(vx * vy)
        rbar = (ndates * rho).sum() / ndates.sum()
        return float((ndates * (rho - rbar) ** 2).sum()), rho
    h_obs, rho = stat(code)
    rng = np.random.default_rng(seed)
    ge = sum(stat(rng.permutation(code))[0] >= h_obs - 1e-12 for _ in range(n_perm))
    return {"option_type": ot, "feature": feature, "blocks_included": int(nb), "dates": int(len(udates)), "rows": int(len(g)),
            "observed_H": round(h_obs, 4), "block_rho_min": round(float(rho.min()), 4), "block_rho_max": round(float(rho.max()), 4),
            "p_value": round((1 + ge) / (1 + n_perm), 4), "n_perm": n_perm,
            "note": "permutes dates between blocks; anti-conservative under serial dependence; blocks of ~30 dates only detect large changes"}


# ----------------------------------------------------------------------------- Part B: expanding-window walk-forward
def walk_forward(el, ot, feature):
    model = MODELS[feature]
    kind, cols = V.SPECS[model]
    d = el[el.option_type == ot]
    inc = d.groupby("block")["date"].nunique()
    inc = sorted(inc[inc >= MIN_DATES_BLOCK].index)
    rows, test_parts, integrity = [], [], []
    for k in inc:
        prev = [b for b in inc if b < k]
        if len(prev) < MIN_TRAIN_BLOCKS:
            continue
        start = k * BLOCK_LEN
        tr = d[d.block.isin(prev) & (d.cal_idx + 1 < start)]       # purge: a training label may not reach the test block
        purged = int((d.block.isin(prev) & (d.cal_idx + 1 >= start)).sum())
        te = d[d.block == k]
        if tr.target_next_up.nunique() < 2 or te.empty:
            continue
        y_tr, y_te = tr.target_next_up.to_numpy(float), te.target_next_up.to_numpy(float)
        a_tr, a_te = V.arrays(tr), V.arrays(te)
        p0 = pd.Series(V.prior_prob(y_tr), index=te.index)
        p1 = pd.Series(V.predict_model(V.fit_model(kind, cols, a_tr, y_tr), a_te), index=te.index)
        V.assert_same_rows({"B0": p0, model: p1})
        boots = V.bootstrap(tr, te, {"B0_training_prior": p0, model: p1}, SEED + int(k), n_boot=N_BOOT)
        lo_e, hi_e = D.ci(boots[model]["eval_only"])
        lo_r, hi_r = D.ci(boots[model]["refit"])
        dse = (p1.to_numpy() - y_te) ** 2 - (p0.to_numpy() - y_te) ** 2
        rows.append({"option_type": ot, "feature": feature, "model": model, "test_block": int(k), "train_blocks": ",".join(map(str, prev)),
                     "train_rows": len(tr), "train_dates": tr.date.nunique(), "purged_train_rows": purged, "test_rows": len(te),
                     "test_dates": te.date.nunique(), "test_first_date": te.date.min().date(), "test_last_date": te.date.max().date(),
                     "brier_prior": round(V.brier(p0, y_te), 5), "brier_model": round(V.brier(p1, y_te), 5), "brier_diff": round(float(dse.mean()), 5),
                     "log_loss_prior": round(V.logloss(p0, y_te), 5), "log_loss_model": round(V.logloss(p1, y_te), 5),
                     "auc_model": round(V.auc(y_te, p1.to_numpy()), 4), "ci95_eval_dates_lo": lo_e, "ci95_eval_dates_hi": hi_e,
                     "ci95_train_and_eval_refit_lo": lo_r, "ci95_train_and_eval_refit_hi": hi_r})
        integrity.append({"option_type": ot, "feature": feature, "test_block": int(k), "test_block_first_date": te.date.min().date(),
                          "max_train_target_date": tr.target_date.max().date(), "train_label_reaches_test_block": int((tr.target_date >= te.date.min()).sum()),
                          "purged_train_rows": purged})
        test_parts.append(pd.DataFrame({"date": te.date.to_numpy(), "dse": dse, "block": k}))
    return pd.DataFrame(rows), pd.DataFrame(integrity), (pd.concat(test_parts, ignore_index=True) if test_parts else pd.DataFrame(columns=["date", "dse", "block"]))


def pooled(test_df, ot, feature, level):
    if test_df.empty:
        return {"option_type": ot, "feature": feature, "test_blocks": 0, "note": "no walk-forward block available (sample-size limitation)"}
    dates, dse = test_df["date"].to_numpy(), test_df["dse"].to_numpy()
    vals = D.cluster_boot(lambda w: (w * dse).sum() / w.sum(), dates, N_BOOT, SEED)
    lo, hi = D.ci(vals, level)
    lo95, hi95 = D.ci(vals, 0.95)
    return {"option_type": ot, "feature": feature, "test_blocks": int(test_df.block.nunique()), "test_rows": len(test_df),
            "test_dates": int(test_df.date.nunique()), "pooled_brier_diff_vs_prior": round(float(dse.mean()), 5),
            "ci_unadjusted95_lo": lo95, "ci_unadjusted95_hi": hi95, f"ci_bonferroni{N_MAIN}_lo": lo, f"ci_bonferroni{N_MAIN}_hi": hi,
            "interval_entirely_below_0": bool(hi < 0), "note": "eval-date resampling only (fitted parameters held fixed); blocks stack in time"}


# ----------------------------------------------------------------------------- orchestration
def coverage_change(before, after):
    rows = []
    for tag, r in (("before (contract-wise files only)", before), ("after (+ local official bhavcopy)", after)):
        for ot in ("CE", "PE"):
            g = r[r.option_type == ot]
            e = g[g.eligible]
            rows.append({"dataset": tag, "option_type": ot, "raw_rows": len(g), "raw_dates": g.date.nunique(), "last_raw_date": g.date.max().date(),
                         "eligible_rows": len(e), "eligible_dates": e.date.nunique(), "last_eligible_date": e.date.max().date() if len(e) else ""})
    return pd.DataFrame(rows)


def run_study(data_dir, out_dir, study, bhav_present, verbose=True):
    out_dir = Path(out_dir)
    r, cal, n_und, rules = prepare(study)
    el = r[r.eligible]
    cw_only = study[study.origin == "contract_wise_csv"]
    rb, _, _, _ = prepare(cw_only)
    cov = coverage_change(rb, r)
    checks = A.Checks()
    checks.add("study", "dates with more than one underlying value", n_und)
    checks.add("study", "eligible rows with target date not after feature date", int((el.target_date <= el.date).sum()))
    checks.add("study", "eligible rows with missing lr1/ur1/target", int(el[["lr1", "ur1", "target_next_up"]].isna().any(axis=1).sum()))
    checks.add("study", "duplicate contract-date keys among eligible rows", int(el.duplicated(["symbol", "expiry", "option_type", "strike", "date"]).sum()))
    checks.add("study", "eligible rows whose source rows disagree between origins", int((~el["R4_no_source_conflict"]).sum()))
    checks.add("study", "eligible rows with settlement below the price floor at t", int((el["settle_t"] < M.PRICE_FLOOR).sum()))
    ds48 = Path(data_dir) / "V4_8_Dataset.csv"
    if ds48.exists():
        old = pd.read_csv(ds48)
        k = lambda df: set(map(tuple, df[["option_type", "strike", "date"]].astype(str).to_numpy()))
        a = k(old.loc[old.eligible]) - k(rb[rb.eligible].assign(date=lambda x: x.date.dt.strftime("%Y-%m-%d")))
        checks.add("continuity", "V4.8-eligible rows missing from V5.0 'before' eligibility", len(a))
    # Part A
    blocks, het = [], []
    for f in ("lr1", "ur1"):
        blocks.append(block_table(el, f))
        for ot in ("CE", "PE"):
            het.append(heterogeneity(el, ot, f))
    blocks, het = pd.concat(blocks, ignore_index=True), pd.DataFrame(het)
    # Part B
    wf, integ, pool = [], [], []
    level = 1 - ALPHA / N_MAIN
    for f in ("lr1", "ur1"):
        for ot in ("CE", "PE"):
            a, b, t = walk_forward(el, ot, f)
            wf.append(a)
            integ.append(b)
            pool.append(pooled(t, ot, f, level))
    wf = pd.concat([w for w in wf if len(w)], ignore_index=True) if any(len(w) for w in wf) else pd.DataFrame()
    integ = pd.concat([w for w in integ if len(w)], ignore_index=True) if any(len(w) for w in integ) else pd.DataFrame()
    pool = pd.DataFrame(pool)
    if len(integ):
        checks.add("walk_forward", "training labels reaching the test block", int(integ["train_label_reaches_test_block"].sum()))
    # block layout / purge report
    layout = (r.groupby(["option_type", "block"]).agg(first_date=("date", "min"), last_date=("date", "max"), raw_rows=("date", "size"),
                                                        eligible_rows=("eligible", "sum"), eligible_dates=("date", lambda s: s[r.loc[s.index, "eligible"]].nunique()))
              .reset_index())
    layout["included_in_tests"] = layout["eligible_dates"] >= MIN_DATES_BLOCK
    layout["first_date"], layout["last_date"] = layout["first_date"].dt.strftime("%Y-%m-%d"), layout["last_date"].dt.strftime("%Y-%m-%d")
    # hypotheses with results
    def hrow(h, f, ot):
        row = het[(het.option_type == ot) & (het.feature == f)].iloc[0]
        p = row.p_value
        return p, row
    out = []
    for hid, tier, ot, stmt, test in HYPOTHESES:
        res, verdict = "", ""
        if hid in ("H1", "H2"):
            f = "lr1" if hid == "H1" else "ur1"
            p, row = hrow(hid, f, ot)
            res = f"p={p}; block rho range [{row.get('block_rho_min', np.nan)}, {row.get('block_rho_max', np.nan)}] over {row.blocks_included} blocks"
            thr = ALPHA / N_MAIN
            verdict = (("INSTABILITY SUPPORTED (homogeneity rejected)" if p < thr / 2 else "INSTABILITY SUPPORTED (borderline: p within 2x of the threshold)")
                       if p < thr else "HOMOGENEITY NOT REJECTED (no evidence of instability; low power)")
        elif hid in ("H3", "H4"):
            f = "lr1" if hid == "H3" else "ur1"
            q = pool[(pool.option_type == ot) & (pool.feature == f)].iloc[0]
            res = f"pooled Brier diff {q.get('pooled_brier_diff_vs_prior')}, Bonferroni interval [{q.get(f'ci_bonferroni{N_MAIN}_lo')}, {q.get(f'ci_bonferroni{N_MAIN}_hi')}], {q.get('test_blocks')} blocks"
            verdict = "SUPPORTED" if bool(q.get("interval_entirely_below_0", False)) else "NOT SUPPORTED"
        else:
            res = "see Heterogeneity_Tests, Walk_Forward and Block_Relationships for PE; blocks below the minimum are listed as excluded"
            verdict = "EXPLORATORY ONLY"
        out.append({"id": hid, "tier": tier, "option_type": ot, "predeclared_statement": stmt, "predeclared_test": test, "result": res, "verdict": verdict,
                    "caveat": "all periods were seen in earlier V4.x experiments; exploratory, not confirmatory"})
    hyp = pd.DataFrame(out)
    # write
    w = A.write_csv
    w(cov, out_dir / f"{PREFIX}Coverage_Change.csv")
    w(layout, out_dir / f"{PREFIX}Block_Layout.csv")
    w(blocks, out_dir / f"{PREFIX}Block_Relationships.csv")
    w(het, out_dir / f"{PREFIX}Heterogeneity_Tests.csv")
    w(wf, out_dir / f"{PREFIX}Walk_Forward.csv")
    w(pool, out_dir / f"{PREFIX}Walk_Forward_Pooled.csv")
    w(integ, out_dir / f"{PREFIX}Split_Integrity.csv")
    w(hyp, out_dir / f"{PREFIX}Hypotheses.csv")
    ds = r[["source_file", "symbol", "expiry", "option_type", "strike", "date", "target_date", "cal_idx", "block", "settle_t", "settle_next", "lr1", "ur1", "sm",
            "bucket", "target_next_up", "eligible", "exclusion_reason"]].copy()
    for c in ("expiry", "date", "target_date"):
        ds[c] = ds[c].dt.strftime("%Y-%m-%d")
    w(ds.round(8).sort_values(["date", "option_type", "strike"]).reset_index(drop=True), out_dir / f"{PREFIX}Study_Dataset.csv")
    w(checks.frame(), out_dir / f"{PREFIX}Study_Validation_Checks.csv")
    if verbose:
        print("=" * 76, "\nV5.0 TEMPORAL STABILITY STUDY (exploratory; no skill or profit claim)\n" + "=" * 76)
        print(cov.to_string(index=False))
        print(layout[layout.option_type.isin(["CE", "PE"])].to_string(index=False))
        print(het[["option_type", "feature", "blocks_included", "observed_H", "block_rho_min", "block_rho_max", "p_value"]].to_string(index=False))
        print(pool.to_string(index=False))
        print(hyp[["id", "option_type", "verdict"]].to_string(index=False))
    return {"coverage": cov, "heterogeneity": het, "walk_forward": wf, "pooled": pool, "hypotheses": hyp, "blocks": blocks, "layout": layout,
            "integrity": integ, "checks": checks.frame()}


def run_all(data_dir="/content", out_dir=None, verbose=True):
    data_dir, out_dir = Path(data_dir), Path(out_dir or data_dir)
    V.check_gate(data_dir, n_source_rows=len(A.load_source(str(data_dir), A.Checks())[0]))   # fail closed on a stale or unresolved V4.7 audit
    x = X.run(data_dir, out_dir, verbose=verbose)
    res = run_study(data_dir, out_dir, x["study"], None, verbose=verbose)
    bad = pd.concat([x["checks"], res["checks"]])
    bad = bad[bad["result"] == "REVIEW"]
    names = sorted(p.name for p in out_dir.glob(PREFIX + "*.csv"))
    zp = out_dir / f"{PREFIX}Reports.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            z.writestr(zipfile.ZipInfo(n, date_time=(2026, 1, 1, 0, 0, 0)), (out_dir / n).read_bytes())
    if verbose:
        print("Saved", len(names) + 1, "files incl.", zp.name, "to", out_dir)
    if len(bad):
        raise AssertionError("unresolved integrity items:\n" + bad[["scope", "check", "issues"]].to_string(index=False))
    return {**res, "data": x}


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    run_all(a[0] if a else "/content", a[1] if len(a) > 1 else None)
