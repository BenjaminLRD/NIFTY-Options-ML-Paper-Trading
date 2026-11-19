"""Manifest-verified ingestion of JSON-lines files into canonical records, with the duplicate/conflict policy and the exclusion ledger.

Each file's hash is checked against its manifest line from the very bytes that are then parsed; a changed file contributes nothing. Rows that fail
validation, duplicates and conflicts all get a ledger entry with a reason code and a pointer to the other copy. The same inputs always give the same
records, ledger and digest: files are handled in sorted path order, rows in file order, and nothing reads the clock.
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from . import records as rec
from .manifest import IntegrityFailure, failure_exclusion, read_verified, verify

SYNTHETIC = "SYNTHETIC_FIXTURE"


@dataclass(frozen=True)
class IngestResult:
    records: tuple
    exclusions: tuple
    integrity_failures: tuple

    def ledger_lines(self):
        """The exclusion ledger and integrity failures as deterministic JSON lines."""
        lines = [json.dumps({"ledger": "exclusion", **e.to_dict()}, sort_keys=True) for e in self.exclusions]
        lines += [json.dumps({"ledger": "integrity", **f.to_dict()}, sort_keys=True) for f in self.integrity_failures]
        return "\n".join(lines) + ("\n" if lines else "")

    def digest(self):
        """One hash over the normalised records and the ledger: equal across repeated runs on equal inputs."""
        parts = [r.content_digest + "|" + r.source_file + "|" + str(r.source_row) for r in self.records]
        return hashlib.sha256(("\n".join(parts) + "\n" + self.ledger_lines()).encode("utf-8")).hexdigest()


def _pairs(pairs):
    names = [k for k, _ in pairs]
    if len(names) != len(set(names)):
        raise ValueError("duplicate key in a record")
    return dict(pairs)


def _reject_constant(name):
    raise ValueError(f"{name} is not allowed")


def _parse_file(entry, data, as_of, retrieved_at):
    """(records, exclusions) for one verified file."""
    found, dropped = [], []

    def drop(row, reason, detail, kind=None, key=None, time_key=None):
        dropped.append(rec.Exclusion(entry.source_id, entry.path, entry.sha256, row, kind or entry.record_type, key, time_key, reason, detail))

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        drop(None, "MALFORMED_FILE", "the file is not valid UTF-8")
        return found, dropped
    for row, line in enumerate(text.split("\n"), 1):
        line = line.rstrip("\r")
        if not line.strip():
            continue
        try:
            raw = json.loads(line, object_pairs_hook=_pairs, parse_constant=_reject_constant)
        except ValueError as error:
            drop(row, "MALFORMED_RECORD", f"not valid JSON: {error}")
            continue
        if not isinstance(raw, dict):
            drop(row, "MALFORMED_RECORD", "a record must be a JSON object")
            continue
        kind = raw.get("record_kind")
        if kind not in rec.KINDS:
            drop(row, "UNKNOWN_RECORD_KIND", f"record_kind {kind!r}")
            continue
        if kind != entry.record_type:
            drop(row, "RECORD_KIND_MISMATCH", f"the manifest declares {entry.record_type} for this file but the row is {kind}", kind)
            continue
        if (entry.origin_class == SYNTHETIC) != (raw.get("synthetic") is True):
            drop(row, "ORIGIN_LABEL_MISMATCH", "a row's synthetic label must agree with the manifest's origin_class", kind)
            continue
        try:
            built = rec.build(kind, raw, as_of=as_of, retrieved_at=retrieved_at)
        except rec.Reject as error:
            drop(row, error.code, error.detail, kind, _guess_key(raw))
            continue
        flags = tuple(sorted(set(built.flags)))
        found.append(rec.Record(
            kind=kind, key=built.key, time_key=built.time_key, values=built.values, observed_at=built.observed_at, available_at=built.available_at,
            retrieved_at=retrieved_at, flags=flags, quality_status="OK_WITH_FLAGS" if flags else "OK", fill_eligible=built.fill_eligible,
            ineligible_reasons=built.ineligible_reasons, source_id=entry.source_id, origin_class=entry.origin_class, source_file=entry.path,
            source_file_sha256=entry.sha256, source_row=row, raw=line, ingested_at_utc=as_of.astimezone(timezone.utc),
            content_digest=rec.digest_of(kind, built)))
    return found, dropped


def _guess_key(raw):
    """A readable identity for the ledger when a row was rejected; informational only, never used as a key."""
    parts = [str(raw.get(n)) for n in ("underlying", "expiry", "strike", "option_type") if raw.get(n) is not None]
    return "|".join(parts) or None


def _where(record):
    return f"{record.source_file}#{record.source_row}"


def _apply_policy(found):
    """Duplicate and conflict policy (config/historical_schema_v1.json, version 1). Returns (kept, exclusions)."""
    groups = {}
    for record in found:                                        # `found` is already in (path, row) order
        groups.setdefault(record.natural_key, []).append(record)
    kept, dropped = [], []
    for group in groups.values():
        first = group[0]
        if len({r.content_digest for r in group}) == 1:
            kept.append(first)
            for other in group[1:]:
                same_file = other.source_file == first.source_file
                dropped.append(_exclusion(other, "DUPLICATE_ROW_IN_FILE" if same_file else "DUPLICATE_OF_EARLIER_COPY", "identical to the kept copy", first))
            continue
        same_file = len({r.source_file for r in group}) == 1                 # conflict: nothing is kept, and none is preferred
        for index, member in enumerate(group):
            other = group[1] if index == 0 else group[0]
            dropped.append(_exclusion(member, "CONFLICTING_ROW_IN_FILE" if same_file else "CONFLICTING_COPIES",
                                      f"{len(group)} records share this natural key with different values; none is kept", other))
    return kept, dropped


def _exclusion(record, reason, detail, other):
    return rec.Exclusion(record.source_id, record.source_file, record.source_file_sha256, record.source_row, record.kind, record.key, record.time_key,
                         reason, detail, _where(other))


def ingest(root, entries, *, as_of):
    """Ingest the manifest's files from `root` (a Path). `as_of` is the aware ingest cut-off; observations after it are refused."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    found, dropped, failures = [], [], []
    for entry in sorted(entries, key=lambda e: e.path):
        data = read_verified(root, entry)
        if isinstance(data, IntegrityFailure):
            failures.append(data)
            dropped.append(failure_exclusion(entry, data))
            continue
        retrieved = None
        if entry.retrieved_at_utc:
            retrieved = datetime.fromisoformat(entry.retrieved_at_utc)
        got, bad = _parse_file(entry, data, as_of, retrieved)
        found += got
        dropped += bad
    listed = verify_unlisted(root, entries)
    failures += listed
    kept, policy_drops = _apply_policy(found)
    dropped += policy_drops
    kept.sort(key=lambda r: (r.available_at, r.kind, r.key, r.time_key, r.source_id, r.source_file, r.source_row))
    dropped.sort(key=lambda e: (e.source_file, e.source_row or 0, e.reason))
    failures.sort(key=lambda f: (f.path, f.code))
    return IngestResult(tuple(kept), tuple(dropped), tuple(failures))


def verify_unlisted(root, entries):
    """Files present under root but not in the manifest: reported, never ingested."""
    return [f for f in verify(root, []) if f.code == "UNLISTED_FILE" and f.path not in {e.path for e in entries}]
