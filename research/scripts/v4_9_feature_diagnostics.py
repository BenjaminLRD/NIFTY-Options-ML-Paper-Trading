"""ML Options Paper Trading V4.9 - diagnose why lagged settlement / underlying returns fail on CE validation.

Diagnostic only (paper research). It never changes the target, timing or features, tunes nothing, and never reads the
exploratory test split for any performance statistic (test rows appear only in structural alignment checks).

Inputs  : the five OPTIDX_NIFTY_*.csv source files and V4_8_Dataset.csv / V4_8_Baseline_Comparison.csv /
          V4_8_Moneyness_Bucket_Rates.csv (V4.8 reports). Needs the V4.7/V4.8 scripts only to reproduce V4.8 predictions.
Part 1  : temporal-alignment audit - an INDEPENDENT dictionary-lookup rebuild of lr1, ur1, signed moneyness, target and
          splits from the raw files, compared row by row with the V4.8 dataset; hand-worked examples; leakage perturbation;
          lead/lag positive control; preprocessing parity.
Part 2-3: feature, calibration, date-level and moneyness diagnostics on TRAIN and VALIDATION only.
Usage   : python v4_9_feature_diagnostics.py [DATA_DIR] [OUT_DIR]
"""
import glob
import math
import re
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import v4_7_model_dataset as M
import v4_7_source_audit as A
import v4_8_feature_research as V

PREFIX = "V4_9_"
SEED = 49
N_BOOT = 1000
N_BOOT_SLOPE = 500
TOL = 1e-7
# predeclared bins (left-closed, fixed before looking at any V4.9 output); roughly +-0.5% underlying, +-5% option move
BINS = {"ur1": ([-np.inf, -0.005, 0.005, np.inf], ["<=-0.5%", "(-0.5%,+0.5%)", ">=+0.5%"]),
        "lr1": ([-np.inf, -0.05, 0.05, np.inf], ["<=-0.05", "(-0.05,+0.05)", ">=+0.05"])}
MONEYNESS_EXAMPLES = [   # (spot, strike, type, hand-calculated signed log-moneyness, expected bucket)
    (23000, 24000, "CE", -0.0425596, "OTM"), (23000, 24000, "PE", 0.0425596, "ITM"),
    (23000, 15000, "CE", 0.4274440, "DEEP_ITM"), (23000, 15000, "PE", -0.4274440, "DEEP_OTM"),
    (24000, 24000, "CE", 0.0, "ATM"), (24000, 24000, "PE", 0.0, "ATM"),
    (23000, 22500, "CE", 0.0219789, "ATM"), (23000, 22500, "PE", -0.0219789, "ATM")]
MODELS = list(V.SPECS)


# ----------------------------------------------------------------------------- independent rebuild (Part 1)
def load_raw(data_dir):
    fs = [f for f in sorted(glob.glob(str(Path(data_dir) / "OPTIDX_NIFTY_*.csv"))) if " (" not in Path(f).name]
    if not fs:
        raise FileNotFoundError("no OPTIDX_NIFTY_*.csv source files found")
    parts = []
    for f in fs:
        d = pd.read_csv(f, dtype=str)
        d.columns = [c.strip() for c in d.columns]
        parts.append(d)
    d = pd.concat(parts, ignore_index=True)
    out = pd.DataFrame({
        "option_type": d["Option type"].str.strip().str.upper(), "strike": pd.to_numeric(d["Strike Price"]).round(2),
        "expiry": pd.to_datetime(d["Expiry"], format="%d-%b-%Y"), "date": pd.to_datetime(d["Date"], format="%d-%b-%Y"),
        "settle": pd.to_numeric(d["Settle Price"], errors="coerce"), "und": pd.to_numeric(d["Underlying Value"], errors="coerce")})
    return out


def independent_features(raw):
    """Dictionary-lookup rebuild (deliberately NOT the shift-based V4.7 code). One output row per raw row."""
    cal = sorted(raw["date"].unique())
    ci = {d: i for i, d in enumerate(cal)}
    price, und, seen, dup = {}, {}, set(), 0
    for r in raw.itertuples():
        k = (r.option_type, r.strike, r.expiry, r.date)
        if k in seen:
            dup += 1
            continue
        seen.add(k)
        price[k] = r.settle
        und.setdefault(r.date, r.und)
    rows = []
    for r in raw.itertuples():
        i = ci[r.date]
        key = (r.option_type, r.strike, r.expiry)
        get = lambda j: price.get((*key, cal[j])) if 0 <= j < len(cal) else None
        s1, s2, s3, sn = get(i - 1), get(i - 2), get(i - 3), get(i + 1)
        u1 = und.get(cal[i - 1]) if i >= 1 else None
        u2 = und.get(cal[i - 2]) if i >= 2 else None
        nan = float("nan")
        lr1 = math.log(s1 / s2) if s1 and s2 and s1 > 0 and s2 > 0 else nan
        ur1 = u1 / u2 - 1 if u1 and u2 else nan
        sm = math.log(u1 / r.strike) * (1 if r.option_type == "CE" else -1) if u1 and u1 > 0 else nan
        rows.append({"option_type": r.option_type, "strike": r.strike, "expiry": r.expiry, "date": r.date, "i": i,
                     "s0": r.settle, "s1": s1 if s1 is not None else nan, "s2": s2 if s2 is not None else nan,
                     "s3": s3 if s3 is not None else nan, "sn": sn if sn is not None else nan,
                     "u1": u1 if u1 is not None else nan, "u2": u2 if u2 is not None else nan,
                     "lr1": lr1, "ur1": ur1, "sm": sm, "target": float(sn > r.settle) if sn is not None else nan,
                     "target_date": cal[i + 1] if sn is not None else pd.NaT})
    ind = pd.DataFrame(rows)
    ind["lr1_lag2"] = [math.log(a / b) if a == a and b == b and a > 0 and b > 0 else float("nan") for a, b in zip(ind.s2, ind.s3)]
    return ind, cal, dup


def independent_split(i, n):
    v0, t0 = int(n * M.TRAIN_FRAC), int(n * (M.TRAIN_FRAC + M.VAL_FRAC))
    base = np.where(i < v0, "train", np.where(i < t0, "validation", "test"))
    purged = ((base == "train") & (i + 1 >= v0)) | ((base == "validation") & (i + 1 >= t0))
    return np.where(purged, "purged", base), v0, t0


def compare_alignment(ind, cal, ds):
    """Row-by-row comparison of the independent rebuild with the V4.8 dataset. Returns (check rows, merged frame)."""
    ds = ds.copy()
    ds["date"] = pd.to_datetime(ds["date"])
    m = ind.merge(ds, on=["option_type", "strike", "date"], how="outer", indicator=True, validate="one_to_one")
    rows = []
    add = lambda name, n, detail="": rows.append({"check": name, "issues": int(n), "result": "PASS" if n == 0 else "FAIL", "detail": detail})
    add("row identity: raw rows missing from V4.8 dataset or vice versa", (m["_merge"] != "both").sum(), f"{len(ind)} raw rows, {len(ds)} dataset rows")
    m = m[m["_merge"] == "both"]
    for a, b in (("lr1_x", "lr1_y"), ("ur1_x", "ur1_y"), ("sm_x", "sm_y")):
        x, y = m[a].to_numpy(float), m[b].to_numpy(float)
        bad = (np.isnan(x) != np.isnan(y)) | (~np.isnan(x) & ~np.isnan(y) & (np.abs(x - y) > TOL))
        add(f"{a[:-2]} equals independent rebuild", bad.sum(), f"{int((~np.isnan(x)).sum())} finite values compared")
    x, y = m["target"].to_numpy(float), m["target_next_up"].to_numpy(float)
    add("target_next_up equals independent next-day comparison (same contract)",
        ((np.isnan(x) != np.isnan(y)) | (~np.isnan(x) & ~np.isnan(y) & (x != y))).sum(), f"{int((~np.isnan(x)).sum())} labelled rows")
    td = pd.to_datetime(m["target_date_y"])
    add("target_date equals next consecutive source trading day", (m["target_date_x"].fillna(pd.Timestamp(0)) != td.fillna(pd.Timestamp(0))).sum())
    split, v0, t0 = independent_split(m["i"].to_numpy(), len(cal))
    add("split label (incl. purge) equals independent rule", (split != m["split"].to_numpy()).sum(), f"validation starts {cal[v0].date()}, test starts {cal[t0].date()}")
    ok = m["eligible"].astype(bool)
    ind_ok = m[["lr1_x", "ur1_x", "sm_x", "target"]].notna().all(axis=1) & (m[["s0", "s1", "s2"]].min(axis=1) >= V.M.PRICE_FLOOR)
    bad = m[ok != ind_ok]
    explained = bad[(~bad["eligible"].astype(bool)) & (bad["exclusion_reason"] == "R1_features")]
    add("V4.8 eligibility differs from the rule on the features the models use, for a reason OTHER than R1", len(bad) - len(explained),
        "every difference must be a row excluded only by R1_features")
    rows.append({"check": "DEFECT V4.8: R1 requires settle_return_lag2 (t-2 vs t-3), which no V4.8 model uses -> rows wrongly excluded",
                 "issues": int(len(explained)), "result": "DEFECT (fixed in V4.9)" if len(explained) else "PASS",
                 "detail": "rows lose eligibility only because a feature no model uses is missing; no leakage, sample loss only"})
    e = m[ok]
    add("eligible rows: every feature date precedes the decision date", ((e["u1"].notna()) & (e["i"] < 2)).sum(), "features use dates t-1 and t-2 only")
    add("eligible rows: target date is 1-4 calendar days after decision date",
        (~((e["target_date_x"] - e["date"]).dt.days.between(1, 4))).sum())
    return rows, m


def ind_match_rule(ds, ind):
    """Independent restatement: rule on the features used, plus the non-R1 V4.8 rules (via exclusion_reason)."""
    f = ind[["option_type", "strike", "date", "lr1", "ur1", "sm", "target"]]
    t = ds[["option_type", "strike", "date", "exclusion_reason"]].merge(f, on=["option_type", "strike", "date"], how="left")
    other = t["exclusion_reason"].fillna("").str.replace("R1_features", "", regex=False).str.replace("|", "", regex=False).eq("")
    return pd.Series(other.to_numpy() & t[["lr1", "ur1", "sm", "target"]].notna().all(axis=1).to_numpy(), index=ds.index)


def corrected_eligibility(ds, ind):
    """V4.9 rule: V4.8 rules R2-R5 unchanged; R1 only needs the features the models actually use (lr1, ur1, sm)."""
    f = ind[["option_type", "strike", "date", "lr1", "ur1", "sm"]].rename(columns={"lr1": "i_lr1", "ur1": "i_ur1", "sm": "i_sm"})
    t = ds[["option_type", "strike", "date", "exclusion_reason"]].merge(f, on=["option_type", "strike", "date"], how="left")
    toks = t["exclusion_reason"].fillna("").map(lambda r: set(x for x in r.split("|") if x))
    only_r1 = toks.map(lambda x: x <= {"R1_features"}).to_numpy()
    fin = t[["i_lr1", "i_ur1", "i_sm"]].notna().all(axis=1).to_numpy()
    return pd.Series(only_r1 & fin, index=ds.index)


def eligibility_effect(d48, d49):
    rows = []
    for sp in ("train", "validation"):
        for ot in ("CE", "PE"):
            a = ((d48.split == sp) & (d48.option_type == ot)).sum()
            b = ((d49.split == sp) & (d49.option_type == ot)).sum()
            rows.append({"view": "eligible rows", "split": sp, "option_type": ot, "model": "", "v48": int(a), "v49": int(b), "change": int(b - a)})
    for ot in ("CE", "PE"):
        pa, pb = {}, {}
        for tag, d, store in (("v48", d48, pa), ("v49", d49, pb)):
            tr, te = d[(d.split == "train") & (d.option_type == ot)], d[(d.split == "validation") & (d.option_type == ot)]
            if te.empty:
                continue
            _, p_te = model_predictions(tr, te)
            for r in metric_rows(ot, "validation", te, p_te):
                store[r["model"]] = r
        for m in pa:
            for k in ("brier", "brier_diff_vs_B0", "log_loss", "auc"):
                rows.append({"view": f"validation {k}", "split": "validation", "option_type": ot, "model": m,
                             "v48": pa[m][k], "v49": pb[m][k], "change": round(pb[m][k] - pa[m][k], 5)})
    return pd.DataFrame(rows)


def perturbation_leak_test(raw, cal_idx):
    """Scale every price and underlying value AFTER date T: features at dates <= T and labels at dates < T must not move."""
    cal = sorted(raw["date"].unique())
    T = cal[cal_idx]
    alt = raw.copy()
    fut = alt["date"] > T
    alt.loc[fut, "settle"] *= 3.7
    alt.loc[fut, "und"] += 900
    a, _, _ = independent_features(raw)
    b, _, _ = independent_features(alt)
    past = a["date"] <= T
    f = sum(int((~np.isclose(a.loc[past, c], b.loc[past, c], equal_nan=True, rtol=0, atol=1e-12)).sum()) for c in ("lr1", "ur1", "sm"))
    lab = a["date"] < T
    t = int((~np.isclose(a.loc[lab, "target"], b.loc[lab, "target"], equal_nan=True)).sum())
    # same-day check: change ONLY day T; features at day T (decision day) and earlier must not move
    alt2 = raw.copy()
    day = alt2["date"] == T
    alt2.loc[day, "settle"] *= 5
    alt2.loc[day, "und"] += 700
    c2, _, _ = independent_features(alt2)
    same_day = sum(int((~np.isclose(a.loc[past, c], c2.loc[past, c], equal_nan=True, rtol=0, atol=1e-12)).sum()) for c in ("lr1", "ur1", "sm"))
    return f, t, same_day, T


def lead_lag(ds, ind_cal, und_by_date, ot, shifts=(-2, -1, 0, 1, 2)):
    """Positive control: date-level up-rate vs underlying return over [t+sh, t+sh+1]. Peak at sh=0 confirms label timing."""
    d = ds[(ds["option_type"] == ot) & ds["eligible"] & ds["split"].isin(["train", "validation"])]
    rate = d.groupby("date")["target_next_up"].mean()
    u = pd.Series(und_by_date).reindex(ind_cal)
    ret = u.pct_change()   # return over [t-1, t] stored at t
    out = []
    for sh in shifts:
        x = ret.shift(-(sh + 1)).reindex(rate.index)
        out.append({"option_type": ot, "shift_days": sh, "window": f"underlying return [t{sh:+d}, t{sh + 1:+d}]",
                    "dates": int(x.notna().sum()), "corr_with_date_up_rate": round(float(rate.corr(x)), 4)})
    return out


# ----------------------------------------------------------------------------- statistics helpers
def date_index(dates):
    u, inv = np.unique(np.asarray(dates), return_inverse=True)
    return inv, len(u)


def cluster_boot(stat, dates, n_boot, seed):
    """stat(weights_per_row)->float; resamples whole dates with replacement."""
    inv, g = date_index(dates)
    rng = np.random.default_rng(seed)
    vals = np.empty(n_boot)
    for b in range(n_boot):
        w = np.bincount(rng.integers(0, g, g), minlength=g)[inv].astype(float)
        vals[b] = stat(w)
    return vals


def wcorr(x, y, w):
    sw = w.sum()
    if sw == 0:
        return np.nan
    mx, my = (w * x).sum() / sw, (w * y).sum() / sw
    vx, vy = (w * (x - mx) ** 2).sum(), (w * (y - my) ** 2).sum()
    return (w * (x - mx) * (y - my)).sum() / math.sqrt(vx * vy) if vx > 0 and vy > 0 else np.nan


def ci(vals, level=0.95):
    lo, hi = np.nanpercentile(vals, [100 * (1 - level) / 2, 100 * (1 + level) / 2])
    return round(float(lo), 4), round(float(hi), 4)


def require_no_test(d):
    if "test" in set(d["split"]):
        raise AssertionError("test-split rows must not enter performance diagnostics")


def calibration_slope(y, p, dates, n_boot=N_BOOT_SLOPE, seed=SEED):
    """Slope of outcome on logit(prediction): 1 = calibrated, <1 = overconfident, <=0 = no/negative relation."""
    x = np.log(p / (1 - p))
    fit = lambda idx, w=None: V.fit_logit(x[idx].reshape(-1, 1), y[idx], lam=1e-6)[1]
    point = fit(np.arange(len(y)))
    inv, g = date_index(dates)
    groups = [np.where(inv == j)[0] for j in range(g)]
    rng = np.random.default_rng(seed)
    bs = []
    for _ in range(n_boot):
        idx = np.concatenate([groups[i] for i in rng.integers(0, g, g)])
        if y[idx].min() != y[idx].max():
            bs.append(fit(idx))
    return float(point), ci(np.array(bs))


# ----------------------------------------------------------------------------- Part 2 diagnostics
def distributions(d):
    require_no_test(d)
    rows = []
    bnd = {(ot, f): np.percentile(g[f].to_numpy(float), V.WINSOR_PCT) for ot, g0 in d.groupby("option_type")
           for g in [g0[g0.split == "train"]] for f in ("lr1", "ur1", "sm")}
    for (ot, sp), g in d.groupby(["option_type", "split"]):
        for f in ("lr1", "ur1", "sm"):
            x = g[f].to_numpy(float)
            fin = x[np.isfinite(x)]
            q = np.percentile(fin, [1, 5, 25, 50, 75, 95, 99])
            rows.append({"option_type": ot, "split": sp, "feature": f, "rows": len(x), "dates": g["date"].nunique(),
                         "missing": int((~np.isfinite(x)).sum()), "mean": fin.mean(), "sd": fin.std(), "min": fin.min(),
                         "p1": q[0], "p5": q[1], "p25": q[2], "p50": q[3], "p75": q[4], "p95": q[5], "p99": q[6], "max": fin.max(),
                         "n_outside_train_p1_p99": int(((fin < bnd[(ot, f)][0]) | (fin > bnd[(ot, f)][1])).sum()),
                         "up_rate": g["target_next_up"].mean()})
    return pd.DataFrame(rows).round(5)


def relationships(d):
    require_no_test(d)
    rows = []
    for (ot, sp), g in d.groupby(["option_type", "split"]):
        y = g["target_next_up"].to_numpy(float)
        dates = g["date"].to_numpy()
        for f in ("lr1", "ur1", "sm"):
            rx, ry = g[f].rank().to_numpy(), pd.Series(y).rank().to_numpy()
            point = wcorr(rx, ry, np.ones(len(y)))
            lo, hi = ci(cluster_boot(lambda w: wcorr(rx, ry, w), dates, N_BOOT, SEED))
            dd = g.groupby("date").agg(f=(f, "mean"), y=("target_next_up", "mean"))
            rows.append({"option_type": ot, "split": sp, "feature": f, "rows": len(g), "dates": len(dd),
                         "spearman_row_level": round(point, 4), "ci95_date_cluster_lo": lo, "ci95_date_cluster_hi": hi,
                         "date_level_pearson": round(float(dd["f"].corr(dd["y"])), 4),
                         "note": "rank corr uses full-sample ranks inside the bootstrap; same-date rows are one cluster"})
    out = pd.DataFrame(rows)
    out["sign_train_vs_validation"] = ""
    for (ot, f), g in out.groupby(["option_type", "feature"]):
        a = g[g.split == "train"]["spearman_row_level"]
        b = g[g.split == "validation"]["spearman_row_level"]
        if len(a) and len(b):
            lab = "OPPOSITE" if a.iloc[0] * b.iloc[0] < 0 else "same"
            out.loc[g.index, "sign_train_vs_validation"] = lab
    return out


def reversal_test(d):
    """Is the train->validation change in the feature/target rank correlation larger than date-level sampling noise?
    DIAGNOSTIC ONLY: nothing is reversed or promoted to a model."""
    require_no_test(d)
    rows = []
    for ot in sorted(d["option_type"].unique()):
        for f in ("lr1", "ur1"):
            parts = {}
            for sp in ("train", "validation"):
                g = d[(d.option_type == ot) & (d.split == sp)]
                inv, ng = date_index(g["date"].to_numpy())
                parts[sp] = (g[f].rank().to_numpy(), g["target_next_up"].rank().to_numpy(), inv, ng)
            if any(v[3] == 0 for v in parts.values()):
                continue
            rho = {k: wcorr(v[0], v[1], np.ones(len(v[0]))) for k, v in parts.items()}
            rng = np.random.default_rng(SEED)
            delta = np.empty(N_BOOT)
            for b in range(N_BOOT):
                r = {}
                for sp, (x, y, inv, ng) in parts.items():
                    r[sp] = wcorr(x, y, np.bincount(rng.integers(0, ng, ng), minlength=ng)[inv].astype(float))
                delta[b] = r["validation"] - r["train"]
            lo, hi = ci(delta)
            rows.append({"option_type": ot, "feature": f, "train_spearman": round(rho["train"], 4), "validation_spearman": round(rho["validation"], 4),
                         "delta_validation_minus_train": round(rho["validation"] - rho["train"], 4), "delta_ci95_lo": lo, "delta_ci95_hi": hi,
                         "share_of_bootstrap_delta_le_0": round(float((delta <= 0).mean()), 3), "train_dates": parts["train"][3],
                         "validation_dates": parts["validation"][3], "sign_flipped": rho["train"] * rho["validation"] < 0,
                         "delta_ci_excludes_0": not (lo <= 0 <= hi),
                         "note": "diagnostic only; flipping a feature after seeing validation would be selection on validation"})
    return pd.DataFrame(rows)


def binned_rates(d):
    require_no_test(d)
    rows = []
    for (ot, sp), g in d.groupby(["option_type", "split"]):
        for f, (edges, labels) in BINS.items():
            b = pd.cut(g[f], edges, labels=labels, right=False)
            for lab in labels:
                h = g[b == lab]
                if h.empty:
                    rows.append({"option_type": ot, "split": sp, "feature": f, "bin": lab, "rows": 0, "dates": 0})
                    continue
                y, dates = h["target_next_up"].to_numpy(float), h["date"].to_numpy()
                lo, hi = ci(cluster_boot(lambda w: (w * y).sum() / w.sum() if w.sum() else np.nan, dates, N_BOOT, SEED))
                rows.append({"option_type": ot, "split": sp, "feature": f, "bin": lab, "rows": len(h), "dates": h["date"].nunique(),
                             "up_rate": round(float(y.mean()), 4), "ci95_date_cluster_lo": lo, "ci95_date_cluster_hi": hi})
    return pd.DataFrame(rows)


def model_predictions(tr, te):
    p_tr, _ = V.predict_all(tr, tr)
    p_te, skipped = V.predict_all(tr, te)
    return p_tr, p_te


def metric_rows(ot, label, df, preds):
    y = df["target_next_up"].to_numpy(float)
    b0 = V.brier(preds["B0_training_prior"], y)
    return [{"option_type": ot, "sample": label, "model": m, "rows": len(y), "dates": df["date"].nunique(),
             "brier": round(V.brier(p, y), 5), "brier_diff_vs_B0": round(V.brier(p, y) - b0, 5),
             "log_loss": round(V.logloss(p, y), 5), "auc": round(V.auc(y, p.to_numpy()), 4),
             "pred_mean": round(float(p.mean()), 4), "pred_sd": round(float(p.std()), 4), "obs_rate": round(float(y.mean()), 4)}
            for m, p in preds.items()]


def calibration_rows(ot, label, df, preds, n_bins=5, n_min=100):
    out = []
    y, dates = df["target_next_up"].to_numpy(float), df["date"].to_numpy()
    for m, p in preds.items():
        if m.startswith("B0") or len(y) < n_min or p.nunique() < 2:
            continue
        q = pd.qcut(p.rank(method="first"), min(n_bins, p.nunique()), labels=False).to_numpy()
        for b in np.unique(q):
            mask = q == b
            yb, db = y[mask], dates[mask]
            lo, hi = ci(cluster_boot(lambda w: (w * yb).sum() / w.sum() if w.sum() else np.nan, db, 300, SEED))
            out.append({"option_type": ot, "sample": label, "model": m, "bin": int(b) + 1, "rows": int(mask.sum()),
                        "dates": len(np.unique(db)), "mean_pred": round(float(p.to_numpy()[mask].mean()), 4),
                        "obs_rate": round(float(yb.mean()), 4), "obs_ci95_lo": lo, "obs_ci95_hi": hi})
    return out


def slope_rows(ot, label, df, preds):
    y, dates = df["target_next_up"].to_numpy(float), df["date"].to_numpy()
    out = []
    for m, p in preds.items():
        if m.startswith("B0") or p.nunique() < 2:
            continue
        s, (lo, hi) = calibration_slope(y, np.clip(p.to_numpy(), 1e-6, 1 - 1e-6), dates)
        out.append({"option_type": ot, "sample": label, "model": m, "calibration_slope": round(s, 4),
                    "slope_ci95_date_cluster_lo": lo, "slope_ci95_date_cluster_hi": hi})
    return out


def date_level(ot, te, preds):
    y = te["target_next_up"].to_numpy(float)
    base = (preds["B0_training_prior"].to_numpy() - y) ** 2
    t = pd.DataFrame({"date": te["date"].to_numpy(), "strike": te["strike"].to_numpy(), "y": y})
    for m, p in preds.items():
        t[f"mean_p_{m[:2]}"] = p.to_numpy()
        t[f"se_{m[:2]}"] = (p.to_numpy() - y) ** 2
        t[f"dse_{m[:2]}"] = t[f"se_{m[:2]}"] - base
    t["ur1"] = te["ur1"].to_numpy()
    agg = {c: "mean" for c in t.columns if c.startswith("mean_p_")}
    agg.update({c: "sum" for c in t.columns if c.startswith("dse_")})
    out = t.groupby("date").agg(rows=("y", "size"), obs_rate=("y", "mean"), ur1=("ur1", "mean"), **{k: (k, v) for k, v in agg.items()}).reset_index()
    out.insert(0, "option_type", ot)
    num = out.select_dtypes("number").columns
    out[num] = out[num].round(5)
    return out


def loss_concentration(ot, te, preds, bounds):
    """Where does the validation excess loss (model - B0) come from: dates, extreme rows."""
    y = te["target_next_up"].to_numpy(float)
    base = (preds["B0_training_prior"].to_numpy() - y) ** 2
    rows = []
    for m, p in preds.items():
        if m.startswith("B0"):
            continue
        dse = (p.to_numpy() - y) ** 2 - base
        by_date = pd.Series(dse).groupby(te["date"].to_numpy()).sum().sort_values(ascending=False)
        total = float(dse.sum())
        worse = by_date[by_date > 0]
        extreme = np.zeros(len(te), bool)
        for c, (lo, hi) in bounds.items():
            if m in ("B1_lag1_settle_logreturn", "B4_logit_lr1_ur1_signedlogm") and c == "lr1":
                x = te[c].to_numpy(float)
                extreme |= (x < lo) | (x > hi)
        rows.append({"option_type": ot, "model": m, "rows": len(te), "dates": len(by_date), "total_excess_brier_sum": round(total, 4),
                     "mean_excess_brier": round(total / len(te), 5), "dates_worse_than_B0": int((by_date > 0).sum()),
                     "top1_date_share_of_loss_on_worse_dates": round(float(worse.iloc[0] / worse.sum()), 3) if len(worse) else np.nan,
                     "top5_dates_share_of_loss_on_worse_dates": round(float(worse.iloc[:5].sum() / worse.sum()), 3) if len(worse) else np.nan,
                     "rows_outside_train_winsor_bounds": int(extreme.sum()),
                     "mean_excess_brier_excluding_those_rows": round(float(dse[~extreme].mean()), 5)})
    return rows


def b3_drivers(ot, tr, te):
    """How much of the B3 validation difference vs B0 is carried by single dates / strikes / buckets."""
    y = te["target_next_up"].to_numpy(float)
    rates = V.BucketRates().fit(tr["bucket"].to_numpy(), tr["target_next_up"].to_numpy(float))
    p3 = rates.transform(te["bucket"].to_numpy())
    p0 = V.prior_prob(tr["target_next_up"].to_numpy(float))
    dse = (p3 - y) ** 2 - (p0 - y) ** 2
    n = len(y)
    out = [{"option_type": ot, "view": "overall", "item": "mean_diff_B3_minus_B0", "value": round(float(dse.mean()), 5),
            "rows": n, "dates": te["date"].nunique()}]
    for by, key in (("date", te["date"].to_numpy()), ("strike", te["strike"].to_numpy())):
        s = pd.Series(dse).groupby(key).agg(["sum", "size"])
        loo = [(dse.sum() - r["sum"]) / (n - r["size"]) for _, r in s.iterrows()]
        out += [{"option_type": ot, "view": f"leave-one-{by}-out", "item": "min_mean_diff", "value": round(min(loo), 5), "rows": n, "dates": te["date"].nunique()},
                {"option_type": ot, "view": f"leave-one-{by}-out", "item": "max_mean_diff", "value": round(max(loo), 5), "rows": n, "dates": te["date"].nunique()},
                {"option_type": ot, "view": f"by-{by}", "item": f"{by}s_where_B3_better", "value": f"{int((s['sum'] < 0).sum())} of {len(s)}", "rows": n, "dates": te["date"].nunique()}]
        neg = s["sum"][s["sum"] < 0].sort_values()
        tot_neg = neg.sum()
        out.append({"option_type": ot, "view": f"by-{by}", "item": f"top3_{by}s_share_of_total_improvement",
                    "value": round(float(neg.iloc[:3].sum() / tot_neg), 3) if len(neg) else np.nan, "rows": n, "dates": te["date"].nunique()})
        out.append({"option_type": ot, "view": f"by-{by}", "item": f"net_sum_without_best_{by}", "value": round(float(dse.sum() - s["sum"].min()), 4),
                    "rows": n, "dates": te["date"].nunique()})
    for lab in V.LOGM_LABELS:
        m = te["bucket"].to_numpy() == lab
        out.append({"option_type": ot, "view": "by-bucket", "item": f"{lab}: sum_diff / rows / train_rate / val_rate",
                    "value": f"{dse[m].sum():.4f} / {int(m.sum())} / {rates.rates.get(lab, rates.prior):.4f} / {(y[m].mean() if m.any() else float('nan')):.4f}",
                    "rows": int(m.sum()), "dates": te.loc[m, "date"].nunique()})
    return out


def moneyness_tables(d, v48_rates, d48=None):
    require_no_test(d)
    ex = []
    for s, k, ot, hand, lab in MONEYNESS_EXAMPLES:
        got = float(V.signed_log_moneyness([s], [k], [ot])[0])
        b = str(V.bucketize([got])[0])
        ex.append({"spot": s, "strike": k, "option_type": ot, "hand_calculated": hand, "code_value": round(got, 7),
                   "abs_diff": round(abs(got - hand), 8), "expected_bucket": lab, "code_bucket": b,
                   "pass": abs(got - hand) < 1e-6 and b == lab})
    rows = []
    for (ot, sp, b), g in d.groupby(["option_type", "split", "bucket"]):
        rows.append({"option_type": ot, "split": sp, "bucket": b, "rows": len(g), "dates": g["date"].nunique(),
                     "contracts": g["strike"].nunique(), "up_rate": round(float(g["target_next_up"].mean()), 4)})
    bt = pd.DataFrame(rows)
    chk = []
    d48 = d if d48 is None else d48
    for ot in sorted(d["option_type"].unique()):
        fit = lambda x: V.BucketRates().fit(*(lambda t: (t["bucket"].to_numpy(), t["target_next_up"].to_numpy(float)))(x[(x["split"] == "train") & (x["option_type"] == ot)]))
        rt, rt49 = fit(d48), fit(d)    # rt: V4.8 eligibility (must reproduce V4.8); rt49: corrected eligibility
        for lab in V.LOGM_LABELS:
            rep = v48_rates[(v48_rates.option_type == ot) & (v48_rates.bucket == lab)]["rate_shrunk"].iloc[0]
            chk.append({"option_type": ot, "bucket": lab, "refit_train_only_v48_eligibility": round(rt.rates.get(lab, rt.prior), 5), "v48_reported": rep,
                        "abs_diff": round(abs(rt.rates.get(lab, rt.prior) - rep), 6), "match": abs(rt.rates.get(lab, rt.prior) - rep) < 2e-5,
                        "refit_train_only_v49_eligibility": round(rt49.rates.get(lab, rt49.prior), 5)})
    return pd.DataFrame(ex), bt, pd.DataFrame(chk)


def underlying_autocorr(und_by_date, cal, d):
    """Lag-1 autocorrelation of underlying daily returns, train vs validation dates only."""
    u = pd.Series(und_by_date).reindex(cal)
    ret = u.pct_change()
    tr_dates = set(d.loc[d.split == "train", "date"])
    va_dates = set(d.loc[d.split == "validation", "date"])
    out = []
    for nm, ds_ in (("train", tr_dates), ("validation", va_dates)):
        idx = [i for i, c in enumerate(cal) if c in ds_ and i >= 2]
        x = ret.iloc[idx].to_numpy()
        y = ret.shift(1).iloc[idx].to_numpy()
        rho = float(np.corrcoef(x, y)[0, 1])
        bs = []
        rng = np.random.default_rng(SEED)
        for _ in range(N_BOOT):   # simple i.i.d. date bootstrap; ignores serial dependence, so it is optimistic
            j = rng.integers(0, len(x), len(x))
            bs.append(np.corrcoef(x[j], y[j])[0, 1])
        out.append({"sample": nm, "dates": len(idx), "lag1_autocorr_underlying_return": round(rho, 4),
                    "ci95_iid_lo": ci(np.array(bs))[0], "ci95_iid_hi": ci(np.array(bs))[1]})
    return out


# ----------------------------------------------------------------------------- conclusions
def conclusions(ev):
    """Rule-based, using the numbers computed in this run. Each row: hypothesis, verdict, evidence."""
    rows = []
    add = lambda h, v, e, kind: rows.append({"hypothesis": h, "verdict": v, "kind": kind, "evidence": e})
    n_fail = ev["alignment_failures"]
    add("H1 implementation / temporal-alignment defect",
        ("MINOR DEFECT CONFIRMED - not an explanation of the failure" if ev["elig_rows"] else "NOT SUPPORTED (checks pass)") if n_fail == 0
        else "CONFIRMED DEFECT - fix before interpreting",
        f"{n_fail} failed alignment checks; V4.8 eligibility required the unused settle_return_lag2: {ev['elig_rows']} rows wrongly excluded, "
        f"max |change| in any validation Brier after the fix = {ev['elig_val_change']:.5f}; leakage perturbation changed {ev['perturb_features']} feature and {ev['perturb_labels']} label rows; "
        f"positive control: CE date up-rate vs underlying return [t,t+1] corr={ev['posctl_ce']:.2f}, PE={ev['posctl_pe']:.2f}, other lags |corr|<={ev['posctl_other_max']:.2f}",
        "implementation")
    for f, name in (("ur1", "underlying"), ("lr1", "settlement")):
        r = ev["rel"][f]
        add(f"H2 unstable relationship across periods ({name} return, CE)",
            ("SUPPORTED (borderline: CI lower bound within 0.02 of 0)" if r["delta_ci"][0] < 0.02 else "SUPPORTED") if r["opposite"] and r["delta_excludes0"]
            else ("POSSIBLE (not statistically separated)" if r["opposite"] else "NOT SUPPORTED"),
            f"Spearman train={r['train']:+.3f} (95% CI {r['train_ci'][0]:+.2f},{r['train_ci'][1]:+.2f}, {r['train_dates']} dates), "
            f"validation={r['val']:+.3f} (CI {r['val_ci'][0]:+.2f},{r['val_ci'][1]:+.2f}, {r['val_dates']} dates); sign {'reversed' if r['opposite'] else 'same'}; "
            f"change validation-train={r['delta']:+.3f}, date-cluster 95% CI {r['delta_ci'][0]:+.2f},{r['delta_ci'][1]:+.2f} "
            f"({'excludes' if r['delta_excludes0'] else 'includes'} 0). Diagnostic only: the feature is not reversed or promoted", "statistical")
    s = ev["slope"]
    add("H3 probability scale amplifies the failure (CE validation; not an independent root cause)",
        "SUPPORTED (amplifier, not root cause)" if s["b2_slope_hi"] < 1 else "NOT SUPPORTED",
        f"calibration slope B2={s['b2']:+.2f} (CI {s['b2_lo']:+.2f},{s['b2_hi']:+.2f}), B1={s['b1']:+.2f}; in-sample Brier vs B0 {ev['insample_b2']:+.4f} but validation {ev['val_b2']:+.4f}; "
        f"prediction sd B2: train {ev['pred_sd_b2_train']:.3f} -> validation {ev['pred_sd_b2']:.3f} (ur1 sd {ev['ur1_sd_train']:.4f} -> {ev['ur1_sd_val']:.4f}); "
        f"observed up-rate sd across validation dates {ev['date_rate_sd']:.3f}. Slope <= 0 means the fitted direction carries no validation information", "statistical")
    add("H4 extreme observations",
        "SUPPORTED" if ev["extreme_b1_explains"] else "NOT SUPPORTED",
        f"B1 CE validation: {ev['b1_rows_outside']} rows outside training winsor bounds; mean excess Brier all rows {ev['b1_excess_all']:+.4f}, excluding them {ev['b1_excess_excl']:+.4f}; "
        f"ur1 has no outliers (max |ur1| val = {ev['ur1_absmax_val']:.3f})", "statistical")
    add("H5 loss concentrated in a few dates",
        "SUPPORTED" if ev["top5_share"] > 0.6 else "NOT SUPPORTED",
        f"B2 CE validation: worse than B0 on {ev['b2_dates_worse']} of {ev['val_dates']} dates; the 5 worst dates carry {ev['top5_share']:.0%} of the loss incurred on worse dates", "statistical")
    add("H6 no measurable signal in either period",
        "CANNOT BE EXCLUDED" if ev["any_ci_includes_zero"] else "NOT SUPPORTED",
        "training and validation date-cluster CIs: " + ev["ci_summary"] + "; effective sample is the number of dates, not rows", "statistical")
    au = ev["auto"]
    add("H8 the CE relationship is just NIFTY daily-return autocorrelation",
        "NOT SUPPORTED (signs disagree; intervals include 0)" if ev["auto_signs_disagree"] else "INCONCLUSIVE",
        f"underlying lag-1 autocorrelation: train {au['train']:+.3f} (iid CI {au['train_ci'][0]:+.2f},{au['train_ci'][1]:+.2f}), validation {au['val']:+.3f} "
        f"(CI {au['val_ci'][0]:+.2f},{au['val_ci'][1]:+.2f}); CE ur1 Spearman train {ev['rel']['ur1']['train']:+.3f}, validation {ev['rel']['ur1']['val']:+.3f}. "
        "CE rows include decaying far-OTM options whose daily direction also depends on volatility changes; this was not decomposed", "statistical")
    b = ev["b3"]
    add("H7 CE validation improvement of the moneyness model (B3) is a real effect",
        "NOT ESTABLISHED (survives dropping any one date/strike; not significant)" if not b["ci_includes_zero"] and not b["v48_selected"]
        else ("NOT ESTABLISHED" if not b["v48_selected"] else "WEAK SUPPORT"),
        f"mean Brier diff {b['mean']:+.4f}; better on {b['dates_better']}; top-3 dates carry {b['top3_share']:.0%} of the improvement; leave-one-date-out range "
        f"[{b['loo_min']:+.4f},{b['loo_max']:+.4f}], leave-one-strike-out [{b['loso_min']:+.4f},{b['loso_max']:+.4f}]; top-3 strikes carry {b['top3_strike_share']:.0%}; "
        f"{b['bucket_note']}; V4.8 did not select B3 (Bonferroni refit interval included 0). Bucket = group of far strikes, so bucket and contract effects are not separable", "statistical")
    order = {"CONFIRMED DEFECT - fix before interpreting": 0, "MINOR DEFECT CONFIRMED - not an explanation of the failure": 0, "SUPPORTED": 1, "WEAK SUPPORT": 2, "POSSIBLE (not statistically separated)": 3,
             "SUPPORTED (borderline: CI lower bound within 0.02 of 0)": 1, "SUPPORTED (amplifier, not root cause)": 2, "CANNOT BE EXCLUDED": 4, "NOT ESTABLISHED": 5, "NOT SUPPORTED (signs disagree; intervals include 0)": 8,
             "NOT ESTABLISHED (survives dropping any one date/strike; not significant)": 5}
    df = pd.DataFrame(rows)
    df["_o"] = df["verdict"].map(lambda v: order.get(v, 9))
    df = df.sort_values(["_o"], kind="stable").drop(columns="_o").reset_index(drop=True)
    df.insert(0, "rank", np.arange(1, len(df) + 1))
    return df


# ----------------------------------------------------------------------------- orchestration
def run(data_dir="/content", out_dir=None, verbose=True):
    data_dir, out_dir = Path(data_dir), Path(out_dir or data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in ("V4_8_Dataset.csv", "V4_8_Baseline_Comparison.csv", "V4_8_Moneyness_Bucket_Rates.csv"):
        if not (data_dir / f).exists():
            raise FileNotFoundError(f"{f} (V4.8 report) not found in {data_dir}")
    ds = pd.read_csv(data_dir / "V4_8_Dataset.csv", parse_dates=["date", "target_date"])
    raw = load_raw(data_dir)
    ind, cal, dup = independent_features(raw)
    w = A.write_csv
    # ---- Part 1
    chk, merged = compare_alignment(ind, cal, ds)
    chk.append({"check": "duplicate contract-date keys in raw files", "issues": dup, "result": "PASS" if dup == 0 else "FAIL", "detail": ""})
    ident = (raw.groupby(["option_type", "strike", "date"]).expiry.nunique() > 1).sum()
    chk.append({"check": "same type/strike/date under more than one expiry", "issues": int(ident), "result": "PASS" if ident == 0 else "FAIL", "detail": f"expiries: {sorted(raw.expiry.dt.strftime('%Y-%m-%d').unique())}"})
    pf, pl, psd, T = perturbation_leak_test(raw, len(cal) // 2)
    chk.append({"check": f"leakage perturbation (prices/underlying after {T.date()} scaled): features at dates <= T changed", "issues": pf, "result": "PASS" if pf == 0 else "FAIL", "detail": ""})
    chk.append({"check": f"leakage perturbation: labels at dates < {T.date()} changed", "issues": pl, "result": "PASS" if pl == 0 else "FAIL", "detail": ""})
    chk.append({"check": f"same-day perturbation (only {T.date()} changed): features at the decision day and earlier changed", "issues": psd,
                "result": "PASS" if psd == 0 else "FAIL", "detail": "features may use t-1 and earlier only"})
    und_by_date = raw.groupby("date")["und"].first().to_dict()
    floor_filtered = ds[(~ds["eligible"]) & ds["exclusion_reason"].fillna("").str.contains("R3")]
    n_with_feat = int(((~merged["eligible"].astype(bool)) & merged["exclusion_reason"].fillna("").str.contains("R3") & merged["lr1_x"].notna()).sum())
    vacuous = len(floor_filtered) == 0
    chk.append({"check": "lags computed before filtering: floor-excluded rows still carry lagged features", "issues": 0 if (n_with_feat > 0 or vacuous) else 1,
                "result": "PASS" if (n_with_feat > 0 or vacuous) else "FAIL", "detail": f"{n_with_feat} of {len(floor_filtered)} price-floor-excluded rows have lr1 from the unfiltered series"})
    # sorting independence of the V4.7 builder
    try:
        src, _ = A.load_source(str(data_dir), A.Checks())
        a, _, _ = M.build_features(src)
        b, _, _ = M.build_features(src.sample(frac=1.0, random_state=SEED).reset_index(drop=True))
        key = ["option_type", "strike", "date"]
        cols = M.FEATURES + ["target_next_up"]
        a, b = a.sort_values(key).reset_index(drop=True), b.sort_values(key).reset_index(drop=True)
        diff = int((~np.isclose(a[cols].to_numpy(float), b[cols].to_numpy(float), equal_nan=True)).sum())
        chk.append({"check": "V4.7 feature builder gives identical output when input rows are shuffled", "issues": diff, "result": "PASS" if diff == 0 else "FAIL", "detail": f"{len(a)} rows"})
    except Exception as e:  # source loader needs the V4.7 audit module only for this check
        chk.append({"check": "V4.7 feature builder shuffle test", "issues": 1, "result": "FAIL", "detail": repr(e)})
    lag_rows = lead_lag(ds, cal, und_by_date, "CE") + lead_lag(ds, cal, und_by_date, "PE")
    # ---- eligible train/validation frames
    ds["eligible_v49"] = corrected_eligibility(ds, ind).to_numpy()
    d48 = ds[ds["eligible"] & ds["split"].isin(["train", "validation"])].copy()
    d = ds[ds["eligible_v49"] & ds["split"].isin(["train", "validation"])].copy()   # corrected eligibility: ALL diagnostics use this
    require_no_test(d)
    require_no_test(d48)
    chk.append({"check": "V4.9 eligibility equals independent rule", "issues": int((ds["eligible_v49"] != ind_match_rule(ds, ind)).sum()),
                "result": "PASS" if (ds["eligible_v49"] == ind_match_rule(ds, ind)).all() else "FAIL", "detail": f"{int(ds['eligible_v49'].sum())} eligible vs V4.8 {int(ds['eligible'].sum())}"})
    effect = eligibility_effect(d48, d)
    v48 = pd.read_csv(data_dir / "V4_8_Baseline_Comparison.csv")
    met, cal_rows, slope, dl, conc, drv, repro = [], [], [], [], [], [], []
    bounds_by_type, preds_store = {}, {}
    for ot in sorted(d["option_type"].unique()):
        tr, te = d[(d.split == "train") & (d.option_type == ot)], d[(d.split == "validation") & (d.option_type == ot)]
        if te.empty:
            continue
        tr48, te48 = d48[(d48.split == "train") & (d48.option_type == ot)], d48[(d48.split == "validation") & (d48.option_type == ot)]
        _, p48 = model_predictions(tr48, te48)
        p_tr, p_te = model_predictions(tr, te)
        preds_store[ot] = (tr, te, p_tr, p_te)
        met += metric_rows(ot, "train_in_sample", tr, p_tr) + metric_rows(ot, "validation", te, p_te)
        cal_rows += calibration_rows(ot, "train_in_sample", tr, p_tr) + calibration_rows(ot, "validation", te, p_te)
        slope += slope_rows(ot, "validation", te, p_te) + slope_rows(ot, "train_in_sample", tr, p_tr)
        dl.append(date_level(ot, te, p_te))
        bounds = {c: tuple(np.percentile(tr[c].to_numpy(float), V.WINSOR_PCT)) for c in ("lr1", "ur1", "sm")}
        bounds_by_type[ot] = bounds
        conc += loss_concentration(ot, te, p_te, bounds)
        drv += b3_drivers(ot, tr, te)
        for m, p in p_te.items():   # reproduce V4.8 headline numbers
            row = v48[(v48.option_type == ot) & (v48.split == "validation") & (v48.model == m)]
            if len(row):
                b48 = V.brier(p48[m], te48["target_next_up"].to_numpy(float))
                repro.append({"option_type": ot, "model": m, "v48_brier": row["brier"].iloc[0], "v49_brier_on_v48_eligibility": round(b48, 5),
                              "abs_diff": abs(row["brier"].iloc[0] - b48)})
    repro = pd.DataFrame(repro)
    chk.append({"check": "V4.8 validation Brier scores reproduced from the V4.8 dataset (tolerance 1e-4)", "issues": int((repro["abs_diff"] > 1e-4).sum()),
                "result": "PASS" if (repro["abs_diff"] <= 1e-4).all() else "FAIL", "detail": f"{len(repro)} model/type pairs"})
    # preprocessing parity (train-fit, applied unchanged to validation)
    par = []
    for ot, (tr, te, _, _) in preds_store.items():
        for c in ("lr1", "ur1", "sm"):
            s = V.Standardizer().fit(tr[c].to_numpy(float))
            s.transform(te[c].to_numpy(float))
            outside = int(((te[c] < s.lo) | (te[c] > s.hi)).sum())
            par.append({"option_type": ot, "feature": c, "fit_rows": len(tr), "train_p1": s.lo, "train_p99": s.hi, "train_mean": s.mu, "train_sd": s.sd,
                        "validation_rows": len(te), "validation_missing_imputed": s.n_imputed, "validation_outside_train_bounds": outside})
    par = pd.DataFrame(par).round(6)
    chk.append({"check": "validation preprocessing: no missing values imputed, same train-fitted parameters", "issues": int(par["validation_missing_imputed"].sum()),
                "result": "PASS" if par["validation_missing_imputed"].sum() == 0 else "FAIL", "detail": "Standardizer fitted on training rows only; transform() never refits"})
    ex, bt, bchk = moneyness_tables(d, pd.read_csv(data_dir / "V4_8_Moneyness_Bucket_Rates.csv"), d48)
    chk.append({"check": "moneyness hand-worked examples (CE/PE formulas and buckets)", "issues": int((~ex["pass"]).sum()), "result": "PASS" if ex["pass"].all() else "FAIL", "detail": f"{len(ex)} examples"})
    chk.append({"check": "bucket rates refit on training only equal V4.8 reported rates", "issues": int((~bchk["match"]).sum()), "result": "PASS" if bchk["match"].all() else "FAIL", "detail": ""})
    # hand-calculated example rows (predeclared rule: closest-to-ATM eligible row at the median date of each type/split)
    exrows = []
    for ot in ("CE", "PE"):
        for sp in ("train", "validation"):
            g = d[(d.option_type == ot) & (d.split == sp)]
            if g.empty:
                continue
            md_ = sorted(g["date"].unique())[len(g["date"].unique()) // 2]
            r = g[g["date"] == md_].loc[lambda x: x["sm"].abs().idxmin()]
            i = merged[(merged.option_type == ot) & (merged.strike == r.strike) & (merged.date == md_)].iloc[0]
            exrows.append({"option_type": ot, "split": sp, "strike": r.strike, "expiry": str(r.expiry)[:10], "decision_date_t": md_.date(),
                           "settle_t-2": i.s2, "settle_t-1": i.s1, "settle_t": i.s0, "settle_t+1": i.sn, "target_date": pd.Timestamp(i.target_date_x).date(),
                           "underlying_t-2": i.u2, "underlying_t-1": i.u1,
                           "lr1_by_hand": round(math.log(i.s1 / i.s2), 8), "ur1_by_hand": round(i.u1 / i.u2 - 1, 8),
                           "signed_logm_by_hand": round(math.log(i.u1 / r.strike) * (1 if ot == "CE" else -1), 8),
                           "target_by_hand": float(i.sn > i.s0), "dataset_lr1": r.lr1, "dataset_ur1": r.ur1, "dataset_sm": r.sm, "dataset_target": r.target_next_up,
                           "all_equal": bool(abs(math.log(i.s1 / i.s2) - r.lr1) < 1e-7 and abs(i.u1 / i.u2 - 1 - r.ur1) < 1e-7 and float(i.sn > i.s0) == r.target_next_up)})
    exrows = pd.DataFrame(exrows)
    chk.append({"check": "hand-calculated example rows equal dataset values", "issues": int((~exrows["all_equal"]).sum()), "result": "PASS" if exrows["all_equal"].all() else "FAIL", "detail": f"{len(exrows)} rows"})
    chk = pd.DataFrame(chk)
    # ---- Part 2 tables
    dist, rel, binr, rev = distributions(d), relationships(d), binned_rates(d), reversal_test(d)
    metrics, calib, slopes = pd.DataFrame(met), pd.DataFrame(cal_rows), pd.DataFrame(slope)
    datelvl, concd, drivers = pd.concat(dl, ignore_index=True), pd.DataFrame(conc), pd.DataFrame(drv)
    auto = pd.DataFrame(underlying_autocorr(und_by_date, cal, d))
    # ---- conclusions (CE focus, per the brief)
    def relrow(f, sp):
        r = rel[(rel.option_type == "CE") & (rel.feature == f) & (rel.split == sp)].iloc[0]
        return r
    revd = {}
    for f in ("ur1", "lr1"):
        a, b = relrow(f, "train"), relrow(f, "validation")
        rv = rev[(rev.option_type == "CE") & (rev.feature == f)].iloc[0]
        revd[f] = {"delta": rv.delta_validation_minus_train, "delta_ci": (rv.delta_ci95_lo, rv.delta_ci95_hi), "delta_excludes0": bool(rv.delta_ci_excludes_0),
                   "train": a.spearman_row_level, "val": b.spearman_row_level, "train_ci": (a.ci95_date_cluster_lo, a.ci95_date_cluster_hi),
                   "val_ci": (b.ci95_date_cluster_lo, b.ci95_date_cluster_hi), "train_dates": int(a.dates), "val_dates": int(b.dates),
                   "opposite": a.spearman_row_level * b.spearman_row_level < 0,
                   "val_ci_excludes_train": not (b.ci95_date_cluster_lo <= a.spearman_row_level <= b.ci95_date_cluster_hi)}
    sl = lambda m, s: slopes[(slopes.option_type == "CE") & (slopes["sample"] == s) & (slopes.model == m)].iloc[0]
    mt = lambda m, s: metrics[(metrics.option_type == "CE") & (metrics["sample"] == s) & (metrics.model == m)].iloc[0]
    cc = lambda m: concd[(concd.option_type == "CE") & (concd.model == m)].iloc[0]
    dv = lambda i_: drivers[(drivers.option_type == "CE") & (drivers.view == i_[0]) & (drivers.item == i_[1])]["value"].iloc[0]
    te_ce = preds_store["CE"][1]
    date_rate_sd = float(te_ce.groupby("date")["target_next_up"].mean().std())
    ll = lag_rows
    oth = max(abs(r["corr_with_date_up_rate"]) for r in ll if r["option_type"] == "CE" and r["shift_days"] != 0)
    oth = max(oth, max(abs(r["corr_with_date_up_rate"]) for r in ll if r["option_type"] == "PE" and r["shift_days"] != 0))
    cis = []
    anyz = False
    for f in ("ur1", "lr1"):
        for sp in ("train", "validation"):
            r = relrow(f, sp)
            inc = r.ci95_date_cluster_lo <= 0 <= r.ci95_date_cluster_hi
            anyz |= inc
            cis.append(f"{f}/{sp}: {'includes' if inc else 'excludes'} 0")
    p_b2 = preds_store["CE"][3]["B2_lag1_underlying_return"]
    vch = effect[effect.view.str.startswith("validation brier")]
    ev = {"alignment_failures": int((chk["result"] == "FAIL").sum()), "perturb_features": pf, "perturb_labels": pl,
          "elig_rows": int(effect[effect.view == "eligible rows"]["change"].sum()),
          "elig_val_change": float(vch["change"].abs().max()) if len(vch) else 0.0,
          "posctl_ce": [r for r in ll if r["option_type"] == "CE" and r["shift_days"] == 0][0]["corr_with_date_up_rate"],
          "posctl_pe": [r for r in ll if r["option_type"] == "PE" and r["shift_days"] == 0][0]["corr_with_date_up_rate"], "posctl_other_max": oth,
          "rel": revd,
          "auto": {"train": float(auto.iloc[0].lag1_autocorr_underlying_return), "val": float(auto.iloc[1].lag1_autocorr_underlying_return),
                   "train_ci": (auto.iloc[0].ci95_iid_lo, auto.iloc[0].ci95_iid_hi), "val_ci": (auto.iloc[1].ci95_iid_lo, auto.iloc[1].ci95_iid_hi)},
          "auto_signs_disagree": bool(auto.iloc[0].lag1_autocorr_underlying_return * revd["ur1"]["train"] < 0
                                      or auto.iloc[1].lag1_autocorr_underlying_return * revd["ur1"]["val"] < 0),
          "slope": {"b2": sl("B2_lag1_underlying_return", "validation").calibration_slope, "b2_lo": sl("B2_lag1_underlying_return", "validation").slope_ci95_date_cluster_lo,
                    "b2_hi": sl("B2_lag1_underlying_return", "validation").slope_ci95_date_cluster_hi, "b1": sl("B1_lag1_settle_logreturn", "validation").calibration_slope,
                    "b2_slope_hi": sl("B2_lag1_underlying_return", "validation").slope_ci95_date_cluster_hi},
          "insample_b2": mt("B2_lag1_underlying_return", "train_in_sample").brier_diff_vs_B0, "val_b2": mt("B2_lag1_underlying_return", "validation").brier_diff_vs_B0,
          "pred_sd_b2": float(p_b2.std()), "date_rate_sd": date_rate_sd,
          "pred_sd_b2_train": float(preds_store["CE"][2]["B2_lag1_underlying_return"].std()),
          "ur1_sd_train": float(preds_store["CE"][0]["ur1"].std()), "ur1_sd_val": float(te_ce["ur1"].std()),
          "b1_rows_outside": int(cc("B1_lag1_settle_logreturn").rows_outside_train_winsor_bounds),
          "b1_excess_all": float(cc("B1_lag1_settle_logreturn").mean_excess_brier), "b1_excess_excl": float(cc("B1_lag1_settle_logreturn").mean_excess_brier_excluding_those_rows),
          "ur1_absmax_val": float(te_ce["ur1"].abs().max()),
          "extreme_b1_explains": bool(cc("B1_lag1_settle_logreturn").mean_excess_brier_excluding_those_rows <= 0.25 * cc("B1_lag1_settle_logreturn").mean_excess_brier),
          "b2_dates_worse": int(cc("B2_lag1_underlying_return").dates_worse_than_B0), "val_dates": int(cc("B2_lag1_underlying_return").dates),
          "top5_share": float(cc("B2_lag1_underlying_return").top5_dates_share_of_loss_on_worse_dates), "any_ci_includes_zero": bool(anyz), "ci_summary": "; ".join(cis),
          "b3": {"mean": float(dv(("overall", "mean_diff_B3_minus_B0"))), "dates_better": dv(("by-date", "dates_where_B3_better")),
                 "top3_share": float(dv(("by-date", "top3_dates_share_of_total_improvement"))),
                 "loo_min": float(dv(("leave-one-date-out", "min_mean_diff"))), "loo_max": float(dv(("leave-one-date-out", "max_mean_diff"))),
                 "loso_min": float(dv(("leave-one-strike-out", "min_mean_diff"))), "loso_max": float(dv(("leave-one-strike-out", "max_mean_diff"))),
                 "top3_strike_share": float(dv(("by-strike", "top3_strikes_share_of_total_improvement"))),
                 "bucket_note": "improvement sits in " + ", ".join(sorted(drivers[(drivers.option_type == "CE") & (drivers.view == "by-bucket")].assign(
                     v=lambda x: x["value"].str.split(" / ").str[0].astype(float)).nsmallest(2, "v")["item"].str.split(":").str[0])) + " buckets",
                 "v48_selected": bool(v48[(v48.option_type == "CE") & (v48.split == "validation") & (v48.model == "B3_signed_moneyness_bucket")]["selected_on_validation"].astype(str).eq("True").any()),
                 "ci_includes_zero": bool(float(dv(("leave-one-date-out", "max_mean_diff"))) >= 0 or float(dv(("leave-one-strike-out", "max_mean_diff"))) >= 0)}}
    concl = conclusions(ev)
    # ---- write
    w(chk, out_dir / f"{PREFIX}Temporal_Alignment_Checks.csv")
    w(exrows, out_dir / f"{PREFIX}Hand_Worked_Examples.csv")
    w(pd.DataFrame(lag_rows), out_dir / f"{PREFIX}Alignment_Lead_Lag_Control.csv")
    w(par, out_dir / f"{PREFIX}Preprocessing_Parity.csv")
    w(dist, out_dir / f"{PREFIX}Feature_Distributions.csv")
    w(rel, out_dir / f"{PREFIX}Feature_Target_Relationships.csv")
    w(binr, out_dir / f"{PREFIX}Binned_Event_Rates.csv")
    w(rev, out_dir / f"{PREFIX}Sign_Reversal_Test.csv")
    w(metrics, out_dir / f"{PREFIX}Model_Metrics.csv")
    w(slopes, out_dir / f"{PREFIX}Calibration_Slopes.csv")
    w(calib, out_dir / f"{PREFIX}Calibration.csv")
    w(datelvl, out_dir / f"{PREFIX}Date_Level_Performance.csv")
    w(concd, out_dir / f"{PREFIX}Loss_Concentration.csv")
    w(auto, out_dir / f"{PREFIX}Underlying_Autocorrelation.csv")
    w(ex, out_dir / f"{PREFIX}Moneyness_Examples.csv")
    w(bt, out_dir / f"{PREFIX}Moneyness_Bucket_Counts.csv")
    w(bchk, out_dir / f"{PREFIX}Moneyness_Train_Only_Check.csv")
    w(drivers, out_dir / f"{PREFIX}B3_Driver_Analysis.csv")
    w(repro.round(6), out_dir / f"{PREFIX}V48_Reproduction.csv")
    w(effect, out_dir / f"{PREFIX}Eligibility_Correction_Effect.csv")
    w(concl, out_dir / f"{PREFIX}Diagnostic_Conclusions.csv")
    names = sorted(p.name for p in out_dir.glob(PREFIX + "*.csv"))
    zp = out_dir / f"{PREFIX}Reports.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            z.writestr(zipfile.ZipInfo(n, date_time=(2026, 1, 1, 0, 0, 0)), (out_dir / n).read_bytes())
    if verbose:
        print("=" * 76, "\nV4.9 FEATURE DIAGNOSTICS (diagnostic only; test split not used)\n" + "=" * 76)
        print(chk[["check", "issues", "result"]].to_string(index=False))
        print(pd.DataFrame(lag_rows).to_string(index=False))
        print(concl[["rank", "hypothesis", "verdict"]].to_string(index=False))
        print("Saved", len(names) + 1, "files incl.", zp.name, "to", out_dir)
    if (chk["result"] == "FAIL").any():   # "DEFECT (fixed in V4.9)" rows are findings, not failures
        raise AssertionError("alignment/consistency checks failed:\n" + chk[chk.result == "FAIL"][["check", "issues"]].to_string(index=False))
    return {"checks": chk, "conclusions": concl, "relationships": rel, "metrics": metrics}


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    run(a[0] if a else "/content", a[1] if len(a) > 1 else None)
