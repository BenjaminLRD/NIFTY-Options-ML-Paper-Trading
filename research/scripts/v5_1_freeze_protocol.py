"""ML Options Paper Trading V5.1 - freeze the evaluation protocol before any new-window outcome is examined.

`freeze(project)` hashes (SHA-256) the protocol text and every project-owned file that can affect the evaluation, and writes
V5_1_Frozen_Protocol.json. `verify(project, expected_combined=None)` recomputes every hash from the files on disk, rechecks the stored
record against them and against the protocol text in this module, recomputes the combined hash itself, and raises on any difference.

This module only reads bytes (hashlib) and the file names in the download log. It imports no data library and never parses the
raw data, so it cannot compute a return, direction, label, eligible-row count or metric.

Limit: a verifier cannot protect itself. This file is in the manifest, but someone who edits it AND re-runs freeze() produces a
self-consistent record. Keep the combined hash that freeze() prints somewhere outside the project and pass it as expected_combined.

Usage: python v5_1_freeze_protocol.py PROJECT_DIR [--verify] [--expected-combined HASH]
"""
import csv
import glob
import hashlib
import json
import platform
import sys
from pathlib import Path

RECORD = "V5_1_Frozen_Protocol.json"
RAW_DIR = "V5_1_Acquired_Raw"
DOWNLOAD_LOG = "V5_1_Download_Log.csv"

PROTOCOL = {
    "version": "V5.1",
    "frozen_before_any_evaluation_outcome_was_examined": True,
    "training_data": "contracts and dates of V5_0_Study_Dataset.csv; eligible rows whose feature date AND label date are on or before 2026-06-29 (labels crossing the boundary are purged)",
    "evaluation_window": {"first_feature_date": "2026-06-30", "last_target_date": "2026-09-29",
                          "contracts": "NIFTY index options, expiry 2026-09-29, the strike grid of V5_0_Normalized_Study_Universe.csv",
                          "source": "official NSE daily F&O bhavcopy, V5_1_Acquired_Raw/*.csv.zip (single origin)",
                          "calendar": "trading days are the dates present in the data; 2026-09-14 is absent (NSE holiday, confirmed from secondary sources, not from the NSE circular)"},
    "option_types": "CE and PE evaluated separately; no pooling",
    "primary_hypothesis": "The relationship between lagged option settlement return (lr1) and the next observed settlement direction is stable across chronological periods",
    "secondary_hypothesis": "Same for the lagged underlying return (ur1)",
    "features": "lr1 and ur1 exactly as computed by v5_0_stability_study.prepare, computed on the full series BEFORE eligibility filtering",
    "label": "target_next_up = 1 if the next consecutive trading day's settlement is above the settlement at t (v4_7_model_dataset.build_features)",
    "eligibility": "rules R1-R5 of v5_0_stability_study.prepare (price floor settle >= 1.0 at t, t-1, t-2), unchanged",
    "models": "B1 = single-feature logistic on lr1; B2 = single-feature logistic on ur1 (V4.8 definition, L2 lambda 1); fitted ONCE on the training data per option type, no refit, no tuning, no sign reversal",
    "baseline": "training-rate prior (fraction of up labels in the training data, per option type)",
    "metric": "pooled Brier score difference (model minus prior) over evaluation rows; lower is better",
    "uncertainty": "moving-block bootstrap over distinct evaluation DATES, block length 5 dates, 2000 resamples, seed 20260630; Bonferroni-adjusted 97.5% percentile interval (2 features)",
    "success_criterion": "the 97.5% interval lies entirely below 0 for that option type and feature; anything else is NOT SUPPORTED",
    "stability_test": "v5_0_stability_study.heterogeneity (date-permutation, 2000 permutations, seed 500) over training blocks 0-5 plus evaluation blocks of 30 dates with the remainder merged into the last block; reject at 0.05/2; blocks with fewer than 15 eligible dates excluded and listed",
    "reporting": "rows and distinct dates for every table; CE and PE separately",
    "one_shot": "the evaluation refuses to run if its outputs exist; the window cannot be re-evaluated after a revision made from its results",
    "not_claimed": "executable profitability. Settlement is not an executable price; no spreads, fees or slippage are modelled; no orders",
    "change_policy": "any change to this file or to a hashed file after the freeze invalidates the window as an independent test",
}
# Every project-owned file that affects the evaluation: the evaluation script, every module it imports (directly or through V5.0),
# its inputs, the hypothesis table, the tests and this verifier.
CODE = ["v4_7_model_dataset.py", "v4_7_source_audit.py", "v4_8_feature_research.py", "v4_9_feature_diagnostics.py",
        "v5_0_data_expansion.py", "v5_0_stability_study.py", "v5_1_acquisition_audit.py", "v5_1_evaluation.py", "v5_1_freeze_protocol.py",
        "test_v5_0.py", "test_v5_1.py",
        "V5_0_Hypotheses.csv", "V5_0_Study_Dataset.csv", "V5_0_Normalized_Study_Universe.csv",
        DOWNLOAD_LOG, "V5_1_Raw_Integrity_Report.csv", "V5_1_Normalized_Bhavcopy_NIFTY_Options.csv"]


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def hashes(project):
    """SHA-256 of every manifest file that exists; a missing file is simply absent (verify reports it)."""
    p = Path(project)
    out = {n: sha256(p / n) for n in CODE if (p / n).is_file()}
    for f in sorted(glob.glob(str(p / RAW_DIR / "*.zip"))):
        out[f"{RAW_DIR}/{Path(f).name}"] = sha256(f)
    return out


def protocol_hash(protocol):
    return hashlib.sha256(json.dumps(protocol, sort_keys=True).encode()).hexdigest()


def combined(file_hashes, proto_hash):
    return hashlib.sha256("\n".join(f"{k}:{v}" for k, v in sorted({**file_hashes, "protocol": proto_hash}.items())).encode()).hexdigest()


def logged_downloads(project):
    """File names the download log says were downloaded (reads names and status only)."""
    with open(Path(project) / DOWNLOAD_LOG, newline="", encoding="utf-8") as f:
        return {r["file"] for r in csv.DictReader(f) if r.get("status") in ("downloaded", "already_present")}


def freeze(project):
    h = hashes(project)
    missing = [n for n in CODE if n not in h]
    if missing:
        raise FileNotFoundError("cannot freeze, manifest files missing: " + ", ".join(missing))
    ph = protocol_hash(PROTOCOL)
    rec = {"protocol": PROTOCOL, "protocol_sha256": ph, "file_sha256": h, "combined_sha256": combined(h, ph),
           "environment_informational": {"python": platform.python_version(), "platform": platform.platform()}}
    (Path(project) / RECORD).write_text(json.dumps(rec, indent=1, sort_keys=True), encoding="utf-8")
    return rec


def verify(project, expected_combined=None):
    """Raise RuntimeError unless the project matches the frozen record exactly. Returns the recomputed combined hash."""
    rec = json.loads((Path(project) / RECORD).read_text(encoding="utf-8"))
    problems = []
    if rec.get("protocol") != PROTOCOL:
        problems.append("stored protocol text differs from the protocol in v5_1_freeze_protocol.py")
    ph = protocol_hash(rec.get("protocol"))
    if ph != rec.get("protocol_sha256"):
        problems.append("stored protocol hash does not match the stored protocol text")
    stored, now = rec.get("file_sha256", {}), hashes(project)
    for n in CODE:
        if n not in stored:
            problems.append(f"required file not in the frozen record: {n}")
        if n not in now:
            problems.append(f"required file missing on disk: {n}")
    for k in sorted(set(stored) | set(now)):
        if k in stored and k in now and stored[k] != now[k]:
            problems.append(f"changed after freeze: {k}")
        elif k in now and k not in stored:
            problems.append(f"added after freeze: {k}")
        elif k in stored and k not in now:
            problems.append(f"removed after freeze: {k}")
    try:
        logged = logged_downloads(project)
        on_disk = {k.split("/", 1)[1] for k in now if k.startswith(RAW_DIR + "/")}
        if logged != on_disk:
            problems.append(f"raw zips differ from the download log: log-only {sorted(logged - on_disk)}, disk-only {sorted(on_disk - logged)}")
    except (OSError, KeyError) as e:
        problems.append(f"download log unreadable: {e}")
    recomputed = combined(now, protocol_hash(PROTOCOL))
    if combined(stored, ph) != rec.get("combined_sha256"):
        problems.append("stored combined hash does not equal the combined hash of the stored protocol and file hashes")
    if recomputed != rec.get("combined_sha256"):
        problems.append("recomputed combined hash differs from the stored combined hash")
    if expected_combined is not None and recomputed != expected_combined:
        problems.append("recomputed combined hash differs from the externally supplied expected hash")
    if problems:
        raise RuntimeError("frozen protocol violated:\n  " + "\n  ".join(problems))
    return recomputed


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    proj = a[0] if a else "."
    exp = sys.argv[sys.argv.index("--expected-combined") + 1] if "--expected-combined" in sys.argv else None
    if exp in a:
        a.remove(exp)
        proj = a[0] if a else "."
    if "--verify" in sys.argv:
        print("VERIFIED", verify(proj, exp))
    else:
        print("FROZEN", freeze(proj)["combined_sha256"])
