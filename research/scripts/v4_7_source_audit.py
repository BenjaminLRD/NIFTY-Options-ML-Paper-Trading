"""ML Options Paper Trading V4.7 - automated source-data audit (paper-research only).

Compares the five original NSE NIFTY option CSVs with every official NSE F&O bhavcopy
found in DATA_DIR. Read-only on inputs; writes V4_7_Audit_*.csv reports to OUT_DIR.
Official settlement agreement is NOT evidence of executable prices, liquidity or profit.

Extreme-movement definitions (kept separate, both reported in `selection_definition`):
  threshold_only          |settlement change| > EXTREME_THRESHOLD (default 100%) between consecutive source observations.
  original_selection_only listed in ORIGINAL_SELECTION_FILE (the earlier hand-built list), if that file is present locally.
                          Matched EXACTLY on option_type, strike, prev_date and date, plus expiry / symbol / source_file
                          whenever the file has those columns. Strike/date alone never qualifies; a file without both
                          dates is rejected with an explicit note instead of being guessed at.
  both                    in both definitions.
The original list is only read, never reconstructed; if the file is absent the pipeline says so in the coverage report.

Usage:  python v4_7_source_audit.py [DATA_DIR] [OUT_DIR]      |   python v4_7_source_audit.py --selftest
"""
import glob
import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

EXTREME_THRESHOLD = 1.0      # |settlement change| > 100% between consecutive source observations
ORIGINAL_SELECTION_FILE = "V4_7_Extreme_Movement_Official_Verification.csv"   # optional, read-only
PRICE_TOL = 0.005            # prices are quoted to 0.01; anything >= 0.005 apart differs
OUT_PREFIX = "V4_7_Audit_"
SOURCE_GLOB = "OPTIDX_NIFTY_*.csv"
NSE_URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d}_F_0000.csv.zip"

ST_BOTH = "Official settlement and close matched"
ST_CLOSE_DIFF = "Settlement matched but close differed"
ST_CLOSE_NA = "Settlement matched; source close missing (close not compared)"
ST_SETTLE_DIFF = "Settlement discrepancy"
ST_NO_RECORD = "No official record found"
ST_NO_DATE = "Official date not available locally"
ST_AMBIG = "Ambiguous or unmatched contract"

SRC_REQUIRED = ["Symbol", "Date", "Expiry", "Option type", "Strike Price", "Close", "Settle Price"]
SRC_OPTIONAL = {"Open": "src_open", "High": "src_high", "Low": "src_low", "LTP": "src_ltp",
                "No. of contracts": "src_contracts", "Open Int": "src_open_interest",
                "Underlying Value": "src_underlying"}
UDIFF_REQUIRED = ["TradDt", "FinInstrmTp", "TckrSymb", "XpryDt", "StrkPric", "OptnTp", "ClsPric",
                  "SttlmPric", "TtlTradgVol", "TtlNbOfTxsExctd", "OpnIntrst"]
LEGACY_REQUIRED = ["INSTRUMENT", "SYMBOL", "EXPIRY_DT", "STRIKE_PR", "OPTION_TYP", "CLOSE",
                   "SETTLE_PR", "CONTRACTS", "OPEN_INT", "TIMESTAMP"]
DATE_FORMATS = ["%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y", "%d-%m-%Y", "%d/%m/%Y"]


# ----------------------------------------------------------------------------- helpers
def parse_dates(s):
    """Strict multi-format date parse; returns (Timestamp series, number of unparseable non-null values)."""
    s = s.astype("string").str.strip()
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    for fmt in DATE_FORMATS:
        todo = out.isna() & s.notna()
        if not todo.any():
            break
        out[todo] = pd.to_datetime(s[todo], format=fmt, errors="coerce")
    return out, int((out.isna() & s.notna() & (s != "") & (s != "-")).sum())


def to_num(s):
    return pd.to_numeric(s.astype("string").str.strip().replace({"-": pd.NA, "": pd.NA}), errors="coerce").astype("float64")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_csv(df, path):
    """Atomic overwrite so a failed or repeated run never leaves a half-written report."""
    tmp = str(path) + ".tmp"
    df.to_csv(tmp, index=False, encoding="utf-8", lineterminator="\n")
    os.replace(tmp, path)


class Checks:
    def __init__(self):
        self.rows = []

    def add(self, scope, check, count, detail=""):
        self.rows.append({"scope": scope, "check": check, "issues": int(count),
                          "result": "PASS" if count == 0 else "REVIEW", "detail": detail})

    def frame(self):
        return pd.DataFrame(self.rows)


# ----------------------------------------------------------------------------- source
def load_source(data_dir, checks):
    paths = sorted(p for p in Path(data_dir).glob(SOURCE_GLOB))
    # browser duplicates such as "name (1).csv": keep the base name when both exist
    base = {re.sub(r"\s*\(\d+\)(?=\.csv$)", "", p.name) for p in paths}
    keep = []
    for b in sorted(base):
        cands = [p for p in paths if re.sub(r"\s*\(\d+\)(?=\.csv$)", "", p.name) == b]
        keep.append(next((p for p in cands if p.name == b), cands[0]))
    if not keep:
        raise FileNotFoundError(f"No {SOURCE_GLOB} files in {data_dir}")
    frames = []
    for p in keep:
        d = pd.read_csv(p, dtype=str, encoding="utf-8")
        d.columns = [str(c).strip() for c in d.columns]
        missing = [c for c in SRC_REQUIRED if c not in d.columns]
        checks.add(f"source:{p.name}", "required columns present", len(missing), ",".join(missing))
        if missing:
            continue
        d["source_file"] = p.name
        frames.append(d)
    if not frames:
        raise ValueError("No source file has the required columns.")
    r = pd.concat(frames, ignore_index=True)
    r["date"], bad_d = parse_dates(r["Date"])
    r["expiry"], bad_e = parse_dates(r["Expiry"])
    checks.add("source", "unparseable trade dates", bad_d)
    checks.add("source", "unparseable expiry dates", bad_e)
    r["symbol"] = r["Symbol"].str.strip().str.upper()
    r["option_type"] = r["Option type"].str.strip().str.upper()
    r["strike"] = to_num(r["Strike Price"]).round(2)
    r["src_close"] = to_num(r["Close"])
    r["src_settle"] = to_num(r["Settle Price"])
    for col, name in SRC_OPTIONAL.items():
        r[name] = to_num(r[col]) if col in r.columns else np.nan
    checks.add("source", "non-numeric/missing settlement", int(r["src_settle"].isna().sum()))
    checks.add("source", "option type not CE/PE", int((~r["option_type"].isin(["CE", "PE"])).sum()))
    key = ["date", "symbol", "expiry", "option_type", "strike"]
    checks.add("source", "duplicate contract/date keys", int(r.duplicated(key, keep=False).sum()))
    r = r.dropna(subset=["date", "expiry", "strike"]).copy()
    r = r.sort_values(key + ["source_file"]).reset_index(drop=True)
    return r, keep


def add_movements(r):
    k = ["symbol", "expiry", "option_type", "strike"]
    g = r.sort_values(k + ["date"]).groupby(k, sort=False)
    r = g.obj.copy()
    r["prev_date"] = g["date"].shift(1)
    r["prev_src_settle"] = g["src_settle"].shift(1)
    r["src_return"] = r["src_settle"] / r["prev_src_settle"] - 1
    r.loc[~np.isfinite(r["src_return"]), "src_return"] = np.nan
    return r.sort_values(["date", "option_type", "strike"]).reset_index(drop=True)


# ----------------------------------------------------------------------------- official
def sniff(path):
    try:
        cols = [c.strip() for c in pd.read_csv(path, nrows=0, encoding="utf-8").columns]
    except Exception as e:  # unreadable / binary / empty
        return "unreadable", [], str(e)
    if {"TradDt", "FinInstrmTp", "TckrSymb"} <= set(cols):
        return "udiff", cols, ""
    if {"INSTRUMENT", "SYMBOL", "SETTLE_PR", "TIMESTAMP"} <= set(cols):
        return "legacy", cols, ""
    return "other", cols, ""


def load_official(data_dir, checks):
    """Return (normalized NIFTY index-option rows, per-file inventory). Only 'BhavCopy*'/'fo*bhav*' names are tried."""
    cands = sorted({*glob.glob(os.path.join(data_dir, "*.csv")), *glob.glob(os.path.join(data_dir, "*.csv.zip"))})
    cands = [c for c in cands if not Path(c).name.startswith(("V4_", OUT_PREFIX, "OPTIDX_NIFTY_"))]
    inv, frames = [], []
    for c in cands:
        p = Path(c)
        kind, cols, err = sniff(p)
        row = {"file": p.name, "format": kind, "sha256": sha256(p), "nifty_option_rows": 0,
               "trade_dates": "", "note": err}
        if kind not in ("udiff", "legacy"):
            row["note"] = row["note"] or "not an F&O bhavcopy (ignored)"
            inv.append(row)
            continue
        req = UDIFF_REQUIRED if kind == "udiff" else LEGACY_REQUIRED
        missing = [x for x in req if x not in cols]
        checks.add(f"official:{p.name}", "required columns present", len(missing), ",".join(missing))
        if missing:
            row["note"] = "missing columns: " + ",".join(missing)
            inv.append(row)
            continue
        d = pd.read_csv(p, dtype=str, usecols=req)
        if kind == "udiff":
            d = d[(d["TckrSymb"].str.strip() == "NIFTY") & (d["FinInstrmTp"].str.strip() == "IDO")]
            n = pd.DataFrame({"date": parse_dates(d["TradDt"])[0], "symbol": "NIFTY",
                              "expiry": parse_dates(d["XpryDt"])[0], "option_type": d["OptnTp"].str.strip().str.upper(),
                              "strike": to_num(d["StrkPric"]).round(2), "off_close": to_num(d["ClsPric"]),
                              "off_settle": to_num(d["SttlmPric"]), "off_volume": to_num(d["TtlTradgVol"]),
                              "off_transactions": to_num(d["TtlNbOfTxsExctd"]), "off_open_interest": to_num(d["OpnIntrst"])})
            bad = int(parse_dates(d["TradDt"])[1] + parse_dates(d["XpryDt"])[1])
        else:
            d = d[(d["INSTRUMENT"].str.strip() == "OPTIDX") & (d["SYMBOL"].str.strip() == "NIFTY")]
            n = pd.DataFrame({"date": parse_dates(d["TIMESTAMP"])[0], "symbol": "NIFTY",
                              "expiry": parse_dates(d["EXPIRY_DT"])[0], "option_type": d["OPTION_TYP"].str.strip().str.upper(),
                              "strike": to_num(d["STRIKE_PR"]).round(2), "off_close": to_num(d["CLOSE"]),
                              "off_settle": to_num(d["SETTLE_PR"]), "off_volume": to_num(d["CONTRACTS"]),
                              "off_transactions": np.nan, "off_open_interest": to_num(d["OPEN_INT"])})
            bad = int(parse_dates(d["TIMESTAMP"])[1] + parse_dates(d["EXPIRY_DT"])[1])
        checks.add(f"official:{p.name}", "unparseable dates", bad)
        n["official_file"] = p.name
        m = re.search(r"(\d{8})", p.name)
        if m and len(n):
            fdate = pd.to_datetime(m.group(1), format="%Y%m%d", errors="coerce")
            checks.add(f"official:{p.name}", "trade date differs from filename date",
                       int((n["date"].dropna() != fdate).sum()) if pd.notna(fdate) else 0)
        row["nifty_option_rows"] = len(n)
        row["trade_dates"] = "|".join(sorted(n["date"].dropna().dt.strftime("%Y-%m-%d").unique()))
        if len(n) == 0:
            row["note"] = "no NIFTY index-option rows"
        inv.append(row)
        frames.append(n)
    off = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["date", "symbol", "expiry", "option_type", "strike", "off_close", "off_settle", "off_volume",
                 "off_transactions", "off_open_interest", "official_file"])
    key = ["date", "symbol", "expiry", "option_type", "strike"]
    full_dups = off.duplicated(keep="first")
    checks.add("official", "exact duplicate rows (collapsed)", int(full_dups.sum()))
    off = off[~full_dups]
    checks.add("official", "conflicting duplicate contract keys (ambiguous)", int(off.duplicated(key, keep=False).sum()))
    return off, pd.DataFrame(inv)


# ----------------------------------------------------------------------------- compare
EV_UNKNOWN = "Unknown / official date unavailable"
EV_POS = "Positive volume and transactions"
EV_NONE = "No positive volume-and-transaction evidence"
NEW_EXT_COLS = ["liquidity_evidence_class", "prev_official_activity_evidence", "current_official_activity_evidence",
                "in_threshold_definition", "in_original_selection", "selection_definition"]


def liquidity_class(prev_ev, cur_ev):
    pu, cu = prev_ev.startswith("Unknown"), cur_ev.startswith("Unknown")
    if pu and cu:
        return "Liquidity unknown on both dates"
    if cu:
        return "Previous-date activity known; current date unavailable"
    if pu:
        return "Current-date activity known; previous date unavailable"
    pp, cp = prev_ev.startswith("Positive"), cur_ev.startswith("Positive")
    if pp and cp:
        return "Positive volume and transactions on both dates"
    if not pp and not cp:
        return "No positive volume-and-transaction evidence on either date"
    return "Positive volume on " + ("previous" if pp else "current") + " date only"


def compare(src, off):
    key = ["date", "symbol", "expiry", "option_type", "strike"]
    cnt = off.groupby(key).size().rename("official_matches").reset_index()
    first = off.drop_duplicates(key, keep="first")
    m = src.merge(cnt, on=key, how="left").merge(first, on=key, how="left")
    if len(m) != len(src):  # a merge fanned out: refuse rather than double-count a contract-date
        raise ValueError(f"Duplicate contract-date matches: {len(src)} source rows became {len(m)}")
    m["source_duplicate_key"] = src.duplicated(key, keep=False).to_numpy()
    have_dates = set(off["date"].dropna().unique())
    m["official_date_available"] = m["date"].isin(have_dates)
    m["official_matches"] = m["official_matches"].fillna(0).astype(int)
    # contract with same date/type/strike but another expiry exists -> flag for diagnosis
    loose = off.groupby(["date", "option_type", "strike"]).size().rename("n_any_expiry").reset_index()
    m = m.merge(loose, on=["date", "option_type", "strike"], how="left")
    if len(m) != len(src):
        raise ValueError("Duplicate matches introduced by the expiry-diagnosis merge")
    m["n_any_expiry"] = m["n_any_expiry"].fillna(0).astype(int)

    sd = (m["src_settle"] - m["off_settle"]).abs()
    cd = (m["src_close"] - m["off_close"]).abs()
    settle_ok = (sd < PRICE_TOL).fillna(False).to_numpy(dtype=bool)
    close_ok = (cd < PRICE_TOL).fillna(False).to_numpy(dtype=bool)
    status = np.select(
        [m["source_duplicate_key"].to_numpy(dtype=bool), ~m["official_date_available"].to_numpy(dtype=bool),
         (m["official_matches"] > 1).to_numpy(), (m["official_matches"] == 0).to_numpy(), ~settle_ok,
         settle_ok & m["src_close"].isna().to_numpy(), settle_ok & close_ok],
        [ST_AMBIG, ST_NO_DATE, ST_AMBIG, ST_NO_RECORD, ST_SETTLE_DIFF, ST_CLOSE_NA, ST_BOTH],
        default=ST_CLOSE_DIFF)
    m["audit_status"] = status
    unmatched = m["audit_status"].isin([ST_NO_DATE, ST_NO_RECORD, ST_AMBIG])
    m.loc[unmatched, [c for c in off.columns if c.startswith("off_")]] = np.nan  # no single official record to report
    m["settle_diff"] = (m["src_settle"] - m["off_settle"]).round(4)
    m["close_diff"] = (m["src_close"] - m["off_close"]).round(4)
    m["volume_check"] = np.select(
        [m["off_volume"].isna() | m["src_contracts"].isna()], ["not comparable"],
        default=np.where((m["off_volume"] - m["src_contracts"]).abs() < 0.5, "match", "differs"))
    m["oi_check"] = np.select(
        [m["off_open_interest"].isna() | m["src_open_interest"].isna()], ["not comparable"],
        default=np.where((m["off_open_interest"] - m["src_open_interest"]).abs() < 0.5, "match", "differs"))
    # official evidence only: what NSE recorded that day, not proof anything was executable at that price
    m["official_trading_activity"] = np.select(
        [m["off_volume"].isna(), m["off_volume"] > 0], ["unknown", "trades recorded (volume>0)"],
        default="no trades recorded (zero volume; price carried/derived)")
    # liquidity evidence = what NSE recorded (volume, transactions); it does not show a fill was available
    pos = (m["off_volume"] > 0).to_numpy()
    tx_pos = (m["off_transactions"] > 0).to_numpy()
    m["activity_evidence"] = np.select(
        [m["audit_status"].eq(ST_NO_DATE).to_numpy(), m["audit_status"].eq(ST_NO_RECORD).to_numpy(),
         m["audit_status"].eq(ST_AMBIG).to_numpy(), pos & tx_pos, pos & m["off_transactions"].isna().to_numpy()],
        [EV_UNKNOWN, "Unknown / no official record", "Unknown / ambiguous official record",
         EV_POS, "Positive volume (transactions not reported)"],
        default=EV_NONE)
    m["wrong_expiry_only_hint"] = (m["audit_status"] == ST_NO_RECORD) & (m["n_any_expiry"] > 0)
    return m


ORIG_ALIASES = {"option_type": ("option_type", "type"), "strike": ("strike", "strike_price"),
                "prev_date": ("prev_date", "previous_date"), "date": ("date", "current_date"),
                "expiry": ("expiry", "expiry_date"), "symbol": ("symbol",), "source_file": ("source_file",)}
ORIG_REQUIRED = ("option_type", "strike", "prev_date", "date")


def load_original_selection(data_dir):
    """Optional earlier hand-built list. Returns (DataFrame or None, note). Never invents entries."""
    p = Path(data_dir) / ORIGINAL_SELECTION_FILE
    if not p.exists():
        return None, f"{ORIGINAL_SELECTION_FILE} not found locally; original selection could not be applied or reconstructed"
    d = pd.read_csv(p, dtype=str, encoding="utf-8-sig")
    d.columns = [c.strip().lower().replace(" ", "_") for c in d.columns]
    pick = {k: next((c for c in al if c in d.columns), None) for k, al in ORIG_ALIASES.items()}
    missing = [k for k in ORIG_REQUIRED if pick[k] is None]
    if missing:
        return None, (f"{ORIGINAL_SELECTION_FILE} present but cannot identify a movement exactly "
                      f"(missing: {', '.join(missing)}); not used, nothing labelled original")
    out = pd.DataFrame({k: d[c] for k, c in pick.items() if c is not None})
    out["option_type"] = out["option_type"].str.strip().str.upper()
    out["strike"] = to_num(out["strike"]).round(2)
    for c in ("prev_date", "date", "expiry"):
        if c in out:
            out[c] = parse_dates(out[c])[0]
    for c in ("symbol", "source_file"):
        if c in out:
            out[c] = out[c].str.strip()
    out["symbol"] = out["symbol"].str.upper() if "symbol" in out else None
    out = out.drop(columns=[c for c in out.columns if out[c].isna().all() and c not in ORIG_REQUIRED])
    fields = [c for c in out.columns]
    return out.drop_duplicates().reset_index(drop=True), (
        f"{len(d)} rows read from {ORIGINAL_SELECTION_FILE}; exact match on: {', '.join(fields)}")


def match_original(m, orig):
    """Exact identity match of the original list against source movements.
    Returns (mask over m, per-original-row match counts). Zero matches = unmatched, >1 = ambiguous."""
    if orig is None:
        return np.zeros(len(m), dtype=bool), None
    fields = list(orig.columns)
    mm = m[fields].copy()
    mm["_i"] = np.arange(len(m))
    j = orig.reset_index(names="_o").merge(mm, on=fields, how="left")
    counts = j.groupby("_o")["_i"].count()
    mask = np.zeros(len(m), dtype=bool)
    mask[j["_i"].dropna().astype(int).to_numpy()] = True
    return mask, counts


def extreme_report(m, threshold, org=None):
    thr = (m["src_return"].abs() > threshold).to_numpy()
    org = np.zeros(len(m), dtype=bool) if org is None else org & m["prev_date"].notna().to_numpy()
    ext = m[thr | org].copy()
    ext["in_threshold_definition"] = thr[thr | org]
    ext["in_original_selection"] = org[thr | org]
    lookup = m.set_index(["date", "option_type", "strike", "expiry"])
    rows = []
    for i, e in enumerate(ext.sort_values(["date", "option_type", "strike"]).itertuples(), 1):
        rec = {"movement_id": i, "option_type": e.option_type, "strike": e.strike, "expiry": e.expiry.date(),
               "prev_date": e.prev_date.date(), "date": e.date.date(), "gap_calendar_days": (e.date - e.prev_date).days,
               "src_prev_settle": e.prev_src_settle, "src_settle": e.src_settle,
               "src_return_pct": round(e.src_return * 100, 2)}
        stat, ev = [], {}
        for tag, d in (("prev", e.prev_date), ("cur", e.date)):
            k = (d, e.option_type, e.strike, e.expiry)
            row = lookup.loc[k] if k in lookup.index else None
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]
            if row is None:
                st, o = ST_NO_DATE, {}
            else:
                st, o = row["audit_status"], row
            stat.append(st)
            rec[f"{tag}_status"] = st
            for f_, c_ in (("off_settle", "off_settle"), ("off_close", "off_close"), ("off_volume", "off_volume"),
                           ("off_transactions", "off_transactions"), ("off_open_interest", "off_open_interest")):
                rec[f"{tag}_{f_}"] = o[c_] if len(o) else np.nan
            rec[f"{tag}_trading_activity"] = o["official_trading_activity"] if len(o) else "unknown"
            ev[tag] = o["activity_evidence"] if len(o) else EV_UNKNOWN
        rec["official_return_pct"] = (round((rec["cur_off_settle"] / rec["prev_off_settle"] - 1) * 100, 2)
                                      if pd.notna(rec["cur_off_settle"]) and pd.notna(rec["prev_off_settle"])
                                      and rec["prev_off_settle"] else np.nan)
        good = {ST_BOTH, ST_CLOSE_DIFF, ST_CLOSE_NA}
        if any(s == ST_SETTLE_DIFF for s in stat):
            rec["movement_verification"] = "Settlement discrepancy on at least one date"
        elif any(s in (ST_NO_RECORD, ST_AMBIG) for s in stat):
            rec["movement_verification"] = "No/ambiguous official record on an available date"
        elif all(s in good for s in stat):
            rec["movement_verification"] = "Both dates: official settlement matched"
        elif any(s in good for s in stat):
            rec["movement_verification"] = "One date verified; other official date not available locally"
        else:
            rec["movement_verification"] = "Neither official date available locally"
        rec["liquidity_evidence_class"] = liquidity_class(ev["prev"], ev["cur"])
        rec["prev_official_activity_evidence"] = ev["prev"]
        rec["current_official_activity_evidence"] = ev["cur"]
        rec["in_threshold_definition"] = bool(e.in_threshold_definition)
        rec["in_original_selection"] = bool(e.in_original_selection)
        rec["selection_definition"] = ("both" if e.in_threshold_definition and e.in_original_selection else
                                       "threshold_only" if e.in_threshold_definition else "original_selection_only")
        rows.append(rec)
    return pd.DataFrame(rows)


def remaining_dates(m, ext):
    """Dates with no local official file, ranked by unresolved extreme-movement legs, then max |return|, then rows."""
    unresolved = m[m["audit_status"] == ST_NO_DATE]
    legs = []
    if len(ext):
        for tag in ("prev", "cur"):
            t = ext[ext[f"{tag}_status"] == ST_NO_DATE][["date" if tag == "cur" else "prev_date", "src_return_pct"]]
            t.columns = ["date", "ret"]
            legs.append(t)
    legs = pd.concat(legs) if legs else pd.DataFrame(columns=["date", "ret"])
    legs["date"] = pd.to_datetime(legs["date"])
    legs["ret"] = legs["ret"].abs() / 100
    a = unresolved.groupby("date").agg(unresolved_rows=("audit_status", "size"),
                                       max_abs_return_any_row=("src_return", lambda s: s.abs().max())).reset_index()
    b = legs.groupby("date").agg(unresolved_extreme_legs=("ret", "size"), max_abs_return_extreme=("ret", "max")).reset_index()
    out = a.merge(b, on="date", how="left")
    out[["unresolved_extreme_legs"]] = out[["unresolved_extreme_legs"]].fillna(0).astype(int)
    out["max_abs_return_extreme"] = out["max_abs_return_extreme"].fillna(0)
    out["max_abs_return_any_row"] = out["max_abs_return_any_row"].fillna(0)
    out = out.sort_values(["unresolved_extreme_legs", "max_abs_return_extreme", "max_abs_return_any_row",
                           "unresolved_rows", "date"], ascending=[False, False, False, False, True]).reset_index(drop=True)
    out.insert(0, "priority_rank", np.arange(1, len(out) + 1))
    ds = out["date"].dt.strftime("%Y%m%d")
    out["expected_filename"] = "BhavCopy_NSE_FO_0_0_0_" + ds + "_F_0000.csv"
    out["candidate_url_unverified"] = ds.map(lambda d: NSE_URL.format(d=d))
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    return out


def coverage(m, ext, rem, inv, threshold, src_files, orig_note=""):
    rows = []
    add = lambda s, k, v: rows.append({"section": s, "metric": k, "value": v})
    add("inputs", "source files", len(src_files))
    add("inputs", "source rows", len(m))
    add("inputs", "source trading dates", m["date"].nunique())
    ok = inv[inv["format"].isin(["udiff", "legacy"]) & (inv["nifty_option_rows"] > 0)] if len(inv) else inv
    add("inputs", "official bhavcopy files processed", len(ok))
    add("inputs", "official files ignored", len(inv) - len(ok))
    add("inputs", "official dates available (all)", int(m.loc[m["official_date_available"], "date"].nunique()))
    for s, n in m["audit_status"].value_counts().items():
        add("row_status", s, int(n))
    for (ot, s), n in m.groupby(["option_type", "audit_status"]).size().items():
        add(f"row_status_{ot}", s, int(n))
    cov = m["official_date_available"].mean()
    add("coverage", "share of source rows whose official date is available locally", round(float(cov), 4))
    add("coverage", "source dates still missing locally", int(rem.shape[0]))
    sm = m[m["audit_status"].isin([ST_BOTH, ST_CLOSE_DIFF, ST_CLOSE_NA, ST_SETTLE_DIFF])]
    add("coverage", "rows with an official match", len(sm))
    add("coverage", "wrong-expiry-only hints (same date/type/strike, other expiry)", int(m["wrong_expiry_only_hint"].sum()))
    add("extreme", f"movements with |source change| > {threshold:.0%}", len(ext))
    if len(ext):
        for s, n in ext["movement_verification"].value_counts().items():
            add("extreme_status", s, int(n))
    if len(ext):
        for s_, n in ext["liquidity_evidence_class"].value_counts().items():
            add("liquidity_class", s_, int(n))
        for s_, n in ext["selection_definition"].value_counts().items():
            add("selection_definition", s_, int(n))
    add("selection_definition", "original selection source", orig_note)
    add("selection_definition", "threshold definition",
        f"|settlement change| > {threshold:.0%} between consecutive source observations")
    for th in (0.5, 0.75, 0.9, 1.0):
        add("extreme_threshold_counts", f"|change| > {th:.0%}", int((m["src_return"].abs() > th).sum()))
    add("note", "scope", "Official settlement agreement verifies the recorded price only; it is not evidence of "
                          "executable prices, liquidity, fills or profitability.")
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------- orchestration
def run_audit(data_dir="/content", out_dir=None, threshold=EXTREME_THRESHOLD, verbose=True):
    data_dir = str(data_dir)
    out_dir = Path(out_dir or data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    checks = Checks()
    src, src_paths = load_source(data_dir, checks)
    src = add_movements(src)
    off, inv = load_official(data_dir, checks)
    m = compare(src, off)
    checks.add("compare", "ambiguous official matches", int((m["audit_status"] == ST_AMBIG).sum()))
    checks.add("compare", "settlement discrepancies", int((m["audit_status"] == ST_SETTLE_DIFF).sum()),
               "discrepancy = source differs from official; no value has been altered")
    checks.add("compare", "source contract-date keys duplicated (marked ambiguous)", int(m["source_duplicate_key"].sum()))
    checks.add("compare", "same date/type/strike under more than one source expiry",
               int(src.groupby(["date", "option_type", "strike"])["expiry"].nunique().gt(1).sum()))
    checks.add("compare", "contract not found but another expiry exists officially (identity check)",
               int(m["wrong_expiry_only_hint"].sum()))
    orig, orig_note = load_original_selection(data_dir)
    org_mask, org_counts = match_original(m, orig)
    ext = extreme_report(m, threshold, org_mask)
    checks.add("extreme", "movement ids not unique", int(ext["movement_id"].duplicated().sum()) if len(ext) else 0)
    if orig is None and "cannot identify" in orig_note:
        checks.add("extreme", "original-selection file unusable (insufficient identity columns)", 1, orig_note)
    if org_counts is not None:
        checks.add("extreme", "original-selection rows with no exact movement match", int((org_counts == 0).sum()))
        checks.add("extreme", "original-selection rows matching more than one movement (ambiguous)",
                   int((org_counts > 1).sum()))
    rem = remaining_dates(m, ext)
    cov = coverage(m, ext, rem, inv, threshold, src_paths, orig_note)

    keep = ["source_file", "date", "symbol", "expiry", "option_type", "strike", "audit_status",
            "src_settle", "off_settle", "settle_diff", "src_close", "off_close", "close_diff",
            "src_contracts", "off_volume", "volume_check", "off_transactions",
            "src_open_interest", "off_open_interest", "oi_check", "official_trading_activity",
            "official_file", "official_matches", "wrong_expiry_only_hint", "prev_date", "src_return"]
    rl = m[keep].copy()
    for c in ("date", "expiry", "prev_date"):
        rl[c] = rl[c].dt.strftime("%Y-%m-%d")
    write_csv(rl, out_dir / f"{OUT_PREFIX}Row_Level_Comparison.csv")
    write_csv(ext.drop(columns=NEW_EXT_COLS, errors="ignore"), out_dir / f"{OUT_PREFIX}Extreme_Movement_Verification.csv")
    write_csv(ext, out_dir / f"{OUT_PREFIX}Extreme_Movement_Liquidity_Review.csv")  # consolidated report
    if len(ext):
        summ = (ext.groupby(["movement_verification", "liquidity_evidence_class"]).size().rename("movement_count")
                .reset_index().sort_values(["movement_verification", "movement_count", "liquidity_evidence_class"],
                                           ascending=[True, False, True]))
    else:
        summ = pd.DataFrame(columns=["movement_verification", "liquidity_evidence_class", "movement_count"])
    write_csv(summ, out_dir / f"{OUT_PREFIX}Liquidity_Review_Summary.csv")
    write_csv(rem, out_dir / f"{OUT_PREFIX}Remaining_Dates_Manifest.csv")
    write_csv(cov, out_dir / f"{OUT_PREFIX}Coverage_Summary.csv")
    write_csv(checks.frame(), out_dir / f"{OUT_PREFIX}Validation_Checks.csv")
    manifest = pd.concat([inv.assign(role="official_candidate"),
                          pd.DataFrame({"file": [p.name for p in src_paths], "sha256": [sha256(p) for p in src_paths],
                                        "format": "source", "role": "source"})], ignore_index=True)
    write_csv(manifest, out_dir / f"{OUT_PREFIX}Input_Manifest.csv")

    if verbose:
        print("=" * 72, "\nV4.7 SOURCE AUDIT SUMMARY (paper research; settlement check != executability)\n" + "=" * 72)
        print(f"Source rows: {len(m)} | source dates: {m['date'].nunique()} | official files processed: "
              f"{int(((inv['format'].isin(['udiff','legacy'])) & (inv['nifty_option_rows'] > 0)).sum()) if len(inv) else 0}")
        print(m["audit_status"].value_counts().to_string())
        print(f"\nExtreme movements (> {threshold:.0%}): {len(ext)}")
        if len(ext):
            print(ext["movement_verification"].value_counts().to_string())
            print("\nLiquidity-evidence classes (official volume/transactions only; not proof of executability):")
            print(ext["liquidity_evidence_class"].value_counts().to_string())
            print("Selection definitions:", ext["selection_definition"].value_counts().to_dict(), "|", orig_note)
        print(f"\nOfficial dates still missing: {len(rem)} (top 10 in manifest by priority)")
        print(rem.head(10)[["priority_rank", "date", "unresolved_extreme_legs", "max_abs_return_extreme",
                            "unresolved_rows"]].to_string(index=False))
        bad = checks.frame()
        bad = bad[bad["result"] == "REVIEW"]
        print("\nValidation items needing review:", "none" if bad.empty else "")
        if len(bad):
            print(bad[["scope", "check", "issues"]].to_string(index=False))
        print("\nReports saved to", out_dir)
    return {"row_level": rl, "extreme": ext, "remaining": rem, "coverage": cov, "checks": checks.frame(), "inventory": inv}


# ----------------------------------------------------------------------------- self-test
def run_selftest():
    tmp = Path(tempfile.mkdtemp(prefix="v47test_"))
    src = pd.DataFrame({
        "Symbol  ": ["NIFTY"] * 11, "Date  ": ["10-Mar-2026"] * 7 + ["11-Mar-2026"] * 3 + ["12-Mar-2026"],
        "Expiry  ": ["29-Sep-2026"] * 11, "Option type  ": ["CE"] * 11,
        "Strike Price  ": ["100", "200", "300", "400", "500", "600", "800", "100", "100", "800", "100"],
        "Close  ": ["10", "10", "10", "-", "10", "10", "10", "10", "10", "20", "10"],
        "Settle Price  ": ["5", "5", "5", "5", "5", "5", "5", "5", "5", "20", "5"]})
    src.to_csv(tmp / "OPTIDX_NIFTY_CE_01-Mar-2026_TO_31-Mar-2026.csv", index=False)

    def u(strike, exp, close, settle):
        return {"TradDt": "2026-03-10", "FinInstrmTp": "IDO", "TckrSymb": "NIFTY", "XpryDt": exp, "StrkPric": strike,
                "OptnTp": "CE", "ClsPric": close, "SttlmPric": settle, "TtlTradgVol": 3, "TtlNbOfTxsExctd": 2, "OpnIntrst": 9}
    rows = [u("100.00", "2026-09-29", "10.00", "5.00"),      # both match
            u("200.00", "2026-09-29", "11.00", "5.00"),      # close differs
            u("300.00", "2026-09-29", "10.00", "6.00"),      # settlement discrepancy
            u("400.00", "2026-09-29", "10.00", "5.00"),      # source close missing
            u("600.00", "2026-12-29", "10.00", "5.00"),      # only other expiry -> no record
            u("500.00", "2026-09-29", "10.00", "5.00"), u("500.00", "2026-09-29", "10.00", "7.00"),  # ambiguous
            u("800.00", "2026-09-29", "10.00", "5.00")]                                               # extreme leg 1
    pd.DataFrame(rows).to_csv(tmp / "BhavCopy_NSE_FO_0_0_0_20260310_F_0000.csv", index=False)
    pd.DataFrame({"INSTRUMENT": ["OPTIDX"] * 2, "SYMBOL": ["NIFTY"] * 2, "EXPIRY_DT": ["29-Sep-2026"] * 2,
                  "STRIKE_PR": [100, 800], "OPTION_TYP": ["CE"] * 2, "CLOSE": [10, 20], "SETTLE_PR": [5, 20],
                  "CONTRACTS": [3, 0], "OPEN_INT": [9, 9], "TIMESTAMP": ["11-Mar-2026"] * 2}
                 ).to_csv(tmp / "fo11MAR2026bhav.csv", index=False)  # legacy format; extreme leg 2 has zero contracts
    pd.DataFrame({"option_type": ["CE", "PE", "CE", "CE"], "strike": [800, 9999, 800, 800],
                  "prev_date": ["2026-03-10", "2026-03-10", "2026-03-09", "2026-03-10"], "date": ["2026-03-11"] * 4,
                  "expiry": ["2026-09-29", "2026-09-29", "2026-09-29", "2026-12-29"]}  # exact / absent / decoy prev_date / wrong expiry
                 ).to_csv(tmp / ORIGINAL_SELECTION_FILE, index=False)
    pd.DataFrame({"DATE": ["12-MAR-2026"], "INSTRUMENT": ["OPTIDX"]}).to_csv(tmp / "FOSett_prce_12032026.csv", index=False)
    res = run_audit(tmp, verbose=False)
    got = res["row_level"].set_index(["date", "strike"])["audit_status"].to_dict()
    exp = {("2026-03-10", 100.0): ST_BOTH, ("2026-03-10", 200.0): ST_CLOSE_DIFF, ("2026-03-10", 300.0): ST_SETTLE_DIFF,
           ("2026-03-10", 400.0): ST_CLOSE_NA, ("2026-03-10", 500.0): ST_AMBIG, ("2026-03-10", 600.0): ST_NO_RECORD,
           ("2026-03-10", 800.0): ST_BOTH, ("2026-03-11", 100.0): ST_AMBIG,  # duplicate source key
           ("2026-03-11", 800.0): ST_BOTH, ("2026-03-12", 100.0): ST_NO_DATE}
    assert got == exp, (got, exp)
    ext = res["extreme"]
    assert len(ext) == 1 and ext.iloc[0]["selection_definition"] == "both", ext
    assert ext.iloc[0]["liquidity_evidence_class"] == "Positive volume on previous date only", ext.iloc[0]
    assert res["checks"].set_index("check").loc["source contract-date keys duplicated (marked ambiguous)", "issues"] == 2
    assert res["checks"].set_index("check").loc["original-selection rows with no exact movement match", "issues"] == 3
    # a file lacking prev_date must label nothing and say why
    (tmp / ORIGINAL_SELECTION_FILE).write_text("option_type,strike,date\nCE,800,2026-03-11\n")
    r2 = run_audit(tmp, verbose=False)
    assert r2["extreme"]["selection_definition"].eq("threshold_only").all()
    assert r2["checks"]["check"].str.contains("original-selection file unusable").any()
    (tmp / ORIGINAL_SELECTION_FILE).unlink()
    r3 = run_audit(tmp, verbose=False)
    assert "not found locally" in r3["coverage"].set_index("metric").loc["original selection source", "value"]
    (tmp / ORIGINAL_SELECTION_FILE).write_text("option_type,strike,prev_date,date,expiry\nCE,800,2026-03-10,2026-03-11,2026-09-29\n")
    for a_, b_, want in [(EV_UNKNOWN, EV_UNKNOWN, "Liquidity unknown on both dates"),
                         (EV_POS, EV_UNKNOWN, "Previous-date activity known; current date unavailable"),
                         (EV_UNKNOWN, EV_NONE, "Current-date activity known; previous date unavailable"),
                         (EV_POS, EV_POS, "Positive volume and transactions on both dates"),
                         (EV_NONE, EV_NONE, "No positive volume-and-transaction evidence on either date"),
                         (EV_NONE, EV_POS, "Positive volume on current date only")]:
        assert liquidity_class(a_, b_) == want, (a_, b_)
    rl_ = res["row_level"]
    assert rl_.loc[(rl_["date"] == "2026-03-10") & (rl_["strike"] == 600.0), "wrong_expiry_only_hint"].all()
    assert "FOSett_prce_12032026.csv" in set(res["inventory"].loc[res["inventory"]["format"] == "other", "file"])
    run_audit(tmp, verbose=False)
    h1 = {p.name: sha256(p) for p in tmp.glob(OUT_PREFIX + "*.csv")}
    run_audit(tmp, verbose=False)
    h2 = {p.name: sha256(p) for p in tmp.glob(OUT_PREFIX + "*.csv")}
    assert h1 == h2 and len(h1) == 8, "rerun changed outputs"
    print("SELFTEST PASSED: status cases, duplicate-key ambiguity, liquidity classes, two selection definitions, idempotent rerun")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        run_selftest()
    else:
        a = [x for x in sys.argv[1:] if not x.startswith("--")]
        run_audit(a[0] if a else "/content", a[1] if len(a) > 1 else None)
