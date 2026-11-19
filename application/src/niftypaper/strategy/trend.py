"""index_sma_trend v1.0.0: the baseline rule written out in docs/STRATEGY_BASELINE.md. A pure function of the index closes visible at the decision time, the
newest visible quote of the contract it would trade, and the account's current holdings. Placeholder constants, not tuned; simulated use only."""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from ..historical import gate
from ..historical import records as rec
from .signals import (BEARISH, BUY_TO_OPEN, BULLISH, INPUT_REJECTED, NO_SIGNAL, SELL_TO_CLOSE, SIGNAL, Evaluation, Signal)

NAME, VERSION = "index_sma_trend", "1.0.0"
BENIGN_FLAGS = ("AVAILABILITY_RULE_DEFAULT",)


@dataclass(frozen=True)
class TrendConfig:
    call_contract: str
    put_contract: str
    underlying: str = "NIFTY"
    window: int = 5
    lots: int = 1
    max_close_age_days: int = 4
    max_gap_days: int = 5
    max_quote_age: timedelta = timedelta(seconds=5)
    valid_for: timedelta = timedelta(minutes=10)

    def __post_init__(self):
        for name in ("window", "lots", "max_close_age_days", "max_gap_days"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a whole number of at least 1")
        if self.window < 2 or not isinstance(self.call_contract, str) or not isinstance(self.put_contract, str) or self.call_contract == self.put_contract:
            raise ValueError("window must be at least 2 and the call and put contracts must be two different strings")
        if not isinstance(self.max_quote_age, timedelta) or self.max_quote_age <= timedelta(0) or not isinstance(self.valid_for, timedelta) or self.valid_for <= timedelta(0):
            raise ValueError("max_quote_age and valid_for must be positive durations")


class IndexSmaTrend:
    name, version = NAME, VERSION

    def __init__(self, config):
        if not isinstance(config, TrendConfig):
            raise TypeError("config must be a TrendConfig")
        self.config = config

    # ---- helpers ---------------------------------------------------------------------------------------------------------------------------
    def _reject(self, now, code, detail, inputs=None):
        return Evaluation(now, INPUT_REJECTED, code, detail, inputs or {})

    def _closes(self, view):
        """(closes, rejection): the newest `window` visible, valid index closes in date order, or an explanation why they cannot be used."""
        now, c = view.now, self.config
        found = sorted((r for r in view.visible(rec.INDEX) if r.key == c.underlying and gate.usable_at(r, now)), key=lambda r: r.time_key)
        if len(found) < c.window:
            return None, ("INSUFFICIENT_HISTORY", f"{len(found)} index closes are visible, {c.window} are needed")
        used = found[-c.window:]
        for r in used:
            close = r.get("close")
            if not isinstance(close, Decimal) or close <= 0 or set(r.flags) - set(BENIGN_FLAGS):
                return None, ("BAD_INDEX_RECORD", f"the close for {r.time_key} is missing, not positive or carries a quality flag")
        days = [r.get("trading_date") for r in used]
        if any((b - a).days > c.max_gap_days for a, b in zip(days, days[1:])):
            return None, ("GAPPED_SERIES", f"two consecutive closes are more than {c.max_gap_days} days apart")
        if (now.astimezone(rec.IST).date() - days[-1]).days > c.max_close_age_days:
            return None, ("STALE_INDEX", f"the newest visible close ({days[-1].isoformat()}) is more than {c.max_close_age_days} days old")
        return used, None

    def _quote_status(self, view, contract):
        quote = view.latest_quote(contract)
        if quote is None:
            return {"status": "MISSING", "contract": contract}
        age = view.stale_by(quote)
        status = {"contract": contract, "observed_at": quote.observed_at.isoformat(), "age_seconds": str(age.total_seconds()), "fill_eligible": quote.fill_eligible,
                  "lot_size": quote.get("lot_size")}
        status["status"] = "NOT_FILL_ELIGIBLE" if not quote.fill_eligible or quote.ineligible_reasons else "STALE" if age > self.config.max_quote_age else "ELIGIBLE"
        return status

    # ---- the rule --------------------------------------------------------------------------------------------------------------------------
    def evaluate(self, view, held):
        """Evaluate at view.now. `held` maps symbol -> open quantity (the account's current holdings; positive integers only)."""
        c, now = self.config, view.now
        if not isinstance(held, dict) or any(isinstance(q, bool) or not isinstance(q, int) or q <= 0 for q in held.values()):
            return self._reject(now, "BAD_HOLDINGS", "holdings must map symbols to positive integer quantities")
        if set(held) - {c.call_contract, c.put_contract}:
            return self._reject(now, "FOREIGN_POSITION", "the account holds something this strategy does not manage")
        if len(held) > 1:
            return self._reject(now, "BOTH_HELD", "the strategy holds one position at a time")
        closes, why = self._closes(view)
        if closes is None:
            return self._reject(now, why[0], why[1])
        values = [r.get("close") for r in closes]
        sma, last = sum(values) / len(values), values[-1]
        direction = BULLISH if last > sma else BEARISH if last < sma else None
        inputs = {"underlying": c.underlying, "window": c.window, "sma": format(sma, "f"), "last_close": format(last, "f"),
                  "closes": [{"trading_date": r.time_key, "close": format(r.get("close"), "f"), "available_at": r.available_at.isoformat()} for r in closes],
                  "held": {s: q for s, q in sorted(held.items())}}
        observed = max(r.available_at for r in closes)
        holding = next(iter(held), None)
        if holding is None:
            if direction is None:
                return Evaluation(now, NO_SIGNAL, "NEUTRAL", "the last close equals the average: no view", inputs)
            contract, action, side = (c.call_contract if direction == BULLISH else c.put_contract), BUY_TO_OPEN, "BUY"
            reason_code, reason = "TREND_UP" if direction == BULLISH else "TREND_DOWN", f"last close {format(last, 'f')} is {'above' if direction == BULLISH else 'below'} the {c.window}-close average {format(sma, 'f')}"
            lots = c.lots
        else:
            held_view = BULLISH if holding == c.call_contract else BEARISH
            if direction == held_view:
                return Evaluation(now, NO_SIGNAL, "HOLD", "the trend still supports the open position", inputs)
            contract, action, side, direction = holding, SELL_TO_CLOSE, "SELL", held_view
            reason_code, reason = "TREND_ENDED", f"last close {format(last, 'f')} no longer {'above' if held_view == BULLISH else 'below'} the {c.window}-close average {format(sma, 'f')}"
            lots = None
        quote = self._quote_status(view, contract)
        if quote["status"] != "ELIGIBLE":
            return self._reject(now, {"MISSING": "NO_QUOTE", "STALE": "STALE_QUOTE", "NOT_FILL_ELIGIBLE": "QUOTE_NOT_FILL_ELIGIBLE"}[quote["status"]],
                                f"the quote for {contract} is {quote['status']}: not usable", {**inputs, "quote": quote})
        if lots is None:
            lot_size = quote["lot_size"]
            if not isinstance(lot_size, int) or lot_size <= 0 or held[contract] % lot_size:
                return self._reject(now, "LOT_SIZE_MISMATCH", "the held quantity is not a whole number of the quoted lot size", {**inputs, "quote": quote})
            lots = held[contract] // lot_size
        invalidation = {"valid_until": (now + c.valid_for).isoformat(),
                        "cancel_if": "valid_until has passed, or re-evaluating the same rule at execution time no longer asks for this action on this contract",
                        "position_ends_when": "the last close is no longer on the entry side of the average" if action == BUY_TO_OPEN else "the position is closed"}
        signal = Signal(NAME, VERSION, now, observed, contract, direction, action, side, lots, reason_code, reason, inputs, invalidation, quote)
        return Evaluation(now, SIGNAL, reason_code, reason, inputs, signal)
