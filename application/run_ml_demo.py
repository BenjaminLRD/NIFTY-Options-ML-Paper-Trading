"""One-command demonstration of the ML-to-dashboard integration on INVENTED fixture data (no network, no provider, no credentials, no market data):

    python run_ml_demo.py                 replay 40 invented days (or show the newest saved replay), then serve the dashboard
    python run_ml_demo.py --new           make another replay (run2, run3, ...); earlier ones are kept untouched
    python run_ml_demo.py --list          list saved replays and exit
    python run_ml_demo.py --run run1      show a specific saved replay
    options: --model MODEL_ID  --days N  --port N  --no-serve  --root DIR  --out-dir DIR  --state-dir DIR

Replays are saved as data/ml_replay/run1, run2, ... and are never overwritten or deleted. A run writes into <run>.partial and renames it only when finished, so an
interrupted run leaves no half result (the .partial folder is not a result; delete it yourself). Needs Python 3.12 or newer and nothing else installed.
Serves 127.0.0.1 only; stop with Ctrl+C. Exit codes: 0 ok, 1 cannot start the server, 2 bad request or refused, 3 the replay failed. See docs/ML_DEMO_GUIDE.md.
"""
import argparse
import sys
from pathlib import Path

if sys.version_info < (3, 12):
    sys.exit(f"error: Python 3.12 or newer is required (this is {sys.version.split()[0]})")

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from niftypaper.dashboard.__main__ import main as dashboard_main  # noqa: E402
from niftypaper.ml import runs, session  # noqa: E402


def _parser():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", default=str(session.DEFAULT_ROOT), help="folder holding run1, run2, ...")
    parser.add_argument("--out-dir", help="use this single folder instead of a numbered run (made if new; an existing replay there is shown)")
    parser.add_argument("--model", default="demo_synthetic_ur1", help="a registered model id; the research models have no parameter file here and cannot trade")
    parser.add_argument("--days", type=int, default=session.DEFAULT_DAYS, help=f"{session.MIN_DAYS} to {session.MAX_DAYS}")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-serve", action="store_true", help="run the replay and print the summary only")
    parser.add_argument("--new", action="store_true", help="make a new numbered replay even if one exists")
    parser.add_argument("--list", action="store_true", help="list saved replays and exit")
    parser.add_argument("--run", help="show this saved replay, for example run1")
    parser.add_argument("--state-dir", help="where the main dashboard keeps its virtual account (default: data/paper_state); use a temporary folder to demonstrate without touching your saved account")
    return parser


def _make(target, args):
    try:
        summary = session.run_replay(target, args.model, args.days)
    except session.SessionError as error:
        print(f"error: {error}", file=sys.stderr)
        return None, 2
    except KeyboardInterrupt:
        print(f"\ninterrupted: no replay was saved. {target}.partial is NOT a result; delete it yourself before running again.", file=sys.stderr)
        return None, 3
    except Exception as error:                      # a failure inside the replay: say so plainly, keep the partial folder as evidence, save nothing
        print(f"error: the replay failed ({error.__class__.__name__}: {error}). No replay was saved; {target}.partial is NOT a result.", file=sys.stderr)
        return None, 3
    h = summary["health"]
    print(f"Replay saved as {target.name} ({summary['data_label']}).")
    print(f"  model {summary['model_id']}; decisions: {summary['decisions']}; fills: {h['fills']}; audit: {h['audit_verdict']} / {h['audit_coverage']}")
    print(f"  performance on the invented series: {summary['performance']}")
    return summary, 0


def main(argv=None):
    args = _parser().parse_args(argv)
    root = Path(args.root)
    if args.list:
        found = runs.list_runs(root)
        print("\n".join(runs.describe(r) for r in found) if found else f"no saved replays in {root}")
        return 0
    if args.out_dir:
        target, serve_root = Path(args.out_dir), Path(args.out_dir)
        if not (target / "summary.json").exists():
            _, code = _make(target, args)
            if code:
                return code
        else:
            print(f"A replay already exists in {target}: showing it unchanged.")
        shown = ""
    else:
        found = runs.list_runs(root)
        good = [r for r in found if r["summary"] is not None]          # older or damaged runs are kept but cannot be shown
        if args.run:
            if args.run not in {r["id"] for r in found}:
                print(f"error: there is no replay named {args.run} in {root} (see --list)", file=sys.stderr)
                return 2
            shown = args.run
            print(f"Showing saved replay {shown} unchanged.")
        elif args.new or not good:
            target = runs.next_run_dir(root)
            _, code = _make(target, args)
            if code:
                return code
            shown = target.name
        else:
            shown = good[-1]["id"]
            print(f"Showing the newest saved replay, {shown}, unchanged (nothing is overwritten or deleted). Use --new for another, --list to see all.")
        serve_root = root
    if args.no_serve:
        return 0
    suffix = f"?run={shown}" if shown else ""
    print(f"\nML page:  http://127.0.0.1:{args.port}/ml{suffix}     (main dashboard: http://127.0.0.1:{args.port}/)")
    extra = ["--state-dir", args.state_dir] if args.state_dir else []
    return dashboard_main(["--port", str(args.port), "--ml-dir", str(serve_root)] + extra)


if __name__ == "__main__":
    sys.exit(main())
