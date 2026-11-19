"""Read-only audit report for a fixture replay (stdlib only; reads the inputs, writes only the optional --out file, never takes the state file's lock).

  python tools/replay_audit.py --events events.jsonl --state replay_<source>.jsonl [--fixtures DIR] [--out report.json] [--text]
                               [--expected-head HASH] [--anchor FILE] [--anchor-trust local|independent]
                               [--require-valid-chain] [--require-anchor]

Exit codes: 0 CONSISTENT, 1 INCONSISTENT, 2 NOT_RECONCILABLE or unusable arguments; 3 only with --require-valid-chain, when the accounting verdict is
CONSISTENT but the event hash chain is not VALID. CONSISTENT means the files agree with each other and with the stated rules; it is not proof that they are
genuine. The report's `chain` section (VALID, INVALID or UNAVAILABLE) is separate from the verdict and never changes it. --expected-head is the head hash
returned when the log was written; it lets the chain detect a log cut at an event boundary. --out refuses to overwrite a file and refuses to write
over or inside an input.

Anchors (see src/niftypaper/historical/anchor.py for the trust model): --anchor FILE compares the log with an anchor record made earlier by
tools/replay_anchor.py; --expected-head compares only the head hash. Both are reported in the `anchor` section, apart from the chain, the accounting
verdict and authenticity (which is never established). --anchor-trust defaults to `local`: the comparison is then a consistency check, NOT an independent
trust anchor. `independent` is YOUR declaration that the anchor/head is held where the log's writer cannot change it; the audit cannot verify that, and
downgrades it when the anchor file sits beside the log or the replay state. --require-anchor exits 3 unless the anchor comparison is a MATCH.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from niftypaper.historical import audit  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--events", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--fixtures")
    parser.add_argument("--out")
    parser.add_argument("--text", action="store_true", help="print a short text view instead of the JSON")
    parser.add_argument("--expected-head", help="64-hex head hash recorded when the event log was written")
    parser.add_argument("--anchor", help="an anchor record file made by tools/replay_anchor.py")
    parser.add_argument("--anchor-trust", choices=("local", "independent"), default="local",
                        help="your declaration about the anchor/expected head: 'independent' only if the log's writer cannot modify it (not verified)")
    parser.add_argument("--require-anchor", action="store_true", help="exit 3 instead of 0 or 1 unless the anchor comparison is a MATCH")
    parser.add_argument("--require-valid-chain", action="store_true", help="exit 3 instead of 0 unless the event hash chain is VALID")
    args = parser.parse_args(argv)
    config = audit.AuditConfig(Path(args.events), Path(args.state), Path(args.fixtures) if args.fixtures else None, expected_head=args.expected_head,
                                 anchor_path=Path(args.anchor) if args.anchor else None, anchor_trust=args.anchor_trust)
    report = audit.build_report(config)
    if args.out:
        out = Path(args.out).resolve()
        inputs = ([Path(args.events).resolve(), Path(args.state).resolve()] + ([Path(args.fixtures).resolve()] if args.fixtures else [])
                  + ([Path(args.anchor).resolve()] if args.anchor else []))
        if any(out == p or p in out.parents for p in inputs):
            print("refusing to write the report over or inside an input", file=sys.stderr)
            return 2
        try:
            with open(out, "xb") as handle:
                handle.write((json.dumps(report, sort_keys=True, indent=2) + "\n").encode("utf-8"))
        except OSError as error:
            print(f"cannot write the report: {error.__class__.__name__}", file=sys.stderr)
            return 2
    print(audit.render_text(report) if args.text or not args.out else
          f"verdict {report['verdict']} coverage {report['coverage']} chain {report['chain']['status']} anchor {report['anchor']['status']}"
          + (f" ({'declared independent, not verified' if report['anchor']['protection'] == 'DECLARED_INDEPENDENT' else 'LOCAL: not an independent anchor'})"
             if report["anchor"]["status"] != "NOT_SUPPLIED" else "") + f" authenticity NOT_ESTABLISHED digest {report['digest']}")
    code = {"CONSISTENT": 0, "INCONSISTENT": 1}.get(report["verdict"], 2)
    if code in (0, 1) and args.require_anchor and report["anchor"]["status"] != "MATCH":
        return 3
    return 3 if code == 0 and args.require_valid_chain and report["chain"]["status"] != "VALID" else code


if __name__ == "__main__":
    sys.exit(main())
