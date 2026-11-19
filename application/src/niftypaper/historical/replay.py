"""A fixture-only chronological replay runner. It contains no strategy: the caller supplies scripted Intents (a time, a contract, BUY or SELL, lots),
and the runner only moves a monotonic clock, shows records once their available_at has passed, and passes each intent through the historical bridge and the
paper engine. It claims nothing about profitability.

Fixture-only by construction: every manifest entry must have origin class SYNTHETIC_FIXTURE and exactly one source id, or the runner refuses to open.
Any integrity failure (changed, missing or unlisted file) also refuses to open. Records that fail validation, or conflict, stay in the exclusion
ledger and the replay goes on without them. The replay account is its own: a dedicated state file `replay_<source id>.jsonl`, an engine bound to
origin HISTORICAL and to this one source, the engine's single-writer lock, daily risk budget and all other checks.

Events and the ledger contain no wall-clock time, so identical fixtures and configuration give identical output.

Input integrity policy. At open the manifest file is read once, hashed and parsed from those same bytes, and every listed fixture is read once, verified
against the manifest and parsed (ingest); the replay then runs ONLY on those in-memory records, so nothing that happens to the files afterwards can mix
into it. The first event states the run's input identity (identity.py). At completion, write_events re-reads the manifest and every listed fixture and
compares them with what was validated at open (and reports unlisted files): the result is recorded as an INPUTS_RECHECKED event, and if anything differs
the log is still published as evidence but write_events raises InputsChanged and the run is never reported as completed. These two checks detect many
changes; they cannot prove an input was never changed and then put back between them, and a change made while the replay runs cannot affect it (it uses
the snapshot) but is not noticed until the recheck.
"""
import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from ..paper.engine import BUY, ORIGIN_HISTORICAL, SELL, CostModel, PaperEngine, RiskLimits, TradeRequest
from ..paper.store import JsonlStore, StoreError
from . import audit, bridge, eventchain, gate, identity, publish
from . import manifest as mf
from .ingest import ingest
from .records import PARSER_VERSION, POLICY_VERSION, QUOTE, SCHEMA_VERSION

FIXTURE_ORIGIN = "SYNTHETIC_FIXTURE"


EventLogError = publish.PublishError    # the code is one of DESTINATION_EXISTS, PARTIAL_FILE_EXISTS, CANNOT_CREATE, WRITE_FAILED, READBACK_MISMATCH, CHAIN_NOT_VALID, PUBLISH_UNSUPPORTED


@dataclass(frozen=True)
class EventLogReceipt:
    path: Path
    events: int
    head: str                      # hash of the last event: record it elsewhere to detect a log cut at an event boundary
    sha256: str                    # SHA-256 of the whole file
    bytes: int


class InputsChanged(Exception):
    """The replay's inputs on disk no longer match what was validated at open. The event log was published as evidence (see `receipt`), but the run is not
    completed: runner.log_receipt stays None."""

    def __init__(self, differences, receipt=None):
        super().__init__("inputs changed during the replay: " + "; ".join(differences))
        self.differences, self.receipt = differences, receipt


class ReplayRefused(Exception):
    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


@dataclass(frozen=True)
class Intent:
    """A scripted wish to trade, supplied by the caller. It is not a signal and carries no logic."""
    at: datetime
    contract_key: str
    side: str
    lots: int


@dataclass(frozen=True)
class ReplayConfig:
    fixture_dir: Path
    state_dir: Path
    as_of: datetime                 # ingest cut-off: observations after it are excluded
    start: datetime                 # the replay clock's first instant
    end: datetime                   # the replay never goes past this (must not be after as_of)
    permitted: bool = False         # explicit opt-in to trading on the fixture data
    starting_balance: Decimal = Decimal("1000000")
    limits: object = None
    costs: object = None
    slippage_ticks: int = 1
    max_quote_age: timedelta = timedelta(seconds=5)


def _plan_of(intents, until, config):
    """The canonical plan, or ReplayRefused. Instants must lie inside the window; an `until` beyond the end is refused as run() would refuse it."""
    script = list(intents)
    if any(not isinstance(i, Intent) for i in script):
        raise ReplayRefused("BAD_INTENT", "the script holds something that is not an Intent")
    try:
        plan = identity.plan(script, until)
    except ValueError as error:
        raise ReplayRefused("BAD_INTENT", str(error)) from None
    if any(not config.start <= i.at <= config.end for i in script) or (until is not None and not config.start <= until <= config.end):
        raise ReplayRefused("PLAN_OUTSIDE_WINDOW", "every intent and `until` must lie between the replay's start and end")
    return plan


def _aware(moment, name):
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        raise ReplayRefused("BAD_CONFIG", f"{name} must be a timezone-aware datetime")


class ReplayRunner:
    def __init__(self, config, result, clock, engine, source_id, inputs, entries, manifest_sha256):
        self._plan_run = False
        self.config, self.result, self.clock, self.engine, self.source_id = config, result, clock, engine, source_id
        self.inputs, self._entries, self._manifest_sha256 = inputs, entries, manifest_sha256   # the identity claimed and the snapshot it was built from
        self.evidence_receipt = None       # set by a write_events that published a log, even when the inputs had changed
        self._view = gate.PointInTimeView(result.records, clock)
        self._announced = set()
        self.events = []
        self._head = eventchain.GENESIS
        self.log_receipt = None            # set only by a write_events that completed
        self._closed = False

    @classmethod
    def open(cls, config, intents=None, until=None):
        """Open a replay. With `intents` (and optionally `until`) the exact ordered script is declared up front: its plan digest becomes part of the input
        identity in the first event, and run() then accepts only that script, once. Without them the identity states no plan (the audit reports that)."""
        for name in ("as_of", "start", "end"):
            _aware(getattr(config, name), name)
        plan = None
        if intents is not None:
            plan = _plan_of(intents, until, config)
        if not config.start <= config.end <= config.as_of:
            raise ReplayRefused("BAD_CONFIG", "need start <= end <= as_of (a replay may not run past the ingest cut-off)")
        root = Path(config.fixture_dir)
        try:
            manifest_bytes = (root / mf.MANIFEST_NAME).read_bytes()                  # read once: these exact bytes are hashed and parsed
        except OSError as error:
            raise ReplayRefused("INVALID_MANIFEST", f"cannot read manifest: {error.__class__.__name__}") from None
        try:
            entries = mf.parse_manifest(manifest_bytes)
        except mf.ManifestError as error:
            raise ReplayRefused("INVALID_MANIFEST", str(error)) from None
        if any(e.origin_class != FIXTURE_ORIGIN for e in entries):
            raise ReplayRefused("NOT_FIXTURE_ONLY", "this runner accepts only SYNTHETIC_FIXTURE manifests")
        sources = {e.source_id for e in entries}
        if len(sources) != 1:
            raise ReplayRefused("MIXED_SOURCES", "a replay account uses exactly one data source: " + ", ".join(sorted(sources)))
        source_id = next(iter(sources))
        result = ingest(root, entries, as_of=config.as_of)
        if result.integrity_failures:
            raise ReplayRefused("INTEGRITY_FAILURE", "; ".join(f"{f.code} {f.path}" for f in result.integrity_failures))
        clock = gate.ReplayClock(config.start)
        costs, limits = config.costs or CostModel(), config.limits or RiskLimits()
        accounting = {"as_of": config.as_of.isoformat(), "starting_balance": str(config.starting_balance), "permitted": config.permitted,
                      "slippage_ticks": config.slippage_ticks, "max_quote_age_seconds": str(config.max_quote_age.total_seconds()),
                      "fee_per_trade": str(costs.fee_per_trade), "fee_turnover_fraction": str(costs.turnover_fraction),
                      "limits": {name: str(value) for name, value in sorted(asdict(limits).items())}, "allowed_origin_classes": [FIXTURE_ORIGIN],
                      "parser_version": PARSER_VERSION, "schema_version": SCHEMA_VERSION, "policy_version": POLICY_VERSION, "chain_version": eventchain.VERSION}
        claim = identity.build(manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(), entries=entries, source_id=source_id, accounting=accounting,
                               window={"start": config.start.isoformat(), "end": config.end.isoformat()}, plan=plan)
        store = JsonlStore(Path(config.state_dir) / f"replay_{source_id}.jsonl", config.starting_balance, account=f"replay_{source_id}",
                           binding=claim["account_binding"], require_initialized=True)
        try:
            store.initialize()                   # a zero-trade account is a header-only file, created now; any other problem is reported by the engine's own restore
        except StoreError as error:
            if error.code in ("WRITE_FAILED", "LOCK_UNAVAILABLE", "NOT_BOUND"):
                store.close()
                raise ReplayRefused("STATE_NOT_INITIALIZED", f"{error.code}: {error.detail}") from None
        engine = PaperEngine(config.starting_balance, costs=config.costs, limits=config.limits, slippage_ticks=config.slippage_ticks, clock=clock,
                             store=store, origin=ORIGIN_HISTORICAL, provider=f"historical:{source_id}", max_quote_age=config.max_quote_age)
        if engine.ledger and engine.ledger[-1].engine_time > config.start:
            engine.close()
            raise ReplayRefused("STATE_AHEAD_OF_CLOCK", "the saved replay account already has trades after the requested start time")
        runner = cls(config, result, clock, engine, source_id, claim, entries, hashlib.sha256(manifest_bytes).hexdigest())
        costs = engine.costs
        runner._emit("REPLAY_OPENED", inputs=claim, source_id=source_id, records=len(result.records), exclusions=len(result.exclusions), restored_trades=engine.restored,
                     ingest_digest=result.digest(), as_of=config.as_of.isoformat(), start=config.start.isoformat(), end=config.end.isoformat(),
                     starting_balance=str(config.starting_balance), permitted=config.permitted, slippage_ticks=config.slippage_ticks,
                     max_quote_age_seconds=str(config.max_quote_age.total_seconds()), fee_per_trade=str(costs.fee_per_trade),
                     fee_turnover_fraction=str(costs.turnover_fraction), **runner._claims())
        if engine.lock_reason:
            runner._emit("STATE_LOCKED", reason=engine.lock_reason)
        runner._announce()
        return runner

    # ---- events ----------------------------------------------------------------------------------------------------------------------------
    def _emit(self, event_type, **fields):
        event = eventchain.seal({"n": len(self.events) + 1, "clock": self.clock.now.isoformat(), "type": event_type, **fields}, self._head)
        self.events.append(event)          # sealing happens first: an event that cannot be chained raises here and is never half-recorded
        self._head = event["hash"]
        return event

    @property
    def head(self):
        """Hash of the newest event (the chain genesis before the first one)."""
        return self._head

    def _claims(self):
        """What the engine says its account holds right now. These are claims for the audit report to check, not values it copies."""
        return {"cash": str(self.engine.cash), "realized_total": str(self.engine.realized_pnl),
                "positions": {s: {"quantity": p.quantity, "basis": str(p.basis), "buy_fees": str(p.cost_paid)} for s, p in sorted(self.engine.positions.items())}}

    def recheck_inputs(self):
        """Re-read the manifest and every listed fixture and compare them with what was validated at open. Records an INPUTS_RECHECKED event and returns the
        list of differences (empty if none). Reads only; never repairs. A change that was made and undone between this and the open-time check is not seen."""
        found = []
        try:
            if hashlib.sha256((Path(self.config.fixture_dir) / mf.MANIFEST_NAME).read_bytes()).hexdigest() != self._manifest_sha256:
                found.append("the manifest file changed")
        except OSError:
            found.append("the manifest file is missing or unreadable")
        found += [f"{failure.code} {failure.path}" for failure in mf.verify(Path(self.config.fixture_dir), self._entries)]
        found.sort()
        self._emit("INPUTS_RECHECKED", status="CHANGED" if found else "UNCHANGED", differences=found, identity_digest=self.inputs["digest"],
                   method="manifest and every listed file re-read and re-hashed; unlisted files reported")
        return found

    def checkpoint(self):
        """Record the engine's account claims at the current clock (the audit report compares the last one with its own totals)."""
        return self._emit("CHECKPOINT", trades=len(self.engine.ledger), **self._claims())

    def write_events(self, path):
        """Write the event lines, after a final checkpoint, to a NEW file and return an EventLogReceipt, or raise EventLogError.

        The log is written through publish.publish_new_file: temporary file, fsync, read-back, chain verification against the in-memory head and event count,
        then a publish that never replaces anything (see that module for the platform notes). A replay is complete only when this returns a receipt.
        """
        destination = Path(path)
        if destination.exists() or destination.is_symlink():
            raise EventLogError("DESTINATION_EXISTS", "an event log is never overwritten")
        sealed = (len(self.events) >= 2 and self.events[-2]["type"] == "INPUTS_RECHECKED" and self.events[-1]["type"] == "CHECKPOINT"
                  and self.events[-1]["clock"] == self.clock.now.isoformat())
        if not sealed:                                                   # a retry after a failed write adds nothing new
            self.recheck_inputs()
            self.checkpoint()
        changed = sorted({d for e in self.events if e["type"] == "INPUTS_RECHECKED" for d in e["differences"]})   # a change seen at ANY recheck counts, even if put back later
        data = self.event_lines().encode("utf-8")
        head, count = self._head, len(self.events)

        def verify(written):
            check = audit.verify_chain(written, expected_head=head)
            if check["status"] != "VALID" or check["events_verified"] != count:
                raise EventLogError("CHAIN_NOT_VALID", f"{check['reason']}: {check['detail']}")

        publish.publish_new_file(data, destination, check=verify)
        receipt = EventLogReceipt(destination, count, head, hashlib.sha256(data).hexdigest(), len(data))
        self.evidence_receipt = receipt
        if changed:
            raise InputsChanged(changed, receipt)                        # published as evidence, but never reported as a completed replay
        self.log_receipt = receipt
        return receipt

    def _announce(self):
        for record in self._view.visible():
            ref = (record.source_file, record.source_row)
            if ref not in self._announced:
                self._announced.add(ref)
                self._emit("RECORD_AVAILABLE", kind=record.kind, key=record.key, time_key=record.time_key, available_at=record.available_at.isoformat(),
                           ref=f"{record.source_file}#{record.source_row}", fill_eligible=record.fill_eligible)

    # ---- the clock and the trades ---------------------------------------------------------------------------------------------------------
    def advance_to(self, moment):
        """Move the clock forward (never back, never past the configured end) and show whatever became available."""
        if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
            raise gate.ClockError("the replay clock needs a timezone-aware datetime")
        if moment > self.config.end:
            raise ReplayRefused("PAST_END", "the replay cannot go past its configured end")
        self.clock.advance_to(moment)               # raises ClockError for a backwards or naive time
        self._announce()

    def try_trade(self, intent):
        """Pass one intent through the bridge and the engine at the current clock. Returns the event describing the outcome."""
        if (not isinstance(intent, Intent) or intent.side not in (BUY, SELL) or not isinstance(intent.contract_key, str)
                or isinstance(intent.lots, bool) or not isinstance(intent.lots, int)):
            return self._emit("INTENT_REFUSED", stage="RUNNER", code="BAD_INTENT")
        about = {"contract": intent.contract_key, "side": intent.side, "lots": intent.lots}
        self._emit("INTENT_RECEIVED", **about)
        options = {"allowed_origin_classes": frozenset({FIXTURE_ORIGIN}), "permitted": self.config.permitted}
        outcome = bridge.executable_view(self._view, intent.contract_key, **options)
        if not outcome.ok:
            return self._emit("INTENT_REFUSED", stage="BRIDGE", code=outcome.code, detail=outcome.detail, **about, **self._claims())
        marks = {}
        for symbol in sorted(self.engine.positions):
            held = bridge.executable_view(self._view, symbol, **options)
            if held.ok:
                marks[symbol] = held.view
        trade = self.engine.simulate(TradeRequest(intent.contract_key, intent.side, intent.lots), outcome.view, marks)
        quote_ref = outcome.view.attestation.reference
        if not trade.accepted:
            return self._emit("INTENT_REFUSED", stage="ENGINE", code=trade.code, quote_ref=quote_ref, **about, **self._claims())
        r = trade.record
        return self._emit("FILL", seq=r.seq, fill_price=str(r.fill_price), quantity=r.quantity, cost=str(r.cost), turnover=str(r.turnover),
                          realized_pnl=None if r.realized_pnl is None else str(r.realized_pnl), data_origin=r.data_origin, provider=r.provider,
                          slippage_per_unit=str(r.slippage_per_unit), quote_ref=quote_ref, **about, **self._claims())

    def run(self, intents, until=None):
        """Run a script: advance to every availability instant and every intent time up to `until` (default: the last intent's time, or no movement
        for an empty script), in order, and try each intent at its time."""
        script = list(intents)
        declared = self.inputs.get("plan")
        if declared is not None:
            if self._plan_run:
                raise ReplayRefused("PLAN_ALREADY_RUN", "the declared plan has already been run; a second run would repeat its intents")
            if _plan_of(script, until, self.config)["digest"] != declared["digest"]:
                raise ReplayRefused("PLAN_MISMATCH", "the script given to run() is not the plan declared when the replay was opened")
            self._plan_run = True
        for intent in script:
            if not isinstance(intent, Intent):
                raise ReplayRefused("BAD_INTENT", "the script holds something that is not an Intent")
            _aware(intent.at, "intent time")
            if intent.at < self.clock.now:
                raise ReplayRefused("INTENT_BEFORE_CLOCK", f"{intent.at.isoformat()} is before the replay clock {self.clock.now.isoformat()}")
            if intent.at > self.config.end:
                raise ReplayRefused("INTENT_AFTER_END", intent.at.isoformat())
        script.sort(key=lambda i: i.at)                                       # stable: intents at the same instant keep the caller's order
        until = until if until is not None else max((i.at for i in script), default=self.clock.now)
        _aware(until, "until")
        if not self.clock.now <= until <= self.config.end or any(i.at > until for i in script):
            raise ReplayRefused("BAD_UNTIL", "until must lie between the clock and the end, and not before the last intent")
        instants = {r.available_at for r in self.result.records if self.clock.now < r.available_at <= until} | {i.at for i in script}
        for moment in sorted(instants):
            self.advance_to(moment)
            for intent in script:
                if intent.at == moment:
                    self.try_trade(intent)
        return self.events

    # ---- audit output ----------------------------------------------------------------------------------------------------------------------
    def event_lines(self):
        return "".join(eventchain.line(e) for e in self.events)

    def ledger_lines(self):
        """Every excluded record or file, with its reason and source reference (ingestion ledger)."""
        return self.result.ledger_lines()

    def digest(self):
        return hashlib.sha256((self.event_lines() + "\x00" + self.ledger_lines()).encode("utf-8")).hexdigest()

    def close(self):
        if not self._closed:
            self._closed = True
            self.engine.close()


__all__ = ["Intent", "ReplayConfig", "ReplayRefused", "ReplayRunner", "QUOTE"]
