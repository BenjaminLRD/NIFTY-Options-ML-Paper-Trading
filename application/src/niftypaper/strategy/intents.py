"""From an approved signal to an intent in the existing replay format. Intent generation only: it does not calculate signals and does not execute anything.

A signal becomes an Intent only if, at the execution time, it has not expired and the strategy, re-evaluated with the data visible at that time, still asks for
the same action on the same contract. Anything else is reported with a reason and produces no intent. The runner, bridge, risk limits and engine then apply
their own checks to the intent exactly as for a hand-written script; this module bypasses none of them.
"""
from dataclasses import dataclass
from datetime import datetime

from ..historical.replay import Intent
from .signals import SIGNAL, Evaluation, Signal

SUBMITTED, SIGNAL_EXPIRED, INVALIDATED, PRECONDITION_FAILED = "SUBMITTED", "SIGNAL_EXPIRED", "INVALIDATED_BEFORE_EXECUTION", "PRECONDITION_FAILED"


@dataclass(frozen=True)
class IntentDecision:
    outcome: str
    reason_code: str
    detail: str
    intent: "Intent | None" = None


def convert(signal, execute_at, current):
    """`current` is the Evaluation of the same strategy at `execute_at` (the caller computes it from data visible at that time)."""
    if not isinstance(signal, Signal) or not isinstance(current, Evaluation):
        return IntentDecision(PRECONDITION_FAILED, "BAD_ARGUMENTS", "a Signal and an Evaluation are required")
    if not isinstance(execute_at, datetime) or execute_at.tzinfo is None or execute_at.utcoffset() is None:
        return IntentDecision(PRECONDITION_FAILED, "BAD_EXECUTION_TIME", "the execution time must be timezone-aware")
    if execute_at < signal.decision_time:
        return IntentDecision(PRECONDITION_FAILED, "EXECUTION_BEFORE_DECISION", "execution cannot precede the decision")
    if current.decision_time != execute_at:
        return IntentDecision(PRECONDITION_FAILED, "STALE_EVALUATION", "the re-evaluation was not made at the execution time")
    if execute_at > datetime.fromisoformat(signal.invalidation["valid_until"]):
        return IntentDecision(SIGNAL_EXPIRED, "VALID_UNTIL_PASSED", f"valid until {signal.invalidation['valid_until']}, execution at {execute_at.isoformat()}")
    if current.status != SIGNAL:
        return IntentDecision(INVALIDATED, current.reason_code, f"at execution time the strategy no longer asks for this action ({current.status}: {current.detail})")
    again = current.signal
    if (again.contract, again.action, again.side, again.lots) != (signal.contract, signal.action, signal.side, signal.lots):
        return IntentDecision(INVALIDATED, "DIFFERENT_ACTION", "at execution time the strategy asks for a different action")
    return IntentDecision(SUBMITTED, "APPROVED", "approved for submission; the runner and engine still apply their own checks",
                          Intent(execute_at, signal.contract, signal.side, signal.lots))
