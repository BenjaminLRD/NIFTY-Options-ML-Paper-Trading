"""Read-only reconciliation and audit report for a fixture replay.

Inputs: the replay's event lines, the replay account's saved state file and, optionally, the invented fixture folder. The report rebuilds the account from
the saved trade records with its OWN arithmetic (it does not import the engine's accounting and does not take the engine's balances as truth); the engine's
balances in the event file are treated as claims and checked against that rebuild.

It only reads. It never opens an input for writing, never takes the state file's lock, never creates, repairs, truncates or rewrites anything, and the
report content has no generation time, so the same inputs always give the same report and digest.

What a CONSISTENT verdict means: the supplied files agree with each other and with the stated rules. It is NOT evidence that the files or the quotes they
describe are genuine: anyone who can write all the files consistently can pass every check here. See docs/HISTORICAL_REPLAY_AUDIT.md.
"""
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from . import anchor as anchor_module
from . import identity as identity_module
from . import manifest as mf
from .ingest import ingest
from .records import QUOTE

REPORT_VERSION = "1"
CENT = Decimal("0.01")
PASS, FAIL, BLOCKED, NOT_CHECKED, SKIPPED = "PASS", "FAIL", "BLOCKED", "NOT_CHECKED", "SKIPPED"
EVENT_TYPES = ("REPLAY_OPENED", "STATE_LOCKED", "RECORD_AVAILABLE", "INTENT_RECEIVED", "INTENT_REFUSED", "FILL", "INPUTS_RECHECKED", "CHECKPOINT")
CHECKS = (
    ("E1_EVENTS_PARSE", "event file present, complete lines, valid JSON objects"),
    ("E2_EVENTS_SEQUENCE", "event numbers run 1..N with no gap, duplicate or reordering"),
    ("E3_EVENTS_CLOCK", "clock aware and never backwards; one REPLAY_OPENED first; records announced only at or after available_at"),
    ("E4_INTENT_PAIRING", "every intent has exactly one decision, and every decision has its intent"),
    ("E5_EVENTS_COMPLETE", "events end with a final checkpoint and account for every saved trade"),
    ("S1_STATE_PARSE", "state file present, complete, header and trade lines valid"),
    ("S2_ACCOUNT_IDENTITY", "state belongs to a replay account of one historical source; trades carry that source"),
    ("S3_STATE_SEQUENCE", "trade numbers run 1..N and trade times never go backwards"),
    ("S4_TRADE_ARITHMETIC", "quantity, turnover, fee and fill price follow from each record's own quote and the stated fee model"),
    ("A1_CASH", "starting balance plus cash movements equals the engine's cash at every claim"),
    ("A2_POSITIONS", "open positions equal accepted buys minus sells, never negative, one lot size"),
    ("A3_FEES_ONCE", "each trade's fee is charged exactly once and equals the fee model"),
    ("A4_COST_BASIS", "premium and buy fees are conserved across partial and full exits"),
    ("A5_REALIZED", "stored realised P&L equals the recomputed value and the engine's total"),
    ("A6_REJECTIONS", "a refused intent leaves cash and positions as they were, and no trade exists for it"),
    ("X1_EVENT_TRADE_MATCH", "each FILL event matches exactly one saved trade, field by field"),
    ("X2_SOURCE_IDENTITY", "events, state header, trades and fixtures name the same source"),
    ("P1_PAIRING", "the state file belongs to the same replay account and the same input identity as the event log (a state from another source, account or inputs is not reconciled)"),
    ("I1_INPUT_IDENTITY", "the inputs the replay claims are consistent, equal the fixtures supplied, and were found unchanged at every recheck"),
    ("PL1_PLAN_IDENTITY", "the intent plan the replay claims is well-formed, matches its digest, and equals the ordered intents the events record"),
    ("F1_FIXTURES", "fixtures verify against their manifest, re-ingest to the digest recorded at open, and back every quote reference"),
    ("M1_MARKS", "open P&L uses only permitted, time-valid quotes that were visible at the event's clock"),
)


@dataclass(frozen=True)
class AuditConfig:
    events_path: Path
    state_path: Path
    fixture_dir: "Path | None" = None
    fee_per_trade: "Decimal | None" = None          # default: the fee model stated in the REPLAY_OPENED event
    fee_fraction: "Decimal | None" = None
    mark_max_age: "timedelta | None" = None         # default: the quote age limit stated in REPLAY_OPENED
    expected_head: "str | None" = None              # head hash recorded when the log was written (anchors the chain; detects a cut at an event boundary)
    anchor_path: "Path | None" = None               # an anchor record made by historical/anchor.py (compared separately from the chain)
    anchor_trust: str = "local"                     # "local" (default) or "independent": YOUR declaration about where the anchor/head came from; never verified


# ---- hash-chain verification (specification: the docstring of the writer-side chain module; re-implemented here so the audit does not depend on the writer) --
CHAIN_VERSION = "1"
CHAIN_GENESIS = hashlib.sha256(b"niftypaper.replay.chain.genesis/1").hexdigest()
_CHAIN_DOMAIN = b"niftypaper.replay.chain.event/1\n"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
CHAIN_NOTICE = ("Tamper evidence for this event file only. A valid chain does not show that the events are genuine, and a writer who rebuilds the chain from an "
                "edit onward produces a valid one. A cut at an event boundary is detected only with an expected head recorded elsewhere.")


def _chain_canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _has_float(obj):
    if isinstance(obj, float):
        return True
    if isinstance(obj, dict):
        return any(_has_float(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_float(v) for v in obj)
    return False


def verify_chain(data, expected_head=None):
    """Independent check of an event log's hash chain. Returns {status VALID|INVALID|UNAVAILABLE, reason, detail, first_failure {line, n} or None,
    events_verified (the valid prefix), head (hash of the last verified event), anchored (an expected head was supplied and matched), version, genesis, notice}.
    Never raises on bad input and never touches any file."""
    def result(status, reason, detail, verified=0, head=None, line=None, n=None, anchored=False):
        return {"status": status, "version": CHAIN_VERSION, "genesis": CHAIN_GENESIS, "reason": reason, "detail": detail,
                "first_failure": {"line": line, "n": n} if line is not None else None, "events_verified": verified,
                "head": head if head is not None else CHAIN_GENESIS if verified == 0 and status != "UNAVAILABLE" else _unavailable("NOT_VERIFIED"),
                "anchored": anchored, "notice": CHAIN_NOTICE}

    if data is None:
        return result("UNAVAILABLE", "MISSING_FILE", "the event file is missing or unreadable")
    if not data:
        return result("INVALID", "EMPTY_FILE", "the event file is empty: a log holds at least its opening event")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return result("INVALID", "NOT_UTF8", "the event file is not valid UTF-8")
    pieces = text.split("\n")
    lines, tail = pieces[:-1], pieces[-1]
    expected, head, verified = 1, CHAIN_GENESIS, 0
    legacy = None
    for number, raw in enumerate(lines, 1):
        def bad(reason, detail, n=None):
            return result("INVALID", reason, detail, verified, head, number, n)
        if not raw.strip():
            return bad("BLANK_LINE", "a blank line inside the log")
        try:
            obj = json.loads(raw, object_pairs_hook=_no_dupes, parse_constant=_no_constants)
        except ValueError:
            return bad("MALFORMED_JSON", "the line is not valid JSON (or repeats a key)")
        if not isinstance(obj, dict):
            return bad("NOT_AN_OBJECT", "the line is not a JSON object")
        present = [name for name in ("chain", "prev", "hash") if name in obj]
        if number == 1:
            legacy = not present
        if legacy and not present:
            continue                                            # a log written before chaining existed: decided at the end
        if legacy or len(present) != 3:
            return bad("MISSING_CHAIN_FIELDS", "chain fields are missing on some lines and present on others" if legacy else "the line lacks " + ", ".join(
                n for n in ("chain", "prev", "hash") if n not in obj), obj.get("n") if isinstance(obj.get("n"), int) else None)
        n = obj.get("n")
        version = obj["chain"]
        if not isinstance(version, str):
            return bad("MALFORMED_VERSION", "the chain version is not a string", n if isinstance(n, int) else None)
        if version != CHAIN_VERSION:
            return bad("UNSUPPORTED_VERSION", f"chain version {version!r} is not supported; the log is not verified", n if isinstance(n, int) else None)
        if isinstance(n, bool) or not isinstance(n, int):
            return bad("MALFORMED_SEQUENCE", "the sequence number n is not an integer")
        if n != expected:
            reason = ("FIRST_SEQUENCE_NOT_ONE" if number == 1 else "SEQUENCE_DUPLICATE" if n == expected - 1 else "SEQUENCE_REORDERED" if n < expected else "SEQUENCE_GAP")
            return bad(reason, f"sequence number {n} where {expected} was expected", n)
        for name in ("prev", "hash"):
            if not isinstance(obj[name], str) or not _HEX64.match(obj[name]):
                return bad("MALFORMED_HASH", f"{name} is not 64 lower-case hex characters", n)
        if _has_float(obj) or raw != _chain_canonical(obj):
            return bad("NON_CANONICAL_LINE", "the line is not in the canonical serialization (spacing, key order, escapes, line ending or a float)", n)
        if obj["prev"] != head:
            return bad("GENESIS_MISMATCH" if number == 1 else "LINK_BROKEN", "prev is not the hash of the preceding event" if number > 1 else "the first event's prev is not the genesis value", n)
        body = {k: v for k, v in obj.items() if k != "hash"}
        if hashlib.sha256(_CHAIN_DOMAIN + _chain_canonical(body).encode("ascii")).hexdigest() != obj["hash"]:
            return bad("HASH_MISMATCH", "the recomputed hash differs from the stored one: the line was modified", n)
        head, expected, verified = obj["hash"], expected + 1, verified + 1
    if legacy:
        return result("UNAVAILABLE", "NO_CHAIN_FIELDS", "the log has no chain fields (written before chaining, or stripped): it cannot be verified") if lines else (
            result("INVALID", "EMPTY_FILE", "the file holds no complete event"))
    if tail:
        return result("INVALID", "INCOMPLETE_FINAL_RECORD", "the last line has no line ending: the log may have been cut short", verified, head, len(lines) + 1)
    if expected_head is not None:
        if not isinstance(expected_head, str) or not _HEX64.match(expected_head):
            return result("INVALID", "MALFORMED_EXPECTED_HEAD", "the expected head is not 64 lower-case hex characters", verified, head)
        if expected_head != head:
            return result("INVALID", "HEAD_MISMATCH", "the last event's hash differs from the recorded head: events were cut off, added or replaced", verified, head)
    return result("VALID", None, f"all {verified} events verify", verified, head, anchored=expected_head is not None)


_PROTECTION_NOTE = {
    "LOCAL_OR_UNDECLARED": "Not an independent trust anchor: it was not declared to be held where the log's writer cannot change it (or it sits beside the log). "
                           "Whoever can rewrite the log could rewrite it to match.",
    "DECLARED_INDEPENDENT": "You declared this independently protected. The audit cannot see who can modify it and did not verify the declaration.",
}


def _protection(trust, beside):
    if trust == "independent" and not beside:
        return "DECLARED_INDEPENDENT", False
    return "LOCAL_OR_UNDECLARED", trust == "independent"


def _anchor_section(config, events_data, chain, events, source):
    """Anchor consistency, reported apart from chain integrity, accounting and authenticity. The anchor file is compared with the chain as it stands
    without any --expected-head (that option has its own entry and must not change how the anchor file is judged)."""
    plain = verify_chain(events_data) if config.expected_head is not None else chain
    if config.anchor_trust not in ("local", "independent"):
        raise ValueError("anchor_trust must be 'local' or 'independent'")
    parts = {}
    if config.expected_head is not None:
        protection, downgraded = _protection(config.anchor_trust, False)
        status = "MATCH" if chain["anchored"] else "MISMATCH"
        parts["expected_head"] = {"status": status, "reason": None if status == "MATCH" else chain["reason"], "protection": protection, "downgraded": downgraded,
                                  "detail": "compared with the head hash supplied directly" if status == "MATCH" else chain["detail"]}
    if config.anchor_path is not None:
        beside = anchor_module.beside(config.anchor_path, config.events_path, config.state_path)
        protection, downgraded = _protection(config.anchor_trust, beside)
        entry = {"protection": protection, "downgraded": downgraded, "beside_log_or_state": beside, "comparisons": {}}
        raw = _read(config.anchor_path)
        if raw is None:
            entry.update(status="UNAVAILABLE", reason="ANCHOR_FILE_MISSING", detail="the anchor file is missing or unreadable")
        else:
            entry["file"] = _file_info(raw)
            try:
                record = anchor_module.parse(raw)
            except anchor_module.AnchorError as error:
                entry.update(status="INVALID_ANCHOR", reason=error.code, detail=error.detail)
            else:
                entry.update(anchor_module.compare(record, events_data, plain, source, events or None))
                entry["anchor"] = {k: record[k] for k in ("source_id", "events", "head", "log_sha256", "log_bytes", "trades")}
        parts["anchor_file"] = entry
    if not parts:
        return {"status": "NOT_SUPPLIED", "reason": None, "detail": "no anchor or expected head was supplied: a log cut at an event boundary or rebuilt whole cannot be detected",
                "protection": "NOT_APPLICABLE", "authenticity": "NOT_ESTABLISHED", "reading": "No anchor comparison was made."}
    order = ("INVALID_ANCHOR", "MISMATCH", "UNAVAILABLE", "MATCH")
    status = min((p["status"] for p in parts.values()), key=order.index)
    protection = "DECLARED_INDEPENDENT" if all(p["protection"] == "DECLARED_INDEPENDENT" for p in parts.values()) else "LOCAL_OR_UNDECLARED"
    if status == "MATCH":
        reading = ("The log is identical to what the anchor recorded. " + (
            "You declared the anchor independently protected; if that is true, the log has not been changed since the anchor was made."
            if protection == "DECLARED_INDEPENDENT" else "The anchor is NOT independently protected, so this is a consistency check only: whoever could change the log could change the anchor."))
    else:
        reading = "The log does not match the anchor, or the comparison could not be made. Nothing was repaired; see the reason."
    return {"status": status, "protection": protection, "protection_note": _PROTECTION_NOTE[protection], "parts": parts, "authenticity": "NOT_ESTABLISHED",
            "reading": reading + " None of this shows that the events or quotes are genuine."}


def _unavailable(reason):
    return {"unavailable": reason}


def _fmt(value):
    return format(value if -value.as_tuple().exponent >= 2 else value.quantize(CENT), "f")


def _dec(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("not a decimal string")
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError("not finite")
    return number


def _when(value):
    moment = datetime.fromisoformat(value) if isinstance(value, str) else None
    if moment is None or moment.utcoffset() is None:
        raise ValueError("not an aware time")
    return moment


def _no_dupes(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def _no_constants(name):
    raise ValueError(name)


def _read(path):
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


class _Checks:
    def __init__(self):
        self.log = {check: {"fail": [], "block": [], "skip": None, "not_checked": None} for check, _ in CHECKS}

    def fail(self, check, message):
        self.log[check]["fail"].append(message)

    def block(self, check, message):
        self.log[check]["block"].append(message)

    def skip(self, check, why):
        self.log[check]["skip"] = why

    def not_checked(self, check, why):
        self.log[check]["not_checked"] = why

    @property
    def blocked(self):
        return any(entry["block"] for entry in self.log.values())

    def table(self):
        rows = []
        for check, text in CHECKS:
            entry = self.log[check]
            if entry["block"]:
                status, notes = BLOCKED, entry["block"]
            elif entry["fail"]:
                status, notes = FAIL, entry["fail"]
            elif entry["skip"]:
                status, notes = SKIPPED, [entry["skip"]]
            elif entry["not_checked"]:
                status, notes = NOT_CHECKED, [entry["not_checked"]]
            else:
                status, notes = PASS, []
            rows.append({"id": check, "rule": text, "status": status, "notes": notes[:8], "note_count": len(notes)})
        return rows


# ---- parsing --------------------------------------------------------------------------------------------------------------------------------
def _json_lines(data, checks, check):
    """(objects, complete) from JSON-lines bytes; structural problems are BLOCKING for `check`."""
    if data is None:
        checks.block(check, "the file is missing or unreadable")
        return [], False
    if not data:
        checks.block(check, "the file is empty")
        return [], False
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        checks.block(check, "the file is not valid UTF-8")
        return [], False
    complete = text.endswith("\n")
    if not complete:
        checks.block(check, "the last line is incomplete (no final newline): the file may be truncated")
    objects = []
    for number, line in enumerate(text.split("\n")[:-1] if complete else text.split("\n")[:-1], 1):
        if not line.strip():
            checks.block(check, f"line {number} is blank")
            continue
        try:
            obj = json.loads(line, object_pairs_hook=_no_dupes, parse_constant=_no_constants)
        except ValueError:
            checks.block(check, f"line {number} is not valid JSON")
            continue
        if not isinstance(obj, dict):
            checks.block(check, f"line {number} is not a JSON object")
            continue
        objects.append(obj)
    return objects, complete


def _read_events(data, checks):
    events, _ = _json_lines(data, checks, "E1_EVENTS_PARSE")
    ok = True
    for index, event in enumerate(events, 1):
        if event.get("n") != index or isinstance(event.get("n"), bool):
            checks.block("E2_EVENTS_SEQUENCE", f"line {index} has n={event.get('n')!r}, expected {index} (missing, duplicated or reordered event)")
            ok = False
    previous = None
    opened = 0
    for index, event in enumerate(events, 1):
        if event.get("type") not in EVENT_TYPES:
            checks.block("E3_EVENTS_CLOCK", f"event {index} has an unknown type {event.get('type')!r}")
            continue
        try:
            moment = _when(event.get("clock"))
        except ValueError:
            checks.block("E3_EVENTS_CLOCK", f"event {index} has no valid timezone-aware clock")
            continue
        event["_clock"] = moment
        if previous is not None and moment < previous:
            checks.block("E3_EVENTS_CLOCK", f"event {index} moves the replay clock backwards")
        previous = moment if previous is None or moment >= previous else previous
        if event["type"] == "REPLAY_OPENED":
            opened += 1
            if index != 1:
                checks.block("E3_EVENTS_CLOCK", "REPLAY_OPENED is not the first event")
        if event["type"] == "RECORD_AVAILABLE":
            try:
                if _when(event.get("available_at")) > moment:
                    checks.fail("E3_EVENTS_CLOCK", f"event {index} shows a record before its available_at")
            except ValueError:
                checks.block("E3_EVENTS_CLOCK", f"event {index} has no valid available_at")
    if events and opened != 1:
        checks.block("E3_EVENTS_CLOCK", f"{opened} REPLAY_OPENED events (exactly one is required)")
    if not events:
        checks.block("E3_EVENTS_CLOCK", "no events")
    return events, ok


def _read_state(data, checks):
    objects, _ = _json_lines(data, checks, "S1_STATE_PARSE")
    if not objects:
        return None, []
    header, trades = objects[0], objects[1:]
    if header.get("kind") != "header" or header.get("schema") != 1:
        checks.block("S1_STATE_PARSE", "the first line is not a schema-1 header")
        return None, []
    try:
        header = {**header, "_start": _dec(header["starting_balance"])}
    except (KeyError, ValueError, InvalidOperation):
        checks.block("S1_STATE_PARSE", "the header has no valid starting balance")
        return None, []
    parsed = []
    for number, trade in enumerate(trades, 1):
        try:
            if trade.get("kind") != "trade":
                raise ValueError("kind")
            row = dict(trade)
            for name in ("quote_bid", "quote_ask", "fill_price", "slippage_per_unit", "cost", "turnover"):
                row["_" + name] = _dec(trade[name])
            row["_realized"] = None if trade["realized_pnl"] is None else _dec(trade["realized_pnl"])
            row["_time"] = _when(trade["engine_time"])
            for name in ("seq", "lots", "lot_size", "quantity"):
                if type(trade[name]) is not int:
                    raise ValueError(name)
            if trade["side"] not in ("BUY", "SELL") or not all(isinstance(trade[n], str) for n in ("symbol", "provider", "data_origin", "data_state", "connection")):
                raise ValueError("labels")
            parsed.append(row)
        except (KeyError, ValueError, InvalidOperation, TypeError):
            checks.block("S1_STATE_PARSE", f"trade line {number} is not a valid trade record")
    return header, parsed


# ---- the independent rebuild ----------------------------------------------------------------------------------------------------------------
@dataclass
class _Step:
    cash: Decimal
    positions: dict          # symbol -> (quantity, basis, buy_fees, lot_size)
    realized_total: Decimal
    realized_trade: "Decimal | None"
    fees: Decimal


def _snapshot(positions):
    return {s: tuple(v) for s, v in sorted(positions.items())}


def _rebuild(header, trades, fee_fixed, fee_fraction, checks):
    """Apply the saved trades one by one with this module's own arithmetic. Returns steps[0..N] (steps[0] is the opening state)."""
    cash, positions, realized_total, fees_total = header["_start"], {}, Decimal("0"), Decimal("0")
    steps = [_Step(cash, {}, realized_total, None, fees_total)]
    lifecycle = {}
    for trade in trades:
        seq, symbol, qty, price = trade["seq"], trade["symbol"], trade["quantity"], trade["_fill_price"]
        turnover, cost = trade["_turnover"], trade["_cost"]
        if qty <= 0 or qty != trade["lots"] * trade["lot_size"] or trade["lots"] < 1 or trade["lot_size"] < 1:
            checks.fail("S4_TRADE_ARITHMETIC", f"trade {seq}: quantity {qty} is not lots x lot size")
        if turnover != (price * qty).quantize(CENT, ROUND_HALF_UP):
            checks.fail("S4_TRADE_ARITHMETIC", f"trade {seq}: turnover is not fill price x quantity")
        if cost != (fee_fixed + turnover * fee_fraction).quantize(CENT, ROUND_HALF_UP):
            checks.fail("A3_FEES_ONCE", f"trade {seq}: fee {cost} differs from the fee model ({fee_fixed} + {fee_fraction} x turnover)")
        slip = trade["_slippage_per_unit"]
        touch = trade["_quote_ask"] + slip if trade["side"] == "BUY" else trade["_quote_bid"] - slip
        if slip <= 0 or price != touch or not 0 < trade["_quote_bid"] < trade["_quote_ask"]:
            checks.fail("S4_TRADE_ARITHMETIC", f"trade {seq}: fill price is not the touch {'plus' if trade['side'] == 'BUY' else 'minus'} the slippage on a valid quote")
        realized = None
        held = positions.get(symbol)
        if trade["side"] == "BUY":
            if held is not None and held[3] != trade["lot_size"]:
                checks.fail("A2_POSITIONS", f"trade {seq}: lot size {trade['lot_size']} differs from the open position's {held[3]}")
            cash -= turnover + cost
            positions[symbol] = ((held[0] if held else 0) + qty, (held[1] if held else Decimal("0")) + turnover, (held[2] if held else Decimal("0")) + cost,
                                 trade["lot_size"])
            life = lifecycle.setdefault(symbol, [Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")])
            life[0] += turnover
            life[1] += cost
            if trade["_realized"] is not None:
                checks.fail("A5_REALIZED", f"trade {seq}: a buy carries realised P&L")
        else:
            if held is None or qty > held[0] or held[3] != trade["lot_size"]:
                checks.fail("A2_POSITIONS", f"trade {seq}: sells {qty} of {symbol} but the rebuilt position is {held[0] if held else 0} (no short selling)")
            else:
                if qty == held[0]:
                    basis_out, fees_out = held[1], held[2]
                else:
                    share = Decimal(qty) / Decimal(held[0])
                    basis_out, fees_out = (held[1] * share).quantize(CENT, ROUND_HALF_UP), (held[2] * share).quantize(CENT, ROUND_HALF_UP)
                realized = turnover - basis_out - fees_out - cost
                cash += turnover - cost
                left = (held[0] - qty, held[1] - basis_out, held[2] - fees_out, held[3])
                life = lifecycle[symbol]
                life[2] += basis_out
                life[3] += fees_out
                if left[1] < 0 or left[2] < 0 or life[0] != life[2] + left[1] or life[1] != life[3] + left[2]:
                    checks.fail("A4_COST_BASIS", f"trade {seq}: premium or buy fees are not conserved across the exit")
                if left[0] == 0:
                    if left[1] != 0 or left[2] != 0:
                        checks.fail("A4_COST_BASIS", f"trade {seq}: a closed position keeps a cost remainder")
                    del positions[symbol]
                    del lifecycle[symbol]
                else:
                    positions[symbol] = left
                realized_total += realized
                if trade["_realized"] != realized:
                    checks.fail("A5_REALIZED", f"trade {seq}: stored realised P&L {trade['_realized']} differs from the recomputed {realized}")
        fees_total += cost
        steps.append(_Step(cash, _snapshot(positions), realized_total, realized, fees_total))
    return steps


def _claim_matches(claim, step):
    """Does an engine claim {cash, positions, realized_total?} equal the rebuilt step? Returns a list of differences."""
    problems = []
    try:
        if _dec(claim["cash"]) != step.cash:
            problems.append(f"cash {claim['cash']} vs rebuilt {step.cash}")
        held = claim["positions"]
        if not isinstance(held, dict) or set(held) != set(step.positions):
            problems.append("open positions differ")
        else:
            for symbol, (qty, basis, fees, _) in step.positions.items():
                c = held[symbol]
                if c["quantity"] != qty or _dec(c["basis"]) != basis or _dec(c["buy_fees"]) != fees:
                    problems.append(f"position {symbol} differs")
        if "realized_total" in claim and _dec(claim["realized_total"]) != step.realized_total:
            problems.append(f"realised total {claim['realized_total']} vs rebuilt {step.realized_total}")
    except (KeyError, ValueError, TypeError, InvalidOperation):
        problems.append("the claim is malformed")
    return problems


# ---- the report -----------------------------------------------------------------------------------------------------------------------------
def build_report(config):
    checks = _Checks()
    events_data, state_data = _read(config.events_path), _read(config.state_path)
    events, _ = _read_events(events_data, checks)
    header, trades = _read_state(state_data, checks)
    opened = next((e for e in events if e.get("type") == "REPLAY_OPENED"), None)
    inputs = {"events": _file_info(events_data), "state": _file_info(state_data), "fixtures": _unavailable("NOT_SUPPLIED")}

    # --- identity (S2 / X2)
    source = None
    if header is not None:
        account = header.get("account")
        if not isinstance(account, str) or not account.startswith("replay_") or len(account) == 7:
            checks.block("S2_ACCOUNT_IDENTITY", f"the state belongs to account {account!r}, not a replay account")
        else:
            source = account[7:]
            if Path(config.state_path).name != f"replay_{source}.jsonl":
                checks.fail("S2_ACCOUNT_IDENTITY", "the state file's name does not match its account")
            for trade in trades:
                if (trade["data_origin"], trade["provider"], trade["data_state"], trade["connection"]) != ("HISTORICAL", f"historical:{source}", "HISTORICAL_REPLAY", "REPLAY"):
                    checks.fail("S2_ACCOUNT_IDENTITY", f"trade {trade['seq']} is not a historical replay trade of source {source}")
    if opened is not None and source is not None and opened.get("source_id") != source:
        checks.fail("X2_SOURCE_IDENTITY", f"events name source {opened.get('source_id')!r}, the state account names {source!r}")
    for index, trade in enumerate(trades, 1):
        if trade["seq"] != index:
            checks.block("S3_STATE_SEQUENCE", f"trade line {index} has seq {trade['seq']} (missing, duplicated or reordered trade)")
        if index > 1 and trade["_time"] < trades[index - 2]["_time"]:
            checks.block("S3_STATE_SEQUENCE", f"trade {trade['seq']} is earlier than the trade before it")

    pairing = _pairing(opened, header, source, checks)

    # --- parameters taken from the events (as claims) unless the caller overrides them
    restored = 0
    fee_fixed = fee_fraction = None
    mark_age = config.mark_max_age
    if opened is not None:
        try:
            restored = opened["restored_trades"]
            if type(restored) is not int or not 0 <= restored <= len(trades):
                raise ValueError("restored")
            claimed_fee, claimed_fraction = _dec(opened["fee_per_trade"]), _dec(opened["fee_turnover_fraction"])
            fee_fixed = config.fee_per_trade if config.fee_per_trade is not None else claimed_fee
            fee_fraction = config.fee_fraction if config.fee_fraction is not None else claimed_fraction
            if (fee_fixed, fee_fraction) != (claimed_fee, claimed_fraction):
                checks.fail("A3_FEES_ONCE", "the fee model given to the audit differs from the one the replay states")
            if header is not None and _dec(opened["starting_balance"]) != header["_start"]:
                checks.fail("A1_CASH", "the events and the state header give different starting balances")
            if mark_age is None:
                mark_age = timedelta(seconds=float(opened["max_quote_age_seconds"]))
        except (KeyError, ValueError, TypeError, InvalidOperation):
            checks.block("E3_EVENTS_CLOCK", "REPLAY_OPENED lacks the fields the audit needs (restored trades, fee model, balance, quote age)")
    blocked = checks.blocked or header is None or opened is None or fee_fixed is None
    steps = None
    if not blocked:
        steps = _rebuild(header, trades, fee_fixed, fee_fraction, checks)

    # --- fixtures (F1) -------------------------------------------------------------------------------------------------------------------
    fixtures = None
    if config.fixture_dir is None:
        checks.not_checked("F1_FIXTURES", "no fixture folder was supplied: quote references, availability events and exclusions are not verified")
        checks.not_checked("M1_MARKS", "no fixture folder was supplied: open P&L cannot be marked")
    elif blocked:
        checks.skip("F1_FIXTURES", "skipped: the events or state are not trustworthy")
        checks.skip("M1_MARKS", "skipped: the events or state are not trustworthy")
    else:
        fixtures = _load_fixtures(config.fixture_dir, opened, source, checks, inputs)
    if blocked:
        for check in ("I1_INPUT_IDENTITY", "PL1_PLAN_IDENTITY"):
            if (config.fixture_dir is not None or check == "PL1_PLAN_IDENTITY") and not checks.log[check]["block"]:
                checks.skip(check, "skipped: the events or state are not trustworthy")
        for check in ("S4_TRADE_ARITHMETIC", "A1_CASH", "A2_POSITIONS", "A3_FEES_ONCE", "A4_COST_BASIS", "A5_REALIZED", "A6_REJECTIONS", "X1_EVENT_TRADE_MATCH", "E4_INTENT_PAIRING", "E5_EVENTS_COMPLETE"):
            if not checks.log[check]["block"]:
                checks.skip(check, "skipped: the events or state are not trustworthy, so no reconciliation was attempted")

    identity_section = _identity_section(opened, config, source, checks, blocked, events)
    rows = _walk(events, trades, steps, restored, opened, fixtures, mark_age, checks, blocked, source)
    table = checks.table()
    statuses = {t["status"] for t in table}
    verdict = "NOT_RECONCILABLE" if BLOCKED in statuses else "INCONSISTENT" if FAIL in statuses else "CONSISTENT"
    coverage = "FULL" if not statuses & {NOT_CHECKED, SKIPPED} else "PARTIAL"
    final = steps[-1] if steps else None
    chain = verify_chain(events_data, config.expected_head)
    anchor_section = _anchor_section(config, events_data, chain, events, source)
    body = {
        "report_version": REPORT_VERSION,
        "notice": "Consistency of the supplied files with each other and with the stated rules. This does not prove that the files, the fixtures or the quotes are genuine.",
        "verdict": verdict, "coverage": coverage,
        "inputs": inputs,
        "account": {"name": header.get("account") if header else _unavailable("STATE_UNUSABLE"), "source_id": source or _unavailable("STATE_UNUSABLE"),
                    "starting_balance": _fmt(header["_start"]) if header else _unavailable("STATE_UNUSABLE"),
                    "state_kind": ("INITIALIZED_NO_TRADES" if not trades else "INITIALIZED_WITH_TRADES") if header else _unavailable("STATE_UNUSABLE")},
        "summary": {"events": len(events), "saved_trades": len(trades), "restored_trades": restored if not blocked else _unavailable("BLOCKED"),
                    "rebuilt_cash": _fmt(final.cash) if final else _unavailable("RECONCILIATION_BLOCKED"),
                    "rebuilt_realized_total": _fmt(final.realized_total) if final else _unavailable("RECONCILIATION_BLOCKED"),
                    "rebuilt_fees_total": _fmt(final.fees) if final else _unavailable("RECONCILIATION_BLOCKED"),
                    "rebuilt_open_positions": ({s: {"quantity": v[0], "basis": _fmt(v[1]), "buy_fees": _fmt(v[2])} for s, v in final.positions.items()}
                                               if final else _unavailable("RECONCILIATION_BLOCKED")),
                    "decisions": _decision_counts(events)},
        "inputs_identity": identity_section,
        "pairing": pairing,
        "chain": chain,
        "anchor": anchor_section,
        "trust": {"accounting": verdict, "chain_integrity": chain["status"], "anchor_consistency": anchor_section["status"],
                  "anchor_protection": anchor_section["protection"], "authenticity": "NOT_ESTABLISHED",
                  "reading": "Four separate questions: do the books reconcile, is the log internally intact, does it match an anchor, and is any of it genuine. "
                             "Only the first three are examined here, and the third is as strong as the anchor's protection."},
        "checks": table,
        "exclusions": fixtures["exclusions"] if fixtures else _unavailable("FIXTURES_NOT_VERIFIED"),
        "rows": rows,
    }
    body["digest"] = hashlib.sha256(json.dumps({k: v for k, v in body.items()}, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return body


def _claimed_identity(opened):
    claimed = opened.get("inputs") if opened else None
    return claimed if isinstance(claimed, dict) and isinstance(claimed.get("inputs"), dict) else None


def _pairing(opened, header, source, checks):
    """Does the state file belong to the same replay as the event log? Compares the account and the input binding the events claim with the state header's.
    A state of another source, account or inputs is never reconciled against these events (the check blocks)."""
    claimed = _claimed_identity(opened)
    if opened is None or header is None:
        return {"status": "NOT_ESTABLISHED", "reason": "the event log or the state file is unusable", "events": _unavailable("NO_USABLE_EVENTS"),
                "state": _unavailable("NO_USABLE_STATE")}
    state = {"account": header.get("account", _unavailable("NOT_RECORDED")), "binding": header.get("binding", _unavailable("NOT_RECORDED"))}
    if claimed is None:
        checks.not_checked("P1_PAIRING", "the events do not state their input identity (written before it existed): pairing with the state is not established")
        return {"status": "NOT_ESTABLISHED", "reason": "the events carry no input identity", "events": _unavailable("NOT_RECORDED"), "state": state}
    events_side = {"source_id": claimed["inputs"].get("source_id"), "account": claimed["inputs"].get("account"), "account_binding": claimed.get("account_binding"),
                   "identity_digest": claimed.get("digest")}
    reasons = []
    if state["account"] != events_side["account"]:
        reasons.append(f"the state belongs to account {state['account']!r} but the events claim {events_side['account']!r} (another source or account)")
    elif isinstance(state["binding"], dict):
        reasons.append("the state header has no binding to any inputs, so it cannot be shown to belong to this replay")
    elif state["binding"] != events_side["account_binding"]:
        reasons.append("the state was set up on different inputs or settings than the events claim (another replay of this source)")
    for reason in reasons:
        checks.block("P1_PAIRING", reason)
    return {"status": "MISMATCH" if reasons else "PAIRED", "reasons": reasons, "events": events_side, "state": state}


def _identity_section(opened, config, source, checks, blocked, events):
    """What inputs the replay claims, whether they agree with each other and with the fixtures supplied, and what the completion recheck found."""
    claimed = _claimed_identity(opened)
    section = {"status": "NOT_CHECKED", "claimed": _unavailable("NOT_RECORDED"), "recomputed": _unavailable("FIXTURES_NOT_SUPPLIED"), "differences": [],
               "completion_recheck": {"status": "NOT_RECORDED", "differences": []}, "method": "the fixture-dependent part is recomputed from the fixture folder supplied; "
               "the rest is compared with the replay's own stated configuration. Start and end checks cannot show an input was never changed and restored in between."}
    if opened is None or claimed is None:
        checks.not_checked("I1_INPUT_IDENTITY", "the events do not state their input identity (written before it existed)")
        section["plan"] = _plan_section(None, events, checks, blocked)
        return section
    section["claimed"] = {"digest": claimed.get("digest"), "account_binding": claimed.get("account_binding"), "version": claimed.get("version"),
                          "manifest_sha256": claimed["inputs"].get("manifest_sha256"), "source_id": claimed["inputs"].get("source_id"),
                          "account": claimed["inputs"].get("account"), "window": claimed.get("window"),
                          "fixtures": [{k: f.get(k) for k in ("path", "sha256", "bytes", "record_type")} for f in claimed["inputs"].get("fixtures", []) if isinstance(f, dict)]}
    problems = []
    accounting, window = claimed.get("accounting"), claimed.get("window")
    if not isinstance(accounting, dict) or not isinstance(window, dict):
        problems.append("the claimed identity lacks its accounting or window section")
    else:
        for name, stated in (("as_of", opened.get("as_of")), ("starting_balance", opened.get("starting_balance")), ("permitted", opened.get("permitted")),
                             ("slippage_ticks", opened.get("slippage_ticks")), ("max_quote_age_seconds", opened.get("max_quote_age_seconds")),
                             ("fee_per_trade", opened.get("fee_per_trade")), ("fee_turnover_fraction", opened.get("fee_turnover_fraction"))):
            if accounting.get(name) != stated:
                problems.append(f"the claimed identity's {name} ({accounting.get(name)!r}) differs from the replay's stated {stated!r}")
        for name in ("start", "end"):
            if window.get(name) != opened.get(name):
                problems.append(f"the claimed identity's window {name} differs from the replay's stated {name}")
    if claimed["inputs"].get("source_id") != opened.get("source_id"):
        problems.append(f"the claimed identity names source {claimed['inputs'].get('source_id')!r} but the replay opened for {opened.get('source_id')!r}")
    if claimed["inputs"].get("account") != f"replay_{claimed['inputs'].get('source_id')}":
        problems.append("the claimed account is not replay_<source>")
    if identity_module.full_digest(claimed) != claimed.get("digest") or identity_module.binding_of(claimed) != claimed.get("account_binding"):
        problems.append("the claimed digest or account binding does not follow from the claimed parts (the claim contradicts itself)")
    recheck = [e for e in events if e.get("type") == "INPUTS_RECHECKED"]
    for earlier in recheck[:-1]:
        if earlier.get("status") != "UNCHANGED":
            problems.append("an earlier recheck found the inputs changed: " + "; ".join(str(d) for d in earlier.get("differences", [])) if isinstance(earlier.get("differences"), list) else "an earlier recheck is malformed")
    if recheck:
        last = recheck[-1]
        section["completion_recheck"] = {"status": last.get("status", "MALFORMED"), "differences": last.get("differences", []) if isinstance(last.get("differences"), list) else []}
        if last.get("status") == "CHANGED":
            problems.append("the replay found its inputs changed when it finished: " + "; ".join(str(d) for d in section["completion_recheck"]["differences"]))
        elif last.get("status") != "UNCHANGED":
            problems.append("the completion recheck has an unknown status")
        elif last.get("identity_digest") != claimed.get("digest"):
            problems.append("the completion recheck names a different identity digest than the one claimed at open")
    if config.fixture_dir is not None and not blocked and isinstance(accounting, dict) and isinstance(window, dict):
        root = Path(config.fixture_dir)
        try:
            manifest_bytes = (root / mf.MANIFEST_NAME).read_bytes()
            entries = mf.parse_manifest(manifest_bytes)
        except (OSError, mf.ManifestError) as error:
            section["recomputed"] = _unavailable("MANIFEST_UNUSABLE")
            problems.append(f"the supplied fixtures have no usable manifest ({error.__class__.__name__})")
        else:
            again = identity_module.build(manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(), entries=entries, source_id=claimed["inputs"].get("source_id"),
                                          accounting=accounting, window=window, plan=claimed.get("plan"))   # the plan is checked against the events by PL1
            section["recomputed"] = {"digest": again["digest"], "account_binding": again["account_binding"], "manifest_sha256": again["inputs"]["manifest_sha256"]}
            differences = identity_module.differences(claimed, again)
            differences += [f"fixture {failure.path} no longer matches its manifest line ({failure.code})" for failure in mf.verify(root, entries)]
            section["differences"] = differences
            problems += [f"the fixtures supplied are not the claimed inputs: {d}" for d in differences]
            section["status"] = "MISMATCH" if differences else "MATCH"
    elif config.fixture_dir is None:
        checks.not_checked("I1_INPUT_IDENTITY", "no fixture folder was supplied: the claimed inputs are not compared with any fixtures")
    for problem in problems:
        checks.fail("I1_INPUT_IDENTITY", problem)
    if problems and section["status"] != "MISMATCH":
        section["status"] = "MISMATCH" if section["status"] == "MATCH" else "INCONSISTENT_CLAIM"
    if not recheck and not problems:
        checks.not_checked("I1_INPUT_IDENTITY", "no completion recheck is recorded (an unfinished log, or one written before rechecks)")
    section["plan"] = _plan_section(claimed, events, checks, blocked)
    return section


def _plan_section(claimed, events, checks, blocked):
    """Compare the intent plan the replay claims with the ordered intents its events record. The claim is never trusted on its own: its digest is recomputed
    from its own intents, and every INTENT_RECEIVED event (and any intent refused before it could be recorded) must be the plan's next intent, at the
    plan's instant. A legacy log that states no plan is reported as not declared, never as matching."""
    section = {"status": "NOT_DECLARED", "claimed_digest": _unavailable("NOT_RECORDED"), "recomputed_digest": _unavailable("NOT_RECORDED"), "claimed_intents": 0,
               "observed_intents": 0, "differences": []}
    plan = claimed.get("plan") if claimed else None
    if claimed is None:
        checks.not_checked("PL1_PLAN_IDENTITY", "the events do not state their input identity (written before it existed), so no plan is claimed")
        return section
    if plan is None:
        checks.not_checked("PL1_PLAN_IDENTITY", "the replay did not declare an intent plan: its intents cannot be compared with any claim")
        return section
    if blocked:
        checks.skip("PL1_PLAN_IDENTITY", "skipped: the events or state are not trustworthy")
        section["status"] = "NOT_CHECKED"
        return section
    fields = ("at", "contract", "side", "lots")
    intents = plan.get("intents") if isinstance(plan, dict) else None
    if (not isinstance(plan, dict) or plan.get("version") != identity_module.PLAN_VERSION or not isinstance(intents, list) or "until" not in plan
            or not all(isinstance(i, dict) and set(i) == set(fields) for i in intents)):
        checks.fail("PL1_PLAN_IDENTITY", "the claimed plan is malformed")
        section["status"] = "MALFORMED"
        return section
    section["claimed_digest"] = plan.get("digest")
    section["claimed_intents"] = len(intents)
    section["recomputed_digest"] = identity_module.plan_digest({"version": plan["version"], "intents": intents, "until": plan["until"]})
    problems = []
    if section["recomputed_digest"] != plan.get("digest"):
        problems.append("the plan digest does not follow from the plan's own intents")
    try:
        times = [_when(i["at"]) for i in intents]
    except (ValueError, TypeError):
        times = None
        problems.append("a planned intent has no valid timezone-aware instant")
    if times is not None:
        if times != sorted(times):
            problems.append("the planned intents are not in execution (time) order")
        window = claimed.get("window") if isinstance(claimed.get("window"), dict) else {}
        try:
            start, end = _when(window["start"]), _when(window["end"])
            if any(not start <= t <= end for t in times):
                problems.append("a planned intent lies outside the replay window")
            if plan["until"] is not None and not start <= _when(plan["until"]) <= end:
                problems.append("the plan's until lies outside the replay window")
        except (KeyError, ValueError, TypeError):
            problems.append("the claimed window is unusable for checking the plan")
    observed = []
    for event in events:
        if event.get("type") == "INTENT_RECEIVED":
            observed.append(event)
        elif event.get("type") == "INTENT_REFUSED" and event.get("stage") == "RUNNER":
            observed.append(event)                       # an intent the runner could not even read: it is outside any well-formed plan
    section["observed_intents"] = len(observed)
    for index, event in enumerate(observed):
        if index >= len(intents):
            problems.append(f"event {event.get('n')}: an intent beyond the {len(intents)} the plan declares")
            break
        planned = intents[index]
        clock = event.get("_clock")
        got = {"at": clock.astimezone(timezone.utc).isoformat() if clock is not None else None, "contract": event.get("contract"), "side": event.get("side"),
               "lots": event.get("lots")}
        try:
            same_instant = _when(planned["at"]) == clock
        except (ValueError, TypeError):
            same_instant = False
        if event.get("type") != "INTENT_RECEIVED" or not same_instant or any(got[k] != planned[k] for k in fields if k != "at"):
            problems.append(f"event {event.get('n')}: intent {index + 1} is {got} but the plan declares {planned}")
            break
    finished = bool(events) and events[-1].get("type") == "CHECKPOINT"
    if len(observed) < len(intents) and not problems:
        if finished:
            problems.append(f"the log is finished but records {len(observed)} of the {len(intents)} planned intents")
        else:
            section["status"] = "INCOMPLETE"
            checks.not_checked("PL1_PLAN_IDENTITY", f"the log is unfinished: {len(observed)} of {len(intents)} planned intents recorded so far, all matching the plan")
            return section
    if plan["until"] is not None and times is not None and not problems:
        last = max((e["_clock"] for e in events if e.get("_clock") is not None), default=None)
        try:
            if last is not None and last > _when(plan["until"]):
                problems.append("an event is later than the plan's until")
        except (ValueError, TypeError):
            problems.append("the plan's until is not a valid instant")
    for problem in problems:
        checks.fail("PL1_PLAN_IDENTITY", problem)
    section["differences"] = problems
    section["status"] = "MISMATCH" if problems else "MATCH"
    return section


def _file_info(data):
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)} if data is not None else _unavailable("MISSING_FILE")


def _decision_counts(events):
    counts = {}
    for event in events:
        if event.get("type") == "FILL":
            key = "FILL"
        elif event.get("type") == "INTENT_REFUSED":
            key = f"REFUSED:{event.get('stage')}:{event.get('code')}"
        else:
            continue
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _load_fixtures(directory, opened, source, checks, inputs):
    root = Path(directory)
    try:
        entries = mf.load_manifest(root / mf.MANIFEST_NAME)
    except mf.ManifestError as error:
        checks.fail("F1_FIXTURES", f"invalid manifest: {error}")
        checks.skip("M1_MARKS", "skipped: the fixtures do not verify")
        return None
    failures = mf.verify(root, entries)
    for failure in failures:
        checks.fail("F1_FIXTURES", f"{failure.code} {failure.path}")
    if failures:
        checks.skip("M1_MARKS", "skipped: the fixtures do not verify")
        return None
    if {e.source_id for e in entries} != {source}:
        checks.fail("X2_SOURCE_IDENTITY", "the fixture manifest names a different source than the replay account")
    try:
        as_of = _when(opened["as_of"])
    except (KeyError, ValueError):
        checks.fail("F1_FIXTURES", "REPLAY_OPENED has no valid as_of to re-ingest with")
        checks.skip("M1_MARKS", "skipped: no cut-off to re-ingest with")
        return None
    result = ingest(root, entries, as_of=as_of)
    if result.integrity_failures:
        checks.fail("F1_FIXTURES", "re-ingestion found integrity failures")
    if result.digest() != opened.get("ingest_digest"):
        checks.fail("F1_FIXTURES", "the fixtures re-ingest to a different digest than the one recorded when the replay opened")
    inputs["fixtures"] = {"manifest_sha256": hashlib.sha256((root / mf.MANIFEST_NAME).read_bytes()).hexdigest(), "files": len(entries),
                          "ingest_digest": result.digest()}
    by_ref = {f"{r.source_file}#{r.source_row}": r for r in result.records}
    exclusions = [{"source_file": e.source_file, "source_row": e.source_row if e.source_row is not None else _unavailable("FILE_LEVEL"),
                   "reason": e.reason, "detail": e.detail, "other_copy": e.other_copy if e.other_copy else _unavailable("NOT_APPLICABLE")} for e in result.exclusions]
    return {"records": result.records, "by_ref": by_ref, "exclusions": exclusions}


def _visible_quotes(fixtures, symbol, moment):
    return [r for r in fixtures["records"] if r.kind == QUOTE and r.key == symbol and r.available_at is not None and r.available_at <= moment
            and r.observed_at is not None and r.observed_at <= moment]


def _mark(fixtures, symbol, moment, max_age):
    """The bid usable to mark `symbol` at `moment`, or the reason there is none. Only fill-eligible fixture quotes that were already available count."""
    quotes = _visible_quotes(fixtures, symbol, moment)
    if not quotes:
        return None, "NO_VISIBLE_QUOTE"
    newest = max(quotes, key=lambda r: (r.available_at, r.observed_at))
    if newest.origin_class != "SYNTHETIC_FIXTURE" or not newest.fill_eligible:
        return None, "NEWEST_QUOTE_NOT_USABLE"
    if moment - newest.observed_at > max_age:
        return None, "MARK_STALE"
    return newest.get("bid"), None


def _walk(events, trades, steps, restored, opened, fixtures, mark_age, checks, blocked, source):
    rows = []
    k = restored if not blocked else 0                       # number of saved trades applied so far
    announced = set()
    pending = None                                           # the intent waiting for its decision
    last_clock = None
    for index, event in enumerate(events, 1):
        kind = event.get("type")
        clock = event.get("_clock")
        last_clock = clock or last_clock
        before_k = k
        row = {"seq": index, "n": event.get("n", _unavailable("MISSING")), "clock": clock.isoformat() if clock else _unavailable("INVALID_CLOCK"), "type": kind,
               "source_id": event.get("source_id", source) if kind == "REPLAY_OPENED" else (source or _unavailable("UNKNOWN")),
               "instrument": event.get("contract", event.get("key", _unavailable("NOT_APPLICABLE"))),
               "intent": None, "decision": None, "quote": _unavailable("NO_QUOTE_FOR_THIS_EVENT"), "fill": _unavailable("NOT_A_FILL"),
               "assumptions": _unavailable("NOT_A_FILL"), "reason": None}
        if kind in ("INTENT_RECEIVED", "FILL", "INTENT_REFUSED") and "contract" in event:
            row["intent"] = {"contract": event.get("contract"), "side": event.get("side"), "lots": event.get("lots")}
        trade = None
        if not blocked and kind == "FILL":
            if k < len(trades):
                trade = trades[k]
                k += 1
            else:
                checks.fail("X1_EVENT_TRADE_MATCH", f"event {index}: a FILL with no saved trade left to match")
        # --- pairing (E4)
        if not blocked:
            if kind == "INTENT_RECEIVED":
                if pending is not None:
                    checks.fail("E4_INTENT_PAIRING", f"event {index}: an intent arrives while the previous one has no decision")
                pending = event
            elif kind in ("FILL", "INTENT_REFUSED"):
                if event.get("stage") == "RUNNER":
                    pass
                elif pending is None:
                    checks.fail("E4_INTENT_PAIRING", f"event {index}: a decision with no intent")
                else:
                    if (pending.get("contract"), pending.get("side"), pending.get("lots"), pending.get("clock")) != (event.get("contract"), event.get("side"), event.get("lots"), event.get("clock")):
                        checks.fail("E4_INTENT_PAIRING", f"event {index}: the decision does not match the intent before it")
                    pending = None
            elif pending is not None and kind != "RECORD_AVAILABLE":
                checks.fail("E4_INTENT_PAIRING", f"event {index}: an intent has no decision before the next event")
                pending = None
        # --- accounting columns
        if blocked:
            cash_b = cash_a = pos_b = pos_a = real = unreal = _unavailable("RECONCILIATION_BLOCKED")
            row.update(cash_before=cash_b, cash_after=cash_a, positions_before=pos_b, positions_after=pos_a, realized_pnl=real, unrealized_pnl=unreal)
        else:
            sb, sa = steps[before_k if kind != "REPLAY_OPENED" else 0], steps[k]
            row["cash_before"], row["cash_after"] = _fmt(sb.cash), _fmt(sa.cash)
            row["positions_before"], row["positions_after"] = _pos_view(sb), _pos_view(sa)
            row["realized_pnl"] = {"this_trade": _fmt(sa.realized_trade) if trade is not None and sa.realized_trade is not None
                                   else _unavailable("BUY_HAS_NO_REALISED_PNL" if trade is not None else "NOT_A_FILL"), "cumulative": _fmt(sa.realized_total)}
            row["unrealized_pnl"] = _unrealized(sa, fixtures, clock, mark_age)
        # --- per-type details and claim checks
        if kind == "REPLAY_OPENED":
            row["decision"] = {"outcome": "OPENED"}
            row["reason"] = f"restored {restored} saved trade(s); {event.get('exclusions', '?')} source record(s) excluded at ingestion (see exclusions)"
            if not blocked:
                _claims(checks, "A1_CASH", event, steps[restored], index)
        elif kind == "RECORD_AVAILABLE":
            ref = event.get("ref")
            row["decision"] = {"outcome": "RECORD_AVAILABLE"}
            if ref in announced:
                checks.fail("F1_FIXTURES" if fixtures else "E3_EVENTS_CLOCK", f"event {index}: {ref} is announced twice")
            announced.add(ref)
            if fixtures:
                record = fixtures["by_ref"].get(ref)
                if record is None or (record.kind, record.key, record.time_key, record.available_at.isoformat()) != (event.get("kind"), event.get("key"), event.get("time_key"), event.get("available_at")):
                    checks.fail("F1_FIXTURES", f"event {index}: {ref} is not a record with those attributes in the fixtures")
        elif kind == "INPUTS_RECHECKED":
            row["decision"] = {"outcome": "INPUTS_RECHECKED", "status": event.get("status")}
            row["reason"] = "; ".join(str(d) for d in event.get("differences", [])) if event.get("differences") else "manifest and listed fixtures unchanged since open"
        elif kind == "INTENT_RECEIVED":
            row["decision"] = {"outcome": "INTENT_RECEIVED"}
        elif kind == "STATE_LOCKED":
            row["decision"] = {"outcome": "STATE_LOCKED"}
            row["reason"] = event.get("reason", _unavailable("NOT_STATED"))
        elif kind == "INTENT_REFUSED":
            row["decision"] = {"outcome": "REFUSED", "stage": event.get("stage"), "code": event.get("code")}
            row["reason"] = event.get("detail") or event.get("code")
            if event.get("quote_ref"):
                row["quote"] = _quote_view(event["quote_ref"], fixtures, trades, None)
            if not blocked and event.get("stage") != "RUNNER":
                problems = _claim_matches(event, steps[k]) if "cash" in event else ["the refusal carries no account claim"]
                for problem in problems:
                    checks.fail("A6_REJECTIONS", f"event {index}: a refused intent changed or misreports the account ({problem})")
        elif kind == "FILL":
            row["decision"] = {"outcome": "FILL"}
            if trade is not None and not blocked:
                _match_fill(checks, event, trade, index, fixtures, source)
                row["quote"] = _quote_view(event.get("quote_ref"), fixtures, trades, trade)
                row["fill"] = {"trade_seq": trade["seq"], "side": trade["side"], "lots": trade["lots"], "lot_size": trade["lot_size"], "quantity": trade["quantity"],
                               "fill_price": _fmt(trade["_fill_price"]), "turnover": _fmt(trade["_turnover"]), "fee": _fmt(trade["_cost"])}
                row["assumptions"] = {"slippage_per_unit": _fmt(trade["_slippage_per_unit"]), "slippage_ticks": _ticks(trade, event.get("quote_ref"), fixtures),
                                      "fee_model": f"{fee_text(opened)}", "fill_basis": trade["fill_basis"],
                                      "stored_assumptions": list(trade["assumptions"]) if isinstance(trade["assumptions"], list) else _unavailable("NOT_STORED")}
                _claims(checks, "A1_CASH", event, steps[k], index)
        elif kind == "CHECKPOINT":
            row["decision"] = {"outcome": "CHECKPOINT"}
            if not blocked:
                _claims(checks, "A1_CASH", event, steps[k], index)
                if event.get("trades") != k:
                    checks.fail("E5_EVENTS_COMPLETE", f"event {index}: the checkpoint counts {event.get('trades')} trades, the events account for {k}")
        rows.append(row)
    if not blocked:
        if pending is not None:
            checks.fail("E4_INTENT_PAIRING", "the last intent has no decision")
        if not events or events[-1].get("type") != "CHECKPOINT":
            checks.fail("E5_EVENTS_COMPLETE", "the events do not end with a checkpoint: the file may have been cut short")
        if k != len(trades):
            checks.fail("X1_EVENT_TRADE_MATCH", f"{len(trades) - k} saved trade(s) have no FILL event")
            checks.fail("E5_EVENTS_COMPLETE", "the state holds trades the events do not account for")
        if fixtures and last_clock is not None:
            due = {f"{r.source_file}#{r.source_row}" for r in fixtures["records"] if r.available_at <= last_clock}
            if due - announced:
                checks.fail("F1_FIXTURES", f"{len(due - announced)} record(s) were available by the last event but never announced (events may be missing)")
            if announced - due and fixtures:
                pass
    return rows


def fee_text(opened):
    try:
        return f"{opened['fee_per_trade']} + {opened['fee_turnover_fraction']} x turnover (as stated by the replay; illustrative)"
    except (KeyError, TypeError):
        return _unavailable("NOT_STATED")


def _claims(checks, check, event, step, index):
    if "cash" not in event:
        checks.fail(check, f"event {index}: no account claim to compare")
        return
    for problem in _claim_matches(event, step):
        target = "A5_REALIZED" if problem.startswith("realised") else "A2_POSITIONS" if problem.startswith(("position", "open positions")) else check
        checks.fail(target, f"event {index} ({event.get('type')}): engine claim differs from the rebuild ({problem})")


def _pos_view(step):
    return {s: {"quantity": v[0], "basis": _fmt(v[1]), "buy_fees": _fmt(v[2]), "lot_size": v[3]} for s, v in step.positions.items()}


def _unrealized(step, fixtures, clock, max_age):
    if not step.positions:
        return {"status": "AVAILABLE", "value": "0.00", "note": "no open positions"}
    if fixtures is None or clock is None:
        return {"status": "UNAVAILABLE", "reason": "NO_VERIFIED_QUOTE_DATA"}
    total, missing = Decimal("0"), {}
    for symbol, (qty, basis, _, _) in step.positions.items():
        bid, why = _mark(fixtures, symbol, clock, max_age)
        if bid is None:
            missing[symbol] = why
        else:
            total += (bid * qty - basis).quantize(CENT, ROUND_HALF_UP)
    if missing:
        return {"status": "UNAVAILABLE", "reason": "NO_USABLE_MARK", "positions": dict(sorted(missing.items()))}
    return {"status": "AVAILABLE", "value": _fmt(total), "basis": "bid x quantity - premium paid; indicative, before exit slippage and fees"}


def _ticks(trade, ref, fixtures):
    record = fixtures["by_ref"].get((ref or "").split(":")[0]) if fixtures else None
    tick = record.get("tick_size") if record else None
    return _fmt(trade["_slippage_per_unit"] / tick) if tick else _unavailable("TICK_UNKNOWN")


def _quote_view(ref, fixtures, trades, trade):
    out = {"reference": ref if ref else _unavailable("NOT_RECORDED")}
    if trade is not None:
        out.update(bid=_fmt(trade["_quote_bid"]), ask=_fmt(trade["_quote_ask"]), observed_at=trade["provider_time_raw"] if trade["provider_time_raw"] else _unavailable("NOT_RECORDED"))
    else:
        out.update(bid=_unavailable("NOT_A_FILL"), ask=_unavailable("NOT_A_FILL"), observed_at=_unavailable("NOT_A_FILL"))
    return out


def _match_fill(checks, event, trade, index, fixtures, source):
    same = {
        "seq": trade["seq"], "contract": trade["symbol"], "side": trade["side"], "lots": trade["lots"], "quantity": trade["quantity"],
        "fill_price": str(trade["fill_price"]), "cost": str(trade["cost"]), "turnover": str(trade["turnover"]), "provider": trade["provider"],
        "data_origin": trade["data_origin"], "slippage_per_unit": str(trade["slippage_per_unit"]),
        "realized_pnl": None if trade["_realized"] is None else str(trade["_realized"]),
    }
    for name, expected in same.items():
        got = event.get(name)
        if name in ("fill_price", "cost", "turnover", "slippage_per_unit", "realized_pnl") and got is not None and expected is not None:
            try:
                equal = _dec(got) == _dec(expected)
            except (ValueError, InvalidOperation):
                equal = False
        else:
            equal = got == expected
        if not equal:
            checks.fail("X1_EVENT_TRADE_MATCH", f"event {index}: {name} is {got!r} in the event but {expected!r} in saved trade {trade['seq']}")
    if event.get("_clock") != trade["_time"]:
        checks.fail("X1_EVENT_TRADE_MATCH", f"event {index}: the event clock differs from saved trade {trade['seq']}'s engine time")
    if fixtures is None:
        return
    ref = event.get("quote_ref") or ""
    location, _, digest = ref.partition(":")
    record = fixtures["by_ref"].get(location)
    if record is None or record.content_digest != digest:
        checks.fail("F1_FIXTURES", f"event {index}: quote reference {ref!r} is not a record in the verified fixtures")
        return
    clock = event["_clock"]
    problems = []
    if record.kind != QUOTE or not record.fill_eligible or record.origin_class != "SYNTHETIC_FIXTURE" or f"historical:{record.source_id}" != trade["provider"]:
        problems.append("is not a fill-eligible fixture quote of this source")
    if record.available_at > clock or record.observed_at > clock:
        problems.append("was not yet available at the fill's clock")
    if (record.get("bid"), record.get("ask")) != (trade["_quote_bid"], trade["_quote_ask"]) or record.key != trade["symbol"] or record.observed_at.isoformat() != trade["provider_time_raw"]:
        problems.append("does not match the quote stored with the trade")
    newest = max(_visible_quotes(fixtures, record.key, clock), key=lambda r: (r.available_at, r.observed_at), default=None)
    if newest is not None and newest is not record:
        problems.append("was not the newest visible quote for the instrument at that clock")
    if record.get("tick_size") and trade["_slippage_per_unit"] % record.get("tick_size"):
        problems.append("slippage is not a whole number of ticks")
    for problem in problems:
        checks.fail("F1_FIXTURES", f"event {index}: quote {ref.split(':')[0]} {problem}")


def render_text(report):
    """A short plain-text view of a report (the JSON is the record)."""
    chain = report["chain"]
    anchor = report["anchor"]
    anchor_text = (f"Anchor: {anchor['status']} (" + ("declared independent by you, not verified" if anchor["protection"] == "DECLARED_INDEPENDENT" else
                                                      "LOCAL or undeclared: NOT an independent trust anchor") + ")") if anchor["status"] != "NOT_SUPPLIED" else "Anchor: not supplied"
    identity_line = f"Inputs: {report['inputs_identity']['status']}   Pairing (state with events): {report['pairing']['status']}   Intent plan: {report['inputs_identity'].get('plan', {}).get('status', 'NOT_DECLARED')}"
    lines = [f"Replay audit report v{report['report_version']}   verdict: {report['verdict']}   coverage: {report['coverage']}", report["notice"],
             f"Event chain: {chain['status']}" + (f" ({chain['reason']} at line {chain['first_failure']['line']})" if chain["first_failure"] else
                                                  f" ({chain['reason']})" if chain["reason"] else f", {chain['events_verified']} events, anchored: {chain['anchored']}"),
             anchor_text, identity_line, "Authenticity: NOT ESTABLISHED (no check here can show the events are genuine)", ""]
    for check in report["checks"]:
        lines.append(f"  {check['status']:<12} {check['id']}")
        lines += [f"      - {note}" for note in check["notes"]]
    lines += ["", "  seq  clock                       type              outcome                      cash after     unrealised"]
    for row in report["rows"]:
        decision = row["decision"] or {}
        outcome = ":".join(str(v) for v in (decision.get("outcome"), decision.get("stage"), decision.get("code")) if v)
        cash = row["cash_after"] if isinstance(row["cash_after"], str) else "n/a"
        unreal = row["unrealized_pnl"]
        unreal = unreal["value"] if isinstance(unreal, dict) and unreal.get("status") == "AVAILABLE" else "n/a"
        lines.append(f"  {row['seq']:>3}  {str(row['clock']):<27} {row['type']:<17} {outcome:<28} {cash:>12} {unreal:>12}")
    lines += ["", f"digest {report['digest']}"]
    return "\n".join(lines) + "\n"
