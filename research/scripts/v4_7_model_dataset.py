"""ML Options Paper Trading V4.7 - separate modelling dataset + baseline comparison (paper research only).

Runs AFTER v4_7_source_audit.py. Gate: the audit's Validation_Checks must exist with no REVIEW rows (override: force=True).
Read-only on source files; writes V4_7_Model_*.csv. No rows are deleted from the dataset: ineligible and purged rows are kept
with an explicit reason. Nothing here claims predictive skill or profitability.

Label: target_next_up = settle(t+1) > settle(t) for the SAME contract, only when t+1 is the next trading day in the source calendar.
Features use data up to t-1 only (same four features as V4.4-V4.6): settle_return_lag1/lag2, underlying_return_lag1, moneyness_lag1.
Splits: chronological by calendar date, TRAIN_FRAC/VAL_FRAC/rest. Purge: a row is dropped from its split when its target date
(t+1) falls on or after the next split's first date, so no training label is decided by a later split's prices.

Usage: python v4_7_model_dataset.py [DATA_DIR] [OUT_DIR]    |    python v4_7_model_dataset.py --selftest
"""
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I / Colab: find the audit module next to this file
import v4_7_source_audit as A

MODEL_PREFIX = "V4_7_Model_"
PRICE_FLOOR = 1.0            # rupees; applies to settle at t, t-1, t-2 only (never to t+1, which would select on the outcome)
TRAIN_FRAC, VAL_FRAC = 0.6, 0.2
FEATURES = ["settle_return_lag1", "settle_return_lag2", "underlying_return_lag1", "moneyness_lag1"]
MONEYNESS_BINS = [0, 0.9, 1.1, np.inf]   # fixed in advance, not fitted
SHRINK = 20                  # m-estimate strength toward the training prior for table baselines
BOOT = 2000

RULES = [
    ("R1_features", "All four features exist", "Lags need consecutive trading days in the source calendar; gaps give NaN, never a stale price."),
    ("R2_target", "Next-trading-day settlement exists for the same contract", "Label is a one-day move; a multi-day gap is a different horizon."),
    ("R3_price_floor", f"min(settle t, t-1, t-2) >= {PRICE_FLOOR}", "Tick-size (0.05) noise dominates sub-rupee prices. Uses past/current prices only, so it cannot select on the outcome."),
    ("R4_official_clean", "No official settlement discrepancy / ambiguous identity in the V4.7 audit", "Rows contradicted by NSE or with unclear contract identity are not modelled. Unverified rows are kept and labelled."),
    ("R5_unique_key", "Single row per contract-date", "Duplicate keys make lags ambiguous."),
]


# ----------------------------------------------------------------------------- build
def gate(out_dir, force):
    p = Path(out_dir) / f"{A.OUT_PREFIX}Validation_Checks.csv"
    if not p.exists():
        raise RuntimeError("Run the source audit first: Validation_Checks.csv not found.")
    bad = pd.read_csv(p)
    bad = bad[bad["result"] == "REVIEW"]
    if len(bad) and not force:
        raise RuntimeError(f"Source audit has {len(bad)} unresolved REVIEW checks; fix or pass force=True.\n"
                           + bad[["scope", "check", "issues"]].to_string(index=False))
    return len(bad)


def build_features(r):
    """r: source rows from A.load_source (with src_underlying). Returns dataframe with features, target, calendar index."""
    cal = np.array(sorted(r["date"].unique()))
    cidx = {d: i for i, d in enumerate(cal)}
    r = r.copy()
    r["cal_idx"] = r["date"].map(cidx)
    und = r.groupby("date")["src_underlying"].agg(["first", "nunique"]).reindex(cal)
    ucal = und["first"].to_numpy(dtype=float)
    k = ["symbol", "expiry", "option_type", "strike"]
    r = r.sort_values(k + ["date"]).reset_index(drop=True)
    g = r.groupby(k, sort=False)

    def lag(col, n):  # value n rows back, only if that row is exactly n trading days back
        ok = (r["cal_idx"] - g["cal_idx"].shift(n)) == n
        return g[col].shift(n).where(ok)
    s1, s2, s3 = lag("src_settle", 1), lag("src_settle", 2), lag("src_settle", 3)
    nxt = g["src_settle"].shift(-1).where((g["cal_idx"].shift(-1) - r["cal_idx"]) == 1)
    r["settle_t"], r["settle_t1"], r["settle_t2"] = r["src_settle"], s1, s2
    r["settle_return_lag1"] = s1 / s2 - 1
    r["settle_return_lag2"] = s2 / s3 - 1
    i = r["cal_idx"].to_numpy()
    u1 = np.where(i >= 1, ucal[np.maximum(i - 1, 0)], np.nan)
    u2 = np.where(i >= 2, ucal[np.maximum(i - 2, 0)], np.nan)
    r["underlying_return_lag1"] = u1 / u2 - 1
    r["moneyness_lag1"] = u1 / r["strike"]
    r["settle_next"] = nxt
    r["target_next_up"] = (nxt > r["src_settle"]).astype(float).where(nxt.notna())
    r["target_tie"] = (nxt == r["src_settle"]) & nxt.notna()
    r["target_date"] = pd.Series(cal[np.minimum(i + 1, len(cal) - 1)], index=r.index).where(nxt.notna())
    for c in FEATURES:
        r.loc[~np.isfinite(r[c]), c] = np.nan
    r["extreme_return_flag"] = (r[["settle_return_lag1", "settle_return_lag2"]].abs() > 1).any(axis=1)
    return r, cal, int((und["nunique"] > 1).sum())


def apply_rules(r, rowlevel):
    key = ["date", "option_type", "strike", "expiry"]
    rl = rowlevel[["date", "option_type", "strike", "expiry", "audit_status"]].copy()
    for c in ("date", "expiry"):
        rl[c] = pd.to_datetime(rl[c])
    rl = rl.drop_duplicates(key)
    r = r.merge(rl, on=key, how="left")
    r["official_status"] = r["audit_status"].fillna("not in audit")
    r["R1_features"] = r[FEATURES].notna().all(axis=1)
    r["R2_target"] = r["target_next_up"].notna()
    r["R3_price_floor"] = r[["settle_t", "settle_t1", "settle_t2"]].min(axis=1) >= PRICE_FLOOR
    bad = {A.ST_SETTLE_DIFF, A.ST_AMBIG, "not in audit"}
    r["R4_official_clean"] = ~r["official_status"].isin(bad)
    r["R5_unique_key"] = ~r.duplicated(["symbol", "expiry", "option_type", "strike", "date"], keep=False)
    rules = [x[0] for x in RULES]
    r["eligible"] = r[rules].all(axis=1)
    r["exclusion_reason"] = r[rules].apply(lambda row: "|".join(k for k in rules if not row[k]), axis=1)
    return r


def assign_splits(r, cal):
    n = len(cal)
    val_start, test_start = int(n * TRAIN_FRAC), int(n * (TRAIN_FRAC + VAL_FRAC))
    base = np.where(r["cal_idx"] < val_start, "train", np.where(r["cal_idx"] < test_start, "validation", "test"))
    tgt_idx = r["cal_idx"] + 1
    purged = ((base == "train") & (tgt_idx >= val_start)) | ((base == "validation") & (tgt_idx >= test_start))
    r["split"] = np.where(purged, "purged", base)
    r["split_if_not_purged"] = base
    return r, cal[val_start], cal[test_start]


def leakage_checks(r, val_start, test_start, checks):
    ok = r[r["eligible"] & r["split"].isin(["train", "validation"])]
    checks.add("model", "train rows whose target date >= validation start",
               int(((ok["split"] == "train") & (ok["target_date"] >= val_start)).sum()))
    checks.add("model", "validation rows whose target date >= test start",
               int(((ok["split"] == "validation") & (ok["target_date"] >= test_start)).sum()))
    checks.add("model", "eligible rows with target date not after feature date", int((ok["target_date"] <= ok["date"]).sum()))
    checks.add("model", "duplicate contract-date keys among eligible rows",
               int(r[r["eligible"]].duplicated(["symbol", "expiry", "option_type", "strike", "date"]).sum()))
    checks.add("model", "eligible rows with non-consecutive lag (settle_t2 missing)", int(r.loc[r["eligible"], "settle_t2"].isna().sum()))


# ----------------------------------------------------------------------------- baselines
def auc(y, s):
    y = np.asarray(y)
    n1, n0 = y.sum(), len(y) - y.sum()
    if n1 == 0 or n0 == 0:
        return np.nan
    rk = pd.Series(s).rank().to_numpy()
    return (rk[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def table_prob(train_key, train_y, test_key, prior):
    df = pd.DataFrame({"k": train_key, "y": train_y}).groupby("k")["y"].agg(["sum", "count"])
    p = (df["sum"] + SHRINK * prior) / (df["count"] + SHRINK)
    return pd.Series(test_key).map(p).fillna(prior).to_numpy()


def sign3(x):
    return np.sign(np.asarray(x, dtype=float)).astype(int)


def baseline_probs(tr, te):
    prior = float(tr["target_next_up"].mean())
    y = tr["target_next_up"].to_numpy()
    mn = lambda d: pd.cut(d["moneyness_lag1"], MONEYNESS_BINS, labels=False, right=False).to_numpy()
    return {
        "B0_training_prior": np.full(len(te), np.clip(prior, 0.01, 0.99)),
        "B1_settle_return_lag1_sign": table_prob(sign3(tr["settle_return_lag1"]), y, sign3(te["settle_return_lag1"]), prior),
        "B2_underlying_return_lag1_sign": table_prob(sign3(tr["underlying_return_lag1"]), y, sign3(te["underlying_return_lag1"]), prior),
        "B3_moneyness_bucket": table_prob(mn(tr), y, mn(te), prior),
    }


def baseline_comparison(d):
    rng = np.random.default_rng(0)
    rows = []
    for ot in sorted(d["option_type"].unique()):
        tr = d[(d["split"] == "train") & (d["option_type"] == ot)]
        if tr["target_next_up"].nunique() < 2:
            continue
        for sp in ("validation", "test"):
            te = d[(d["split"] == sp) & (d["option_type"] == ot)].reset_index(drop=True)
            if te.empty:
                rows.append({"option_type": ot, "split": sp, "model": "(no eligible rows)", "rows": 0})
                continue
            y = te["target_next_up"].to_numpy()
            probs = baseline_probs(tr, te)
            dates = te["date"].to_numpy()
            groups = [np.where(dates == u)[0] for u in np.unique(dates)]
            b0 = probs["B0_training_prior"]
            for name, p in probs.items():
                brier = float(np.mean((p - y) ** 2))
                diffs, aucs = [], []
                for _ in range(BOOT):
                    ix = np.concatenate([groups[i] for i in rng.integers(0, len(groups), len(groups))])
                    diffs.append(np.mean((p[ix] - y[ix]) ** 2) - np.mean((b0[ix] - y[ix]) ** 2))
                    aucs.append(auc(y[ix], p[ix]))
                lo, hi = np.percentile(diffs, [2.5, 97.5])
                pc = np.clip(p, 1e-6, 1 - 1e-6)
                rows.append({
                    "option_type": ot, "split": sp, "model": name, "rows": len(te), "dates": len(groups),
                    "train_rows": len(tr), "train_up_rate": round(float(tr["target_next_up"].mean()), 4),
                    "eval_up_rate": round(float(y.mean()), 4), "brier": round(brier, 5),
                    "brier_vs_B0": round(brier - float(np.mean((b0 - y) ** 2)), 5),
                    "brier_diff_ci_lo": round(lo, 5), "brier_diff_ci_hi": round(hi, 5),
                    "log_loss": round(float(-np.mean(y * np.log(pc) + (1 - y) * np.log(1 - pc))), 5),
                    "accuracy_at_0.5": round(float(((p >= 0.5) == (y == 1)).mean()), 4),
                    "auc": round(float(auc(y, p)), 4), "auc_ci_lo": round(float(np.nanpercentile(aucs, 2.5)), 4),
                    "auc_ci_hi": round(float(np.nanpercentile(aucs, 97.5)), 4),
                    "evidence_vs_B0": ("n/a (reference)" if name.startswith("B0") else
                                       "interval excludes 0 (better)" if hi < 0 else
                                       "interval excludes 0 (worse)" if lo > 0 else "no evidence of difference"),
                    "note": "uncorrected for multiple comparisons" + ("; test window overlaps data seen in earlier experiments" if sp == "test" else "")})
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------- orchestration
def run_model_dataset(data_dir="/content", out_dir=None, force=False, verbose=True):
    data_dir, out_dir = str(data_dir), Path(out_dir or data_dir)
    n_review = gate(out_dir, force)
    checks = A.Checks()
    src, _ = A.load_source(data_dir, checks)
    r, cal, n_und_conflict = build_features(src)
    checks.add("model", "dates with more than one underlying value", n_und_conflict)
    rowlevel = pd.read_csv(out_dir / f"{A.OUT_PREFIX}Row_Level_Comparison.csv")
    r = apply_rules(r, rowlevel)
    r, val_start, test_start = assign_splits(r, cal)
    leakage_checks(r, val_start, test_start, checks)
    elig = r[r["eligible"] & r["split"].isin(["train", "validation", "test"])]

    cols = ["source_file", "symbol", "expiry", "option_type", "strike", "date", "target_date", "cal_idx", "settle_t",
            "settle_next"] + FEATURES + ["target_next_up", "target_tie", "extreme_return_flag", "official_status",
                                          "eligible", "exclusion_reason", "split", "split_if_not_purged"]
    ds = r[cols].copy()
    for c in ("expiry", "date", "target_date"):
        ds[c] = ds[c].dt.strftime("%Y-%m-%d")
    ds = ds.sort_values(["date", "option_type", "strike"]).reset_index(drop=True)
    write_csv = A.write_csv
    write_csv(ds, out_dir / f"{MODEL_PREFIX}Dataset.csv")
    write_csv(pd.DataFrame(RULES, columns=["rule", "condition", "rationale"]), out_dir / f"{MODEL_PREFIX}Eligibility_Rules.csv")
    rule_ids = [x[0] for x in RULES]
    elig_sum = pd.DataFrame({"rule": rule_ids,
                             "rows_failing": [int((~r[k]).sum()) for k in rule_ids],
                             "rows_failing_first": [int(r["exclusion_reason"].str.split("|").str[0].eq(k).sum()) for k in rule_ids]})
    elig_sum.loc[len(elig_sum)] = ["ELIGIBLE (all rules)", int(r["eligible"].sum()), 0]
    elig_sum.loc[len(elig_sum)] = ["total rows", len(r), 0]
    write_csv(elig_sum, out_dir / f"{MODEL_PREFIX}Eligibility_Summary.csv")
    sp = (r[r["eligible"] | (r["split"] == "purged")].assign(is_eligible=lambda x: x["eligible"])
          .groupby(["split", "option_type"]).agg(rows=("eligible", "size"), eligible_rows=("eligible", "sum"),
                                                 dates=("date", "nunique"), first_date=("date", "min"), last_date=("date", "max"),
                                                 up_rate=("target_next_up", "mean")).reset_index())
    sp["first_date"], sp["last_date"] = sp["first_date"].dt.strftime("%Y-%m-%d"), sp["last_date"].dt.strftime("%Y-%m-%d")
    write_csv(sp.round(4), out_dir / f"{MODEL_PREFIX}Split_Summary.csv")
    cmp_ = baseline_comparison(elig)
    write_csv(cmp_, out_dir / f"{MODEL_PREFIX}Baseline_Comparison.csv")
    write_csv(checks.frame(), out_dir / f"{MODEL_PREFIX}Validation_Checks.csv")

    if verbose:
        print("=" * 72, "\nV4.7 MODELLING DATASET + BASELINES (exploratory; no skill or profit claim)\n" + "=" * 72)
        print(f"Audit gate: passed ({n_review} unresolved review checks)")
        print(f"Calendar dates: {len(cal)} | validation starts {pd.Timestamp(val_start).date()} | test starts {pd.Timestamp(test_start).date()}")
        print(f"Rows: {len(r)} | eligible: {int(r['eligible'].sum())} | purged at boundaries: {int((r['split'] == 'purged').sum())}")
        print(elig_sum.to_string(index=False))
        print(sp.to_string(index=False))
        show = ["option_type", "split", "model", "rows", "dates", "brier", "brier_vs_B0", "brier_diff_ci_lo", "brier_diff_ci_hi",
                "auc", "evidence_vs_B0"]
        print(cmp_[[c for c in show if c in cmp_.columns]].to_string(index=False))
        bad = checks.frame()
        bad = bad[bad["result"] == "REVIEW"]
        print("\nModel validation items needing review:", "none" if bad.empty else "")
        if len(bad):
            print(bad[["scope", "check", "issues"]].to_string(index=False))
        print("\nReports saved to", out_dir)
    return {"dataset": ds, "comparison": cmp_, "checks": checks.frame(), "split_summary": sp, "eligibility": elig_sum}


# ----------------------------------------------------------------------------- self-test
def run_selftest():
    tmp = Path(tempfile.mkdtemp(prefix="v47model_"))
    rng = np.random.default_rng(1)
    dates = pd.bdate_range("2026-01-05", periods=30)
    rows = []
    for ot in ("CE", "PE"):
        for strike in (100, 200):
            px = 50.0
            for i, d in enumerate(dates):
                px = max(px * (1 + rng.normal(0, 0.05)), 2.0)
                if ot == "CE" and strike == 100 and i == 10:   # missing observation -> lag/target gaps
                    continue
                rows.append({"Symbol": "NIFTY", "Date": d.strftime("%d-%b-%Y"), "Expiry": "29-Sep-2026", "Option type": ot,
                             "Strike Price": strike, "Close": round(px, 2), "Settle Price": round(px, 2),
                             "Underlying Value": 20000 + 10 * i})
    pd.DataFrame(rows).to_csv(tmp / "OPTIDX_NIFTY_CE_01-Jan-2026_TO_31-Mar-2026.csv", index=False)
    A.run_audit(tmp, verbose=False)
    res = run_model_dataset(tmp, verbose=False)
    ds = res["dataset"]
    assert (res["checks"]["issues"] == 0).all(), res["checks"]
    c = ds[(ds.option_type == "CE") & (ds.strike == 100)].set_index("date")
    d10, d9, d11, d12 = (dates[i].strftime("%Y-%m-%d") for i in (10, 9, 11, 12))
    assert pd.isna(c.loc[d9, "target_next_up"]), "row before a gap must have no target"
    assert pd.isna(c.loc[d11, "settle_return_lag1"]) and pd.isna(c.loc[d12, "settle_return_lag1"]), "lags must not span a gap"
    assert set(ds.loc[ds.split == "purged", "split_if_not_purged"]) == {"train", "validation"}
    e = ds[ds.eligible & ds.split.isin(["train", "validation"])]
    vs = ds.loc[ds.split == "validation", "date"].min()
    assert (e.loc[e.split == "train", "target_date"] < vs).all()
    h1 = {p.name: A.sha256(p) for p in tmp.glob(MODEL_PREFIX + "*.csv")}
    run_model_dataset(tmp, verbose=False)
    assert h1 == {p.name: A.sha256(p) for p in tmp.glob(MODEL_PREFIX + "*.csv")} and len(h1) == 6, "rerun changed outputs"
    print("MODEL SELFTEST PASSED: gap handling, purge, no cross-split labels, eligibility rules, idempotent rerun")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        run_selftest()
    else:
        a = [x for x in sys.argv[1:] if not x.startswith("--")]
        run_model_dataset(a[0] if a else "/content", a[1] if len(a) > 1 else None)
