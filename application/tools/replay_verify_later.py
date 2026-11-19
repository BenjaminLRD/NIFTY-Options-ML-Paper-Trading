"""Strict "verify later" check of a saved fixture replay (stdlib only; READ-ONLY: it opens its inputs for reading, creates, repairs and deletes nothing, takes
no lock, and has no output option).

  python tools/replay_verify_later.py --events events.jsonl --state replay_<source>.jsonl --fixtures DIR
                                      (--anchor FILE | --expected-head HASH) [--anchor-trust independent] [--detail]

It runs the existing audit (historical/audit.py, the same code as tools/replay_audit.py) and requires ALL of:
  PLAN       the replay declared an intent plan and the events record exactly that plan
  CHAIN      the event hash chain is VALID
  ANCHOR     the log matches the anchor file and/or the expected head you supplied, which you DECLARE independent (--anchor-trust independent) and which is
             not beside the log, the state or the fixtures; one or the other must be supplied
  IDENTITY   the input identity recomputed from the fixtures equals the one the replay claimed, and the inputs were unchanged at every recheck
  PAIRING    the state file belongs to the same account and input binding as the events
  ACCOUNTING the books rebuild consistently with FULL coverage (every check PASS, none skipped or not checked)
Anything missing, malformed, mismatched or only partly covered is FAIL; nothing is downgraded to a weaker check.

The last line is exactly PASS or FAIL. Exit codes: 0 PASS, 1 FAIL (some requirement not met), 2 FAIL because the arguments or the audit itself were unusable.

PASS means the supplied files agree with each other and with the anchor/head you supplied. It does NOT show that the events or quotes are genuine, and it
cannot verify your declaration that the anchor/head is held where the log's writer cannot change it: that, and who made the original files, stay assumptions.
"""
import argparse
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from niftypaper.historical import anchor as anchor_module  # noqa: E402
from niftypaper.historical import audit  # noqa: E402
from niftypaper.historical import manifest as mf  # noqa: E402

HEX64 = re.compile(r"[0-9a-f]{64}")
ASSUMPTIONS = (
    "Not verified by this tool: that the anchor/expected head is really held where the log's writer cannot change it (only your declaration is used),",
    "who created the fixtures, log and state, and whether the original events or quotes were genuine. At most, consistency was shown.",
)


class UsageError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def _short(text, limit=240):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _failed_notes(report, wanted):
    out = []
    for check in report["checks"]:
        if check["id"] in wanted and check["status"] != "PASS":
            out.append(f"{check['id']} is {check['status']}: " + _short("; ".join(check["notes"]) or "no detail"))
    return out


def requirements(report, args):
    """[(name, [reasons it failed])] for a finished audit report; an empty list of reasons means the requirement was met."""
    identity, anchor, chain, pairing = report["inputs_identity"], report["anchor"], report["chain"], report["pairing"]
    plan = identity.get("plan", {"status": "NOT_DECLARED", "differences": []})
    rows = []

    reasons = []
    if plan["status"] != "MATCH":
        reasons.append(f"intent plan is {plan['status']}" + (": " + _short("; ".join(plan["differences"])) if plan["differences"] else ""))
    reasons += _failed_notes(report, {"PL1_PLAN_IDENTITY"}) if not reasons else []
    rows.append(("PLAN", reasons))

    reasons = [] if chain["status"] == "VALID" else [f"event chain is {chain['status']} ({chain.get('reason')}): {_short(chain.get('detail'))}"]
    rows.append(("CHAIN", reasons))

    reasons = []
    if anchor["status"] == "NOT_SUPPLIED":
        reasons.append("no anchor and no expected head was supplied")
    else:
        for name, part in sorted(anchor.get("parts", {}).items()):
            if part["status"] != "MATCH":
                reasons.append(f"{name} is {part['status']} ({part.get('reason')}): {_short(part.get('detail'))}")
            if part["protection"] != "DECLARED_INDEPENDENT":
                reasons.append(f"{name} is not declared independent, or sits beside the log or state, so it is only a local consistency check")
    rows.append(("ANCHOR", reasons))

    reasons = [] if identity["status"] == "MATCH" else [f"input identity is {identity['status']}: " + _short("; ".join(identity["differences"]) or "see I1_INPUT_IDENTITY")]
    reasons += _failed_notes(report, {"I1_INPUT_IDENTITY"}) if identity["status"] != "MATCH" or report["inputs_identity"]["completion_recheck"]["status"] != "UNCHANGED" else []
    if not reasons and identity["completion_recheck"]["status"] != "UNCHANGED":
        reasons.append(f"completion recheck is {identity['completion_recheck']['status']}")
    rows.append(("IDENTITY", reasons))

    reasons = [] if pairing["status"] == "PAIRED" else [f"state pairing is {pairing['status']}: " + _short("; ".join(pairing.get("reasons", [])) or pairing.get("reason", ""))]
    rows.append(("PAIRING", reasons))

    reasons = []
    if report["verdict"] != "CONSISTENT":
        reasons.append(f"accounting verdict is {report['verdict']}")
    if report["coverage"] != "FULL":
        reasons.append(f"coverage is {report['coverage']}")
    bad = [c for c in report["checks"] if c["status"] != "PASS"]
    if bad:
        reasons.append("checks not PASS: " + ", ".join(f"{c['id']}={c['status']}" for c in bad))
    rows.append(("ACCOUNTING", reasons))
    return rows


def preflight(args):
    problems = []
    if args.anchor_trust != "independent":
        problems.append("--anchor-trust independent is required: you must declare where the anchor/head is held (the tool cannot verify it)")
    if args.anchor is None and args.expected_head is None:
        problems.append("one of --anchor or --expected-head is required")
    if args.expected_head is not None and not HEX64.fullmatch(args.expected_head):
        problems.append("--expected-head must be 64 lowercase hex characters")
    for flag, path, kind in (("--events", args.events, "file"), ("--state", args.state, "file"), ("--anchor", args.anchor, "file"), ("--fixtures", args.fixtures, "dir")):
        if path is not None and not (Path(path).is_file() if kind == "file" else Path(path).is_dir()):
            problems.append(f"{flag} {'file' if kind == 'file' else 'folder'} does not exist")
    if args.anchor is not None and not problems and anchor_module.beside(Path(args.anchor), Path(args.fixtures) / mf.MANIFEST_NAME):
        problems.append("the anchor sits beside or inside the fixture folder, so it is not held apart from what it protects")
    return problems


def main(argv=None):
    parser = Parser(description=__doc__.split("\n")[0])
    parser.add_argument("--events", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--fixtures", required=True)
    parser.add_argument("--anchor")
    parser.add_argument("--expected-head")
    parser.add_argument("--anchor-trust", choices=("local", "independent"), default="local")
    parser.add_argument("--detail", action="store_true", help="also print the audit's text report (no state contents)")
    try:
        args = parser.parse_args(argv)
    except UsageError as error:
        print(f"unusable arguments: {error}")
        print("FAIL")
        return 2
    problems = preflight(args)
    if problems:
        print("Replay strict verification: arguments not acceptable")
        for problem in problems:
            print(f"  - {problem}")
        print("FAIL")
        return 2
    try:
        report = audit.build_report(audit.AuditConfig(Path(args.events), Path(args.state), Path(args.fixtures), expected_head=args.expected_head,
                                                      anchor_path=Path(args.anchor) if args.anchor else None, anchor_trust="independent"))
        rows = requirements(report, args)
    except Exception as error:                                           # the audit must not crash into a PASS or a traceback: any surprise is a FAIL
        print(f"the audit could not be completed ({error.__class__.__name__})")
        print("FAIL")
        return 2
    print("Replay strict verification (read-only; nothing was created, repaired or deleted)")
    print(f"  audit verdict {report['verdict']}, coverage {report['coverage']}, chain {report['chain']['status']}, anchor {report['anchor']['status']}, "
          f"plan {report['inputs_identity'].get('plan', {}).get('status')}, pairing {report['pairing']['status']}, report digest {report['digest']}")
    for name, reasons in rows:
        print(f"  {'ok  ' if not reasons else 'FAIL'} {name}")
        for reason in reasons:
            print(f"         - {reason}")
    if args.detail:
        print()
        print(audit.render_text(report))
    print("Evidence checked: " + ", ".join(name for name, _ in rows) + ". Anchor/head protection is YOUR declaration (independent), not verified.")
    for line in ASSUMPTIONS:
        print("  " + line)
    failed = [name for name, reasons in rows if reasons]
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
