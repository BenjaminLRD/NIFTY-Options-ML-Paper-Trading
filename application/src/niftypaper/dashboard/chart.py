"""Synthetic history for the dashboard chart, contract descriptions and the order-entry estimates. Pure functions, no I/O.

SIMULATED HISTORY. The sample provider gives one quote per contract, which is not a time series, so the chart draws a series made up here purely for
visualisation. It is generated from a fixed seed with integer arithmetic (identical on every refresh and every machine), ends at the contract's current synthetic
premium, and is never market history, never a quote and never an input to the paper engine: nothing in this module imports the engine, and its numbers are
not passed to it. Candles come from the one-minute series, so every larger candle is built from the same underlying values.
"""
import hashlib
from decimal import ROUND_HALF_UP, Decimal

SEED = "niftypaper-synthetic-history-v1"
MINUTES = 240                                   # four hours of one-minute candles
SUBSTEPS = 4                                    # random moves inside each minute (they decide high and low)
WINDOWS = {"30m": (30, 1), "1h": (60, 5), "2h": (120, 5), "4h": (240, 5)}      # label -> (minutes shown, minutes per candle)
_MASK = (1 << 64) - 1
CENT = Decimal("0.01")

DESCRIPTIONS = {
    "kind": "CE means Call option; PE means Put option.",
    "strike": "The contract's reference level. It is not the price you pay.",
    "premium": "The price per unit of the option (synthetic here).",
    "bid": "The simulated price a seller receives: what you get if you sell.",
    "ask": "The simulated price a buyer pays: what you pay if you buy.",
    "lot": "One synthetic lot is a made-up simulator size, not a verified exchange lot size.",
}
NOT_REAL = "Synthetic examples: not actual NSE contracts or quotes."


def _stream(label):
    state = int.from_bytes(hashlib.sha256(f"{SEED}|{label}".encode("utf-8")).digest()[:8], "big")
    while True:
        state = (state * 6364136223846793005 + 1442695040888963407) & _MASK
        yield state >> 33


def minute_candles(symbol, price, tick, minutes=MINUTES):
    """`minutes` one-minute candles (oldest first) as lists [minutes_ago, open, high, low, close] of Decimal prices on the tick grid. The last close equals `price`.
    Deterministic for a given (symbol, price, tick)."""
    price, tick = Decimal(price), Decimal(tick)
    if price <= 0 or tick <= 0:
        raise ValueError("price and tick must be positive")
    end = int((price / tick).to_integral_value(ROUND_HALF_UP))
    scale = max(1, end // 1500)                 # move size grows with the price so cheap and dear contracts both look plausible
    draw = _stream(symbol)

    def move():
        return ((next(draw) % 7) - 3) * scale

    raw, close = [], 0
    for _ in range(minutes):
        open_ = close
        points = []
        level = open_
        for _ in range(SUBSTEPS):
            level += move()
            points.append(level)
        close = points[-1]
        raw.append((open_, max([open_] + points), min([open_] + points), close))
    shift = end - raw[-1][3]                    # the series ends exactly at the current synthetic premium
    if min(low for _, _, low, _ in raw) + shift < 1:
        raise ValueError("the price is too small for a synthetic series")
    out = []
    for i, (o, h, l, c) in enumerate(raw):
        out.append([minutes - 1 - i] + [(v + shift) * tick for v in (o, h, l, c)])
    return out


def aggregate(candles, step):
    """Combine consecutive one-minute candles into `step`-minute candles (open of the first, close of the last, highest high, lowest low)."""
    if step == 1:
        return [list(c) for c in candles]
    if step < 1 or len(candles) % step:
        raise ValueError("the candle count must be a multiple of the step")
    out = []
    for i in range(0, len(candles), step):
        group = candles[i:i + step]
        out.append([group[-1][0], group[0][1], max(c[2] for c in group), min(c[3] for c in group), group[-1][4]])
    return out


def windows(candles):
    """{label: candles} for every window: the last `minutes` one-minute candles, aggregated to the window's candle size."""
    return {label: aggregate(candles[-minutes:], step) for label, (minutes, step) in WINDOWS.items()}


def valid_ohlc(candle):
    _, o, h, l, c = candle
    return l <= min(o, c) and h >= max(o, c) and l > 0


def _text(value):
    return format(Decimal(value).quantize(CENT, ROUND_HALF_UP), "f")


def contract_details(row):
    """What a user needs to read a contract without decoding its symbol."""
    kind = {"CE": "Call option (CE)", "PE": "Put option (PE)"}.get(row.kind, row.kind)
    return {"symbol": row.symbol, "kind": row.kind, "kind_text": kind, "strike": row.strike, "strike_text": f"{row.strike:,}", "premium": _text(row.price),
            "bid": _text(row.bid), "ask": _text(row.ask), "lot_size": row.lot_size, "title": f"{kind}, strike {row.strike:,}"}


def order_table(view, engine, report, holding_lots, open_premium, open_count):
    """Backend-calculated numbers for the order panel: for one contract, every whole-lot size the engine could accept, for BUY and SELL.
    The amounts use the engine's own cost model and the same sums its daily risk check uses; the engine still decides every order.
    BUY: order value (fill at the ask plus slippage), fee, estimated exit fee, the amount added to the committed worst case, whether it fits the remaining
    daily budget (and by how much it does not), and the other limits it could fail. SELL: proceeds at the bid less slippage and the fee, for the lots held."""
    limits, costs = engine.limits, engine.costs
    lot_size, tick, slip_ticks = view.lot_size, view.tick, engine.slippage_ticks
    unit_slippage = tick * slip_ticks
    buy, sell = {}, {}
    cap = limits.max_exposure_fraction * engine.starting_balance
    for lots in range(1, limits.max_lots_per_trade + 1):
        quantity = lots * lot_size
        turnover = ((view.ask + unit_slippage) * quantity).quantize(CENT, ROUND_HALF_UP)
        fee = costs.cost(turnover)
        exit_fee = costs.exit_estimate(turnover, lots)
        added = turnover + fee + exit_fee
        excess = max(added - report.headroom, Decimal("0"))
        buy[str(lots)] = {"units": quantity, "value": _text(turnover), "fee": _text(fee), "slippage": _text(unit_slippage * quantity), "exit_fee": _text(exit_fee),
                          "risk": _text(added), "fits": added <= report.headroom and not report.limit_reached, "excess": _text(excess),
                          "exposure_ok": open_premium + turnover <= cap, "cash_ok": turnover + fee <= engine.cash}
    for lots in range(1, max(holding_lots, 0) + 1):
        quantity = lots * lot_size
        turnover = ((view.bid - unit_slippage) * quantity).quantize(CENT, ROUND_HALF_UP)
        sell[str(lots)] = {"units": quantity, "proceeds": _text(turnover), "fee": _text(costs.cost(turnover)), "slippage": _text(unit_slippage * quantity)}
    return {"buy": buy, "sell": sell, "held_lots": holding_lots, "held_units": holding_lots * lot_size,
            "new_position_blocked": view.symbol not in engine.positions and open_count >= limits.max_open_positions}
