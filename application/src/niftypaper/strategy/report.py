"""A basic strategy report from a finished Outcome. Every figure is SIMULATED. A value that cannot be known is {"unavailable": REASON}, never a zero."""
from collections import Counter
from decimal import Decimal

from ..paper.engine import SELL

NOTICE = ("SIMULATED results on invented data, produced to exercise the signal, intent and audit path. Nothing here is evidence that the rule is profitable, "
          "and the audit shows consistency of files, not that any market event was genuine.")


def _unavailable(reason):
    return {"unavailable": reason}


def build_report(outcome, strategy):
    decisions = outcome.decisions
    submitted = [d for d in decisions if d.outcome == "SUBMITTED"]
    accepted = [d for d in submitted if d.result.get("event") == "FILL"]
    refused = [d for d in submitted if d.result.get("event") == "INTENT_REFUSED"]
    held, completed = {}, 0
    for trade in outcome.ledger:
        held[trade.symbol] = held.get(trade.symbol, 0) + (-trade.quantity if trade.side == SELL else trade.quantity)
        if trade.side == SELL and held[trade.symbol] == 0:
            completed += 1
    sells = any(t.side == SELL for t in outcome.ledger)
    if not outcome.ledger:
        fees = slippage = _unavailable("NO_FILLS")
    else:
        fees = format(sum((t.cost for t in outcome.ledger), Decimal("0")), "f")
        slippage = format(sum((t.slippage_per_unit * t.quantity for t in outcome.ledger), Decimal("0")), "f")
    if not outcome.positions:
        unrealized = _unavailable("NO_OPEN_POSITIONS")
    elif all("unrealized" in m for m in outcome.marks.values()):
        unrealized = format(sum((Decimal(m["unrealized"]) for m in outcome.marks.values()), Decimal("0")), "f")
    else:
        unrealized = _unavailable("NO_MARK_FOR_" + "_AND_".join(s for s, m in sorted(outcome.marks.items()) if "unrealized" not in m))
    return {
        "report_version": 1, "simulated": True, "notice": NOTICE,
        "strategy": {"name": strategy.name, "version": strategy.version},
        "observations": len(decisions),
        "signals_generated": sum(1 for d in decisions if d.evaluation.signal is not None),
        "evaluations": dict(sorted(Counter(d.evaluation.status for d in decisions).items())),
        "not_submitted": [{"decision_time": d.decision_time.isoformat(), "outcome": d.outcome, "reason_code": d.reason_code}
                          for d in decisions if d.outcome not in ("SUBMITTED", "NO_SIGNAL")],
        "intents_submitted": len(submitted),
        "intents_accepted": len(accepted),
        "intents_rejected": [{"decision_time": d.decision_time.isoformat(), "contract": d.intent.contract_key, "stage": d.result["stage"], "code": d.result["code"]}
                             for d in refused],
        "completed_trades": completed,
        "open_trades": len(outcome.positions),
        "realized_pnl": format(outcome.realized, "f") if sells else _unavailable("NO_CLOSED_TRADES"),
        "unrealized_pnl": unrealized,
        "fees": fees, "slippage": slippage,
        "marks": outcome.marks,
        "plan_digest": outcome.plan_digest, "identity_digest": outcome.identity_digest,
        "event_log": {"events": outcome.receipt.events, "head": outcome.receipt.head},
    }
