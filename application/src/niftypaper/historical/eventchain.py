"""Hash chain for replay event logs (chain version "1"). The writer side; the read-only audit re-implements the same specification on its own.

Each event line is one JSON object holding the event's own fields plus three chain fields:

    chain   the string "1" (the chain version)
    prev    hex SHA-256 of the previous event ("hash" field), or GENESIS for the first event
    hash    hex SHA-256 of this event, defined below

The sequence number is the event's existing field `n` (1, 2, 3, ...), so it is covered by the hash.

Canonical serialization (used for the hash and for the line itself): the JSON object with every key sorted, separators "," and ":" with no spaces, only
ASCII (non-ASCII escaped as \\uXXXX), no NaN or infinity, followed by a single "\\n" on the line. Numbers are integers only: the writer refuses floats, and
decimals, times and durations are carried as strings. Nothing path-dependent or time-of-writing is added.

Event hash = SHA-256( EVENT_DOMAIN + canonical(event with the "hash" field removed) ), lower-case hex. So the hash covers every field of the line
except `hash` itself, including `chain`, `n` and `prev`, and cannot include itself. GENESIS = SHA-256(GENESIS_LABEL), lower-case hex. No event type is
special: every line, known type or not, is chained.

What this gives: tamper evidence. An edit, insertion, deletion, duplication or reordering in the middle of a log breaks the chain at a definite line. It does
NOT prove the events are genuine, and it does not stop a writer who rebuilds the whole chain from the edit onward. A log cut cleanly at an event boundary stays
a valid chain; the final head hash (returned when the log is written) must be recorded elsewhere to detect that.
"""
import hashlib
import json

VERSION = "1"
GENESIS_LABEL = b"niftypaper.replay.chain.genesis/1"
EVENT_DOMAIN = b"niftypaper.replay.chain.event/1\n"
GENESIS = hashlib.sha256(GENESIS_LABEL).hexdigest()
RESERVED = ("chain", "prev", "hash")


class ChainError(ValueError):
    pass


def canonical(obj):
    """The canonical text of a JSON-compatible object (no floats)."""
    _no_floats(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _no_floats(obj):
    if isinstance(obj, float):
        raise ChainError("floats are not allowed in chained events (carry numbers as strings or integers)")
    if isinstance(obj, dict):
        for key, value in obj.items():
            if not isinstance(key, str):
                raise ChainError("event keys must be strings")
            _no_floats(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _no_floats(value)
    elif obj is not None and not isinstance(obj, (str, int, bool)):
        raise ChainError(f"cannot chain a value of type {type(obj).__name__}")


def event_hash(event):
    """Hash of an event given as a dict; any `hash` field in it is ignored."""
    body = {k: v for k, v in event.items() if k != "hash"}
    return hashlib.sha256(EVENT_DOMAIN + canonical(body).encode("ascii")).hexdigest()


def seal(event, prev):
    """A new dict: `event` plus chain, prev and hash. Raises ChainError instead of producing an event it cannot chain."""
    clash = [name for name in RESERVED if name in event]
    if clash:
        raise ChainError("event fields clash with the chain fields: " + ", ".join(clash))
    linked = {**event, "chain": VERSION, "prev": prev}
    linked["hash"] = event_hash(linked)
    return linked


def line(event):
    return canonical(event) + "\n"
