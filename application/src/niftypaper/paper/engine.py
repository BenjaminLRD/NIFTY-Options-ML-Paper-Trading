"""The virtual trading engine. All trades are simulated; there is no real-money trading, and no module here imports a provider, the dashboard or a network library.

A trade is simulated only when the caller's MarketView says the prices are verified current prices AND the provider's terms for paper trading are
recorded as confirmed. For provider data the quote must also pass the V6-0.1 eligibility boundary (a live-type quote with a quote time and displayed sizes);
data without them is refused, never guessed. The fill price is the touch (ask for BUY, bid for SELL) plus adverse slippage, so a fill is never the same
thing as a market quote, and every record keeps both. Missing or crossed bid/ask means no fill. Costs and limits below are illustrative defaults, not actual charges.

Daily risk-entry limit (see docs/PAPER_ACCOUNTING.md for the exact formula): a BUY is refused if the projected worst-case loss for the trading day would
exceed the budget. This limits NEW risk; it does not guarantee that realised loss stays below the budget (gaps, thin or missing liquidity, bad prices and
unavailable exits can lose more), and the engine never liquidates anything by itself. Every accepted trade is written to the durable store before it is applied;
if the store cannot be trusted the engine locks and refuses every trade.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from ..eligibility import EligibilityPolicy, IneligibleQuoteError, Side, fill_price, require_fillable
from ..quotes import Quote, QuoteType
from . import attest
from .store import StoreError

IST = timezone(timedelta(hours=5, minutes=30))        # the trading day is the calendar date in India Standard Time
BUY, SELL = "BUY", "SELL"
ORIGIN_SYNTHETIC, ORIGIN_PROVIDER, ORIGIN_HISTORICAL = "SYNTHETIC", "PROVIDER", "HISTORICAL"
CENT = Decimal("0.01")

MESSAGES = {
    "BAD_REQUEST": "The request is not valid (side must be BUY or SELL and lots a whole number of at least 1).",
    "UNSUPPORTED_INSTRUMENT": "That instrument is not in the current market data, so it cannot be traded.",
    "TERMS_NOT_CONFIRMED": "Paper trading with this provider's data is not enabled: its terms of use have not been confirmed locally.",
    "DATA_NOT_VERIFIED": "Current prices are not verified, so no virtual trade can be made.",
    "NO_BID_ASK": "Bid or ask is missing or empty, so no reliable spread or fill price is known.",
    "CROSSED_OR_LOCKED": "Bid is not below ask, so no reliable fill price is known.",
    "OFF_TICK": "Bid or ask is not a multiple of the contract's tick size, so the quote cannot be used.",
    "LOT_SIZE_UNKNOWN": "The contract's lot size or tick size is not known, so the quantity cannot be sized.",
    "LOT_SIZE_CHANGED": "The contract's lot size differs from the open position's, so the quantity cannot be reconciled; nothing was guessed.",
    "BOUNDARY_REJECTED": "The quote failed the execution-eligibility checks.",
    "EXCEEDS_DISPLAYED_SIZE": "The quantity is larger than the size displayed at the touch; partial fills are not simulated.",
    "FILL_PRICE_NOT_POSITIVE": "The slippage-adjusted fill price would not be positive.",
    "RISK_MAX_LOTS": "More lots than the per-trade limit.",
    "RISK_MAX_POSITIONS": "The limit on open positions is reached.",
    "RISK_EXPOSURE": "The trade would take open premium above the exposure limit.",
    "RISK_DAILY_LOSS": "Blocked by the daily loss limit: in the worst case, today's realised loss plus all open premium, buy fees and estimated exit costs plus this trade's premium and fees would exceed the daily budget.",
    "RISK_DAILY_LIMIT_REACHED": "The daily loss limit has been reached: no new risk is accepted today. Exits are still allowed.",
    "INSUFFICIENT_BALANCE": "Not enough virtual cash for the premium and costs.",
    "NO_POSITION": "There is no open position in that instrument to sell.",
    "SIZE_EXCEEDS_POSITION": "More quantity than the open position holds; short selling is not simulated.",
    "STATE_UNAVAILABLE": "Virtual trading is locked: the saved paper-trading state could not be loaded safely, so the daily risk budget cannot be known. Nothing was changed; fix or move the state file yourself.",
    "PERSISTENCE_FAILED": "The trade could not be saved to disk, so it was not made: the daily risk budget must survive a restart.",
    "PROVENANCE_INVALID": "A historical price must come from the historical bridge unaltered and pass the engine's own checks; this one did not, so no trade was made.",
    "ACCOUNT_MISMATCH": "These prices belong to a different virtual account (synthetic data and provider data never share an account), so no trade was made.",
}


@dataclass(frozen=True)
class MarketView:
    """What the engine is allowed to know about one instrument right now. Built by the caller from a data snapshot."""
    origin: str                  # ORIGIN_SYNTHETIC, ORIGIN_PROVIDER or ORIGIN_HISTORICAL (replayed past quote; see niftypaper.historical.bridge)
    provider: str                # source label
    state: str                   # data state name, e.g. SIMULATED, LIVE, UNVERIFIED
    connection: str              # connection status shown to the user
    symbol: str
    bid: "Decimal | None"
    ask: "Decimal | None"
    last: "Decimal | None"
    bid_qty: "int | None"
    ask_qty: "int | None"
    tick: "Decimal | None"
    lot_size: "int | None"
    received_at: "datetime | None"
    provider_time_raw: object
    provider_time: "datetime | None"
    verified: bool               # prices passed the data-quality checks (LIVE, or SIMULATED synthetic data)
    permitted: bool              # paper trading with this data source is allowed (synthetic: always; provider: terms confirmed locally)
    reason: str = ""
    attestation: object = None   # HISTORICAL views only: issued by niftypaper.historical.bridge over attestation_fields(); see paper/attest.py


_HISTORICAL_SYMBOL = re.compile(r"^[A-Z0-9&-]{1,20}\|\d{4}-\d{2}-\d{2}\|\d+\.\d{2}\|(CE|PE)$")


def attestation_fields(view):
    """The fields an attestation covers: everything a fill is computed from, plus the labels that say where it came from."""
    moment = view.provider_time.isoformat() if isinstance(view.provider_time, datetime) else None
    return (view.origin, view.provider, view.state, view.connection, view.symbol, view.bid, view.ask, view.last, view.bid_qty, view.ask_qty,
            view.tick, view.lot_size, moment, view.verified, view.permitted)


@dataclass(frozen=True)
class CostModel:
    fee_per_trade: Decimal = Decimal("20")
    turnover_fraction: Decimal = Decimal("0.0005")
    label: str = "illustrative example, not actual charges"

    def cost(self, turnover):
        return (self.fee_per_trade + turnover * self.turnover_fraction).quantize(CENT, ROUND_HALF_UP)

    def exit_estimate(self, amount, lots):
        """Upper estimate of the fees for getting out of `lots` lots worth `amount`: the exit may be made one lot at a time, each trade paying the flat fee."""
        return (self.fee_per_trade * max(lots, 1) + amount * self.turnover_fraction).quantize(CENT, ROUND_HALF_UP)


@dataclass(frozen=True)
class RiskLimits:
    max_lots_per_trade: int = 2
    max_open_positions: int = 3
    max_exposure_fraction: Decimal = Decimal("0.10")      # of the starting balance, in open premium
    daily_loss_fraction: Decimal = Decimal("0.02")        # of the configured starting balance: the daily loss budget (see PaperEngine.risk_report)
    exit_slippage_ticks: int = 2                          # ticks per unit taken off a marked position's value when judging the loss already incurred

    def __post_init__(self):
        if isinstance(self.exit_slippage_ticks, bool) or not isinstance(self.exit_slippage_ticks, int) or self.exit_slippage_ticks < 0:
            raise ValueError("exit_slippage_ticks must be a whole number of ticks, 0 or more")


@dataclass(frozen=True)
class TradeRequest:
    symbol: str
    side: str
    lots: int


_DECIMALS = ("quote_bid", "quote_ask", "quote_last", "spread", "fill_price", "slippage_per_unit", "cost", "turnover", "realized_pnl")
_DATETIMES = ("engine_time", "received_at")


@dataclass(frozen=True)
class TradeRecord:
    seq: int
    engine_time: datetime
    side: str
    symbol: str
    lots: int
    lot_size: int
    quantity: int
    data_origin: str
    provider: str
    data_state: str
    connection: str
    quote_bid: Decimal
    quote_ask: Decimal
    quote_last: "Decimal | None"
    spread: Decimal
    received_at: "datetime | None"
    provider_time_raw: object
    fill_price: Decimal
    fill_basis: str
    slippage_per_unit: Decimal
    cost: Decimal
    turnover: Decimal
    realized_pnl: "Decimal | None"
    assumptions: tuple

    def to_dict(self):
        out = {}
        for name, value in self.__dict__.items():
            out[name] = (value.isoformat() if isinstance(value, datetime) else str(value) if isinstance(value, Decimal)
                         else list(value) if isinstance(value, tuple) else value)
        out["simulated"] = True
        out["quote_vs_fill"] = "quote_* fields are the market quote; fill_price is a simulated fill, not a market price"
        return out

    @classmethod
    def from_dict(cls, data):
        """Rebuild a record from its stored form. Raises KeyError, ValueError, TypeError or InvalidOperation if it is not one."""
        values = {}
        for name in cls.__dataclass_fields__:
            value = data[name]
            if name in _DECIMALS:
                value = None if value is None else Decimal(value)
                if value is not None and not value.is_finite():
                    raise ValueError(name)
            elif name in _DATETIMES:
                value = None if value is None else datetime.fromisoformat(value)
            elif name == "assumptions":
                value = tuple(value)
            values[name] = value
        record = cls(**values)
        if (record.side not in (BUY, SELL) or not all(type(getattr(record, n)) is int for n in ("seq", "lots", "lot_size", "quantity"))
                or record.engine_time.utcoffset() is None or not all(isinstance(getattr(record, n), str) for n in ("symbol", "data_origin", "provider"))):
            raise ValueError("record")
        return record


@dataclass(frozen=True)
class TradeResult:
    accepted: bool
    code: str
    message: str
    record: "TradeRecord | None" = None


@dataclass(frozen=True)
class RiskReport:
    """The daily risk budget at one moment. All amounts are rupees, positive = loss. See PaperEngine.risk_report."""
    day: object                  # the IST trading day this applies to
    budget: Decimal
    realized_today: Decimal      # signed net realised P&L of the day, fees included (profit positive)
    unrealized_loss: Decimal     # sum of per-position losses against cost where a verified mark is known (gains never offset)
    marks_known: bool            # False if some position had no verified mark (its loss so far is then treated as not yet incurred)
    remaining_max_loss: Decimal  # what the open positions could still lose: premium paid less the loss already counted above
    open_buy_fees: Decimal       # buy fees paid on positions still open (not yet in realised P&L)
    exit_cost_estimate: Decimal  # estimated cost of exiting every open position
    incurred: Decimal            # loss incurred so far today: -realized + unrealized loss + open buy fees
    committed: Decimal           # worst case if nothing new is bought: incurred + remaining max loss + exit cost estimate
    headroom: Decimal            # budget - committed: what a new trade (premium + fees + its exit estimate) may still use
    limit_reached: bool          # incurred >= budget
    locked: "str | None"         # why the engine refuses all trades, if it does
    exit_slippage_ticks: int = 0                       # the allowance used (ticks per unit)
    exit_slippage_allowance: Decimal = Decimal("0")    # how much of `unrealized_loss` is that allowance rather than the quoted bid


@dataclass
class Position:
    """An open long position. Money is tracked in whole paise: `basis` is the premium paid for the open quantity (sum of fill turnovers still open)
    and `cost_paid` the buy fees carried by it. `average_price` is for display only; no money is ever computed from it (it rarely terminates)."""
    symbol: str
    quantity: int
    basis: Decimal
    cost_paid: Decimal
    lot_size: int = 0

    @property
    def average_price(self):
        return self.basis / self.quantity


def _refuse(code, detail=""):
    return TradeResult(False, code, MESSAGES[code] + (f" ({detail})" if detail else ""))


def _money(value):
    return f"{value:,.2f}"


def trading_day(moment):
    """The trading day of a timezone-aware moment: its calendar date in India Standard Time (00:00 IST rolls the day; no holiday calendar)."""
    return moment.astimezone(IST).date()


class PaperEngine:
    def __init__(self, starting_balance, *, costs=None, limits=None, slippage_ticks=1, max_quote_age=timedelta(seconds=5), clock=None, store=None, origin=None, provider=None):
        if origin not in (None, ORIGIN_SYNTHETIC, ORIGIN_PROVIDER, ORIGIN_HISTORICAL):
            raise ValueError("origin must be None, SYNTHETIC, PROVIDER or HISTORICAL")
        self.provider = provider                     # if set, the only data source label this account may trade on (a replay account is bound to one source)
        self.origin = origin                         # the only kind of market data this account may trade on (None: not restricted, for unit tests)
        self.starting_balance = Decimal(starting_balance)
        self.cash = self.starting_balance
        self.costs, self.limits = costs or CostModel(), limits or RiskLimits()
        self.slippage_ticks = slippage_ticks
        self._policy_age = max_quote_age
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.positions = {}
        self.ledger = []
        self.realized_pnl = Decimal("0")
        self._store = store
        self.lock_reason = None
        self.restored = 0
        if store is not None:
            self._restore()

    def close(self):
        """Release the single-writer lock on the saved state, if any."""
        if self._store is not None:
            self._store.close()

    # ---- durable state -------------------------------------------------------------------------------------------------------------------
    def _restore(self):
        try:
            for data in self._store.load():
                self._replay(TradeRecord.from_dict(data))
        except StoreError as error:
            self._lock(f"{error.code}{': ' + error.detail if error.detail else ''}")
        except (KeyError, ValueError, TypeError, ArithmeticError):
            self._lock("a stored trade record is malformed")
        else:
            self.restored = len(self.ledger)

    def _lock(self, why):
        self.cash, self.positions, self.ledger, self.realized_pnl = self.starting_balance, {}, [], Decimal("0")
        self.lock_reason = f"saved paper-trading state could not be trusted ({why}); file: {self._store.path.name}"

    def _replay(self, rec):
        """Re-apply one stored trade with the same code that applied it, checking the record is consistent with everything before it."""
        if (rec.seq != len(self.ledger) + 1 or rec.quantity != rec.lots * rec.lot_size or rec.quantity <= 0
                or rec.turnover != (rec.fill_price * rec.quantity).quantize(CENT, ROUND_HALF_UP) or rec.cost < 0
                or (self.ledger and rec.engine_time < self.ledger[-1].engine_time)):
            raise StoreError("INCONSISTENT", f"record {rec.seq} does not follow from the ones before it")
        if self.origin is not None and rec.data_origin != self.origin:
            raise StoreError("DIFFERENT_ACCOUNT", f"record {rec.seq} was made on {rec.data_origin} data, this account uses {self.origin} data")
        if (self.provider is not None and rec.provider != self.provider) or (rec.data_origin == ORIGIN_HISTORICAL and not rec.provider.startswith("historical:")):
            raise StoreError("DIFFERENT_ACCOUNT", f"record {rec.seq} was made on data from {rec.provider!r}, which this account does not use")
        plan = None
        if rec.side == SELL:
            held = self.positions.get(rec.symbol)
            if held is None or rec.quantity > held.quantity or held.lot_size != rec.lot_size:
                raise StoreError("INCONSISTENT", f"record {rec.seq} sells more than was held")
            plan = self._plan_sell(held, rec.quantity, rec.turnover, rec.cost)
        if (plan[2] if plan else None) != rec.realized_pnl:
            raise StoreError("INCONSISTENT", f"record {rec.seq} has a different realised P&L from the one recomputed")
        self._commit(rec.side, rec.symbol, rec.quantity, rec.lot_size, rec.turnover, rec.cost, plan)
        self.ledger.append(rec)

    # ---- the daily risk budget -----------------------------------------------------------------------------------------------------------
    @property
    def budget(self):
        return self.limits.daily_loss_fraction * self.starting_balance

    def realized_today(self, now=None):
        """Signed net realised P&L (fees included) of the sales made on the trading day of `now`. A restart does not change it: it is derived from the ledger."""
        day = trading_day(now or self._clock())
        return sum((r.realized_pnl for r in self.ledger if r.side == SELL and trading_day(r.engine_time) == day), Decimal("0"))

    def risk_report(self, marks=None, now=None):
        """The daily risk budget.

        budget      = daily_loss_fraction x the configured starting balance (fixed; it is not re-based when equity changes or a new day starts)
        incurred    = -realised_today + sum(unrealised loss of each position, losses only) + buy fees of open positions
        remaining   = sum(premium paid for each open position - its unrealised loss counted above)       [what could still be lost]
        exit costs  = sum(estimated exit fees of each open position: the flat fee for every lot, since the exit may be made one lot at a time, plus the
                      proportional fee on the larger of the premium paid and the current value)
        committed   = incurred + remaining + exit costs  =  -realised_today + sum(premium paid) + open buy fees + exit costs
        A new BUY adds (premium + buy fee + estimated exit fees of that position). It is refused if committed + that > budget.
        The unrealised-loss part and the remaining-loss part always add up to the premium paid, so a loss already shown as unrealised is never counted twice,
        and the committed total does not depend on marks at all; marks only change how it is split and whether `limit_reached` is known.
        Exit slippage: a marked position is valued at the bid less `exit_slippage_ticks` ticks per unit (never below zero), so the loss incurred (and `limit_reached`)
        allows for a poor exit. It is NOT added to `committed`: the worst case already assumes the whole premium is lost, and no exit price can lose more than
        that (proceeds cannot be negative), so adding slippage there would count the same premium twice. Entry slippage is already in the premium paid.
        """
        now = now or self._clock()
        marks = marks or {}
        realized = self.realized_today(now)
        unrealized = remaining = fees = exits = allowance = Decimal("0")
        known = True
        for p in self.positions.values():
            view = marks.get(p.symbol)
            usable = (isinstance(view, MarketView) and view.symbol == p.symbol and view.verified is True and view.permitted is True
                      and view.bid is not None and view.bid > 0 and view.tick is not None and view.tick > 0)
            quoted = view.bid * p.quantity if usable else None
            cushion = min(quoted, view.tick * self.limits.exit_slippage_ticks * p.quantity) if usable else Decimal("0")
            lost = max(Decimal("0"), p.basis - (quoted - cushion)) if usable else Decimal("0")     # loss if sold now at the bid less the allowance
            known = known and usable
            unrealized += lost
            allowance += cushion if lost > 0 else Decimal("0")
            remaining += p.basis - lost
            fees += p.cost_paid
            exits += self.costs.exit_estimate(max(p.basis, quoted or Decimal("0")), p.quantity // p.lot_size if p.lot_size else 1)
        incurred = -realized + unrealized + fees
        committed = incurred + remaining + exits
        return RiskReport(trading_day(now), self.budget, realized, unrealized, known, remaining, fees, exits, incurred, committed, self.budget - committed,
                          incurred >= self.budget, self.lock_reason, self.limits.exit_slippage_ticks, allowance)

    # ---- trading -------------------------------------------------------------------------------------------------------------------------
    def simulate(self, request, view, marks=None):
        """Try one virtual trade. `marks` is {symbol: MarketView} for the other open positions (used for the unrealised part of the risk report).
        A refused trade changes nothing at all: no cash, position, fee, P&L, ledger entry, day state or file."""
        now = self._clock()
        if self.lock_reason:
            return _refuse("STATE_UNAVAILABLE", self.lock_reason)
        if (not isinstance(request, TradeRequest) or request.side not in (BUY, SELL) or isinstance(request.lots, bool)
                or not isinstance(request.lots, int) or request.lots < 1):
            return _refuse("BAD_REQUEST")
        if not isinstance(view, MarketView) or view.symbol != request.symbol or view.origin not in (ORIGIN_SYNTHETIC, ORIGIN_PROVIDER, ORIGIN_HISTORICAL):
            return _refuse("UNSUPPORTED_INSTRUMENT")
        if self.origin is not None and view.origin != self.origin:
            return _refuse("ACCOUNT_MISMATCH")
        if self.provider is not None and view.provider != self.provider:
            return _refuse("ACCOUNT_MISMATCH")
        if view.origin == ORIGIN_HISTORICAL:
            problem = self._historical_problem(view, now)
            if problem:
                return _refuse("PROVENANCE_INVALID", problem)
        if view.permitted is not True:
            return _refuse("TERMS_NOT_CONFIRMED")
        if view.verified is not True:
            return _refuse("DATA_NOT_VERIFIED", view.reason or view.state)
        if view.bid is None or view.ask is None or view.bid <= 0 or view.ask <= 0:
            return _refuse("NO_BID_ASK")
        if view.bid >= view.ask:
            return _refuse("CROSSED_OR_LOCKED")
        if not (isinstance(view.lot_size, int) and view.lot_size > 0 and view.tick is not None and view.tick > 0):
            return _refuse("LOT_SIZE_UNKNOWN")
        if view.bid % view.tick != 0 or view.ask % view.tick != 0:
            return _refuse("OFF_TICK")
        held = self.positions.get(request.symbol)
        if held is not None and held.lot_size != view.lot_size:
            return _refuse("LOT_SIZE_CHANGED")
        touch = self._touch(view, request.side, now)
        if isinstance(touch, TradeResult):
            return touch
        quantity = request.lots * view.lot_size
        shown = view.ask_qty if request.side == BUY else view.bid_qty
        if shown is not None and quantity > shown:
            return _refuse("EXCEEDS_DISPLAYED_SIZE", f"{quantity} > {shown}")
        slip = view.tick * self.slippage_ticks
        price = touch + slip if request.side == BUY else touch - slip
        if price <= 0:
            return _refuse("FILL_PRICE_NOT_POSITIVE")
        turnover = (price * quantity).quantize(CENT, ROUND_HALF_UP)
        cost = self.costs.cost(turnover)
        plan = None
        if request.side == BUY:
            blocked = self._check_buy(request, view, turnover, cost, marks, now)
        else:
            blocked = self._check_sell(request, quantity)
            plan = None if blocked else self._plan_sell(held, quantity, turnover, cost)
        if blocked:
            return blocked
        record = TradeRecord(
            seq=len(self.ledger) + 1, engine_time=now, side=request.side, symbol=request.symbol, lots=request.lots, lot_size=view.lot_size,
            quantity=quantity, data_origin=view.origin, provider=view.provider, data_state=view.state, connection=view.connection,
            quote_bid=view.bid, quote_ask=view.ask, quote_last=view.last, spread=view.ask - view.bid, received_at=view.received_at,
            provider_time_raw=view.provider_time_raw, fill_price=price,
            fill_basis=f"{'ask' if request.side == BUY else 'bid'} {touch} {'+' if request.side == BUY else '-'} {self.slippage_ticks} tick(s) slippage",
            slippage_per_unit=slip, cost=cost, turnover=turnover, realized_pnl=plan[2] if plan else None,
            assumptions=("virtual trade: no real order exists", f"costs are {self.costs.label}",
                         "full quantity fills at one price at the touch; no partial fills or queue position",
                         "displayed size checked" if shown is not None else "displayed size unknown, not checked",
                         "limits are illustrative defaults"))
        if self._store is not None:
            try:
                self._store.append(record.to_dict())            # durable first: if this fails nothing has been applied
            except StoreError as error:
                if error.code == "CHANGED":                      # someone else wrote to the file: what is on screen can no longer be trusted
                    self.lock_reason = f"the saved state file was changed by something else while running ({self._store.path.name}); restart to reload it"
                return _refuse("PERSISTENCE_FAILED", error.code)
        self._commit(request.side, request.symbol, quantity, view.lot_size, turnover, cost, plan)
        self.ledger.append(record)
        return TradeResult(True, "FILLED", "Virtual trade recorded. This is a simulated fill, not a market price.", record)

    @staticmethod
    def _historical_problem(view, now):
        """Why a HISTORICAL view cannot be trusted, or None. The attestation must match the fields as they are now, and the engine re-checks the
        properties a fill depends on itself, so a bridge that attested a bad view would still be caught."""
        if not attest.verify(view.attestation, attestation_fields(view)):
            return "no valid attestation from the historical bridge (the view was built by hand or altered)"
        if view.state != "HISTORICAL_REPLAY" or view.connection != "REPLAY" or not isinstance(view.provider, str) or not view.provider.startswith("historical:"):
            return "the source labels are not those of a replayed quote"
        if not isinstance(view.symbol, str) or not _HISTORICAL_SYMBOL.match(view.symbol):
            return "the instrument is not a full contract key (underlying, expiry, strike, option type)"
        decimals = (view.bid, view.ask, view.tick)
        if not all(isinstance(d, Decimal) and d.is_finite() for d in decimals) or not 0 < view.bid < view.ask or view.tick <= 0:
            return "bid, ask and tick must be finite decimals with 0 < bid < ask and tick > 0"
        if view.bid % view.tick or view.ask % view.tick:
            return "bid or ask is not on the tick"
        if isinstance(view.lot_size, bool) or not isinstance(view.lot_size, int) or view.lot_size <= 0:
            return "lot size is not a positive whole number"
        for size in (view.bid_qty, view.ask_qty):
            if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
                return "displayed sizes must be positive whole numbers"
        if not isinstance(view.provider_time, datetime) or view.provider_time.utcoffset() is None:
            return "the quote time is missing or has no timezone"
        if view.provider_time > now:
            return "the quote time is after the replay clock"
        return None

    def _touch(self, view, side, now):
        """The touch price the fill starts from. Provider and historical quotes must pass the V6-0.1 boundary (against the engine clock, which a replay
        sets to the replay clock); synthetic ones are simulation inputs."""
        if view.origin == ORIGIN_SYNTHETIC:
            return view.ask if side == BUY else view.bid
        if view.bid_qty is None or view.ask_qty is None or view.provider_time is None:
            return _refuse("BOUNDARY_REJECTED", "no displayed sizes or quote time")
        try:
            quote = Quote(symbol=view.symbol, quote_type=QuoteType.LIVE_QUOTE, bid=view.bid, ask=view.ask, bid_qty=view.bid_qty,
                          ask_qty=view.ask_qty, ts=view.provider_time, source=view.provider)
            policy = EligibilityPolicy(max_age=self._policy_age, max_future_skew=timedelta(seconds=1), tick_size=view.tick)
            return fill_price(require_fillable(quote, now=now, policy=policy), Side.BUY if side == BUY else Side.SELL)
        except IneligibleQuoteError as error:
            return _refuse("BOUNDARY_REJECTED", ", ".join(sorted(r.name for r in error.result.reasons)))

    def _check_buy(self, request, view, turnover, cost, marks, now):
        limits = self.limits
        if request.lots > limits.max_lots_per_trade:
            return _refuse("RISK_MAX_LOTS", f"limit {limits.max_lots_per_trade}")
        if request.symbol not in self.positions and len(self.positions) >= limits.max_open_positions:
            return _refuse("RISK_MAX_POSITIONS", f"limit {limits.max_open_positions}")
        open_premium = sum(p.basis for p in self.positions.values())
        if open_premium + turnover > limits.max_exposure_fraction * self.starting_balance:
            return _refuse("RISK_EXPOSURE")
        report = self.risk_report({**(marks or {}), request.symbol: view}, now)
        if report.limit_reached:
            return _refuse("RISK_DAILY_LIMIT_REACHED", f"loss incurred {_money(report.incurred)} against a budget of {_money(report.budget)}")
        added = turnover + cost + self.costs.exit_estimate(turnover, request.lots)      # premium + buy fee + estimated exit fees of this position (lot by lot)
        projected = report.committed + added
        if projected > report.budget:
            return _refuse("RISK_DAILY_LOSS", f"projected worst-case daily loss {_money(projected)} against a budget of {_money(report.budget)}: "
                                              f"already committed {_money(report.committed)}, this trade adds {_money(added)}")
        if turnover + cost > self.cash:
            return _refuse("INSUFFICIENT_BALANCE")
        return None

    def _check_sell(self, request, quantity):
        held = self.positions.get(request.symbol)
        if held is None:
            return _refuse("NO_POSITION")
        return _refuse("SIZE_EXCEEDS_POSITION") if quantity > held.quantity else None

    def _plan_sell(self, held, quantity, turnover, cost):
        """(premium leaving the position, buy fee leaving the position, realised P&L) for selling `quantity`; changes nothing."""
        if quantity == held.quantity:
            basis_out, buy_cost = held.basis, held.cost_paid                       # the last part takes the exact remainder, so nothing drifts
        else:
            share = Decimal(quantity) / Decimal(held.quantity)
            basis_out = (held.basis * share).quantize(CENT, ROUND_HALF_UP)
            buy_cost = (held.cost_paid * share).quantize(CENT, ROUND_HALF_UP)
        return basis_out, buy_cost, turnover - basis_out - buy_cost - cost         # proceeds - premium paid - buy fee share - sell fee, all in paise

    def _commit(self, side, symbol, quantity, lot_size, turnover, cost, plan):
        """Apply an already-validated trade. This is the only place cash and positions change."""
        if side == BUY:
            self.cash -= turnover + cost
            held = self.positions.get(symbol)
            if held is None:
                self.positions[symbol] = Position(symbol, quantity, turnover, cost, lot_size)
            else:
                held.quantity, held.basis, held.cost_paid = held.quantity + quantity, held.basis + turnover, held.cost_paid + cost
            return
        basis_out, buy_cost, realized = plan
        held = self.positions[symbol]
        self.cash += turnover - cost
        held.quantity -= quantity
        held.basis -= basis_out
        held.cost_paid -= buy_cost
        if held.quantity == 0:
            del self.positions[symbol]
        self.realized_pnl += realized

    def unrealized(self, symbol, view):
        """Indicative open P&L marked at the bid, or None when prices are not verified (never guessed)."""
        held = self.positions.get(symbol)
        if held is None or not isinstance(view, MarketView) or view.symbol != symbol or not (view.verified and view.permitted) or view.bid is None:
            return None
        return (view.bid * held.quantity - held.basis).quantize(CENT, ROUND_HALF_UP)

    def summary(self):
        return {"starting_balance": self.starting_balance, "cash": self.cash.quantize(CENT), "realized_pnl": self.realized_pnl,
                "realized_today": self.realized_today(), "trades": len(self.ledger), "open_positions": len(self.positions),
                "restored_trades": self.restored, "locked": self.lock_reason, "origin": self.origin,
                "account": self._store.account if self._store is not None else None,
                "state_file": self._store.path.name if self._store is not None else None}
