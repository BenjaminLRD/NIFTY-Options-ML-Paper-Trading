"""Runs a strategy over a fixture replay in two passes (see docs/STRATEGY_BASELINE.md section 3).

Pass 1 asks the strategy for decisions on a throwaway, isolated copy of the account (a temporary state folder this module creates and removes). Pass 2 opens
the real replay with exactly the intents pass 1 produced declared as its plan, runs them and publishes the event log. Pass 2 must reproduce pass 1's outcomes.
Strategy calculations stay in the strategy; execution stays in the runner and engine.
"""
import dataclasses
import tempfile
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from ..historical import bridge, gate
from ..historical import replay
from . import intents as intent_module


class StrategyError(Exception):
    def __init__(self, code, detail):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


class StrategyDivergence(StrategyError):
    pass


@dataclass(frozen=True)
class Decision:
    decision_time: object
    execute_at: object
    outcome: str                  # NO_SIGNAL, INPUT_REJECTED, SUBMITTED, SIGNAL_EXPIRED, INVALIDATED_BEFORE_EXECUTION or PRECONDITION_FAILED
    reason_code: str
    detail: str
    evaluation: object
    intent: object = None
    result: dict = field(default_factory=dict)      # for a submitted intent: what the runner/engine did with it (event type, stage, code)


@dataclass
class Outcome:
    decisions: list
    plan: list
    until: object
    events: list
    ledger: list
    positions: dict
    realized: object
    cash: object
    starting_balance: object
    marks: dict
    receipt: object
    plan_digest: str
    identity_digest: str


def _signature(event):
    return (event["type"], event.get("stage"), event.get("code"), event.get("seq"))


def _held(runner):
    return {symbol: position.quantity for symbol, position in runner.engine.positions.items()}


def _decide(runner, strategy, schedule, delay):
    view = gate.PointInTimeView(runner.result.records, runner.clock)
    decisions = []
    for index, moment in enumerate(schedule):
        execute_at = moment + delay
        runner.advance_to(moment)
        evaluation = strategy.evaluate(view, _held(runner))
        if evaluation.signal is None:
            decisions.append(Decision(moment, None, evaluation.status, evaluation.reason_code, evaluation.detail, evaluation))
            continue
        runner.advance_to(execute_at)
        current = strategy.evaluate(view, _held(runner))
        choice = intent_module.convert(evaluation.signal, execute_at, current)
        if choice.intent is None:
            decisions.append(Decision(moment, execute_at, choice.outcome, choice.reason_code, choice.detail, evaluation))
            continue
        event = runner.try_trade(choice.intent)
        result = {"event": event["type"], "stage": event.get("stage"), "code": event.get("code"), "seq": event.get("seq")}
        decisions.append(Decision(moment, execute_at, choice.outcome, choice.reason_code, choice.detail, evaluation, choice.intent, result))
    return decisions


def _check_schedule(schedule, delay, config):
    if not isinstance(delay, timedelta) or delay < timedelta(0):
        raise StrategyError("BAD_DELAY", "the execution delay must be a non-negative duration")
    last = last_execution = None
    for moment in schedule:
        if getattr(moment, "tzinfo", None) is None or moment.utcoffset() is None:
            raise StrategyError("BAD_SCHEDULE", "every decision time must be timezone-aware")
        if not config.start <= moment <= config.end or not config.start <= moment + delay <= config.end:
            raise StrategyError("BAD_SCHEDULE", "every decision and execution time must lie inside the replay window")
        if last is not None and moment <= last or last_execution is not None and last_execution > moment:
            raise StrategyError("BAD_SCHEDULE", "decision times must increase, and each execution must happen no later than the next decision")
        last, last_execution = moment, moment + delay


def run_strategy(config, strategy, schedule, events_path, *, execution_delay=timedelta(0)):
    """Decide (pass 1), then replay the declared plan and publish the event log (pass 2). Returns an Outcome. The real state folder must be empty or absent:
    the driver does not continue an existing account."""
    schedule = list(schedule)
    _check_schedule(schedule, execution_delay, config)
    state = Path(config.state_dir)
    if state.exists() and any(state.iterdir()):
        raise StrategyError("STATE_NOT_EMPTY", "the driver starts a fresh account; the state folder already has files")
    with tempfile.TemporaryDirectory(prefix="strategy_pass1_") as scratch:
        first = replay.ReplayRunner.open(dataclasses.replace(config, state_dir=Path(scratch)))
        try:
            decisions = _decide(first, strategy, schedule, execution_delay)
            expected = [_signature(e) for e in first.events if e["type"] in ("FILL", "INTENT_REFUSED")]
        finally:
            first.close()
    plan = [d.intent for d in decisions if d.intent is not None]
    until = max([d.decision_time for d in decisions] + [d.execute_at for d in decisions if d.execute_at is not None], default=None)
    runner = replay.ReplayRunner.open(config, intents=plan, until=until)
    try:
        runner.run(plan, until=until)
        got = [_signature(e) for e in runner.events if e["type"] in ("FILL", "INTENT_REFUSED")]
        if got != expected:
            raise StrategyDivergence("PASS_MISMATCH", "replaying the declared plan did not reproduce the decision pass; nothing was written")
        marks = _marks(runner, config)
        receipt = runner.write_events(events_path)
        engine = runner.engine
        return Outcome(decisions, plan, until, list(runner.events), list(engine.ledger), dict(engine.positions), engine.realized_pnl, engine.cash,
                       engine.starting_balance, marks, receipt, runner.inputs["plan"]["digest"], runner.inputs["digest"])
    finally:
        runner.close()


def _marks(runner, config):
    view = gate.PointInTimeView(runner.result.records, runner.clock)
    marks = {}
    for symbol in sorted(runner.engine.positions):
        found = bridge.executable_view(view, symbol, allowed_origin_classes=frozenset({replay.FIXTURE_ORIGIN}), permitted=config.permitted)
        if not found.ok:
            marks[symbol] = {"unavailable": found.code}
            continue
        pnl = runner.engine.unrealized(symbol, found.view)
        marks[symbol] = {"unavailable": "NO_VERIFIED_PRICE"} if pnl is None else {"unrealized": str(pnl), "quote_observed_at": found.view.provider_time.isoformat()}
    return marks
