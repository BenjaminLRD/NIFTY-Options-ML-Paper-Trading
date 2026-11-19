"""Finding saved replays. A replay is a folder run1, run2, ... directly under a root folder (default data/ml_replay). Nothing here writes, renames or deletes."""
import json
import re
from pathlib import Path

RUN_ID = re.compile(r"^run([0-9]{1,4})$")
MAX_SUMMARY_BYTES = 8_000_000
SUMMARY_VERSION = 2
REQUIRED = ("data_label", "model_id", "models", "decisions", "reasons", "performance", "research", "equity", "fills", "refused", "simulator", "health", "limitations")


def run_number(name):
    match = RUN_ID.match(name)
    return int(match.group(1)) if match else None


def read_summary(folder):
    """(summary, problem): the saved summary of one run, checked to be labelled as invented fixture data, or (None, why)."""
    path = Path(folder) / "summary.json"
    try:
        if path.stat().st_size > MAX_SUMMARY_BYTES:
            return None, "the replay files are unexpectedly large"
        summary = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "no replay has been run here yet"
    except (OSError, ValueError):
        return None, "the saved replay could not be read"
    health = summary.get("health") if isinstance(summary, dict) else None
    if not isinstance(summary, dict) or not isinstance(health, dict):
        return None, "the saved replay is not in the expected format"
    if summary.get("summary_version") != SUMMARY_VERSION:
        return None, f"the saved replay was made by another version of the program (format {summary.get('summary_version')}, this one reads {SUMMARY_VERSION}); it is kept but not shown"
    if [k for k in REQUIRED if k not in summary]:
        return None, "the saved replay is not in the expected format"
    if health.get("data_origin") != "SYNTHETIC_FIXTURE" or health.get("provider_connected") is not False:
        return None, "the saved replay is not labelled as invented fixture data with no provider, so it is not shown"
    return summary, None


def list_runs(root):
    """Every run folder under `root`, oldest first: {id, path, summary or None, problem}. A folder with a leftover .partial name is not a run."""
    root = Path(root)
    try:
        names = sorted((n for n in (p.name for p in root.iterdir() if p.is_dir()) if run_number(n) is not None), key=run_number)
    except OSError:
        return []
    out = []
    for name in names:
        summary, problem = read_summary(root / name)
        out.append({"id": name, "path": root / name, "summary": summary, "problem": problem})
    return out


def next_run_dir(root):
    """The first unused run folder name after the highest existing one (an interrupted run's .partial folder counts as used)."""
    root = Path(root)
    highest = 0
    try:
        for p in root.iterdir():
            number = run_number(p.name[:-8] if p.name.endswith(".partial") else p.name)
            if number is not None:
                highest = max(highest, number)
    except OSError:
        pass
    return root / f"run{highest + 1}"


def describe(run):
    """One line for a listing."""
    s = run["summary"]
    if s is None:
        return f'{run["id"]}: unreadable ({run["problem"]})'
    h, d = s["health"], s["decisions"]
    return (f'{run["id"]}: model {s["model_id"]}, {h["decisions"]} decisions (BUY {d["BUY"]}, SELL {d["SELL"]}, NO TRADE {d["NO TRADE"]}), {h["fills"]} fills, '
            f'audit {h["audit_verdict"]}/{h["audit_coverage"]}')
