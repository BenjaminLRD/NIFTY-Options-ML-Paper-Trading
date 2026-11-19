"""Create or verify a SHA-256 manifest for raw historical-data files (byte-level integrity only; stdlib only; reads files, writes only MANIFEST.jsonl).

  python tools/historical_manifest.py create --root DIR --source-id ID --origin-class SYNTHETIC_FIXTURE --origin "text" --file a.jsonl=QUOTE_SNAPSHOT ...
  python tools/historical_manifest.py verify --root DIR

`create` refuses to overwrite an existing manifest. `verify` exits 1 on any changed, missing or unlisted file. A matching hash does not show that the
content is genuine market data.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from niftypaper.historical import manifest as mf  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--root", required=True)
    create.add_argument("--source-id", required=True)
    create.add_argument("--origin-class", required=True)
    create.add_argument("--origin", required=True)
    create.add_argument("--retrieved-at-utc")
    create.add_argument("--file", action="append", required=True, metavar="PATH=KIND")
    sub.add_parser("verify").add_argument("--root", required=True)
    args = parser.parse_args(argv)
    root = Path(args.root)
    try:
        if args.command == "create":
            files = dict(item.split("=", 1) for item in args.file)
            entries = mf.describe(root, files, source_id=args.source_id, origin_class=args.origin_class, origin=args.origin, retrieved_at_utc=args.retrieved_at_utc)
            mf.write_manifest(root / mf.MANIFEST_NAME, entries)
            print(f"wrote {root / mf.MANIFEST_NAME} ({len(entries)} files)")
            return 0
        failures = mf.verify(root, mf.load_manifest(root / mf.MANIFEST_NAME))
    except (mf.ManifestError, OSError, ValueError) as error:
        print(f"manifest error: {error}", file=sys.stderr)
        return 2
    for failure in failures:
        print(f"{failure.code}: {failure.path}: {failure.detail}")
    print("OK: every listed file matches" if not failures else f"{len(failures)} integrity failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
