"""Process-local attestation of historical market views.

A MarketView with origin HISTORICAL is accepted by the engine only if it carries an Attestation whose tag matches its critical fields (instrument, bid,
ask, sizes, tick, lot, quote time, source label). The tag is an HMAC under a random key made when this module is imported, so a view that is built by hand,
copied with `dataclasses.replace`, edited after issue, or restored from anywhere else fails verification.

What this does NOT do: Python cannot hide the key or the `issue` function from code running in the same process. Any code that imports this module can mint a
tag. The rule that only `niftypaper.historical.bridge` calls `issue` is enforced by a static test over the source tree, not by the language. The engine
therefore also re-validates the fields itself (see PaperEngine._historical_problem) and treats an attestation as necessary, not sufficient.
"""
import hashlib
import hmac
import os
from dataclasses import dataclass

_KEY = os.urandom(32)


@dataclass(frozen=True)
class Attestation:
    tag: str
    reference: str                 # what the view was made from, e.g. "file#row:content digest"; informational


def _material(fields):
    return "\x1f".join("" if value is None else str(value) for value in fields).encode("utf-8")


def issue(fields, reference):
    return Attestation(hmac.new(_KEY, _material(fields), hashlib.sha256).hexdigest(), reference)


def verify(attestation, fields):
    return isinstance(attestation, Attestation) and isinstance(attestation.tag, str) and hmac.compare_digest(
        attestation.tag, hmac.new(_KEY, _material(fields), hashlib.sha256).hexdigest())
