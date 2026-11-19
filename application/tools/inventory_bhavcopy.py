"""Read-only inventory of NSE derivatives files found under a folder: what dates, instruments and fields they really hold, with hashes.

    python tools/inventory_bhavcopy.py <folder> [--out report.json]

It reads files (never writes, renames or deletes anything under <folder>), recognises two kinds:
  * UDiFF F&O bhavcopy, "BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv" or the same file zipped (*.csv.zip)
  * the NSE website's contract-wise historical download, "OPTIDX_NIFTY_{CE,PE}_<from>_TO_<to>*.csv"
and reports facts only: counts, date ranges, fields present, duplicate or conflicting copies, identifier stability. It makes no judgement about quality
beyond what the files themselves show, and it never invents a field. The only file it writes is the optional --out report.
Standard library only; no network.
"""
import argparse
import csv
import hashlib
import io
import json
import re
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

SCHEMA = "niftypaper.inventory/1"
UDIFF_NAME = re.compile(r"^BhavCopy_NSE_FO_0_0_0_(\d{8})_F_0000\.csv(\.zip)?$")
OPTIDX_NAME = re.compile(r"^OPTIDX_NIFTY_(CE|PE)_.*\.csv$", re.I)
UDIFF_COLUMNS = ("TradDt", "BizDt", "Sgmt", "Src", "FinInstrmTp", "FinInstrmId", "ISIN", "TckrSymb", "SctySrs", "XpryDt", "FininstrmActlXpryDt", "StrkPric",
                 "OptnTp", "FinInstrmNm", "OpnPric", "HghPric", "LwPric", "ClsPric", "LastPric", "PrvsClsgPric", "UndrlygPric", "SttlmPric", "OpnIntrst",
                 "ChngInOpnIntrst", "TtlTradgVol", "TtlTrfVal", "TtlNbOfTxsExctd", "SsnId", "NewBrdLotQty", "Rmks")
QUOTE_LIKE = ("bid", "ask", "offer", "bbp", "bsp")        # a column name containing one of these would be a quote; none of the known layouts has one
SKIP_DIRS = {"__pycache__", ".git"}


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _dec(text):
    try:
        value = Decimal(str(text).strip())
        return value if value.is_finite() else None
    except InvalidOperation:
        return None


def _iso(d):
    return d.isoformat() if d is not None else None


def read_payload(path):
    """(csv bytes, how) for a plain CSV or the single CSV inside a zip. Raises ValueError for a zip that does not hold exactly one file."""
    if path.name.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if not n.endswith("/")]
            if len(names) != 1:
                raise ValueError(f"zip holds {len(names)} files, expected 1")
            return archive.read(names[0]), "zip"
    return Path(path).read_bytes(), "csv"


def inspect_udiff(path, payload):
    """Facts about one UDiFF file's NIFTY index-option rows. `payload` is the CSV bytes."""
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
    columns = tuple(reader.fieldnames or ())
    rows = 0
    types = Counter()
    nifty = Counter()
    dates, expiries, lots, sessions, sources = set(), set(), set(), set(), set()
    strikes = []
    ids = defaultdict(set)
    positive = Counter()
    for row in reader:
        rows += 1
        types[row.get("FinInstrmTp", "")] += 1
        dates.add(row.get("TradDt", ""))
        if row.get("TckrSymb") != "NIFTY" or row.get("FinInstrmTp") != "IDO" or row.get("OptnTp") not in ("CE", "PE"):
            continue
        nifty["rows"] += 1
        nifty[row["OptnTp"]] += 1
        expiries.add(row.get("XpryDt", ""))
        lots.add(row.get("NewBrdLotQty", ""))
        sessions.add(row.get("SsnId", ""))
        sources.add(row.get("Src", ""))
        strike = _dec(row.get("StrkPric"))
        if strike is not None:
            strikes.append(strike)
        ids[(row.get("XpryDt"), row.get("StrkPric"), row.get("OptnTp"))].add(row.get("FinInstrmId"))
        for label, key in (("traded_volume", "TtlTradgVol"), ("traded_transactions", "TtlNbOfTxsExctd"), ("close_gt0", "ClsPric"), ("last_gt0", "LastPric"),
                           ("open_gt0", "OpnPric"), ("settle_gt0", "SttlmPric"), ("open_interest_gt0", "OpnIntrst")):
            value = _dec(row.get(key))
            if value is not None and value > 0:
                positive[label] += 1
        if _dec(row.get("ClsPric")) is not None and _dec(row.get("LastPric")) is not None and _dec(row["ClsPric"]) > 0 and _dec(row["LastPric"]) == 0:
            positive["close_without_last_trade"] += 1
    return {
        "rows": rows, "columns": list(columns), "has_expected_columns": all(c in columns for c in UDIFF_COLUMNS), "quote_like_columns": [c for c in columns if any(q in c.lower() for q in QUOTE_LIKE)],
        "trade_dates_inside": sorted(d for d in dates if d), "instrument_types": dict(types),
        "nifty_option_rows": nifty["rows"], "nifty_ce": nifty["CE"], "nifty_pe": nifty["PE"], "nifty_expiries": sorted(e for e in expiries if e),
        "nifty_strike_min": str(min(strikes)) if strikes else None, "nifty_strike_max": str(max(strikes)) if strikes else None,
        "nifty_lot_sizes": sorted(lots), "nifty_session_ids": sorted(sessions), "nifty_sources": sorted(sources), "nifty_rows_with": dict(positive),
        "_ids": {k: sorted(v) for k, v in ids.items()},
    }


def inspect_optidx(payload):
    """Facts about one contract-wise historical file (header names have stray spaces; '-' means no value)."""
    reader = csv.reader(io.StringIO(payload.decode("utf-8-sig")))
    header = [h.strip() for h in next(reader, [])]
    index = {h: i for i, h in enumerate(header)}
    dates, expiries, types = set(), set(), Counter()
    rows = 0
    dash = Counter()
    strikes = []
    for row in reader:
        if not row or len(row) != len(header):
            continue
        rows += 1
        try:
            dates.add(datetime.strptime(row[index["Date"]].strip(), "%d-%b-%Y").date())
            expiries.add(datetime.strptime(row[index["Expiry"]].strip(), "%d-%b-%Y").date())
        except (KeyError, ValueError):
            continue
        types[row[index["Option type"]].strip()] += 1
        value = _dec(row[index["Strike Price"]])
        if value is not None:
            strikes.append(value)
        for name in ("Open", "High", "Low", "Close", "LTP", "Settle Price", "No. of contracts", "Open Int"):
            if row[index[name]].strip() == "-":
                dash[name] += 1
    return {"rows": rows, "columns": header, "quote_like_columns": [c for c in header if any(q in c.lower() for q in QUOTE_LIKE)],
            "first_date": _iso(min(dates)) if dates else None, "last_date": _iso(max(dates)) if dates else None, "distinct_dates": len(dates),
            "expiry_min": _iso(min(expiries)) if expiries else None, "expiry_max": _iso(max(expiries)) if expiries else None, "option_types": dict(types),
            "strike_min": str(min(strikes)) if strikes else None, "strike_max": str(max(strikes)) if strikes else None, "rows_with_dash_as": dict(dash)}


def walk(root):
    for path in sorted(Path(root).rglob("*")):
        if path.is_file() and not (SKIP_DIRS & set(path.relative_to(root).parts)):
            yield path


def build_inventory(root, now=None):
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    udiff, optidx, ignored = [], [], 0
    ids = defaultdict(set)
    for path in walk(root):
        rel = path.relative_to(root).as_posix()
        match = UDIFF_NAME.match(path.name)
        if match:
            entry = {"path": rel, "name_date": _iso(datetime.strptime(match.group(1), "%Y%m%d").date()), "bytes": path.stat().st_size, "sha256_file": sha256_file(path)}
            try:
                payload, how = read_payload(path)
            except (ValueError, zipfile.BadZipFile, OSError) as error:
                entry.update({"form": "unreadable", "error": type(error).__name__})
                udiff.append(entry)
                continue
            facts = inspect_udiff(path, payload)
            for key, found in facts.pop("_ids").items():
                ids[key] |= {(i, entry["name_date"]) for i in found}
            entry.update({"form": how, "sha256_content": sha256_bytes(payload), **facts})
            udiff.append(entry)
        elif OPTIDX_NAME.match(path.name):
            payload = path.read_bytes()
            optidx.append({"path": rel, "bytes": len(payload), "sha256_file": sha256_bytes(payload), **inspect_optidx(payload)})
        else:
            ignored += 1
    return {"schema": SCHEMA, "generated_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "root_name": root.name, "ignored_files": ignored,
            "udiff": {"files": udiff, "summary": summarise_udiff(udiff, ids)}, "optidx": {"files": optidx, "summary": summarise_optidx(optidx)}}


def summarise_udiff(files, ids):
    readable = [f for f in files if f.get("form") in ("csv", "zip")]
    by_date = defaultdict(list)
    for f in readable:
        by_date[f["name_date"]].append(f)
    dates = sorted(by_date)
    conflicts = sorted(d for d, group in by_date.items() if len({g["sha256_content"] for g in group}) > 1)
    mismatched = sorted(f["path"] for f in readable if f["trade_dates_inside"] != [f["name_date"]])
    missing_weekdays = []
    if dates:
        day, last = date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])
        have = set(dates)
        while day <= last:
            if day.weekday() < 5 and day.isoformat() not in have:
                missing_weekdays.append(day.isoformat())
            day += timedelta(days=1)
    expiries = sorted({e for f in readable for e in f["nifty_expiries"]})
    unstable = sorted(str(k) for k, v in ids.items() if len({i for i, _ in v}) > 1)
    token_to_contract = defaultdict(set)
    for key, found in ids.items():
        for token, _ in found:
            token_to_contract[token].add(key)
    return {
        "file_count": len(files), "unreadable_files": [f["path"] for f in files if f.get("form") == "unreadable"], "distinct_trade_dates": len(dates),
        "first_date": dates[0] if dates else None, "last_date": dates[-1] if dates else None, "dates": dates,
        "dates_held_in_more_than_one_copy": sorted(d for d, g in by_date.items() if len(g) > 1), "dates_with_conflicting_copies": conflicts,
        "files_whose_inner_date_differs_from_name": mismatched, "missing_weekdays_between_first_and_last": missing_weekdays,
        "all_files_have_expected_columns": all(f["has_expected_columns"] for f in readable), "distinct_column_sets": len({tuple(f["columns"]) for f in readable}),
        "quote_like_columns_anywhere": sorted({c for f in readable for c in f["quote_like_columns"]}),
        "nifty_option_rows_total_in_distinct_dates": sum(group[0]["nifty_option_rows"] for group in by_date.values()),
        "nifty_expiries_seen": expiries, "nifty_expiries_count": len(expiries),
        "nifty_lot_sizes_seen": sorted({lot for f in readable for lot in f["nifty_lot_sizes"]}), "nifty_session_ids_seen": sorted({s for f in readable for s in f["nifty_session_ids"]}),
        "contracts_seen": len(ids), "contracts_with_more_than_one_token": unstable, "tokens_shared_by_more_than_one_contract": sorted(t for t, v in token_to_contract.items() if len(v) > 1),
    }


def summarise_optidx(files):
    if not files:
        return {"file_count": 0}
    return {"file_count": len(files), "first_date": min(f["first_date"] for f in files if f["first_date"]), "last_date": max(f["last_date"] for f in files if f["last_date"]),
            "rows_total": sum(f["rows"] for f in files), "quote_like_columns_anywhere": sorted({c for f in files for c in f["quote_like_columns"]})}


def compact(report):
    """A smaller report for keeping in a repository: per-file hashes and counts, but not the per-file column and expiry lists."""
    keep = ("path", "form", "name_date", "bytes", "sha256_file", "sha256_content", "rows", "nifty_option_rows", "error")
    udiff = {"files": [{k: f[k] for k in keep if k in f} for f in report["udiff"]["files"]], "summary": report["udiff"]["summary"]}
    drop = ("columns",)
    optidx = {"files": [{k: v for k, v in f.items() if k not in drop} for f in report["optidx"]["files"]], "summary": report["optidx"]["summary"]}
    return {**report, "udiff": udiff, "optidx": optidx, "compact": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only inventory of NSE derivatives files.")
    parser.add_argument("folder")
    parser.add_argument("--out", help="write the JSON report here (the only thing this program writes)")
    parser.add_argument("--compact", action="store_true", help="leave out per-file column and expiry lists")
    args = parser.parse_args(argv)
    root = Path(args.folder)
    if not root.is_dir():
        print(f"error: {root} is not a folder", file=sys.stderr)
        return 2
    if args.out:
        out = Path(args.out).resolve()
        if root.resolve() == out.parent or root.resolve() in out.parents:
            print("error: --out must be outside the folder being inventoried (this tool never writes into it)", file=sys.stderr)
            return 2
    report = build_inventory(root)
    if args.compact:
        report = compact(report)
    text = json.dumps(report, indent=1, sort_keys=True) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}: {report['udiff']['summary']['distinct_trade_dates']} UDiFF dates, {len(report['optidx']['files'])} contract-wise files")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
