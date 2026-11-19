"""ML Options Paper Trading V4.8 - controlled feature research (paper research only; no broker, no orders).

Question: do a few PREDECLARED features add out-of-sample value over a training-rate prior?
Reuses the V4.7 scripts unchanged (audit, lag/target/eligibility/split code). Defects found in V4.7 are fixed here only:
  D1 moneyness buckets were option-type-blind (same S/K bin = ITM for calls, OTM for puts).      -> signed log-moneyness, ITM>0 for both.
  D2 sign-table baselines mapped NaN to an arbitrary integer category.                           -> explicit MISSING/unseen policy.
  D3 audit gate only looked for REVIEW rows; it never checked the reports belong to the current inputs. -> hash + row-count gate (fail closed).
  D4 confidence intervals resampled evaluation dates only (fitted parameters treated as fixed).   -> adds train+eval date resampling WITH refit.
  D5 no multiplicity handling.                                                                    -> Bonferroni over the 8 predeclared validation comparisons.

PREDECLARED EXPERIMENT (fixed before any V4.8 comparison was run)
  Decision time: start of trading day t. Everything used is an end-of-day value from day t-1 or earlier. The label compares
  settle(t+1) with settle(t); settle(t) is NOT known at decision time, so the label is a research target, not a tradable return.
  B0 training-rate prior | B1 logistic on ln(settle[t-1]/settle[t-2]) | B2 logistic on underlying return (t-2 -> t-1)
  B3 train-fitted rates by signed-moneyness bucket | B4 logistic on B1+B2+signed log-moneyness (only if events/variable >= 10).
  Selection rule (validation only): a model is "selected" if the Bonferroni-adjusted (8 comparisons) train+eval refit
  interval for its paired Brier difference vs B0 lies entirely below 0. Test results are reported, never used to choose.
Usage: python v4_8_feature_research.py [DATA_DIR] [OUT_DIR]
"""
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import v4_7_model_dataset as M
import v4_7_source_audit as A

PREFIX = "V4_8_"
SEED = 20260801
N_BOOT = 1000
ALPHA = 0.05
N_COMPARISONS = 8                      # B1..B4 x {CE, PE} on validation
LOGM_EDGES = [-np.inf, -0.10, -0.03, 0.03, 0.10, np.inf]
LOGM_LABELS = ["DEEP_OTM", "OTM", "ATM", "ITM", "DEEP_ITM"]
MISSING = "MISSING"
WINSOR_PCT = (1, 99)
LAMBDA = 1.0                           # L2 on standardized features (intercept unpenalized); fixed, never tuned
SHRINK = 20                            # m-estimate pseudo-count toward the training prior for bucket rates
MIN_EVENTS_PER_VARIABLE = 10
PRIOR_CLIP = (0.01, 0.99)
SPECS = {
    "B1_lag1_settle_logreturn": ("num", ["lr1"]),
    "B2_lag1_underlying_return": ("num", ["ur1"]),
    "B3_signed_moneyness_bucket": ("bucket", ["bucket"]),
    "B4_logit_lr1_ur1_signedlogm": ("num", ["lr1", "ur1", "sm"]),
}
FEATURE_DEFS = [
    ("lr1", "ln(settle[t-1]/settle[t-2])", "close of day t-1", "winsorize 1/99 pct (train), standardize (train)", "NaN -> train mean (0 after standardizing), counted"),
    ("ur1", "underlying[t-1]/underlying[t-2]-1", "close of day t-1", "winsorize 1/99 pct (train), standardize (train)", "NaN -> train mean (0 after standardizing), counted"),
    ("sm", "CE: ln(S[t-1]/K); PE: ln(K/S[t-1])  (>0 = in the money for both)", "close of day t-1", "winsorize 1/99 pct (train), standardize (train)", "NaN -> train mean, counted"),
    ("bucket", "sm cut at -0.10,-0.03,0.03,0.10 -> DEEP_OTM,OTM,ATM,ITM,DEEP_ITM", "close of day t-1", "train-fitted rates, m-estimate shrink=20", "NaN or unseen label -> MISSING -> training prior, counted"),
    ("target_next_up", "settle[t+1] > settle[t], same contract, t+1 = next source trading day", "close of day t+1", "none", "no next consecutive day -> NaN -> ineligible"),
]


class GateError(RuntimeError):
    pass


# ----------------------------------------------------------------------------- gate (D3)
def check_gate(data_dir, n_source_rows=None):
    """Fail closed unless V4.7 audit reports exist, are clean, and belong to the current input files."""
    d = Path(data_dir)
    need = ["Validation_Checks", "Input_Manifest", "Row_Level_Comparison"]
    for n in need:
        if not (d / f"{A.OUT_PREFIX}{n}.csv").exists():
            raise GateError(f"V4.7 audit report missing: {A.OUT_PREFIX}{n}.csv (run the audit first)")
    vc = pd.read_csv(d / f"{A.OUT_PREFIX}Validation_Checks.csv")
    bad = vc[vc["result"] == "REVIEW"]
    if len(bad):
        raise GateError("V4.7 audit has unresolved REVIEW checks:\n" + bad[["scope", "check", "issues"]].to_string(index=False))
    man = pd.read_csv(d / f"{A.OUT_PREFIX}Input_Manifest.csv")
    tracked = man[man["role"].eq("source") | man["format"].isin(["udiff", "legacy"])]
    for r in tracked.itertuples():
        f = d / r.file
        if not f.exists():
            raise GateError(f"audit manifest lists {r.file} but it is not present")
        if A.sha256(f) != r.sha256:
            raise GateError(f"{r.file} changed since the audit ran (hash mismatch): rerun the V4.7 audit")
    listed = set(tracked["file"])
    for f in list(d.glob(A.SOURCE_GLOB)) + list(d.glob("BhavCopy_NSE_FO_*.csv*")):
        if " (" not in f.name and f.name not in listed:
            raise GateError(f"{f.name} is present but was not part of the audit: rerun the V4.7 audit")
    if n_source_rows is not None:
        rl = pd.read_csv(d / f"{A.OUT_PREFIX}Row_Level_Comparison.csv", usecols=["date"])
        if len(rl) != n_source_rows:
            raise GateError(f"audit row-level report has {len(rl)} rows, source has {n_source_rows}: stale audit")
    return len(tracked)


# ----------------------------------------------------------------------------- features (D1, D2)
def signed_log_moneyness(spot, strike, option_type):
    """>0 means in the money for BOTH calls and puts. Raises on unknown option types."""
    spot, strike, ot = np.asarray(spot, float), np.asarray(strike, float), np.asarray(option_type)
    if not np.isin(ot, ["CE", "PE"]).all():
        raise ValueError("option_type must be CE or PE")
    with np.errstate(divide="ignore", invalid="ignore"):
        m = np.log(spot / strike)
    m = np.where(np.isfinite(m), m, np.nan)
    return np.where(ot == "CE", m, -m)


def bucketize(sm):
    sm = np.asarray(sm, float)
    out = np.full(len(sm), MISSING, dtype=object)
    ok = np.isfinite(sm)
    out[ok] = pd.cut(sm[ok], LOGM_EDGES, labels=LOGM_LABELS, right=False).astype(str)
    return out


def add_v48_features(r):
    r = r.copy()
    with np.errstate(divide="ignore", invalid="ignore"):
        lr = np.log1p(r["settle_return_lag1"].to_numpy(float))
    r["lr1"] = np.where(np.isfinite(lr), lr, np.nan)
    r["ur1"] = r["underlying_return_lag1"].astype(float)
    r["sm"] = signed_log_moneyness(r["moneyness_lag1"], 1.0, r["option_type"])   # moneyness_lag1 is already S[t-1]/K
    r["bucket"] = bucketize(r["sm"])
    return r


# ----------------------------------------------------------------------------- train-only transformations
class Standardizer:
    """Winsorize + standardize one numeric column; every parameter comes from the training data only."""

    def fit(self, x):
        x = np.asarray(x, float)
        x = x[np.isfinite(x)]
        self.lo, self.hi = np.percentile(x, WINSOR_PCT)
        c = np.clip(x, self.lo, self.hi)
        self.mu, sd = float(c.mean()), float(c.std())
        self.sd = sd if sd > 0 else 1.0
        return self

    def transform(self, x):
        x = np.asarray(x, float)
        z = (np.clip(x, self.lo, self.hi) - self.mu) / self.sd   # clip(NaN) stays NaN
        self.n_imputed = int((~np.isfinite(z)).sum())
        return np.where(np.isfinite(z), z, 0.0)                    # 0 = training mean


class BucketRates:
    def fit(self, labels, y):
        labels, y = np.asarray(labels, object), np.asarray(y, float)
        self.prior = float(y.mean())
        self.rates = {}
        for k in LOGM_LABELS:
            m = labels == k
            if m.any():
                self.rates[k] = (float(y[m].sum()) + SHRINK * self.prior) / (m.sum() + SHRINK)
        self.counts = {k: int((labels == k).sum()) for k in LOGM_LABELS}
        return self

    def transform(self, labels):
        labels = np.asarray(labels, object)
        p = np.array([self.rates.get(k, self.prior) for k in labels], float)   # unseen or MISSING -> prior
        self.n_fallback = int(sum(k not in self.rates for k in labels))
        return p


def fit_logit(X, y, lam=LAMBDA, iters=60):
    n, k = X.shape
    Z = np.c_[np.ones(n), X]
    w, pen = np.zeros(k + 1), np.r_[0.0, np.full(k, lam)]
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(Z @ w, -30, 30)))
        g = Z.T @ (p - y) + pen * w
        H = (Z.T * (p * (1 - p))) @ Z + np.diag(pen) + 1e-9 * np.eye(k + 1)
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-9:
            break
    return w


def fit_model(kind, cols, arrs, y):
    if kind == "num":
        tx = [Standardizer().fit(arrs[c]) for c in cols]
        X = np.column_stack([t.transform(arrs[c]) for t, c in zip(tx, cols)])
        return {"kind": kind, "cols": cols, "tx": tx, "w": fit_logit(X, y)}
    return {"kind": kind, "cols": cols, "tab": BucketRates().fit(arrs["bucket"], y)}


def predict_model(mod, arrs):
    if mod["kind"] == "num":
        X = np.column_stack([t.transform(arrs[c]) for t, c in zip(mod["tx"], mod["cols"])])
        p = 1 / (1 + np.exp(-np.clip(np.c_[np.ones(len(X)), X] @ mod["w"], -30, 30)))
    else:
        p = mod["tab"].transform(arrs["bucket"])
    return np.clip(p, 1e-6, 1 - 1e-6)


def arrays(df):
    return {c: df[c].to_numpy() for c in ("lr1", "ur1", "sm", "bucket")}


def sub(arrs, idx):
    return {k: v[idx] for k, v in arrs.items()}


def prior_prob(y):
    return float(np.clip(np.mean(y), *PRIOR_CLIP))


def b4_justified(y_train):
    events = min(int(np.sum(y_train == 1)), int(np.sum(y_train == 0)))
    nvar = len(SPECS["B4_logit_lr1_ur1_signedlogm"][1])
    return events / nvar >= MIN_EVENTS_PER_VARIABLE, events, nvar


# ----------------------------------------------------------------------------- metrics + paired evaluation
def brier(p, y):
    return float(np.mean((np.asarray(p) - np.asarray(y)) ** 2))


def logloss(p, y):
    p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
    y = np.asarray(y, float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def auc(y, s):
    y = np.asarray(y)
    n1, n0 = y.sum(), len(y) - y.sum()
    if n1 == 0 or n0 == 0:
        return np.nan
    rk = pd.Series(s).rank().to_numpy()
    return float((rk[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def predict_all(tr, te):
    """Fit on tr only, predict te. Every model returns a Series indexed by te.index (paired rows)."""
    y = tr["target_next_up"].to_numpy(float)
    a_tr, a_te = arrays(tr), arrays(te)
    preds = {"B0_training_prior": pd.Series(prior_prob(y), index=te.index)}
    skipped = {}
    for name, (kind, cols) in SPECS.items():
        if name.startswith("B4"):
            ok, ev, nv = b4_justified(y)
            if not ok:
                skipped[name] = f"B4 skipped: {ev} minority-class events / {nv} variables < {MIN_EVENTS_PER_VARIABLE}"
                continue
        preds[name] = pd.Series(predict_model(fit_model(kind, cols, a_tr, y), a_te), index=te.index)
    assert_same_rows(preds)
    return preds, skipped


def assert_same_rows(preds):
    ref = None
    for k, s in preds.items():
        ref = s.index if ref is None else ref
        if not s.index.equals(ref):
            raise AssertionError(f"model {k} evaluated on different rows than the others")


# ----------------------------------------------------------------------------- uncertainty (D4)
def date_groups(dates):
    u, inv = np.unique(dates, return_inverse=True)
    return [np.where(inv == j)[0] for j in range(len(u))], inv


def bootstrap(tr, te, preds, seed, n_boot=None):
    """Paired Brier difference (model - B0) with two interval methods.
    eval_only : resample evaluation DATES; fitted models held fixed.
    refit     : resample training dates AND evaluation dates, refit every model (and the prior) on the resampled
                training rows, score on the resampled evaluation rows.  Captures sampling noise in fitted parameters and in
                the evaluation window. It does NOT capture serial dependence between neighbouring dates, contract
                selection, structural change, data errors, or the use of the same window in earlier experiments."""
    n_boot = N_BOOT if n_boot is None else n_boot
    rng = np.random.default_rng(seed)
    y_te = te["target_next_up"].to_numpy(float)
    y_tr = tr["target_next_up"].to_numpy(float)
    g_te, id_te = date_groups(te["date"].to_numpy())
    g_tr, _ = date_groups(tr["date"].to_numpy())
    se0 = (preds["B0_training_prior"].to_numpy() - y_te) ** 2
    models = [m for m in preds if not m.startswith("B0")]
    out = {m: {"eval_only": np.empty(n_boot), "refit": np.full(n_boot, np.nan)} for m in models}
    n_g = np.array([len(g) for g in g_te])
    s_g = {m: np.array([(((preds[m].to_numpy() - y_te) ** 2) - se0)[g].sum() for g in g_te]) for m in models}
    a_tr, a_te = arrays(tr), arrays(te)
    for b in range(n_boot):
        je = rng.integers(0, len(g_te), len(g_te))
        jt = rng.integers(0, len(g_tr), len(g_tr))
        den = n_g[je].sum()
        for m in models:
            out[m]["eval_only"][b] = s_g[m][je].sum() / den
        idx = np.concatenate([g_tr[i] for i in jt])
        yb = y_tr[idx]
        if yb.min() == yb.max():
            continue
        w = np.bincount(je, minlength=len(g_te))[id_te].astype(float)
        se0b = (prior_prob(yb) - y_te) ** 2
        at = sub(a_tr, idx)
        for m in models:
            kind, cols = SPECS[m]
            pb = predict_model(fit_model(kind, cols, at, yb), a_te)
            out[m]["refit"][b] = np.sum(w * ((pb - y_te) ** 2 - se0b)) / w.sum()
    return out


def interval(x, level):
    lo, hi = np.nanpercentile(x, [100 * (1 - level) / 2, 100 * (1 - (1 - level) / 2)])
    return float(lo), float(hi)


def calibration(y, p, n_min=200, n_bins=5):
    if len(y) < n_min or np.unique(p).size < 2:
        return None
    q = pd.qcut(pd.Series(p), min(n_bins, np.unique(p).size), duplicates="drop")
    d = pd.DataFrame({"y": y, "p": p, "bin": q.astype(str)}).groupby("bin", observed=True).agg(
        n=("y", "size"), mean_pred=("p", "mean"), obs_rate=("y", "mean")).reset_index()
    return d


# ----------------------------------------------------------------------------- orchestration
def run_experiment(d):
    """d: eligible rows with split in (train, validation, test). Returns report frames."""
    comp, ivals, calib, bucket_rows = [], [], [], []
    adj_level = 1 - ALPHA / N_COMPARISONS
    for k, ot in enumerate(sorted(d["option_type"].unique())):
        tr = d[(d["split"] == "train") & (d["option_type"] == ot)]
        if tr["target_next_up"].nunique() < 2:
            raise RuntimeError(f"{ot}: training data has a single class; cannot fit baselines")
        rates = BucketRates().fit(tr["bucket"].to_numpy(), tr["target_next_up"].to_numpy(float))
        for lab in LOGM_LABELS:
            bucket_rows.append({"option_type": ot, "bucket": lab, "train_rows": rates.counts[lab],
                                "rate_shrunk": round(rates.rates.get(lab, rates.prior), 5), "train_prior": round(rates.prior, 5)})
        for j, sp in enumerate(("validation", "test")):
            te = d[(d["split"] == sp) & (d["option_type"] == ot)]
            base = {"option_type": ot, "split": sp, "status": "confirmatory-style (selection split)" if sp == "validation"
                    else "EXPLORATORY: window overlaps data used in earlier experiments; never used to choose"}
            if te.empty:
                comp.append({**base, "model": "(no eligible rows)", "eval_rows": 0, "eval_dates": 0})
                continue
            preds, skipped = predict_all(tr, te)
            y = te["target_next_up"].to_numpy(float)
            b0b, b0l = brier(preds["B0_training_prior"], y), logloss(preds["B0_training_prior"], y)
            boots = bootstrap(tr, te, preds, SEED + 100 * k + 10 * j)
            for name, p in preds.items():
                row = {**base, "model": name, "eval_rows": len(te), "eval_dates": te["date"].nunique(),
                       "train_rows": len(tr), "train_dates": tr["date"].nunique(),
                       "train_up_rate": round(float(tr["target_next_up"].mean()), 4), "eval_up_rate": round(float(y.mean()), 4),
                       "brier": round(brier(p, y), 5), "log_loss": round(logloss(p, y), 5), "auc": round(auc(y, p.to_numpy()), 4),
                       "accuracy_at_0.5": round(float(((p.to_numpy() >= 0.5) == (y == 1)).mean()), 4),
                       "brier_diff_vs_B0": round(brier(p, y) - b0b, 5), "logloss_diff_vs_B0": round(logloss(p, y) - b0l, 5),
                       "selected_on_validation": ""}
                if name != "B0_training_prior":
                    for meth in ("eval_only", "refit"):
                        for lvl, tag in ((1 - ALPHA, f"{int((1 - ALPHA) * 100)}%_unadjusted"),
                                         (adj_level, f"{adj_level * 100:.3f}%_bonferroni_{N_COMPARISONS}")):
                            lo, hi = interval(boots[name][meth], lvl)
                            ivals.append({"option_type": ot, "split": sp, "model": name, "metric": "brier_diff_vs_B0",
                                          "method": meth, "level": tag, "point": row["brier_diff_vs_B0"], "lo": round(lo, 5),
                                          "hi": round(hi, 5), "n_boot": N_BOOT, "valid_replicates": int(np.isfinite(boots[name][meth]).sum()),
                                          "eval_rows": len(te), "eval_dates": te["date"].nunique(), "train_dates": tr["date"].nunique()})
                            if sp == "validation" and meth == "refit" and "bonferroni" in tag:
                                row["selected_on_validation"] = bool(hi < 0)
                comp.append(row)
                c = calibration(y, p.to_numpy())
                if c is not None and name != "B0_training_prior":
                    calib.append(c.assign(option_type=ot, split=sp, model=name))
            for name, why in skipped.items():
                comp.append({**base, "model": name, "eval_rows": len(te), "eval_dates": te["date"].nunique(), "note": why})
    cal = pd.concat(calib, ignore_index=True) if calib else pd.DataFrame(
        columns=["bin", "n", "mean_pred", "obs_rate", "option_type", "split", "model"])
    return pd.DataFrame(comp), pd.DataFrame(ivals), cal, pd.DataFrame(bucket_rows)


def run(data_dir="/content", out_dir=None, verbose=True):
    data_dir, out_dir = Path(data_dir), Path(out_dir or data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    checks = A.Checks()
    src, _ = A.load_source(str(data_dir), checks)
    n_gate_files = check_gate(data_dir, n_source_rows=len(src))
    checks.add("gate", "V4.7 audit gate (clean, current hashes, row count)", 0, f"{n_gate_files} input files hash-verified")

    r, cal, n_und = M.build_features(src)                      # lags + target on the FULL series, before any filter
    checks.add("v47", "dates with more than one underlying value", n_und)
    rowlevel = pd.read_csv(data_dir / f"{A.OUT_PREFIX}Row_Level_Comparison.csv")
    r = M.apply_rules(r, rowlevel)                             # eligibility AFTER lags
    r, val_start, test_start = M.assign_splits(r, cal)
    M.leakage_checks(r, val_start, test_start, checks)
    r = add_v48_features(r)

    el = r[r["eligible"] & r["split"].isin(["train", "validation", "test"])].copy()
    # V4.8 assertions (fail loudly)
    bad = []
    if (el.loc[el["split"] == "train", "target_date"] >= val_start).any(): bad.append("train label reaches validation")
    if (el.loc[el["split"] == "validation", "target_date"] >= test_start).any(): bad.append("validation label reaches test")
    if (el["target_date"] <= el["date"]).any(): bad.append("target date not after feature date")
    if el[M.FEATURES + ["lr1", "ur1", "sm", "target_next_up"]].isna().any().any(): bad.append("NaN in eligible rows")
    if el["bucket"].eq(MISSING).any(): bad.append("MISSING bucket among eligible rows")
    if bad:
        raise AssertionError("; ".join(bad))
    checks.add("v48", "critical assertions (split labels, target order, NaN, buckets)", 0)

    v47 = data_dir / f"{M.MODEL_PREFIX}Dataset.csv"
    if v47.exists():
        old = pd.read_csv(v47)
        same = set(map(tuple, old.loc[old["eligible"], ["option_type", "strike", "date"]].astype(str).to_numpy())) == \
            set(map(tuple, r.loc[r["eligible"], ["option_type", "strike", "date"]].assign(
                date=lambda x: x["date"].dt.strftime("%Y-%m-%d")).astype(str).to_numpy()))
        checks.add("v47", "eligible row set identical to V4_7_Model_Dataset.csv", 0 if same else 1)
    else:
        checks.add("v47", "eligible row set identical to V4_7_Model_Dataset.csv (file not present; not compared)", 0)

    comp, ivals, calib, brate = run_experiment(el)
    checks.add("v48", "paired evaluation: identical rows for all models", 0, "asserted inside predict_all")

    # ---- reports
    w = A.write_csv
    cols = ["source_file", "symbol", "expiry", "option_type", "strike", "date", "target_date", "cal_idx", "settle_t", "settle_next",
            "lr1", "ur1", "sm", "bucket", "target_next_up", "target_tie", "extreme_return_flag", "official_status", "eligible",
            "exclusion_reason", "split", "split_if_not_purged"]
    ds = r[cols].copy()
    for c in ("expiry", "date", "target_date"):
        ds[c] = ds[c].dt.strftime("%Y-%m-%d")
    w(ds.sort_values(["date", "option_type", "strike"]).round(8).reset_index(drop=True), out_dir / f"{PREFIX}Dataset.csv")
    rules = [x[0] for x in M.RULES]
    elig = pd.DataFrame({"rule": rules, "condition": [x[1] for x in M.RULES],
                         "rows_failing": [int((~r[k]).sum()) for k in rules],
                         "rows_failing_first": [int(r["exclusion_reason"].str.split("|").str[0].eq(k).sum()) for k in rules]})
    elig.loc[len(elig)] = ["ELIGIBLE", "all rules pass", int(r["eligible"].sum()), 0]
    elig.loc[len(elig)] = ["TOTAL", "raw source rows kept in dataset (none deleted)", len(r), 0]
    elig.loc[len(elig)] = ["TIES", "eligible rows where settle[t+1]==settle[t] (counted as not-up)", int(r.loc[r["eligible"], "target_tie"].sum()), 0]
    w(elig, out_dir / f"{PREFIX}Dataset_Eligibility.csv")

    integ = []
    for (sp, ot), g in r[r["split"].isin(["train", "validation", "test", "purged"])].groupby(["split", "option_type"]):
        e = g[g["eligible"]]
        integ.append({"split": sp, "option_type": ot, "rows": len(g), "eligible_rows": len(e),
                      "first_feature_date": g["date"].min().date(), "last_feature_date": g["date"].max().date(),
                      "max_target_date_eligible": e["target_date"].max().date() if len(e) else "",
                      "validation_start": pd.Timestamp(val_start).date(), "test_start": pd.Timestamp(test_start).date(),
                      "train_labels_reaching_validation": int(((e["target_date"] >= val_start).sum()) if sp == "train" else 0),
                      "validation_labels_reaching_test": int(((e["target_date"] >= test_start).sum()) if sp == "validation" else 0)})
    w(pd.DataFrame(integ), out_dir / f"{PREFIX}Split_Integrity.csv")
    w(pd.DataFrame(FEATURE_DEFS, columns=["feature", "definition", "available_at", "train_only_transform", "missing_unseen_policy"]),
      out_dir / f"{PREFIX}Feature_Definitions.csv")
    w(comp, out_dir / f"{PREFIX}Baseline_Comparison.csv")
    w(ivals, out_dir / f"{PREFIX}Uncertainty_Intervals.csv")
    w(calib.round(5), out_dir / f"{PREFIX}Calibration.csv")
    w(brate, out_dir / f"{PREFIX}Moneyness_Bucket_Rates.csv")
    w(checks.frame(), out_dir / f"{PREFIX}Validation_Checks.csv")
    names = sorted(p.name for p in out_dir.glob(PREFIX + "*.csv"))
    zip_path = out_dir / f"{PREFIX}Reports.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            zi = zipfile.ZipInfo(n, date_time=(2026, 1, 1, 0, 0, 0))   # fixed timestamp -> byte-identical reruns
            z.writestr(zi, (out_dir / n).read_bytes())

    if verbose:
        print("=" * 76, "\nV4.8 CONTROLLED FEATURE RESEARCH (paper research; no skill or profit claim)\n" + "=" * 76)
        print(f"Gate OK ({n_gate_files} input files hash-verified). Rows {len(r)} | eligible {int(r['eligible'].sum())} | purged {int((r['split']=='purged').sum())}")
        print(f"Validation starts {pd.Timestamp(val_start).date()} | test starts {pd.Timestamp(test_start).date()} (test = exploratory)")
        show = ["option_type", "split", "model", "eval_rows", "eval_dates", "brier", "brier_diff_vs_B0", "log_loss", "auc", "selected_on_validation"]
        print(comp[[c for c in show if c in comp.columns]].to_string(index=False))
        sel = comp[(comp["split"] == "validation") & (comp["selected_on_validation"] == True)]
        print("\nSelected on validation (Bonferroni refit interval below 0):", "NONE" if sel.empty else sel[["option_type", "model"]].values.tolist())
        print("Intervals: see Uncertainty_Intervals.csv. Results are not confirmatory evidence of skill or profitability.")
        print("Saved", len(names) + 1, "files incl.", zip_path.name, "to", out_dir)
    return {"comparison": comp, "intervals": ivals, "calibration": calib, "dataset": ds, "checks": checks.frame()}


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    run(a[0] if a else "/content", a[1] if len(a) > 1 else None)
