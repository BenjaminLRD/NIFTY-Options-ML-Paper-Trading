"""Create or show an anchor record for a finished replay event log (stdlib only; explicit operations, never run by the audit).

  python tools/replay_anchor.py create --events events.jsonl --state replay_<source>.jsonl --out PATH [--declare-protected]
  python tools/replay_anchor.py show --anchor PATH

`create` verifies the event hash chain first and refuses a missing, invalid or unfinished log, a log whose source differs from the replay account's, an
existing destination (it never overwrites), and a destination in the same folder tree as the log or the state. It reads the log and state, writes only the
new anchor file, and takes no lock. The output always says what the location is worth: this program cannot make an anchor independent. An anchor is
independent only if it is held where the replay's writer cannot modify it, which YOU must arrange (see docs/HISTORICAL_REPLAY_AUDIT.md). --declare-protected
only changes the wording of the message to say you declared that; nothing is checked and nothing is stored in the anchor.

Exit codes: 0 done, 2 refused or unusable (the reason code is printed).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from niftypaper.historical import anchor  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--events", required=True)
    create.add_argument("--state", required=True)
    create.add_argument("--out", required=True)
    create.add_argument("--declare-protected", action="store_true", help="say in the message that you declare the destination protected (not verified)")
    show = sub.add_parser("show")
    show.add_argument("--anchor", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "show":
            record = anchor.parse(Path(args.anchor).read_bytes())
            print(anchor.canonical_line(record), end="")
            print("(read-only view of the record; compare it with a log using replay_audit.py --anchor)")
            return 0
        record = anchor.create(Path(args.events), Path(args.state), Path(args.out))
    except OSError as error:
        print(f"refused: {error.__class__.__name__}", file=sys.stderr)
        return 2
    except anchor.AnchorError as error:
        print(f"refused: {error.code}: {error.detail}", file=sys.stderr)
        return 2
    print(f"anchor created: {args.out}")
    print(f"  replay {record['account']}  events {record['events']}  head {record['head']}")
    print(f"  log sha256 {record['log_sha256']}  bytes {record['log_bytes']}")
    if args.declare_protected:
        print("  location: you declared this destination protected from the replay writer. That is your declaration; this program did not and cannot check it.")
    else:
        print("  location: LOCAL. This is NOT an independent trust anchor: anyone who can modify the log and this file can make them agree.")
        print("  To make it independent, copy it to a place the replay writer cannot modify, and audit with --anchor <that copy> --anchor-trust independent.")
    print("  Even a protected anchor shows only that the log is unchanged since now; it does not show the events are genuine.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
