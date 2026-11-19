"""Raw-file manifests: one JSON line per file with its relative path, SHA-256, size, declared source and record type, and retrieval metadata.

A matching hash proves only that the bytes are the ones the manifest was written for. It says nothing about whether the content is genuine market
data, who produced it, or whether the manifest itself was honest. Raw files are only ever opened for reading; a manifest is never rewritten in place.
"""
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from .records import KINDS, ORIGIN_CLASSES, Exclusion

MANIFEST_NAME = "MANIFEST.jsonl"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SOURCE_ID = re.compile(r"^[a-z0-9_]{1,40}$")
FIELDS = ("path", "sha256", "bytes", "source_id", "record_type", "origin_class", "origin", "retrieved_at_utc", "terms_url", "terms_checked_on")


class ManifestError(Exception):
    """The manifest itself is not usable. Nothing is ingested from a manifest that fails to load."""


@dataclass(frozen=True)
class Entry:
    path: str                      # relative, forward slashes, inside the raw root
    sha256: str
    bytes: int
    source_id: str
    record_type: str               # the one record kind the file may contain
    origin_class: str              # SYNTHETIC_FIXTURE, UNVERIFIED_SOURCE or UNKNOWN
    origin: str                    # free text: where the file came from
    retrieved_at_utc: "str | None" = None
    terms_url: "str | None" = None
    terms_checked_on: "str | None" = None

    def to_dict(self):
        return {name: getattr(self, name) for name in FIELDS}


@dataclass(frozen=True)
class IntegrityFailure:
    path: str
    code: str                      # HASH_MISMATCH_WITH_MANIFEST, SIZE_MISMATCH, MISSING_FILE, UNLISTED_FILE
    detail: str

    def to_dict(self):
        return {"path": self.path, "code": self.code, "detail": self.detail}


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _safe_path(text):
    path = PurePosixPath(text)
    if not text or "\\" in text or path.is_absolute() or ".." in path.parts or text != path.as_posix() or ":" in text:
        raise ManifestError(f"unsafe manifest path {text!r}")
    return text


def entry_from_dict(data):
    if not isinstance(data, dict) or set(data) != set(FIELDS):
        raise ManifestError("a manifest line must have exactly the fields " + ", ".join(FIELDS))
    _safe_path(data["path"])
    if not isinstance(data["sha256"], str) or not SHA256.match(data["sha256"]):
        raise ManifestError(f"{data['path']}: sha256 must be 64 lower-case hex characters")
    if isinstance(data["bytes"], bool) or not isinstance(data["bytes"], int) or data["bytes"] < 0:
        raise ManifestError(f"{data['path']}: bytes must be a non-negative integer")
    if not isinstance(data["source_id"], str) or not SOURCE_ID.match(data["source_id"]):
        raise ManifestError(f"{data['path']}: source_id is not a plain lower-case id")
    if data["record_type"] not in KINDS:
        raise ManifestError(f"{data['path']}: record_type must be one of {', '.join(KINDS)}")
    if data["origin_class"] not in ORIGIN_CLASSES:
        raise ManifestError(f"{data['path']}: origin_class must be one of {', '.join(ORIGIN_CLASSES)}")
    if not isinstance(data["origin"], str) or not data["origin"]:
        raise ManifestError(f"{data['path']}: origin must say where the file came from")
    stamp = data["retrieved_at_utc"]
    if stamp is not None:
        try:
            if datetime.fromisoformat(stamp).utcoffset() is None:
                raise ValueError
        except (TypeError, ValueError):
            raise ManifestError(f"{data['path']}: retrieved_at_utc must be a timezone-aware ISO time or null") from None
    return Entry(**data)


def load_manifest(path):
    """Strict load: unknown fields, duplicate paths, unsafe paths and blank lines all refuse the whole manifest."""
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        raise ManifestError(f"cannot read manifest: {error}") from None
    return parse_manifest(data)


def parse_manifest(data):
    """The same strict parse from bytes already read, so a caller can hash and parse exactly the same bytes."""
    entries, seen = [], set()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ManifestError(f"cannot read manifest: {error}") from None
    for number, line in enumerate(text.split("\n"), 1):
        if not line:
            continue
        try:
            data = json.loads(line, object_pairs_hook=_no_duplicate_keys)
        except ValueError as error:
            raise ManifestError(f"manifest line {number} is not valid JSON: {error}") from None
        entry = entry_from_dict(data)
        if entry.path in seen:
            raise ManifestError(f"manifest lists {entry.path} twice")
        seen.add(entry.path)
        entries.append(entry)
    if not entries:
        raise ManifestError("manifest lists no files")
    return tuple(sorted(entries, key=lambda e: e.path))


def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def describe(root, files, *, source_id, origin_class, origin, retrieved_at_utc=None, terms_url=None, terms_checked_on=None):
    """Entries for `files`, a {relative path: record kind} mapping, read from `root` (read-only)."""
    entries = []
    for rel, kind in sorted(files.items()):
        data = _read(root, _safe_path(rel))
        entries.append(entry_from_dict({"path": rel, "sha256": sha256_bytes(data), "bytes": len(data), "source_id": source_id, "record_type": kind,
                                        "origin_class": origin_class, "origin": origin, "retrieved_at_utc": retrieved_at_utc, "terms_url": terms_url,
                                        "terms_checked_on": terms_checked_on}))
    return entries


def write_manifest(path, entries):
    """Write a new manifest. An existing manifest is never overwritten: the caller must remove or move it deliberately."""
    body = "".join(json.dumps(e.to_dict(), sort_keys=True) + "\n" for e in sorted(entries, key=lambda e: e.path))
    with open(path, "xb") as handle:
        handle.write(body.encode("utf-8"))


def _read(root, rel):
    target = root / rel
    if target.is_symlink() or not target.is_file():
        raise ManifestError(f"{rel} is not a regular file inside the raw root")
    return target.read_bytes()


def read_verified(root, entry):
    """The file's bytes if they match the manifest, else an IntegrityFailure. The hash is computed from the same bytes that are returned, so the file
    cannot change between the check and its use."""
    try:
        data = _read(root, entry.path)
    except ManifestError:
        return IntegrityFailure(entry.path, "MISSING_FILE", "the file is missing or is not a regular file")
    except OSError as error:
        return IntegrityFailure(entry.path, "MISSING_FILE", f"cannot read: {error.__class__.__name__}")
    if len(data) != entry.bytes:
        return IntegrityFailure(entry.path, "SIZE_MISMATCH", f"manifest says {entry.bytes} bytes, file has {len(data)}")
    if sha256_bytes(data) != entry.sha256:
        return IntegrityFailure(entry.path, "HASH_MISMATCH_WITH_MANIFEST", "the file's SHA-256 differs from the manifest")
    return data


def verify(root, entries):
    """Every integrity failure under `root`: changed, missing and unlisted files. Writes nothing."""
    failures = []
    for entry in entries:
        result = read_verified(root, entry)
        if isinstance(result, IntegrityFailure):
            failures.append(result)
    listed = {e.path for e in entries}
    for found in sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()):
        if found not in listed and found != MANIFEST_NAME:
            failures.append(IntegrityFailure(found, "UNLISTED_FILE", "the file is not in the manifest and will not be ingested"))
    return failures


def failure_exclusion(entry, failure):
    return Exclusion(entry.source_id, entry.path, entry.sha256, None, entry.record_type, None, None, failure.code, failure.detail)
