"""Signal types. Signal generation only: nothing here imports the replay runner, the paper engine or an intent type.

A strategy turns the information visible at a decision time (a historical.gate.PointInTimeView) plus the account's current holdings into an Evaluation:
a Signal, an explicit "no signal" with the reason, or a rejection of its inputs with the reason. It never fills in missing data.
"""
import hashlib
from dataclasses import dataclass, field
from datetime import datetime

from ..historical import eventchain

SIGNAL, NO_SIGNAL, INPUT_REJECTED = "SIGNAL", "NO_SIGNAL", "INPUT_REJECTED"
BUY_TO_OPEN, SELL_TO_CLOSE = "BUY_TO_OPEN", "SELL_TO_CLOSE"
BULLISH, BEARISH = "BULLISH", "BEARISH"


@dataclass(frozen=True)
class Signal:
    strategy: str
    version: str
    decision_time: datetime
    data_observed_through: datetime       # the newest availability instant among the inputs (never after decision_time)
    contract: str
    direction: str                        # the view the position expresses: BULLISH (long call) or BEARISH (long put)
    action: str                           # BUY_TO_OPEN or SELL_TO_CLOSE
    side: str                             # BUY or SELL
    lots: int
    reason_code: str
    reason: str
    inputs: dict                          # the values the decision rests on (strings and integers only)
    invalidation: dict                    # valid_until (ISO), the condition that cancels the signal and the condition that ends the position
    quote_status: dict                    # the required quote/data-quality status at decision time
    signal_id: str = field(default="")

    def __post_init__(self):
        if self.data_observed_through > self.decision_time:
            raise ValueError("a signal cannot rest on data observed after its decision time")
        if not self.signal_id:
            object.__setattr__(self, "signal_id", signal_digest(self))

    def to_dict(self):
        out = {name: getattr(self, name) for name in self.__dataclass_fields__}
        out["decision_time"] = self.decision_time.isoformat()
        out["data_observed_through"] = self.data_observed_through.isoformat()
        return out


def signal_digest(signal):
    body = {name: getattr(signal, name) for name in signal.__dataclass_fields__ if name != "signal_id"}
    body["decision_time"], body["data_observed_through"] = signal.decision_time.isoformat(), signal.data_observed_through.isoformat()
    return hashlib.sha256(b"niftypaper.strategy.signal/1\n" + eventchain.canonical(body).encode("ascii")).hexdigest()


@dataclass(frozen=True)
class Evaluation:
    decision_time: datetime
    status: str                           # SIGNAL, NO_SIGNAL or INPUT_REJECTED
    reason_code: str
    detail: str
    inputs: dict
    signal: "Signal | None" = None

    def to_dict(self):
        return {"decision_time": self.decision_time.isoformat(), "status": self.status, "reason_code": self.reason_code, "detail": self.detail,
                "inputs": self.inputs, "signal": self.signal.to_dict() if self.signal else None}
