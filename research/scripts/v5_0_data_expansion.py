"""ML Options Paper Trading V5.0 - data expansion: raw coverage audit, source acquisition manifest, schema/integrity checks and a
separate normalized dataset (paper research only).

Read-only on every raw file (hashes are compared before and after). Nothing is fabricated or interpolated: a date with no
official record stays absent. Close and settlement prices are carried as separate columns and are never aliased.

Inputs : OPTIDX_NIFTY_*.csv (contract-wise exports) and BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv[.zip] (official UDiFF F&O bhavcopy)
Outputs: V5_0_*.csv (see run()).   Usage: python v5_0_data_expansion.py [DATA_DIR] [OUT_DIR]
"""
import glob
import re
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import v4_7_source_audit as A

PREFIX = "V5_0_"
STUDY_EXPIRY = pd.Timestamp("2026-09-29")
TOL = 0.005
CW_RE = re.compile(r"^OPTIDX_NIFTY_(CE|PE)_(\d{2}-[A-Za-z]{3}-\d{4})_TO_(\d{2}-[A-Za-z]{3}-\d{4})\.csv$")
BHAV_RE = re.compile(r"^BhavCopy_NSE_FO_0_0_0_(\d{8})_F_0000\.csv(\.zip)?$")
NORM_COLS = ["date", "symbol", "expiry", "option_type", "strike", "open", "high", "low", "close", "last", "settle", "prev_close",
             "underlying", "volume", "transactions", "turnover_lakhs", "traded_value_rs", "open_interest", "change_in_oi", "origin", "source_file"]
BHAV_NEED = ["TradDt", "FinInstrmTp", "TckrSymb", "XpryDt", "StrkPric", "OptnTp", "OpnPric", "HghPric", "LwPric", "ClsPric", "LastPric",
             "PrvsClsgPric", "UndrlygPric", "SttlmPric", "OpnIntrst", "ChngInOpnIntrst", "TtlTradgVol", "TtlTrfVal", "TtlNbOfTxsExctd"]
CW_NEED = ["Symbol", "Date", "Expiry", "Option type", "Strike Price", "Open", "High", "Low", "Close", "LTP", "Settle Price",
           "No. of contracts", "Open Int", "Change in OI", "Underlying Value"]
KNOWN_SOURCES = [
    ("NSE website > Reports > Derivatives (daily F&O bhavcopy / UDiFF common bhavcopy)", "https://www.nseindia.com/all-reports-derivatives",
     "Identified via web search; page could not be loaded by the author of this pipeline. Confirm file availability, format and NSE terms of use yourself."),
    ("NSE website > Historical Data > Derivatives (contract-wise export; origin of the existing OPTIDX_NIFTY_* files by filename pattern)", "",
     "URL not verified; use the same screen you used for the existing files (index options, NIFTY, CE or PE, <=3-month window)."),
]


# ----------------------------------------------------------------------------- loaders (normalized, long format)
def _num(s):
    return A.to_num(s)


def load_contract_wise(data_dir):
    rows, inv = [], []
    for f in sorted(glob.glob(str(Path(data_dir) / "OPTIDX_NIFTY_*.csv"))):
        p = Path(f)
        if " (" in p.name:          # browser duplicate "name (1).csv"
            continue
        d = pd.read_csv(p, dtype=str, encoding="utf-8")
        d.columns = [c.strip() for c in d.columns]
        miss = [c for c in CW_NEED if c not in d.columns]
        to = next((c for c in d.columns if c.startswith("Turnover")), None)
        inv.append({"file": p.name, "kind": "contract_wise_csv", "missing_columns": ",".join(miss), "rows_raw": len(d)})
        if miss:
            continue
        n = pd.DataFrame({
            "date": A.parse_dates(d["Date"])[0], "symbol": d["Symbol"].str.strip().str.upper(), "expiry": A.parse_dates(d["Expiry"])[0],
            "option_type": d["Option type"].str.strip().str.upper(), "strike": _num(d["Strike Price"]).round(2),
            "open": _num(d["Open"]), "high": _num(d["High"]), "low": _num(d["Low"]), "close": _num(d["Close"]), "last": _num(d["LTP"]),
            "settle": _num(d["Settle Price"]), "prev_close": np.nan, "underlying": _num(d["Underlying Value"]),
            "volume": _num(d["No. of contracts"]), "transactions": np.nan, "turnover_lakhs": _num(d[to]) if to else np.nan,
            "traded_value_rs": np.nan, "open_interest": _num(d["Open Int"]), "change_in_oi": _num(d["Change in OI"]),
            "origin": "contract_wise_csv", "source_file": p.name})
        rows.append(n)
    return (pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=NORM_COLS)), pd.DataFrame(inv)


def load_bhavcopy(data_dir):
    """All NIFTY index-option rows (every expiry/strike) from local official bhavcopy files, normalized."""
    rows, inv = [], []
    for f in sorted(set(glob.glob(str(Path(data_dir) / "BhavCopy_NSE_FO_*.csv*")))):
        p = Path(f)
        m = BHAV_RE.match(p.name)
        d = pd.read_csv(p, dtype=str, encoding="utf-8")
        d.columns = [c.strip() for c in d.columns]
        miss = [c for c in BHAV_NEED if c not in d.columns]
        row = {"file": p.name, "kind": "official_bhavcopy_udiff", "missing_columns": ",".join(miss), "rows_raw": len(d),
               "filename_date": m.group(1) if m else ""}
        if miss:
            inv.append({**row, "nifty_option_rows": 0})
            continue
        d = d[(d["TckrSymb"].str.strip() == "NIFTY") & (d["FinInstrmTp"].str.strip() == "IDO")]
        n = pd.DataFrame({
            "date": A.parse_dates(d["TradDt"])[0], "symbol": "NIFTY", "expiry": A.parse_dates(d["XpryDt"])[0],
            "option_type": d["OptnTp"].str.strip().str.upper(), "strike": _num(d["StrkPric"]).round(2),
            "open": _num(d["OpnPric"]), "high": _num(d["HghPric"]), "low": _num(d["LwPric"]), "close": _num(d["ClsPric"]),
            "last": _num(d["LastPric"]), "settle": _num(d["SttlmPric"]), "prev_close": _num(d["PrvsClsgPric"]),
            "underlying": _num(d["UndrlygPric"]), "volume": _num(d["TtlTradgVol"]), "transactions": _num(d["TtlNbOfTxsExctd"]),
            "turnover_lakhs": np.nan, "traded_value_rs": _num(d["TtlTrfVal"]), "open_interest": _num(d["OpnIntrst"]),
            "change_in_oi": _num(d["ChngInOpnIntrst"]), "origin": "official_bhavcopy_udiff", "source_file": p.name})
        inv.append({**row, "nifty_option_rows": len(n)})
        rows.append(n)
    return (pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=NORM_COLS)), pd.DataFrame(inv)


# ----------------------------------------------------------------------------- study universe
KEY = ["date", "option_type", "strike", "expiry"]


def build_study_universe(cw, bh):
    """One row per contract-date. Universe = STUDY_EXPIRY and the strike grid present in the contract-wise files (per type).
    Preference: contract-wise row if present, else official bhavcopy row. Disagreements are flagged, never overwritten."""
    grid = cw.groupby("option_type")["strike"].apply(set).to_dict()
    in_u = lambda df: df[(df["expiry"] == STUDY_EXPIRY) & df.apply(lambda r: r["strike"] in grid.get(r["option_type"], set()), axis=1)]
    cwu, bhu = in_u(cw).copy(), in_u(bh).copy()
    bk = bhu.set_index(KEY)
    ck = cwu.set_index(KEY)
    both = ck.index.intersection(bk.index)
    out = pd.concat([cwu, bhu[~bhu.set_index(KEY).index.isin(ck.index)]], ignore_index=True)
    out["also_in_bhavcopy"] = out.set_index(KEY).index.isin(bk.index)
    conf_s = pd.Series(False, index=out.index)
    conf_c = pd.Series(False, index=out.index)
    if len(both):
        ds = (ck.loc[both, "settle"] - bk.loc[both, "settle"]).abs() > TOL
        dc = (ck.loc[both, "close"] - bk.loc[both, "close"]).abs() > TOL
        ix = pd.MultiIndex.from_frame(out[KEY])
        conf_s = pd.Series(ix.isin(ds[ds].index), index=out.index)
        conf_c = pd.Series(ix.isin(dc[dc].index), index=out.index)
    out["settle_conflict"], out["close_conflict"] = conf_s, conf_c
    return out.sort_values(KEY).reset_index(drop=True)


# ----------------------------------------------------------------------------- coverage + manifest
def trading_calendar(cw, bh):
    return sorted(set(cw["date"].dropna()) | set(bh["date"].dropna()))


def availability(cal, cw, bh):
    rows = []
    for d in cal:
        r = {"date": d.date().isoformat()}
        for ot in ("CE", "PE"):
            r[f"{ot}_contract_wise_rows"] = int(((cw["date"] == d) & (cw["option_type"] == ot)).sum())
            r[f"{ot}_bhavcopy_study_rows"] = int(((bh["date"] == d) & (bh["option_type"] == ot) & (bh["expiry"] == STUDY_EXPIRY)).sum())
            r[f"{ot}_available"] = r[f"{ot}_contract_wise_rows"] > 0 or r[f"{ot}_bhavcopy_study_rows"] > 0
        r["bhavcopy_file_present"] = bool((bh["date"] == d).any())
        rows.append(r)
    return pd.DataFrame(rows)


def gap_ranges(av, ot):
    out, start, prev, n = [], None, None, 0
    for r in av.itertuples():
        missing = not getattr(r, f"{ot}_available")
        if missing and start is None:
            start, n = r.date, 0
        if missing:
            n += 1
            prev = r.date
        if not missing and start is not None:
            out.append({"option_type": ot, "first_missing_date": start, "last_missing_date": prev, "missing_trading_dates": n})
            start = None
    if start is not None:
        out.append({"option_type": ot, "first_missing_date": start, "last_missing_date": prev, "missing_trading_dates": n})
    return pd.DataFrame(out, columns=["option_type", "first_missing_date", "last_missing_date", "missing_trading_dates"])


def raw_file_audit(cw, bh, cw_inv, bh_inv, data_dir):
    rows = []
    for f, g in cw.groupby("source_file"):
        m = CW_RE.match(f)
        w0, w1 = (pd.to_datetime(m.group(2), format="%d-%b-%Y"), pd.to_datetime(m.group(3), format="%d-%b-%Y")) if m else (pd.NaT, pd.NaT)
        rows.append({"file": f, "kind": "contract_wise_csv", "option_type": g["option_type"].iloc[0], "declared_window_start": w0.date() if pd.notna(w0) else "",
                     "declared_window_end": w1.date() if pd.notna(w1) else "", "first_date": g["date"].min().date(), "last_date": g["date"].max().date(),
                     "rows": len(g), "trading_dates": g["date"].nunique(), "strikes": g["strike"].nunique(), "expiries": g["expiry"].nunique(),
                     "bytes": (Path(data_dir) / f).stat().st_size, "sha256": A.sha256(Path(data_dir) / f)})
    for f, g in bh.groupby("source_file"):
        rows.append({"file": f, "kind": "official_bhavcopy_udiff", "option_type": "CE+PE", "declared_window_start": g["date"].min().date(),
                     "declared_window_end": g["date"].max().date(), "first_date": g["date"].min().date(), "last_date": g["date"].max().date(),
                     "rows": len(g), "trading_dates": g["date"].nunique(), "strikes": g["strike"].nunique(), "expiries": g["expiry"].nunique(),
                     "bytes": (Path(data_dir) / f).stat().st_size, "sha256": A.sha256(Path(data_dir) / f)})
    return pd.DataFrame(rows)


def acquisition_manifest(cw, bh, av, data_dir, last_cw_end):
    """Everything still needed, ranked; nothing is claimed as acquired unless the file is present locally."""
    rows = []
    add = lambda **k: rows.append(k)
    have = lambda name: (Path(data_dir) / name).exists()
    pe_missing = av[~av["PE_available"]]["date"].tolist()
    # A. the missing contract-wise PE files (one download each)
    cw_pe = sorted(cw[cw.option_type == "PE"]["date"].unique())
    pe_last = pd.Timestamp(cw_pe[-1]) if len(cw_pe) else None
    ce_last = pd.Timestamp(cw[cw.option_type == "CE"]["date"].max())
    wins = [("30-Mar-2026", "29-Jun-2026"), ("30-Jun-2026", "28-Sep-2026"), ("29-Sep-2026", "29-Sep-2026")]
    prio = 1
    for ot in ("PE", "CE"):
        for a, b in wins:
            name = f"OPTIDX_NIFTY_{ot}_{a}_TO_{b}.csv"
            w0, w1 = pd.to_datetime(a, format="%d-%b-%Y"), pd.to_datetime(b, format="%d-%b-%Y")
            have_ot = set(cw[cw.option_type == ot]["date"])
            in_cal = [d for d in av["date"] if w0 <= pd.Timestamp(d) <= w1]
            miss_known = [d for d in in_cal if pd.Timestamp(d) not in have_ot]
            future = w0 > ce_last
            add(priority=prio, request=f"contract-wise {ot} export {a} to {b}", kind="contract_wise_csv", expected_filename=name,
                status="present locally" if have(name) else "NOT ACQUIRED",
                dates_in_known_calendar=len(in_cal), dates_missing_for_this_type=len(miss_known),
                why=("fills the PE gap in the known calendar" if ot == "PE" and not future else
                     "extends coverage past the last locally known date; would be the first window not used in any V4.x experiment" if future else
                     "CE already covers this window"),
                source=KNOWN_SOURCES[1][0], url=KNOWN_SOURCES[1][1], verification="filename follows the pattern of your existing files; window inferred",
                sha256=A.sha256(Path(data_dir) / name) if have(name) else "")
            prio += 1
    # B. alternative: daily official bhavcopy per missing PE date (both types, all expiries)
    for d in pe_missing:
        ds = d.replace("-", "")
        name = f"BhavCopy_NSE_FO_0_0_0_{ds}_F_0000.csv"
        loc = have(name) or have(name + ".zip")
        add(priority=prio, request=f"daily official F&O bhavcopy {d} (alternative to the PE export)", kind="official_bhavcopy_udiff",
            expected_filename=name, status="present locally" if loc else "NOT ACQUIRED", dates_in_known_calendar=1, dates_missing_for_this_type=1,
            why="would supply PE rows for this date", source=KNOWN_SOURCES[0][0], url=KNOWN_SOURCES[0][1],
            verification="daily file name follows your local files; availability for this date NOT verified",
            sha256="")
        prio += 1
    # C. what is already acquired
    for f, g in bh.groupby("source_file"):
        add(priority=0, request=f"official bhavcopy {g['date'].iloc[0].date()}", kind="official_bhavcopy_udiff", expected_filename=f, status="present locally",
            dates_in_known_calendar=1, dates_missing_for_this_type=0, why="already in the study", source=KNOWN_SOURCES[0][0], url="",
            verification="parsed and hash-recorded", sha256=A.sha256(Path(data_dir) / f))
    df = pd.DataFrame(rows)
    # "present locally" rows for dates already available must not be re-requested
    return df.sort_values(["priority"]).reset_index(drop=True)


# ----------------------------------------------------------------------------- integrity checks
def integrity_checks(cw, bh, cw_inv, bh_inv, study, norm_all, hashes_before, hashes_after):
    c = A.Checks()
    for r in cw_inv.itertuples():
        c.add(f"contract_wise:{r.file}", "required columns present", len(r.missing_columns.split(",")) if r.missing_columns else 0, r.missing_columns)
    for r in bh_inv.itertuples():
        c.add(f"bhavcopy:{r.file}", "required columns present", len(r.missing_columns.split(",")) if r.missing_columns else 0, r.missing_columns)
        if r.filename_date and r.nifty_option_rows:
            fd = pd.to_datetime(r.filename_date, format="%Y%m%d")
            c.add(f"bhavcopy:{r.file}", "trade date differs from filename date", int((bh[bh.source_file == r.file]["date"] != fd).sum()))
    for nm, df in (("contract_wise", cw), ("bhavcopy", bh)):
        c.add(nm, "unparseable dates", int(df["date"].isna().sum() + df["expiry"].isna().sum()))
        c.add(nm, "option type not CE/PE", int((~df["option_type"].isin(["CE", "PE"])).sum()))
        if nm == "contract_wise":
            c.add(nm, "non-positive or missing settlement", int((~(df["settle"] > 0)).sum()))
        else:   # official files carry settlement 0.00 on some deep-OTM rows that traded at a positive close: kept as is, never imputed
            u = df[(df.expiry == STUDY_EXPIRY)]
            # Informational, not a REVIEW item: settle <= 0 is below the V4.7 price floor (1.0), so such rows are never eligible
            # at their own date (the study validation checks assert this). Reported, never repaired.
            n0 = int((~(u["settle"] > 0)).sum())
            c.rows.append({"scope": "bhavcopy (study expiry)", "check": "non-positive or missing settlement", "issues": n0,
                           "result": "NOTE" if n0 else "PASS",
                           "detail": f"all expiries: {int((~(df['settle'] > 0)).sum())} rows with settlement <= 0, of which {int(((~(df['settle'] > 0)) & (df['close'] > 0)).sum())} have a positive close; left unchanged and excluded by the price-floor rule"})
        c.add(nm, "duplicate contract-date keys within origin", int(df.duplicated(KEY, keep=False).sum()))
    for f, g in cw.groupby("source_file"):
        m = CW_RE.match(f)
        if m:
            w0, w1 = pd.to_datetime(m.group(2), format="%d-%b-%Y"), pd.to_datetime(m.group(3), format="%d-%b-%Y")
            c.add(f"contract_wise:{f}", "rows outside declared filename window", int(((g.date < w0) | (g.date > w1)).sum()))
    c.add("contract_wise", "rows with an expiry other than the study expiry", int((cw.expiry != STUDY_EXPIRY).sum()))
    # close vs settlement are different fields and must stay different
    for nm, df in (("contract_wise", cw), ("bhavcopy", bh)):
        s = df.dropna(subset=["close", "settle"])
        c.add(nm, "close and settlement kept as separate columns (columns exist and are not identical everywhere)",
              0 if (len(s) and ((s.close - s.settle).abs() > TOL).any()) else 1,
              f"{int(((s.close - s.settle).abs() > TOL).sum())} of {len(s)} rows have close != settlement")
    # cross-origin agreement on overlapping contracts
    m = cw.merge(bh, on=KEY, suffixes=("_cw", "_bh"), validate="one_to_one")
    for a, b, tol in (("settle", "settle", TOL), ("close", "close", TOL), ("volume", "volume", 0.5), ("open_interest", "open_interest", 0.5),
                      ("underlying", "underlying", 0.011)):
        x, y = m[f"{a}_cw"], m[f"{b}_bh"]
        c.add("cross_origin", f"{a}: contract-wise vs official bhavcopy disagree", int(((x - y).abs() > tol).sum()), f"{len(m)} overlapping contract-dates")
    ud = pd.concat([cw[["date", "underlying"]], bh[["date", "underlying"]]]).dropna().groupby("date")["underlying"].agg(lambda s: s.max() - s.min())
    c.add("cross_origin", "dates whose underlying value differs between rows/origins by > 0.011", int((ud > 0.011).sum()))
    c.add("study_universe", "duplicate contract-date keys", int(study.duplicated(KEY, keep=False).sum()))
    c.add("study_universe", "settlement conflicts between origins (flagged, not altered)", int(study["settle_conflict"].sum()))
    c.add("study_universe", "close conflicts between origins (flagged, not altered)", int(study["close_conflict"].sum()))
    c.add("conservation", "contract-wise rows in normalized data equal raw rows", abs(len(cw) - int(cw_inv["rows_raw"].sum())))
    c.add("conservation", "bhavcopy NIFTY option rows equal the per-file counts", abs(len(bh) - int(bh_inv["nifty_option_rows"].sum())))
    c.add("raw_preservation", "raw input files unchanged during the run (sha256 before == after)", int(hashes_before != hashes_after))
    return c.frame()


def hash_inputs(data_dir):
    return {Path(f).name: A.sha256(Path(f)) for f in sorted(glob.glob(str(Path(data_dir) / "OPTIDX_NIFTY_*.csv")) + glob.glob(str(Path(data_dir) / "BhavCopy_NSE_FO_*.csv*")))}


# ----------------------------------------------------------------------------- orchestration
def run(data_dir="/content", out_dir=None, verbose=True):
    data_dir, out_dir = Path(data_dir), Path(out_dir or data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    h0 = hash_inputs(data_dir)
    cw, cw_inv = load_contract_wise(data_dir)
    bh, bh_inv = load_bhavcopy(data_dir)
    if cw.empty:
        raise FileNotFoundError("no usable OPTIDX_NIFTY_*.csv contract-wise files found")
    study = build_study_universe(cw, bh)
    cal = trading_calendar(cw, bh)
    av = availability(cal, cw, bh)
    gaps = pd.concat([gap_ranges(av, "CE"), gap_ranges(av, "PE")], ignore_index=True)
    manifest = acquisition_manifest(cw, bh, av, data_dir, cw["date"].max())
    audit = raw_file_audit(cw, bh, cw_inv, bh_inv, data_dir)
    norm_all = bh.copy()
    h1 = hash_inputs(data_dir)
    checks = integrity_checks(cw, bh, cw_inv, bh_inv, study, norm_all, h0, h1)
    w = A.write_csv
    fmt = lambda df: df.assign(**{c: df[c].dt.strftime("%Y-%m-%d") for c in ("date", "expiry") if c in df.columns})
    w(fmt(bh)[NORM_COLS], out_dir / f"{PREFIX}Normalized_Bhavcopy_NIFTY_Options.csv")
    w(fmt(study)[NORM_COLS + ["also_in_bhavcopy", "settle_conflict", "close_conflict"]], out_dir / f"{PREFIX}Normalized_Study_Universe.csv")
    w(audit, out_dir / f"{PREFIX}Raw_Coverage_Audit.csv")
    w(av, out_dir / f"{PREFIX}Calendar_Coverage.csv")
    w(gaps, out_dir / f"{PREFIX}Coverage_Gaps.csv")
    w(manifest, out_dir / f"{PREFIX}Source_Acquisition_Manifest.csv")
    w(checks, out_dir / f"{PREFIX}Schema_Integrity_Checks.csv")
    w(pd.DataFrame(sorted(h1.items()), columns=["file", "sha256"]), out_dir / f"{PREFIX}Raw_File_Hashes.csv")
    if verbose:
        print("=" * 76, "\nV5.0 DATA EXPANSION AUDIT (raw files untouched)\n" + "=" * 76)
        print(f"contract-wise rows {len(cw)} | official bhavcopy files {len(bh_inv)} | NIFTY option rows {len(bh)} | study-universe rows {len(study)}")
        print(f"known trading calendar: {len(cal)} dates {cal[0].date()} .. {cal[-1].date()}")
        print(gaps.to_string(index=False))
        need = manifest[(manifest.status != 'present locally')]
        print(f"acquisition manifest: {len(need)} items NOT acquired (first: {need.iloc[0]["request"] if len(need) else '-'})")
        bad = checks[checks.result == "REVIEW"]
        print("integrity items needing review:", "none" if bad.empty else "")
        if len(bad):
            print(bad[["scope", "check", "issues"]].to_string(index=False))
    return {"cw": cw, "bh": bh, "study": study, "availability": av, "gaps": gaps, "manifest": manifest, "checks": checks}


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    run(a[0] if a else "/content", a[1] if len(a) > 1 else None)
