"""Anchor records for replay event logs (anchor format version "1").

An anchor is a small file that records what an event log looked like when it was finished: its head hash, event count, size and SHA-256, and which replay
it belongs to. Comparing a log with an anchor made earlier detects what the hash chain alone cannot: a log cut cleanly at an event boundary, and a log whose
whole chain was rebuilt after an edit.

TRUST MODEL (read this before relying on an anchor)
  A. A head hash typed in directly (--expected-head) is a trustworthy comparison only if it came from a source the log's writer cannot change.
  B. An anchor file kept anywhere the same user or process can write, including next to the log or in the same account's folders, is a convenience and
     a consistency check. It is NOT an independent trust anchor: whoever can rewrite the log can rewrite that file to match.
  C. An independently protected anchor is a copy held where the log's writer cannot modify it (a read-only medium or share, another account with its own
     access rights, a printed or separately stored note of the head hash). This program cannot create such a place, cannot see who can write to a path, and
     does not assume one exists. When you tell the audit that an anchor is independent, it records that as YOUR declaration, not as something it checked,
     and it downgrades the declaration when the anchor sits beside the log or the state.
  Even a perfectly protected anchor shows only that the log is the one that existed when the anchor was made. It does not show that the events, the quotes
  or the replay were genuine.

Record: one canonical JSON line (keys sorted, compact separators, ASCII, no floats, one "\\n"), with exactly these fields:
  anchor (the string "1"), kind, notice, source_id, account ("replay_<source_id>"), chain ("1"), genesis, events (count, >= 1), head (hash of the last event),
  log_sha256 and log_bytes (the whole event file), first_clock and last_clock (aware ISO times of the first and last event), trades (from the final
  checkpoint), final_event ("CHECKPOINT").
No secrets, paths or generation time are stored, so the same log always gives the same anchor bytes.
"""
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from .publish import PublishError, publish_new_file

ANCHOR_VERSION = "1"
KIND = "niftypaper.replay.event_log_anchor"
NOTICE = ("A copy of this record kept where the replay writer can modify it is not an independent trust anchor. "
          "Matching it shows the log is unchanged since the anchor was made, not that the log is genuine.")
GENESIS = hashlib.sha256(b"niftypaper.replay.chain.genesis/1").hexdigest()
FIELDS = ("anchor", "kind", "notice", "source_id", "account", "chain", "genesis", "events", "head", "log_sha256", "log_bytes", "first_clock", "last_clock",
          "trades", "final_event")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE = re.compile(r"^[a-z0-9_]{1,40}$")


class AnchorError(Exception):
    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def canonical_line(record):
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n"


def _no_dupes(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def _no_constants(name):
    raise ValueError(name)


def _int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _aware(value):
    try:
        return isinstance(value, str) and datetime.fromisoformat(value).utcoffset() is not None
    except ValueError:
        return False


def parse(data):
    """The anchor record in `data` (bytes), or AnchorError. Strict: canonical bytes only, exactly the documented fields, version "1"."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise AnchorError("EMPTY_ANCHOR", "the anchor file is empty or unreadable")
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        raise AnchorError("MALFORMED_ANCHOR", "not valid UTF-8") from None
    if not text.endswith("\n") or text.count("\n") != 1:
        raise AnchorError("MALFORMED_ANCHOR", "an anchor is exactly one line ending in a line break")
    try:
        record = json.loads(text, object_pairs_hook=_no_dupes, parse_constant=_no_constants)
    except ValueError:
        raise AnchorError("MALFORMED_ANCHOR", "not valid JSON (or repeats a key)") from None
    if not isinstance(record, dict):
        raise AnchorError("MALFORMED_ANCHOR", "not a JSON object")
    version = record.get("anchor")
    if not isinstance(version, str):
        raise AnchorError("MALFORMED_VERSION", "the anchor version is missing or not a string")
    if version != ANCHOR_VERSION:
        raise AnchorError("UNSUPPORTED_VERSION", f"anchor version {version!r} is not supported")
    if set(record) != set(FIELDS):
        raise AnchorError("MALFORMED_ANCHOR", "fields differ from the documented set: " + ", ".join(sorted(set(record) ^ set(FIELDS))))
    problems = []
    if record["kind"] != KIND:
        problems.append("kind")
    if record["notice"] != NOTICE:
        problems.append("notice")
    if not isinstance(record["source_id"], str) or not _SOURCE.match(record["source_id"]):
        problems.append("source_id")
    elif record["account"] != f"replay_{record['source_id']}":
        problems.append("account")
    if record["chain"] != "1" or record["genesis"] != GENESIS:
        problems.append("chain/genesis")
    for name in ("head", "log_sha256"):
        if not isinstance(record[name], str) or not _HEX64.match(record[name]):
            problems.append(name)
    if not _int(record["events"]) or record["events"] < 1 or not _int(record["log_bytes"]) or record["log_bytes"] < 1 or not _int(record["trades"]):
        problems.append("counts")
    if not _aware(record["first_clock"]) or not _aware(record["last_clock"]) or (
            _aware(record["first_clock"]) and _aware(record["last_clock"]) and datetime.fromisoformat(record["first_clock"]) > datetime.fromisoformat(record["last_clock"])):
        problems.append("clocks")
    if record["final_event"] != "CHECKPOINT":
        problems.append("final_event")
    if problems:
        raise AnchorError("MALFORMED_ANCHOR", "invalid field(s): " + ", ".join(problems))
    if canonical_line(record) != text:
        raise AnchorError("NON_CANONICAL_ANCHOR", "the anchor is not in the canonical serialization")
    return record


def _log_facts(events_bytes):
    """(events, source_id) from a verified log, or AnchorError. The caller has already required a VALID chain."""
    events = [json.loads(line) for line in events_bytes.decode("utf-8").split("\n")[:-1]]
    if not events or events[0].get("type") != "REPLAY_OPENED" or not isinstance(events[0].get("source_id"), str):
        raise AnchorError("LOG_INCOMPLETE", "the log does not start with a REPLAY_OPENED event naming its source")
    if events[-1].get("type") != "CHECKPOINT" or not _int(events[-1].get("trades")):
        raise AnchorError("LOG_INCOMPLETE", "the log does not end with a final checkpoint: it may be unfinished or cut short")
    return events


def build(events_bytes, state_source_id):
    """The anchor record for a finished log. Refuses a missing, invalid or incomplete log, and a log whose source differs from the replay account's."""
    from . import audit                                            # imported here: the audit imports this module
    chain = audit.verify_chain(events_bytes)
    if chain["status"] != "VALID":
        raise AnchorError("LOG_NOT_VALID", f"chain {chain['status']}: {chain['reason']}: {chain['detail']}")
    events = _log_facts(events_bytes)
    source = events[0]["source_id"]
    if source != state_source_id:
        raise AnchorError("SOURCE_MISMATCH", f"the log names source {source!r} but the replay account names {state_source_id!r}")
    record = {"anchor": ANCHOR_VERSION, "kind": KIND, "notice": NOTICE, "source_id": source, "account": f"replay_{source}", "chain": "1", "genesis": GENESIS,
              "events": len(events), "head": chain["head"], "log_sha256": hashlib.sha256(events_bytes).hexdigest(), "log_bytes": len(events_bytes),
              "first_clock": events[0]["clock"], "last_clock": events[-1]["clock"], "trades": events[-1]["trades"], "final_event": "CHECKPOINT"}
    parse(canonical_line(record).encode("ascii"))                  # never write a record this module would itself reject
    return record


def state_source(state_path):
    """The source id of a replay account state file (from its header), or AnchorError. Read-only; no lock is taken."""
    try:
        first = Path(state_path).read_bytes().split(b"\n", 1)[0]
        header = json.loads(first.decode("utf-8"), object_pairs_hook=_no_dupes)
    except (OSError, ValueError):
        raise AnchorError("STATE_UNREADABLE", "the state file is missing or its header is not valid") from None
    account = header.get("account") if isinstance(header, dict) and header.get("kind") == "header" else None
    if not isinstance(account, str) or not account.startswith("replay_") or len(account) == 7:
        raise AnchorError("NOT_A_REPLAY_ACCOUNT", "the state file does not belong to a replay account")
    return account[7:]


def beside(path, *others):
    """True if `path` is in, or in a folder above, any of `others` (so it is not separate from them)."""
    target = Path(path).resolve()
    for other in others:
        folder = Path(other).resolve().parent
        if target.parent == folder or folder in target.parents or target.parent in folder.parents:
            return True
    return False


def create(events_path, state_path, anchor_path):
    """Explicitly create an anchor for a finished replay (creating the destination folder if it is missing, after the checks pass). Never overwrites, never runs as a side effect of auditing, and refuses a destination in the same
    folder as (or above or below) the event log or the state file. Returns the record written."""
    if beside(anchor_path, events_path, state_path):
        raise AnchorError("ANCHOR_BESIDE_LOG", "an anchor in the same folder tree as the log or the replay state is not separate from them; choose another place")
    try:
        data = Path(events_path).read_bytes()
    except OSError:
        raise AnchorError("LOG_NOT_VALID", "the event log is missing or unreadable") from None
    record = build(data, state_source(state_path))
    try:
        Path(anchor_path).parent.mkdir(parents=True, exist_ok=True)       # only after the log and the destination were accepted
    except OSError as error:
        raise AnchorError("CANNOT_CREATE", error.__class__.__name__) from None
    try:
        publish_new_file(canonical_line(record).encode("ascii"), anchor_path)
    except PublishError as error:
        raise AnchorError(error.code, error.detail) from None
    return record


def compare(record, events_bytes, chain, state_source_id, events):
    """How a parsed anchor relates to an event log. `chain` is the audit's chain section for that log; `events` its parsed events (or None if unparseable).
    Returns {status MATCH|MISMATCH|UNAVAILABLE, comparisons {field: MATCH|MISMATCH|UNAVAILABLE}, reason, detail}."""
    if events_bytes is None:
        return {"status": "UNAVAILABLE", "comparisons": {}, "reason": "MISSING_FILE", "detail": "the event file is missing or unreadable"}
    seen = {s for s in (state_source_id, events[0].get("source_id") if events else None) if s is not None}
    comparisons = {
        "source_id": "UNAVAILABLE" if not seen else "MATCH" if seen == {record["source_id"]} else "MISMATCH",
        "events": _same(record["events"], chain.get("events_verified")) if chain["status"] == "VALID" else "UNAVAILABLE",
        "head": _same(record["head"], chain.get("head")) if chain["status"] == "VALID" else "UNAVAILABLE",
        "log_sha256": _same(record["log_sha256"], hashlib.sha256(events_bytes).hexdigest()),
        "log_bytes": _same(record["log_bytes"], len(events_bytes)),
        "first_clock": _same(record["first_clock"], events[0].get("clock")) if events else "UNAVAILABLE",
        "last_clock": _same(record["last_clock"], events[-1].get("clock")) if events else "UNAVAILABLE",
    }
    if chain["status"] != "VALID":
        return {"status": "MISMATCH", "comparisons": comparisons, "reason": "CHAIN_NOT_VALID",
                "detail": "the event chain is not valid, so the log cannot match the anchor; compared the fields that could be compared"}
    bad = [name for name, value in comparisons.items() if value != "MATCH"]
    if bad:
        return {"status": "MISMATCH", "comparisons": comparisons, "reason": "FIELDS_DIFFER", "detail": "differs from the anchor: " + ", ".join(bad)}
    return {"status": "MATCH", "comparisons": comparisons, "reason": None, "detail": "the log is identical to the one the anchor was made from"}


def _same(a, b):
    return "UNAVAILABLE" if b is None else "MATCH" if a == b else "MISMATCH"
