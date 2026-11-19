"""Read-only strategy evaluation of an audited fixture replay (stdlib only; reads the inputs, writes only the optional --out file).

  python tools/strategy_evaluate.py --events events.jsonl --state replay_<source>.jsonl --fixtures DIR [--anchor FILE] [--expected-head HASH]
                                    [--anchor-trust local|independent] [--strategy index_sma_trend] [--strategy-version 1.0.0] [--out report.json]

It runs the existing audit first and refuses (no metrics) unless the audit is CONSISTENT with FULL coverage, the chain is VALID, the state is paired, the
input identity and declared intent plan match, and any anchor supplied matches. See docs/STRATEGY_EVALUATION.md. Results are SIMULATED and say nothing about
real profitability; the report digest gives reproducibility, not authenticity.

Exit codes: 0 evaluated; 1 refused (reasons printed, no metrics); 2 unusable arguments or an unwritable --out. --out refuses to overwrite a file and refuses
to write over or inside an input.
"""
import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from niftypaper.evaluation import evaluate as evaluation  # noqa: E402
from niftypaper.historical import audit  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--events", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--fixtures", required=True)
    parser.add_argument("--anchor")
    parser.add_argument("--expected-head")
    parser.add_argument("--anchor-trust", choices=("local", "independent"), default="local")
    parser.add_argument("--strategy", default="index_sma_trend", help="the strategy name you declare produced the plan (not recorded in the replay)")
    parser.add_argument("--strategy-version", default="1.0.0")
    parser.add_argument("--out", help="write the JSON report to this NEW file instead of printing it")
    args = parser.parse_args(argv)
    inputs = [Path(args.events).resolve(), Path(args.state).resolve(), Path(args.fixtures).resolve()] + ([Path(args.anchor).resolve()] if args.anchor else [])
    out = Path(args.out).resolve() if args.out else None
    if out is not None and any(out == p or p in out.parents for p in inputs):
        print("refusing to write the report over or inside an input", file=sys.stderr)
        return 2
    if out is not None and (out.exists() or out.is_symlink()):
        print("refusing to overwrite an existing file", file=sys.stderr)
        return 2
    config = audit.AuditConfig(Path(args.events), Path(args.state), Path(args.fixtures), expected_head=args.expected_head,
                               anchor_path=Path(args.anchor) if args.anchor else None, anchor_trust=args.anchor_trust)
    report = evaluation.evaluate(config, args.strategy, args.strategy_version)
    text = json.dumps(report, sort_keys=True, indent=2) + "\n"
    if out is not None:
        try:
            with open(out, "xb") as handle:
                handle.write(text.encode("utf-8"))
        except OSError as error:
            print(f"cannot write the report: {error.__class__.__name__}", file=sys.stderr)
            return 2
    else:
        sys.stdout.write(text)
    summary = f"{report['status']} (SIMULATED) report digest {report['digest']}"
    if report["status"] == "REFUSED":
        summary += "; " + "; ".join(report["refusal_reasons"])
    print(summary if out is not None else "# " + summary, file=sys.stdout if out is not None else sys.stderr)
    return 0 if report["status"] == "EVALUATED" else 1


if __name__ == "__main__":
    sys.exit(main())
