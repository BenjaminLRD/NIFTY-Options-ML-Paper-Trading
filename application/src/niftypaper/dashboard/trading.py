"""Glue between the market-data side and the virtual-trading engine: turns a data snapshot into MarketViews and the page's view model.

The engine never sees a provider; it sees only a MarketView that already says whether its prices are verified current prices.
Synthetic data counts as verified only while its state is SIMULATED. Provider data counts as verified only in the state LIVE, which needs
explicit evidence of real-time delivery and a documented quote time; UNVERIFIED, DELAYED, STALE and UNAVAILABLE data can be shown but never traded.
"""
from decimal import Decimal

from ..paper.engine import ORIGIN_PROVIDER, ORIGIN_SYNTHETIC, MarketView
from ..providers.registry import PROVIDERS, credential_flags, real_provider_ids
from . import chart
from .status import VALUE_STATES, DataState, Snapshot, connection_status


def build_views(health, snapshot, permitted):
    """{symbol: MarketView} for the option rows that may be shown; empty when no values may be shown."""
    if not isinstance(snapshot, Snapshot) or health.state not in VALUE_STATES:
        return {}
    synthetic = snapshot.synthetic is True
    verified = health.state is (DataState.SIMULATED if synthetic else DataState.LIVE)
    connection = connection_status(health.state, not synthetic)
    return {row.symbol: MarketView(
        origin=ORIGIN_SYNTHETIC if synthetic else ORIGIN_PROVIDER, provider=snapshot.source, state=health.state.value, connection=connection,
        symbol=row.symbol, bid=row.bid, ask=row.ask, last=row.price, bid_qty=row.bid_qty, ask_qty=row.ask_qty, tick=row.tick, lot_size=row.lot_size,
        received_at=snapshot.received_at or snapshot.as_of, provider_time_raw=snapshot.provider_time_raw, provider_time=snapshot.provider_time,
        verified=verified, permitted=bool(permitted), reason=health.reason) for row in snapshot.chain}


def trade_gate(health, views, permitted):
    """(allowed, explanation) for the trading panel."""
    if health.state not in VALUE_STATES:
        return False, f"Prices are {health.state.value}: {health.reason}."
    if not views:
        return False, "No option contract is available to trade."
    first = next(iter(views.values()))
    if not first.verified:
        return False, f"Current prices are {health.state.value}, not verified current prices: {health.reason}."
    if not permitted:
        return False, "Virtual trading with this provider's data is disabled until its terms permission is recorded locally."
    return True, "Quote check passed: current prices are eligible for simulated fills. This is not an order result: an order can still be refused by a risk or order rule. Fills are simulated and never real orders."


def build_ui(app, health, snapshot, now):
    provider_mode = app.mode == "provider"
    info = PROVIDERS.get(app.provider_id)
    permitted, permission_note = app.permission()
    views = build_views(health, snapshot, permitted)
    allowed, why = trade_gate(health, views, permitted)
    engine = app.engine
    held = []
    if engine is not None:
        held = [(p.symbol, p.quantity, p.average_price, engine.unrealized(p.symbol, views.get(p.symbol))) for p in engine.positions.values()]
    risk = engine.risk_report(views) if engine is not None else None
    notice, notice_kind, notice_code = app.notice, app.notice_kind, app.notice_code
    app.notice = app.notice_kind = app.notice_code = None
    position_rows = []
    if engine is not None:
        for p in engine.positions.values():
            view = views.get(p.symbol)
            pnl = engine.unrealized(p.symbol, view)
            position_rows.append({"symbol": p.symbol, "quantity": p.quantity, "lots": p.quantity // p.lot_size if p.lot_size else 0, "average": p.average_price,
                                  "bid": view.bid if (pnl is not None and view is not None) else None, "pnl": pnl})
    estimates = []
    if engine is not None:
        for symbol in sorted(views):
            v = views[symbol]
            if v.lot_size and v.ask is not None and v.tick:
                unit_slippage = v.tick * engine.slippage_ticks
                premium = (v.ask + unit_slippage) * v.lot_size
                fee = engine.costs.cost(premium)
                estimates.append({"symbol": symbol, "lot_size": v.lot_size, "premium": premium, "fee": fee, "slippage": unit_slippage * v.lot_size,
                                  "risk_added": premium + fee + engine.costs.exit_estimate(premium, 1)})
    workspace = build_workspace(health, snapshot, views, engine, risk)
    return {
        "csrf": app.csrf, "nonce": app.issue_nonce(), "ui_mode": "realtime" if provider_mode else "synthetic", "provider_id": app.provider_id,
        "provider_name": info.display_name if info else app.provider_id, "info": info, "can_switch": app.factories is not None,
        "real_providers": [(i, PROVIDERS[i].display_name) for i in real_provider_ids()],
        "connection": connection_status(health.state, provider_mode), "state": health.state.value,
        "credentials": credential_flags(info, app.env) if info and info.needs_access else [],
        "permitted": permitted, "permission_note": permission_note,
        "trade_allowed": engine is not None and allowed and not engine.lock_reason,
        "trade_note": (engine.lock_reason or why) if engine is not None else "Virtual trading is not available in this session.", "risk": risk,
        "instruments": sorted(views), "positions": held,
        "ledger": [r.to_dict() for r in (engine.ledger[-20:] if engine else [])], "summary": engine.summary() if engine else None,
        "slippage_ticks": engine.slippage_ticks if engine else None, "costs": engine.costs if engine else None, "limits": engine.limits if engine else None,
        "notice": notice, "notice_kind": notice_kind, "notice_code": notice_code, "eligible": bool(allowed and not engine_locked(engine)),
        "eligibility_text": (engine.lock_reason if engine is not None and engine.lock_reason else why), "data_state_text": health.state.value, "data_reason": health.reason,
        "unpriced": [row for row in position_rows if row["pnl"] is None], "max_age_seconds": int(app.max_age.total_seconds()), "workspace": workspace, "position_rows": position_rows, "estimates": estimates,
    }


def engine_locked(engine):
    return engine is not None and bool(engine.lock_reason)


def position_status(engine, views):
    """Every open position with whether a verified price exists for it right now. A position without one is UNPRICED: nothing is inferred for it."""
    out = []
    for p in engine.positions.values():
        view = views.get(p.symbol)
        pnl = engine.unrealized(p.symbol, view)
        out.append({"symbol": p.symbol, "quantity": p.quantity, "lots": p.quantity // p.lot_size if p.lot_size else None, "average_fill": str(p.average_price),
                    "priced": pnl is not None, "unrealized": None if pnl is None else str(pnl)})
    return out


def build_workspace(health, snapshot, views, engine, risk):
    """Everything the chart and the order panel show, calculated here and not in the page: contract descriptions, a SIMULATED HISTORY series per contract, and
    the engine-backed order estimates. History exists only for fresh synthetic data; provider data (or no data) gets no chart and nothing synthetic in its place.
    None of it is an executable quote and none of it is passed to the engine."""
    if not isinstance(snapshot, Snapshot) or snapshot.synthetic is not True or health.state is not DataState.SIMULATED or not views:
        return {"chart": False, "reason": "No chart: it is drawn only for synthetic data in the SIMULATED state. Provider data and unavailable or stale data are never charted, "
                                          "and nothing synthetic is shown in their place."}
    rows = {row.symbol: row for row in snapshot.chain}
    symbols = sorted(views)
    try:
        details = {s: chart.contract_details(rows[s]) for s in symbols}
        history = {s: {label: [[c[0]] + [format(v, "f") for v in c[1:]] for c in candles]
                       for label, candles in chart.windows(chart.minute_candles(s, rows[s].price, rows[s].tick)).items()} for s in symbols}
    except Exception:                       # a failing picture must never take trading or the page down: the chart just disappears, with a message
        return {"chart": False, "reason": "No chart: the simulated history could not be generated. Trading and quotes are not affected."}
    out = {"chart": True, "symbols": symbols, "details": details, "history": history, "windows": list(chart.WINDOWS), "default": symbols[0],
           "descriptions": chart.DESCRIPTIONS, "not_real": chart.NOT_REAL, "order": None}
    if engine is not None and risk is not None:
        held = {p.symbol: (p.quantity // p.lot_size if p.lot_size else 0) for p in engine.positions.values()}
        open_premium = sum((p.basis for p in engine.positions.values()), Decimal("0"))
        out["order"] = {
            "symbols": {s: chart.order_table(views[s], engine, risk, held.get(s, 0), open_premium, len(engine.positions)) for s in symbols},
            "headroom": format(risk.headroom, "f"), "limit_reached": risk.limit_reached, "max_lots": engine.limits.max_lots_per_trade,
            "max_open": engine.limits.max_open_positions, "locked": bool(engine.lock_reason)}
    return out
