"""Deterministic identity of what a fixture replay was run on (identity version "1").

A replay claims its inputs in its first event. The claim is built here, from values the runner already holds in memory, and the read-only audit recomputes
the fixture-dependent part from the fixture folder it is given, so a reviewer can see exactly which inputs a run says it used and whether the fixtures in
front of them are those inputs.

Canonical serialization is eventchain.canonical (sorted keys, compact separators, ASCII, no floats). Nothing here contains a path, a user name, a
generation time or a file the manifest does not list.

  inputs      manifest_sha256 (SHA-256 of the manifest file's bytes), source_id, account (replay_<source_id>) and fixtures: one object per manifest line, in path
              order, with path (relative), sha256, bytes, source_id, record_type, origin_class, origin, retrieved_at_utc, terms_url, terms_checked_on
  accounting  everything that changes what the replay does with the inputs: ingest cut-off, starting balance, permission flag, slippage, quote-age limit, the
              fee model, every risk limit, the allowed origin classes and the parser, schema, policy and chain versions
  window      the replay's start and end instants

  account_binding = SHA-256( BINDING_DOMAIN + canonical({version, inputs, accounting}) )          kept in the saved state's header; a restart may move the window
  digest          = SHA-256( DIGEST_DOMAIN  + canonical({version, inputs, accounting, window[, plan]}) )   the whole claim

The optional plan is the ordered list of scripted intents (see plan() below). It is part of the full digest and NOT part of the account binding: the
binding decides whether a saved account may continue, and a continuation with a different script is legitimate. A claim without a plan hashes exactly as
it did before plans existed.

  plan        version, intents (execution order: stable sort by instant, so list order matters only between intents at the same instant), until (the
              explicit end of the script, or the last intent's instant; null for an empty script) and digest = SHA-256( PLAN_DOMAIN + canonical({version,
              intents, until}) ). Each intent: at (UTC, ISO-8601), contract, side, lots.
"""
import hashlib
from datetime import timezone

from . import eventchain

VERSION = "1"
DIGEST_DOMAIN = b"niftypaper.replay.inputs/1\n"
BINDING_DOMAIN = b"niftypaper.replay.account-binding/1\n"
PLAN_VERSION = "1"
PLAN_DOMAIN = b"niftypaper.replay.plan/1\n"


def _hash(domain, body):
    return hashlib.sha256(domain + eventchain.canonical(body).encode("ascii")).hexdigest()


def plan_digest(body):
    """Digest of a plan body {version, intents, until}."""
    return _hash(PLAN_DOMAIN, body)


def plan(intents, until=None):
    """The canonical plan for a script of intents (objects with at, contract_key, side, lots). Raises ValueError for anything that is not a well-formed
    intent, so a plan never holds an item whose interpretation is unclear."""
    items = list(intents)
    for item in items:
        if (getattr(getattr(item, "at", None), "tzinfo", None) is None or item.at.utcoffset() is None or not isinstance(item.contract_key, str)
                or item.side not in ("BUY", "SELL") or isinstance(item.lots, bool) or not isinstance(item.lots, int)):
            raise ValueError("every intent needs a timezone-aware time, a contract string, side BUY or SELL and an integer lot count")
    if until is not None and (getattr(until, "tzinfo", None) is None or until.utcoffset() is None):
        raise ValueError("until must be timezone-aware")
    items.sort(key=lambda i: i.at)                                   # stable, exactly as the runner orders its script
    rows = [{"at": i.at.astimezone(timezone.utc).isoformat(), "contract": i.contract_key, "side": i.side, "lots": i.lots} for i in items]
    end = until.astimezone(timezone.utc).isoformat() if until is not None else (rows[-1]["at"] if rows else None)
    body = {"version": PLAN_VERSION, "intents": rows, "until": end}
    return {**body, "digest": plan_digest(body)}


def full_digest(claim):
    """The digest a claim should carry, recomputed from the claim's own parts."""
    body = {"version": claim.get("version"), "inputs": claim.get("inputs"), "accounting": claim.get("accounting"), "window": claim.get("window")}
    if claim.get("plan") is not None:
        body["plan"] = {k: claim["plan"].get(k) for k in ("version", "intents", "until")}
    return _hash(DIGEST_DOMAIN, body)


def binding_of(claim):
    """The account binding a claim should carry, recomputed from the claim's own parts."""
    return _hash(BINDING_DOMAIN, {"version": claim.get("version"), "inputs": claim.get("inputs"), "accounting": claim.get("accounting")})


def build(*, manifest_sha256, entries, source_id, accounting, window, plan=None):
    """The identity claim: a plain dict of strings, integers, lists and dicts (it goes into the hash-chained event log as it is)."""
    inputs = {"manifest_sha256": manifest_sha256, "source_id": source_id, "account": f"replay_{source_id}",
              "fixtures": [entry.to_dict() for entry in sorted(entries, key=lambda e: e.path)]}
    claim = {"version": VERSION, "inputs": inputs, "accounting": accounting, "window": window}
    if plan is not None:
        claim["plan"] = plan
    return {**claim, "digest": full_digest(claim), "account_binding": binding_of(claim)}


def differences(claimed, recomputed):
    """Human-readable differences between two identity dicts (empty if they are the same)."""
    found = []
    for part in ("version", "digest", "account_binding"):
        if claimed.get(part) != recomputed.get(part):
            found.append(f"{part} differs")
    a, b = claimed.get("inputs", {}), recomputed.get("inputs", {})
    for name in ("manifest_sha256", "source_id", "account"):
        if a.get(name) != b.get(name):
            found.append(f"inputs.{name} differs")
    claimed_files = {f.get("path"): f for f in a.get("fixtures", []) if isinstance(f, dict)}
    actual_files = {f["path"]: f for f in b.get("fixtures", [])}
    for path in sorted(set(claimed_files) | set(actual_files)):
        if path not in actual_files:
            found.append(f"fixture {path} is claimed but not in the manifest")
        elif path not in claimed_files:
            found.append(f"fixture {path} is in the manifest but not claimed")
        elif claimed_files[path] != actual_files[path]:
            found.append(f"fixture {path} differs ({', '.join(k for k in actual_files[path] if claimed_files[path].get(k) != actual_files[path][k])})")
    for part in ("accounting", "window", "plan"):
        if claimed.get(part) != recomputed.get(part):
            found.append(f"{part} differs")
    return found
