"""The ML replay session: invented fixtures -> point-in-time predictions -> decisions -> the existing replay runner, bridge, risk limits and paper engine.

run_replay() writes a fresh output folder (it refuses to use one that already exists):
  fixtures/             the invented input set and its manifest
  state/                the replay account's saved trades (the engine's own append-only store)
  events.jsonl          the hash-chained event log written by the replay runner
  decisions.jsonl       one line per decision time: BUY / SELL / NO TRADE, the explanation, and the predictions it rested on
  summary.json          what the dashboard shows: model cards, performance on THIS invented replay, equity history, fills, health

Nothing in it is market data. The performance numbers describe the invented series only.
"""
import hashlib
import json
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from ..historical import audit
from ..historical.replay import ReplayConfig
from ..paper.engine import MESSAGES, CostModel, RiskLimits
from ..strategy.driver import run_strategy
from ..strategy.trend import TrendConfig
from . import fixtures as fx
from .models import DEFAULT_PARAMS_DIR, build_registry
from .research_record import RESEARCH_RESULTS, RESEARCH_SOURCE
from .strategy import MlGatedStrategy, decision_label

DEFAULT_ROOT = Path(__file__).resolve().parents[3] / "data" / "ml_replay"       # every replay is a folder run1, run2, ... under here (see ml/runs.py)
DEFAULT_OUT_DIR = DEFAULT_ROOT / "run1"
DEFAULT_DAYS = 40
MIN_DAYS, MAX_DAYS = 5, 250
STARTING_BALANCE = "1000000"
SUMMARY_VERSION = 2          # 2: adds the simulator assumptions and refusal messages (V6-0.4); older summaries are listed but not shown
DATA_LABEL = "REPLAY OF INVENTED FIXTURE DATA: synthetic, not market data, not a provider"
NOTICE = ("Everything on this page comes from a replay of invented fixture data. No provider is connected, no market data was used, and nothing here shows that any "
          "model is profitable.")


class SessionError(Exception):
    def __init__(self, code, detail):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


def _json_bytes(rows):
    return "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows).encode("utf-8")


def _write_new(path, data):
    with open(path, "xb") as handle:
        handle.write(data)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _labels(days):
    """label[(contract, i)] = whether the contract's next quote midpoint is strictly above this day's. Used only AFTER the replay, to score predictions."""
    out = {}
    for i, d in enumerate(days):
        out[(fx.CE, i)] = int(d.settle_ce > d.quote_mid_ce)
        out[(fx.PE, i)] = int(d.settle_pe > d.quote_mid_pe)
    return out


def _brier(pairs):
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def performance(decisions, days):
    """Score each probability against the realised label (after the fact), against an expanding-mean prior that uses only EARLIER labels."""
    labels, rows = _labels(days), []
    seen = {"CE": [], "PE": []}
    for i, record in enumerate(decisions):
        for contract, p in sorted(record["inputs"]["predictions"].items()):
            kind = contract.split("|")[-1]
            if p["probability"] is not None:
                prior = sum(seen[kind]) / len(seen[kind]) if seen[kind] else 0.5
                rows.append((float(p["probability"]), prior, labels[(contract, i)], kind))
        for contract in (fx.CE, fx.PE):
            seen[contract.split("|")[-1]].append(labels[(contract, i)])      # becomes known when the next day's quote arrives
    if not rows:
        return {"scored": 0, "note": "no probabilities were produced, so nothing can be scored"}
    model, prior = _brier([(p, y) for p, _, y, _ in rows]), _brier([(q, y) for _, q, y, _ in rows])
    hits = sum((p > 0.5) == bool(y) for p, _, y, _ in rows)
    return {"scored": len(rows), "brier_model": round(model, 5), "brier_prior": round(prior, 5), "brier_difference": round(model - prior, 5),
            "hit_rate": round(hits / len(rows), 4),
            "note": "Scored on the invented series only. A difference on 40 invented days says nothing about any market; it is shown to prove the scoring works."}


def equity_history(ledger, decisions, days, starting_balance):
    """Cash plus open positions marked at the bid, at each decision time, rebuilt from the engine's own saved trades."""
    cash, held, out, cursor = Decimal(starting_balance), {}, [], 0
    for i, record in enumerate(decisions):
        moment = record["decision_time"]
        while cursor < len(ledger) and ledger[cursor].engine_time.isoformat() <= moment:
            t = ledger[cursor]
            cursor += 1
            sign = 1 if t.side == "BUY" else -1
            cash += -sign * t.turnover - t.cost
            held[t.symbol] = held.get(t.symbol, 0) + sign * t.quantity
        value = Decimal(0)
        for symbol, quantity in held.items():
            if quantity:
                mid = days[i].quote_mid_ce if symbol == fx.CE else days[i].quote_mid_pe
                value += (mid - fx.HALF_SPREAD) * quantity
        out.append({"at": moment, "cash": str(cash.quantize(Decimal("0.01"))), "positions_value": str(value.quantize(Decimal("0.01"))),
                    "equity": str((cash + value).quantize(Decimal("0.01"))), "open_quantity": sum(held.values())})
    return out


def simulator_assumptions(config):
    """The rules the replay's paper engine ran under, read from the objects it used (engine defaults where the config leaves them unset)."""
    limits, costs = config.limits or RiskLimits(), config.costs or CostModel()
    start = Decimal(config.starting_balance)
    return {"starting_balance": str(start), "max_lots_per_trade": limits.max_lots_per_trade, "max_open_positions": limits.max_open_positions,
            "max_exposure_fraction": str(limits.max_exposure_fraction), "daily_loss_fraction": str(limits.daily_loss_fraction),
            "daily_loss_budget": str(start * limits.daily_loss_fraction), "exit_slippage_ticks": limits.exit_slippage_ticks, "fee_per_trade": str(costs.fee_per_trade),
            "turnover_fraction": str(costs.turnover_fraction), "cost_label": costs.label, "slippage_ticks": config.slippage_ticks,
            "max_quote_age_seconds": int(config.max_quote_age.total_seconds()), "lot_size": fx.LOT, "tick_size": str(fx.TICK), "half_spread": str(fx.HALF_SPREAD)}


def _decision_row(d):
    e = d.evaluation
    return {"decision_time": d.decision_time.isoformat(), "label": decision_label(e), "outcome": d.outcome, "reason_code": d.reason_code, "explanation": e.detail,
            "inputs": e.inputs, "engine_result": d.result}


def run_replay(out_dir=DEFAULT_OUT_DIR, model_id="demo_synthetic_ur1", days=DEFAULT_DAYS, params_dir=DEFAULT_PARAMS_DIR):
    """Run a replay into `out_dir` without ever leaving a half-finished result there. The work happens in a sibling folder named <out>.partial and is renamed to
    <out> only after summary.json (the last file) is written. An interrupted or failed run leaves only the .partial folder, which is never a result: nothing reads it,
    and the next run refuses to start until you inspect or delete it yourself."""
    out = Path(out_dir)
    if isinstance(days, bool) or not isinstance(days, int) or not MIN_DAYS <= days <= MAX_DAYS:
        raise SessionError("BAD_DAYS", f"days must be a whole number from {MIN_DAYS} to {MAX_DAYS}")
    if out.exists() and any(out.iterdir()):
        raise SessionError("DESTINATION_EXISTS", f"{out} already has files; the replay never overwrites or deletes: choose another folder")
    partial = out.with_name(out.name + ".partial")
    if partial.exists():
        raise SessionError("PARTIAL_EXISTS", f"{partial} was left by an interrupted run. It is not a result and nothing reads it. Inspect or delete it yourself, then run again")
    registry = build_registry(params_dir)
    if model_id not in registry:
        raise SessionError("UNKNOWN_MODEL", f"{model_id!r} is not a registered model ({', '.join(sorted(registry))})")
    summary = _build(partial, registry, model_id, days)
    if out.exists():
        out.rmdir()                                  # an empty folder only (checked above)
    os.rename(partial, out)
    return summary


def _build(out, registry, model_id, days):
    model = registry[model_id]
    series = fx.series(days + 1)                                  # one extra day: the final decision's outcome and mark
    decision_days = series[:days]
    out.mkdir(parents=True)
    fixture_dir = fx.write_fixture_set(out / "fixtures", series)
    config = ReplayConfig(fixture_dir=fixture_dir, state_dir=out / "state", as_of=fx.at(series[-1].day + timedelta(days=1), 12, 0),
                          start=fx.at(series[0].day, 9, 0), end=fx.at(series[-1].day, 15, 30), permitted=True)
    strategy = MlGatedStrategy(TrendConfig(call_contract=fx.CE, put_contract=fx.PE, lots=1), model)
    schedule = [fx.decision_time(d.day) for d in decision_days]
    outcome = run_strategy(config, strategy, schedule, out / "events.jsonl")
    rows = [_decision_row(d) for d in outcome.decisions]
    decisions_bytes = _json_bytes(rows)
    _write_new(out / "decisions.jsonl", decisions_bytes)
    report = audit.build_report(audit.AuditConfig(out / "events.jsonl", out / "state" / f"replay_{fx.SOURCE_ID}.jsonl", fixture_dir))
    counts = {k: sum(r["label"] == k for r in rows) for k in ("BUY", "SELL", "NO TRADE")}
    reasons = {}
    for r in rows:
        reasons[r["reason_code"]] = reasons.get(r["reason_code"], 0) + 1
    summary = {
        "summary_version": SUMMARY_VERSION, "data_label": DATA_LABEL, "notice": NOTICE, "model_id": model_id,
        "models": [{**{k: getattr(m.card, k) for k in m.card.__dataclass_fields__}, "allowed_origins": list(m.card.allowed_origins)} for m in registry.values()],
        "decisions": counts, "reasons": dict(sorted(reasons.items())), "performance": performance(rows, decision_days),
        "research": {"source": RESEARCH_SOURCE, "rows": RESEARCH_RESULTS},
        "equity": equity_history(outcome.ledger, rows, decision_days, outcome.starting_balance),
        "fills": [t.to_dict() for t in outcome.ledger],
        "refused": [{"stage": e.get("stage"), "code": e.get("code"), "contract": e.get("contract"), "message": MESSAGES.get(e.get("code"), "")}
                    for e in outcome.events if e["type"] == "INTENT_REFUSED"],
        "simulator": simulator_assumptions(config),
        "health": {"data_origin": "SYNTHETIC_FIXTURE", "provider_connected": False, "provider_note": "none: no provider is approved and none is connected",
                   "audit_verdict": report.get("verdict"), "audit_coverage": report.get("coverage"), "events": len(outcome.events), "fills": len(outcome.ledger),
                   "decisions": len(rows), "decisions_sha256": _sha(decisions_bytes), "plan_digest": outcome.plan_digest, "identity_digest": outcome.identity_digest,
                   "event_log_head": outcome.receipt.head, "event_log_sha256": outcome.receipt.sha256, "starting_balance": str(outcome.starting_balance),
                   "final_cash": str(outcome.cash), "open_positions": {s: p.quantity for s, p in outcome.positions.items()}, "marks": outcome.marks},
        "limitations": [
            "Invented data: results say nothing about any market, any model's skill, or profitability.",
            "A research model without a parameter file (b1_lr1_logistic, b2_ur1_logistic) predicts nothing here; research models also have no demonstrated edge, so they cannot trade.",
            "Fills use the replay's illustrative cost and slippage model, not real charges.",
            "Positions are exited only when the model says so; there is no time or loss stop in this layer (the paper engine's daily risk limit still applies to new risk)."]}
    _write_new(out / "summary.json", (json.dumps(summary, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    return summary
