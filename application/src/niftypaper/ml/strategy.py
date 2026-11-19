"""The decision layer: predictions in, BUY / SELL / NO TRADE out, with a recorded explanation.

`MlGatedStrategy` has the same `evaluate(view, held)` interface as the baseline strategy, so the existing driver, intent conversion, replay runner, bridge,
risk limits and paper engine apply unchanged. It adds no execution path of its own.

Rule (placeholder constants, not tuned on any data):
  flat      score the call and the put; BUY the one with the higher probability if that probability is ELIGIBLE and at least `buy_threshold`;
            otherwise NO TRADE, and the reason says which gate stopped it.
  holding   score the held contract; SELL (close it) if the prediction is ELIGIBLE and below `exit_threshold`; otherwise NO TRADE (hold).
A prediction that is UNAVAILABLE or DISPLAY_ONLY never produces BUY or SELL. There is no default action and no fallback signal: when the model has nothing to say,
or no demonstrated right to say it, the answer is NO TRADE. A research model with standing NONE therefore never trades, by construction.
"""
from datetime import timedelta
from decimal import Decimal

from ..strategy.signals import (BEARISH, BUY_TO_OPEN, BULLISH, INPUT_REJECTED, NO_SIGNAL, SELL_TO_CLOSE, SIGNAL, Evaluation, Signal)
from ..strategy.trend import IndexSmaTrend, TrendConfig
from . import predict as pred

NAME, VERSION = "ml_gated", "1.0.0"
BUY, SELL, NO_TRADE = "BUY", "SELL", "NO TRADE"


def decision_label(evaluation):
    """The label shown to a reader for an Evaluation."""
    if evaluation.signal is None:
        return NO_TRADE
    return BUY if evaluation.signal.action == BUY_TO_OPEN else SELL


class MlGatedStrategy(IndexSmaTrend):
    name, version = NAME, VERSION

    def __init__(self, config, model, buy_threshold=Decimal("0.55"), exit_threshold=Decimal("0.50")):
        super().__init__(config)                 # reuses the baseline's holdings checks and quote-status helper; its moving-average rule is not used
        if not isinstance(config, TrendConfig):
            raise TypeError("config must be a TrendConfig")
        if not Decimal("0.5") <= buy_threshold < 1 or not 0 < exit_threshold <= buy_threshold:
            raise ValueError("need 0.5 <= buy_threshold < 1 and 0 < exit_threshold <= buy_threshold")
        self.model, self.buy_threshold, self.exit_threshold = model, buy_threshold, exit_threshold

    def _strings(self, predictions):
        return {c: p.to_dict() for c, p in sorted(predictions.items())}

    def evaluate(self, view, held):
        c, now = self.config, view.now
        if not isinstance(held, dict) or any(isinstance(q, bool) or not isinstance(q, int) or q <= 0 for q in held.values()):
            return self._reject(now, "BAD_HOLDINGS", "holdings must map symbols to positive integer quantities")
        if set(held) - {c.call_contract, c.put_contract} or len(held) > 1:
            return self._reject(now, "BAD_HOLDINGS", "the strategy manages one position in its own two contracts")
        holding = next(iter(held), None)
        contracts = (holding,) if holding else (c.call_contract, c.put_contract)
        predictions = {k: pred.predict(self.model, view, k) for k in contracts}
        inputs = {"model": self.model.model_id, "buy_threshold": str(self.buy_threshold), "exit_threshold": str(self.exit_threshold),
                  "held": {s: q for s, q in sorted(held.items())}, "predictions": self._strings(predictions)}
        usable = {k: p for k, p in predictions.items() if p.status == pred.ELIGIBLE}
        if not usable:
            return self._no_trade(now, predictions, inputs, holding)
        if holding is None:
            contract, best = max(usable.items(), key=lambda kv: kv[1].probability)
            if best.probability < float(self.buy_threshold):
                return Evaluation(now, NO_SIGNAL, "BELOW_BUY_THRESHOLD", f"NO TRADE: the highest probability, {best.probability:.4f} for {contract}, is below the buy "
                                  f"threshold {self.buy_threshold}", inputs)
            action, side, direction, lots = BUY_TO_OPEN, "BUY", BULLISH if contract == c.call_contract else BEARISH, c.lots
            code, why = "MODEL_ABOVE_THRESHOLD", f"BUY {contract}: probability {best.probability:.4f} is at least the buy threshold {self.buy_threshold} ({self.model.model_id})"
        else:
            best = usable[holding]
            if best.probability >= float(self.exit_threshold):
                return Evaluation(now, NO_SIGNAL, "HOLD", f"NO TRADE: probability {best.probability:.4f} for {holding} is not below the exit threshold "
                                  f"{self.exit_threshold}; the position stays open", inputs)
            contract, action, side = holding, SELL_TO_CLOSE, "SELL"
            direction = BULLISH if holding == c.call_contract else BEARISH
            code, why, lots = "MODEL_BELOW_EXIT", f"SELL {holding}: probability {best.probability:.4f} fell below the exit threshold {self.exit_threshold}", None
        quote = self._quote_status(view, contract)
        inputs = {**inputs, "quote": quote}
        if quote["status"] != "ELIGIBLE":
            return Evaluation(now, NO_SIGNAL, {"MISSING": "NO_QUOTE", "STALE": "STALE_QUOTE", "NOT_FILL_ELIGIBLE": "QUOTE_NOT_FILL_ELIGIBLE"}[quote["status"]],
                              f"NO TRADE: the model asked for it ({why}) but the quote for {contract} is {quote['status']}", inputs)
        if lots is None:
            lot_size = quote["lot_size"]
            if not isinstance(lot_size, int) or lot_size <= 0 or held[contract] % lot_size:
                return self._reject(now, "LOT_SIZE_MISMATCH", "the held quantity is not a whole number of the quoted lot size", inputs)
            lots = held[contract] // lot_size
        invalidation = {"valid_until": (now + c.valid_for).isoformat(),
                        "cancel_if": "valid_until has passed, or re-evaluating the same rule at execution time no longer asks for this action on this contract",
                        "position_ends_when": "the model probability falls below the exit threshold" if action == BUY_TO_OPEN else "the position is closed"}
        signal = Signal(NAME, VERSION, now, best.data_observed_through, contract, direction, action, side, lots, code, why, inputs, invalidation, quote)
        return Evaluation(now, SIGNAL, code, why, inputs, signal)

    def _no_trade(self, now, predictions, inputs, holding):
        """Nothing eligible: say which gate stopped it, in this order: no probability at all, then no demonstrated right to trade on it."""
        reasons = {code for p in predictions.values() for code, _ in p.reasons}
        if all(p.status == pred.UNAVAILABLE for p in predictions.values()):
            first = next(iter(predictions.values())).reasons[0]
            return Evaluation(now, NO_SIGNAL, "PREDICTION_UNAVAILABLE", f"NO TRADE: no prediction could be made ({first[0]}: {first[1]})", inputs)
        if "NO_DEMONSTRATED_EDGE" in reasons:
            return Evaluation(now, NO_SIGNAL, "NO_DEMONSTRATED_EDGE", "NO TRADE: the model's probability is shown for information, but the model has no demonstrated "
                              "edge over the prior, so it cannot trigger a simulated trade", inputs)
        if "ORIGIN_NOT_ALLOWED" in reasons:
            return Evaluation(now, NO_SIGNAL, "ORIGIN_NOT_ALLOWED", "NO TRADE: the model may not be used on this data origin", inputs)
        return Evaluation(now, NO_SIGNAL, "PREDICTION_UNAVAILABLE", "NO TRADE: no eligible prediction", inputs)
