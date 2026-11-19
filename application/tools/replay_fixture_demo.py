"""One-command demonstration of the replay workflow on the INVENTED fixtures (stdlib only; no network, no market data, no credentials).

  python tools/replay_fixture_demo.py [--out-dir DIR]

It runs the fixed, scripted fixture replay (tests/fixtures/historical_synthetic, source "synthetic_fixture"), publishes the hash-chained event log with the
same never-overwrite writer the runner uses, then audits what it wrote with the read-only audit. Only if every check passes does it print a success
summary and the PowerShell commands to create an anchor and audit the replay later. It does NOT create an anchor: that stays a deliberate, separate step.

Output goes to DIR (default: data/replay_demo/run1 under the repository; data/ is git-ignored and nothing here touches the research folders):
  DIR/events.jsonl                          the event log
  DIR/state/replay_synthetic_fixture.jsonl  the replay account's saved trades (what the audit reconciles the log against)
Nothing is overwritten and nothing is deleted: if DIR/events.jsonl or a non-empty DIR/state exists the script stops and suggests another --out-dir. The
configuration (cut-off, clock, script, fixtures) is fixed, so runs written to separate folders have identical event bytes and head hash.

Exit codes: 0 replay done, log published and every check passed; 2 refused before doing anything (existing output, bad folder, unexpected fixture source);
3 the replay or the write failed (evidence is left as it is); 4 the log was written but a check failed (the files are kept and nothing is repaired; no
success message and no commands are printed).
"""
import argparse
import hashlib
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from niftypaper.historical import anchor, audit, replay  # noqa: E402
from niftypaper.historical import manifest as mf  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
FIXTURE_DIR = REPO / "tests" / "fixtures" / "historical_synthetic"
DEFAULT_OUT_DIR = REPO / "data" / "replay_demo" / "run1"
EXPECTED_SOURCE = "synthetic_fixture"
EXPECTED_ACCOUNT = "replay_synthetic_fixture"
STATE_NAME = f"{EXPECTED_ACCOUNT}.jsonl"
AS_OF = datetime(2031, 3, 10, 12, 0, tzinfo=IST)
START = datetime(2031, 3, 3, 10, 14, tzinfo=IST)
END = datetime(2031, 3, 3, 10, 30, tzinfo=IST)
CE = "NIFTY|2031-03-27|25000.00|CE"
PE = "NIFTY|2031-03-27|24000.00|PE"


def _at(h, m, s=0):
    return datetime(2031, 3, 3, h, m, s, tzinfo=IST)


def script():
    """The fixed scripted intents (no strategy): two buys, a stale-quote refusal, a partial and a final exit, two bridge refusals."""
    return [
        replay.Intent(_at(10, 15, 2), PE, "BUY", 1),
        replay.Intent(_at(10, 15, 31), CE, "BUY", 2),
        replay.Intent(_at(10, 15, 31), PE, "SELL", 1),     # refused at the engine: that quote is 31 s old
        replay.Intent(_at(10, 15, 32), CE, "SELL", 1),     # partial exit
        replay.Intent(_at(10, 15, 33), CE, "SELL", 1),     # final exit
        replay.Intent(_at(10, 16, 2), CE, "BUY", 1),       # refused at the bridge: the newest quote has a receive time, not a quote time
        replay.Intent(_at(10, 20), "NIFTY", "BUY", 1),     # refused at the bridge: an index has no quote
    ]


class Refused(Exception):
    """Stopped before anything was written."""


class Failed(Exception):
    """The replay or the write failed."""


class NotVerified(Exception):
    """The log was written but a check failed."""

    def __init__(self, problems):
        super().__init__("; ".join(problems))
        self.problems = problems


def ps_quote(text):
    """A PowerShell single-quoted string: nothing inside is expanded; a quote is doubled."""
    return "'" + str(text).replace("'", "''") + "'"


def _suggest(out_dir):
    match = re.fullmatch(r"(.*?)(\d+)", out_dir.name)
    name = f"{match.group(1)}{int(match.group(2)) + 1}" if match else f"{out_dir.name}_2"
    return out_dir.with_name(name)


def preflight(out_dir):
    """Checks that need no writing. Raises Refused with a message that says what to do."""
    try:
        entries = mf.load_manifest(FIXTURE_DIR / mf.MANIFEST_NAME)
    except mf.ManifestError as error:
        raise Refused(f"the fixture manifest cannot be used: {error}") from None
    if {e.source_id for e in entries} != {EXPECTED_SOURCE} or any(e.origin_class != "SYNTHETIC_FIXTURE" for e in entries):
        raise Refused(f"the fixtures are not the invented '{EXPECTED_SOURCE}' set this demonstration is for")
    if out_dir.is_file():
        raise Refused(f"{out_dir} is a file, not a folder. Choose another --out-dir, for example {_suggest(out_dir)}")
    if out_dir == FIXTURE_DIR or FIXTURE_DIR in out_dir.parents or out_dir in FIXTURE_DIR.parents:
        raise Refused("the output folder may not be, contain or sit inside the fixture folder. Choose another --out-dir")
    events, state = out_dir / "events.jsonl", out_dir / "state"
    if events.exists() or events.is_symlink():
        raise Refused(f"{events} already exists and is never overwritten. Use a different folder, for example --out-dir {_suggest(out_dir)}")
    if state.exists() and (state.is_file() or any(state.iterdir())):
        raise Refused(f"{state} already holds files from an earlier run and is never reused or deleted. Use a different folder, for example --out-dir {_suggest(out_dir)}")


def run_replay(out_dir):
    """Run the fixed replay and publish its event log. Returns the writer's receipt and the input identity digest the replay claimed."""
    config = replay.ReplayConfig(fixture_dir=FIXTURE_DIR, state_dir=out_dir / "state", as_of=AS_OF, start=START, end=END, permitted=True)
    try:
        runner = replay.ReplayRunner.open(config, intents=script())
    except replay.ReplayRefused as error:
        raise Failed(f"the replay could not start: {error}") from None
    try:
        if runner.source_id != EXPECTED_SOURCE:
            raise Failed(f"the replay account is for source {runner.source_id!r}, not {EXPECTED_SOURCE!r}")
        runner.run(script())
        return runner.write_events(out_dir / "events.jsonl"), runner.inputs["digest"]
    except replay.InputsChanged as error:
        raise NotVerified([f"the fixtures or manifest changed while the replay ran ({'; '.join(error.differences)}); the log was published as evidence but the run is not completed"]) from None
    except (replay.ReplayRefused, replay.EventLogError, OSError) as error:
        raise Failed(f"the replay or the event log write failed: {error}") from None
    finally:
        runner.close()


def verify(out_dir, receipt, identity_digest):
    """Audit what is on disk and compare it with the writer's receipt and the replay's claimed input identity. Returns the report; raises NotVerified with every
    problem found."""
    events, state = out_dir / "events.jsonl", out_dir / "state" / STATE_NAME
    report = audit.build_report(audit.AuditConfig(events, state, FIXTURE_DIR))
    problems = []
    chain = report["chain"]
    if chain["status"] != "VALID":
        problems.append(f"event chain is {chain['status']} ({chain['reason']}: {chain['detail']})")
    if report["verdict"] != "CONSISTENT" or report["coverage"] != "FULL":
        failing = [f"{c['id']} {c['status']}" for c in report["checks"] if c["status"] != "PASS"]
        problems.append(f"accounting reconciliation is {report['verdict']} with {report['coverage']} coverage ({', '.join(failing) or 'no failing check listed'})")
    if report["account"]["name"] != EXPECTED_ACCOUNT or report["account"]["source_id"] != EXPECTED_SOURCE:
        problems.append(f"the report names account {report['account']['name']!r} and source {report['account']['source_id']!r}, expected {EXPECTED_ACCOUNT!r} and {EXPECTED_SOURCE!r}")
    identity = report["inputs_identity"]
    if identity["status"] != "MATCH" or report["pairing"]["status"] != "PAIRED" or identity["plan"]["status"] != "MATCH":
        problems.append(f"input identity is {identity['status']}, the intent plan is {identity['plan']['status']} and the state pairing is {report['pairing']['status']} "
                        f"({'; '.join(identity['differences'] + identity['plan']['differences'] + report['pairing'].get('reasons', [])) or 'see the audit report'})")
    elif identity["claimed"]["digest"] != identity_digest or identity["completion_recheck"]["status"] != "UNCHANGED":
        problems.append("the audit's view of the input identity or of the completion recheck differs from what the replay reported")
    if chain.get("events_verified") != receipt.events:
        problems.append(f"the chain verifies {chain.get('events_verified')} events but the writer reported {receipt.events}")
    if chain.get("head") != receipt.head:
        problems.append("the chain's head hash differs from the writer's receipt")
    try:
        data = events.read_bytes()
    except OSError:
        data = b""
    if hashlib.sha256(data).hexdigest() != receipt.sha256 or len(data) != receipt.bytes:
        problems.append("the file on disk differs from what the writer reported publishing")
    if problems:
        raise NotVerified(problems)
    return report


def render_success(out_dir, receipt, report):
    claimed = report["inputs_identity"]["claimed"]
    events, state = out_dir / "events.jsonl", out_dir / "state" / STATE_NAME
    anchor_dir = out_dir.with_name(out_dir.name + "_anchor")
    suggested = anchor_dir / f"{out_dir.name}.anchor.json"
    assert not anchor.beside(suggested, events, state)             # the suggested place is, by construction, apart from the log and the state
    lines = [
        "Synthetic fixture replay finished and verified (invented data only; not market data)",
        f"  source        {EXPECTED_SOURCE}   account {EXPECTED_ACCOUNT}",
        f"  event count   {receipt.events}",
        f"  head hash     {receipt.head}",
        f"  event log     {events}",
        f"  log sha256    {receipt.sha256}   ({receipt.bytes} bytes)",
        f"  input identity {claimed['digest']}   ({len(claimed['fixtures'])} fixture files; manifest sha256 {claimed['manifest_sha256']})",
        f"  chain         VALID ({report['chain']['events_verified']} events)",
        f"  inputs        MATCH (recomputed from the fixtures; unchanged at completion)   state pairing {report['pairing']['status']}",
        f"  intent plan   MATCH ({report['inputs_identity']['plan']['observed_intents']} intents, digest {report['inputs_identity']['plan']['claimed_digest']})",
        f"  accounting    {report['verdict']} (coverage {report['coverage']}); rebuilt cash {report['summary']['rebuilt_cash']}",
        f"  audit digest  {report['digest']}",
        "",
        "What this shows: the replay ran to the end, the log was published without replacing anything, its hash chain is intact, the books rebuild from the",
        "saved trades, and the head hash and event count match what the writer reported. What it does NOT show: that the events are genuine, or that nothing was",
        "changed by someone who can rewrite every file. No anchor has been created. Write down the head hash above where this folder's writer cannot edit it.",
        "",
        "Next steps. Nothing below has been run for you. Paste into PowerShell (it keeps the variables):",
        "",
        f"Set-Location {ps_quote(REPO)}",
        f"$Events   = {ps_quote(events)}",
        f"$State    = {ps_quote(state)}",
        f"$Fixtures = {ps_quote(FIXTURE_DIR)}",
        f"$Head     = {ps_quote(receipt.head)}",
        f"$Anchor   = {ps_quote(suggested)}",
        "",
        "# 1. Create the anchor (a deliberate step; refuses to overwrite, and refuses a place beside the log or the state)",
        "python tools\\replay_anchor.py create --events $Events --state $State --out $Anchor",
        "",
        "# 2. Inspect it (read-only)",
        "python tools\\replay_anchor.py show --anchor $Anchor",
        "",
        "# 3. Audit the log against it. This anchor is LOCAL: anyone who can rewrite the log can rewrite it too, so a match is a consistency check only.",
        "python tools\\replay_audit.py --events $Events --state $State --fixtures $Fixtures --anchor $Anchor --text",
        "",
        "# 4. To make it count, copy the anchor (and keep $Head) somewhere the replay writer cannot modify, such as a write-protected USB stick, another user's",
        "#    folder or paper. Then set $Protected to that copy and audit later with every requirement switched on. --anchor-trust independent is YOUR",
        "#    declaration; the audit cannot check it. Exit code 3 means the chain or the anchor did not pass.",
        "$Protected = 'E:\\replay\\" + out_dir.name + ".anchor.json'   # CHANGE THIS to your protected copy",
        "python tools\\replay_audit.py --events $Events --state $State --fixtures $Fixtures --anchor $Protected --anchor-trust independent --expected-head $Head --require-valid-chain --require-anchor --text",
        "",
        "Exit codes of the audit: 0 consistent, 1 inconsistent, 2 not reconcilable, 3 a required chain or anchor check failed. Nothing is ever repaired for you.",
    ]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out-dir", help=f"output folder (default: {DEFAULT_OUT_DIR}); must not already hold an event log or saved state")
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir if args.out_dir else DEFAULT_OUT_DIR).resolve()
    try:
        preflight(out_dir)
        receipt, identity_digest = run_replay(out_dir)
        report = verify(out_dir, receipt, identity_digest)
    except Refused as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    except Failed as error:
        print(f"FAILED: {error}", file=sys.stderr)
        print(f"Anything already written is left as it is in {out_dir}; nothing was repaired or deleted. No success is reported.", file=sys.stderr)
        return 3
    except NotVerified as error:
        print("NOT VERIFIED: the log was written but these checks failed:", file=sys.stderr)
        for problem in error.problems:
            print(f"  - {problem}", file=sys.stderr)
        print(f"The files in {out_dir} are kept exactly as they are as evidence; nothing was repaired. No success is reported and no anchor commands are shown.", file=sys.stderr)
        return 4
    print(render_success(out_dir, receipt, report), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
