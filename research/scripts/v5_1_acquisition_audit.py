"""ML Options Paper Trading V5.1 - acquisition, structural validation and contamination audit.

Reads only: the V5.1 raw ZIPs (official daily F&O bhavcopy, UDiFF), the V5.0 download log and the earlier project artifacts.
It never computes a return, a direction, a label or any model output for the new window: every number it reports is a count,
a date or a hash. Raw files are not modified; the normalized dataset is written to a separate file.

Usage: python v5_1_acquisition_audit.py PROJECT_DIR [OUT_DIR]
"""
import glob
import hashlib
import re
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

PREFIX = "V5_1_"
BOUNDARY = pd.Timestamp("2026-06-29")          # last trade date used by any earlier experiment (established by the contamination audit)
WINDOW_START, WINDOW_END = pd.Timestamp("2026-06-30"), pd.Timestamp("2026-09-29")
STUDY_EXPIRY = pd.Timestamp("2026-09-29")
NEEDED = ["TradDt", "FinInstrmTp", "TckrSymb", "XpryDt", "StrkPric", "OptnTp", "OpnPric", "HghPric", "LwPric", "ClsPric", "LastPric",
          "PrvsClsgPric", "UndrlygPric", "SttlmPric", "OpnIntrst", "ChngInOpnIntrst", "TtlTradgVol", "TtlNbOfTxsExctd"]
RENAME = {"TradDt": "date", "XpryDt": "expiry", "StrkPric": "strike", "OptnTp": "option_type", "OpnPric": "open", "HghPric": "high",
          "LwPric": "low", "ClsPric": "close", "LastPric": "last", "PrvsClsgPric": "prev_close", "UndrlygPric": "underlying",
          "SttlmPric": "settle", "OpnIntrst": "open_interest", "ChngInOpnIntrst": "change_in_oi", "TtlTradgVol": "volume",
          "TtlNbOfTxsExctd": "transactions"}
KEY = ["date", "expiry", "option_type", "strike"]
BHAV_RE = re.compile(r"^BhavCopy_NSE_FO_0_0_0_(\d{8})_F_0000\.csv\.zip$")


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def load_zip(path):
    """NIFTY index-option rows of one official daily file, plus the file-level facts needed for the manifest."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        raw = pd.read_csv(z.open(names[0]), dtype=str)
    facts = {"members": len(names), "all_rows": len(raw), "columns": len(raw.columns),
             "missing_columns": ";".join(c for c in NEEDED if c not in raw.columns)}
    if facts["missing_columns"]:
        return pd.DataFrame(), facts
    d = raw[(raw["TckrSymb"].str.strip() == "NIFTY") & (raw["FinInstrmTp"].str.strip() == "IDO")][NEEDED].rename(columns=RENAME)
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    d["expiry"] = pd.to_datetime(d["expiry"], errors="coerce")
    d["option_type"] = d["option_type"].str.strip()
    for c in ["strike", "open", "high", "low", "close", "last", "prev_close", "underlying", "settle", "open_interest", "change_in_oi", "volume", "transactions"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d["source_file"] = Path(path).name
    return d.reset_index(drop=True), facts


def contamination_audit(project):
    """Latest TRADE date found in every earlier dataset / report (expiry dates are not trade dates and are not counted)."""
    p = Path(project)
    rows = []

    def add(name, kind, dates):
        dates = pd.to_datetime(pd.Series(dates), errors="coerce", format="mixed").dropna()
        rows.append({"artifact": name, "kind": kind, "rows_with_dates": len(dates), "distinct_trade_dates": int(dates.nunique()),
                     "first_trade_date": dates.min().date() if len(dates) else "", "last_trade_date": dates.max().date() if len(dates) else "",
                     "uses_dates_in_new_window": bool((dates >= WINDOW_START).any())})

    for f in sorted(glob.glob(str(p / "OPTIDX_NIFTY_*.csv"))):
        df = pd.read_csv(f)
        add(Path(f).name, "contract-wise export", df[[c for c in df.columns if c.strip().lower() == "date"][0]])
    for f in sorted(glob.glob(str(p / "BhavCopy_NSE_FO_*.csv"))) + sorted(glob.glob(str(p / "V5_0_Acquired_Raw" / "*.zip"))):
        m = re.search(r"_(\d{8})_F_", f)
        add(Path(f).name, "official bhavcopy (file date)", [pd.Timestamp(m.group(1))])
    for f, col in [("NIFTY_Options_V4_1_Audited.csv", "date"), ("NIFTY_Options_V4_1_Settlement_Model_Dataset.csv", "date"),
                   ("NIFTY_Options_V4_Combined_Clean.csv", "date"), ("NIFTY_Options_V4_Model_Dataset.csv", "date"),
                   ("V4_7_Model_Dataset.csv", "date"), ("V4_8_Dataset.csv", "date"), ("V5_0_Study_Dataset.csv", "date"),
                   ("V5_0_Normalized_Study_Universe.csv", "date"), ("V5_0_Normalized_Bhavcopy_NIFTY_Options.csv", "date"),
                   ("NIFTY_Options_NSE_Daily_Combined.csv", "date")]:
        fp = p / f
        if fp.exists():
            df = pd.read_csv(fp)
            c = [x for x in df.columns if x.lower() in (col, "trade_date", "tradedate")]
            if c:
                add(f, "model/study dataset", df[c[0]])
    for f in sorted(glob.glob(str(p / "V4_9_*.csv"))) + sorted(glob.glob(str(p / "V4_8_*.csv"))) + sorted(glob.glob(str(p / "V5_0_*.csv"))):
        name = Path(f).name
        if name in {r["artifact"] for r in rows} or name.startswith("V5_0_Normalized"):
            continue
        try:
            df = pd.read_csv(f)
        except Exception:
            continue
        c = [x for x in df.columns if x.lower() in ("date", "trade_date", "first_date", "last_date", "feature_date", "target_date")]
        if c:
            vals = pd.concat([df[x] for x in c])
            add(name, "report", vals)
    return pd.DataFrame(rows)


def acquisition_manifest(raw_dir, log_path):
    log = pd.read_csv(log_path)
    out = []
    for r in log.itertuples():
        f = Path(raw_dir) / r.file
        rec = {"trade_date": r.trade_date, "file": r.file, "source_url": r.url, "retrieved_utc": r.retrieved_utc, "http_status": r.http_status,
               "retrieval_status": r.status, "bytes_logged": r.bytes, "sha256_logged": r.sha256, "on_disk": f.exists()}
        if f.exists():
            rec["bytes_disk"] = f.stat().st_size
            rec["sha256_disk"] = sha256(f)
            rec["hash_matches_log"] = rec["sha256_disk"] == r.sha256 and rec["bytes_disk"] == r.bytes
            d, facts = load_zip(f)
            rec.update({"schema_columns": facts["columns"], "missing_required_columns": facts["missing_columns"], "all_rows": facts["all_rows"],
                        "nifty_option_rows": len(d), "trade_dates_in_file": int(d["date"].nunique()) if len(d) else 0,
                        "first_trade_date": d["date"].min().date() if len(d) else "", "last_trade_date": d["date"].max().date() if len(d) else ""})
        out.append(rec)
    return pd.DataFrame(out)


def load_all(raw_dir):
    parts = [load_zip(f)[0] for f in sorted(glob.glob(str(Path(raw_dir) / "BhavCopy_NSE_FO_*.csv.zip")))]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def integrity_checks(df, manifest, grid, expected_dates):
    rows = []

    def add(scope, check, issues, detail="", note=False):
        rows.append({"scope": scope, "check": check, "issues": int(issues), "result": "PASS" if issues == 0 else ("NOTE" if note else "REVIEW"), "detail": detail})

    got = manifest[manifest["on_disk"]]
    add("files", "download hash and size equal the log", int((~got["hash_matches_log"].astype(bool)).sum()))
    add("files", "required columns present", int((got["missing_required_columns"].fillna("") != "").sum()))
    fd = got["file"].str.extract(r"_(\d{8})_F_")[0]
    add("files", "file name date equals trade date inside the file", int((pd.to_datetime(fd, format="%Y%m%d") != pd.to_datetime(got["first_trade_date"])).sum()))
    add("files", "one trade date per file", int((got["trade_dates_in_file"] != 1).sum()))
    add("files", "file not retrieved (HTTP error)", int((manifest["retrieval_status"] != "downloaded").sum()),
        "; ".join(manifest.loc[manifest["retrieval_status"] != "downloaded", "trade_date"].astype(str)) + " (an NSE trading holiday must be confirmed against NSE's holiday list)", note=True)
    add("rows", "unparseable date, expiry or strike", int(df[["date", "expiry", "strike"]].isna().any(axis=1).sum()))
    add("rows", "option type not CE/PE", int((~df["option_type"].isin(["CE", "PE"])).sum()))
    add("rows", "duplicate contract-date keys (date, expiry, option type, strike)", int(df.duplicated(KEY, keep=False).sum()))
    add("rows", "missing settlement or close", int(df[["settle", "close"]].isna().any(axis=1).sum()))
    add("rows", "negative price, volume or open interest", int((df[["open", "high", "low", "close", "settle", "volume", "open_interest"]] < 0).any(axis=1).sum()))
    add("rows", "high below low", int((df["high"] < df["low"]).sum()))
    add("rows", "trade date outside the declared window", int(((df["date"] < WINDOW_START) | (df["date"] > WINDOW_END)).sum()))
    add("rows", "trade date on a weekend", int((df["date"].dt.dayofweek >= 5).sum()))
    add("rows", "expiry before trade date", int((df["expiry"] < df["date"]).sum()))
    und = df.groupby("date")["underlying"].nunique()
    add("rows", "dates with more than one underlying value", int((und > 1).sum()))
    add("rows", "settlement <= 0 (kept unchanged; excluded by the frozen price floor)", int((df["settle"] <= 0).sum()), f"{int(((df['settle'] <= 0) & (df['close'] > 0)).sum())} of them have a positive close", note=True)
    add("rows", "zero volume (informational)", int((df["volume"] == 0).sum()), f"{int((df['volume'] == 0).sum())} of {len(df)} rows", note=True)
    traded = df[df["volume"] > 0]
    add("rows", "traded rows with zero transactions", int((traded["transactions"] == 0).sum()))
    add("rows", "close differs from settlement on traded rows (informational, kept as separate columns)", int((traded["close"] != traded["settle"]).sum()),
        f"{int((traded['close'] != traded['settle']).sum())} of {len(traded)} traded rows", note=True)
    s = df.sort_values(KEY[1:] + ["date"])
    same = s.groupby(KEY[1:])["settle"].diff().eq(0) & (s["volume"] > 0) & (s.groupby(KEY[1:])["volume"].shift(1) > 0)
    add("rows", "settlement identical to the previous date on two consecutive traded dates (possible stale value; informational)", int(same.sum()), note=True)
    present = set(df["date"].dt.date)
    missing = [d.date() for d in expected_dates if d.date() not in present]
    add("calendar", "weekday in the window with no file (see retrieval status)", len(missing), ", ".join(map(str, missing)), note=True)
    study = df[df["expiry"] == STUDY_EXPIRY]
    add("study grid", "study-expiry rows outside the frozen V5.0 strike grid", int((~study["strike"].isin(grid)).sum()), "informational: extra strikes are ignored by the frozen protocol", note=True)
    cov = study[study["strike"].isin(grid)].groupby(["date", "option_type"]).size().unstack(fill_value=0)
    full = len(grid)
    add("study grid", "date/option-type pairs missing part of the frozen strike grid", int(((cov < full).sum().sum())), f"grid size {full}", note=True)
    return pd.DataFrame(rows)


def run(project, out_dir=None, verbose=True):
    project = Path(project)
    out = Path(out_dir) if out_dir else project
    out.mkdir(parents=True, exist_ok=True)
    raw = project / "V5_1_Acquired_Raw"
    manifest = acquisition_manifest(raw, project / "V5_1_Download_Log.csv")
    df = load_all(raw)
    grid = sorted(pd.read_csv(project / "V5_0_Normalized_Study_Universe.csv", usecols=["strike"])["strike"].unique())
    cal = pd.bdate_range(WINDOW_START, WINDOW_END)
    checks = integrity_checks(df, manifest, grid, cal)
    contam = contamination_audit(project)
    study = df[(df["expiry"] == STUDY_EXPIRY) & df["strike"].isin(grid)]
    cov = (df.groupby(["date", "option_type"]).size().unstack(fill_value=0).add_prefix("all_expiries_rows_").join(
        study.groupby(["date", "option_type"]).size().unstack(fill_value=0).add_prefix("study_expiry_grid_rows_"), how="outer").fillna(0).astype(int).reset_index())
    cov["frozen_grid_size"] = len(grid)
    cov["traded_study_rows_CE"] = cov["date"].map(study[(study.option_type == "CE") & (study.volume > 0)].groupby("date").size()).fillna(0).astype(int)
    cov["traded_study_rows_PE"] = cov["date"].map(study[(study.option_type == "PE") & (study.volume > 0)].groupby("date").size()).fillna(0).astype(int)
    by_type = (study.groupby("option_type").agg(rows=("strike", "size"), distinct_dates=("date", "nunique"), contracts=("strike", "nunique"),
                                              first_date=("date", "min"), last_date=("date", "max")).reset_index())
    norm = df.drop(columns=["source_file"]).assign(origin="official_bhavcopy_udiff").sort_values(KEY)
    files = {"Source_Acquisition_Manifest": manifest, "Raw_Integrity_Report": checks, "Contamination_Audit": contam,
             "Coverage_By_Date": cov, "Study_Window_Coverage": by_type, "Normalized_Bhavcopy_NIFTY_Options": norm}
    for k, v in files.items():
        v.to_csv(out / f"{PREFIX}{k}.csv", index=False)
    if verbose:
        print(f"files: {len(manifest)} logged, {int((manifest.retrieval_status == 'downloaded').sum())} downloaded; NIFTY option rows {len(df)}, dates {df['date'].nunique()}")
        print(checks[checks.result != "PASS"][["scope", "check", "issues", "result"]].to_string(index=False))
        print(by_type.to_string(index=False))
        print("latest trade date in any earlier artifact:", pd.to_datetime(contam["last_trade_date"]).max().date(),
              "| artifacts using dates >= window start:", int(contam["uses_dates_in_new_window"].sum()))
    return files


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    run(a[0] if a else ".", a[1] if len(a) > 1 else None)
