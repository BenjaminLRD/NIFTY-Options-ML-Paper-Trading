"""Read-only evaluation of what happened in an audited paper replay (simulated; see docs/STRATEGY_EVALUATION.md).

The evaluator asks the existing audit (historical/audit.py) to rebuild the books from the saved event log, state file and fixtures, and calculates its metrics
only from that audit report's rows. It does not generate signals, run a replay or reconstruct accounting itself, and it never writes to the replay artifacts.
If the audit is not CONSISTENT with FULL coverage, or the chain, pairing, input identity or declared plan do not check out, it REFUSES: the report then lists the
reasons and carries no performance metrics. A value that cannot be known is {"unavailable": REASON}, never a zero.

The report digest makes the output reproducible for identical verified inputs. It is not a signature and shows nothing about who made the inputs or whether the
market events were genuine.
"""
import hashlib
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from ..historical import audit, eventchain

REPORT_VERSION = 1
DIGEST_DOMAIN = b"niftypaper.strategy.evaluation/1\n"
SUPPORTED = {"index_sma_trend": ("1.0.0",)}
CENT, RATIO = Decimal("0.01"), Decimal("0.000001")

NOTICE = ("SIMULATED paper-trading evaluation on invented data. It describes what the audited replay did under its stated rules; it is not evidence that the "
          "strategy is, or would be, profitable, and it uses no real market data. The digest gives reproducibility, not authenticity.")
DATA_ELIGIBILITY = {"BRIDGE": {"NO_VISIBLE_QUOTE", "NOT_AVAILABLE_YET", "NOT_FILL_ELIGIBLE", "NOT_A_QUOTE_SNAPSHOT"},
                    "ENGINE": {"NO_BID_ASK", "CROSSED_OR_LOCKED", "OFF_TICK", "LOT_SIZE_UNKNOWN", "LOT_SIZE_CHANGED", "BOUNDARY_REJECTED", "DATA_NOT_VERIFIED",
                               "EXCEEDS_DISPLAYED_SIZE", "PROVENANCE_INVALID", "FILL_PRICE_NOT_POSITIVE"}}
LIMITATIONS = (
    "Signals generated and no-signal decisions are not recorded in the replay's events, so they are unavailable here; only the intents the strategy submitted are known.",
    "The strategy name and version are the caller's declaration; the replay does not record which strategy produced its plan.",
    "Equity is sampled at recorded event times (open, each fill, the final checkpoint), marked at the bid of the newest quote that is recent enough; it is not a continuous curve.",
    "The only baseline is a no-trade account. No buy-and-hold or index benchmark is reconstructed: the paper model cannot trade the index and no comparable series exists in the saved artifacts.",
    "Fees and slippage are the illustrative models the replay states, not real charges. Nothing here is evidence from real, permitted market data.",
)


class EvaluationError(Exception):
    pass


def unavailable(reason):
    return {"unavailable": reason}


def _d(text):
    try:
        value = Decimal(text)
    except (InvalidOperation, TypeError):
        raise EvaluationError(f"not a decimal: {text!r}")
    if not value.is_finite():
        raise EvaluationError("not finite")
    return value


def _m(value):
    return format(value.quantize(CENT, ROUND_HALF_UP), "f")


def _ratio(value):
    return format(value.quantize(RATIO, ROUND_HALF_UP), "f")


def _when(text):
    return datetime.fromisoformat(text)


def _category(stage, code):
    if code in DATA_ELIGIBILITY.get(stage, ()):
        return "DATA_ELIGIBILITY"
    if isinstance(code, str) and (code.startswith("RISK_") or code in ("INSUFFICIENT_BALANCE", "NO_POSITION", "SIZE_EXCEEDS_POSITION")):
        return "RISK_OR_ACCOUNT_LIMIT"
    return "OTHER"


# ---- gate -----------------------------------------------------------------------------------------------------------------------------------
def refusal_reasons(report, strategy_name, strategy_version):
    reasons = []
    if strategy_name not in SUPPORTED or strategy_version not in SUPPORTED.get(strategy_name, ()):
        reasons.append(f"strategy {strategy_name!r} version {strategy_version!r} is not supported (supported: index_sma_trend 1.0.0)")
    if report["verdict"] != "CONSISTENT":
        reasons.append(f"audit verdict is {report['verdict']}, not CONSISTENT")
    if report["coverage"] != "FULL":
        reasons.append(f"audit coverage is {report['coverage']}, not FULL")
    if report["chain"]["status"] != "VALID":
        reasons.append(f"event chain is {report['chain']['status']}")
    if report["pairing"]["status"] != "PAIRED":
        reasons.append(f"state pairing is {report['pairing']['status']}")
    identity = report["inputs_identity"]
    if identity["status"] != "MATCH":
        reasons.append(f"input identity is {identity['status']}")
    if identity.get("plan", {}).get("status") != "MATCH":
        reasons.append(f"declared intent plan is {identity.get('plan', {}).get('status', 'NOT_DECLARED')}")
    if report["anchor"]["status"] not in ("NOT_SUPPLIED", "MATCH"):
        reasons.append(f"anchor comparison is {report['anchor']['status']}")
    failing = [c["id"] for c in report["checks"] if c["status"] != "PASS"]
    if failing:
        reasons.append("audit checks not PASS: " + ", ".join(failing))
    if not reasons and report["summary"].get("restored_trades"):
        reasons.append("the account already held trades before this replay started; only fresh accounts are evaluated")
    return reasons


# ---- metrics from the audit's rows ------------------------------------------------------------------------------------------------------------
def _trades(rows):
    """Lifecycles (a position from flat to flat, per contract) with fee allocation taken from the audit's before/after positions."""
    open_, done, fills = {}, [], []
    for row in rows:
        if row["type"] != "FILL":
            continue
        fill, symbol, side = row["fill"], row["instrument"], row["fill"]["side"]
        fee, turnover, qty = _d(fill["fee"]), _d(fill["turnover"]), fill["quantity"]
        slippage = _d(row["assumptions"]["slippage_per_unit"]) * qty
        fills.append({"fee": fee, "slippage": slippage})
        life = open_.setdefault(symbol, {"contract": symbol, "opened_at": row["clock"], "fills": 0, "gross": Decimal("0"), "net": Decimal("0"), "closed_at": None})
        life["fills"] += 1
        if side == "BUY":
            continue
        before, after = row["positions_before"][symbol], row["positions_after"].get(symbol)
        basis = _d(before["basis"]) - (_d(after["basis"]) if after else Decimal("0"))
        buy_fees = _d(before["buy_fees"]) - (_d(after["buy_fees"]) if after else Decimal("0"))
        gross = turnover - basis
        net = gross - buy_fees - fee
        if net != _d(row["realized_pnl"]["this_trade"]):
            raise EvaluationError("the fee/basis allocation does not reproduce the audit's realised P&L for a sale")
        life["gross"] += gross
        life["net"] += net
        if after is None:
            life["closed_at"] = row["clock"]
            done.append(open_.pop(symbol))
    return done, open_, fills


def _curve(rows):
    points = []
    for row in rows:
        if row["type"] not in ("REPLAY_OPENED", "FILL", "CHECKPOINT"):
            continue
        cash = _d(row["cash_after"])
        held = sum((_d(p["basis"]) for p in row["positions_after"].values()), Decimal("0"))
        mark = row["unrealized_pnl"]
        if mark.get("status") == "AVAILABLE":
            points.append({"clock": row["clock"], "type": row["type"], "equity": cash + held + _d(mark["value"]), "open_premium": held,
                           "open": bool(row["positions_after"])})
        else:
            points.append({"clock": row["clock"], "type": row["type"], "equity": None, "open_premium": held, "open": bool(row["positions_after"]),
                           "why": mark.get("reason", "UNAVAILABLE")})
    return points


def _drawdown(points):
    missing = [p for p in points if p["equity"] is None]
    if missing:
        return unavailable(f"EQUITY_UNAVAILABLE_AT_{len(missing)}_OF_{len(points)}_POINTS")
    peak, peak_clock, worst = points[0]["equity"], points[0]["clock"], (Decimal("0"), Decimal("0"), points[0]["clock"], points[0]["clock"])
    for p in points:
        if p["equity"] > peak:
            peak, peak_clock = p["equity"], p["clock"]
        drop = peak - p["equity"]
        if drop > worst[0]:
            worst = (drop, drop / peak if peak > 0 else Decimal("0"), peak_clock, p["clock"])
    return {"amount": _m(worst[0]), "fraction_of_peak": _ratio(worst[1]), "peak_at": worst[2], "trough_at": worst[3], "points": len(points),
            "note": "largest peak-to-trough fall in the sampled equity points"}


def _intents(rows):
    received = sum(1 for r in rows if r["type"] == "INTENT_RECEIVED")
    accepted = sum(1 for r in rows if r["type"] == "FILL")
    refused = [r for r in rows if r["type"] == "INTENT_REFUSED"]
    by_reason, by_category = {}, {}
    for r in refused:
        stage, code = r["decision"].get("stage"), r["decision"].get("code")
        by_reason[(stage, code)] = by_reason.get((stage, code), 0) + 1
        category = _category(stage, code)
        by_category[category] = by_category.get(category, 0) + 1
    return {"recorded": received, "accepted": accepted, "rejected": len(refused),
            "rejected_by_reason": [{"stage": s, "code": c, "count": n} for (s, c), n in sorted(by_reason.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1])))],
            "rejected_by_category": dict(sorted(by_category.items())),
            "data_eligibility_failures": by_category.get("DATA_ELIGIBILITY", 0)}


def _exposure(points):
    if len(points) < 2:
        return unavailable("TOO_FEW_RECORDED_TIMES")
    span = (_when(points[-1]["clock"]) - _when(points[0]["clock"])).total_seconds()
    inside = sum((_when(b["clock"]) - _when(a["clock"])).total_seconds() for a, b in zip(points, points[1:]) if a["open"])
    peak = max(p["open_premium"] for p in points)
    out = {"observed_span_seconds": format(Decimal(str(span)), "f"), "time_in_market_seconds": format(Decimal(str(inside)), "f"),
           "peak_open_premium": _m(peak), "note": "time with an open position between recorded event times, over the span from the replay's open to its final checkpoint"}
    out["fraction_of_observed_span"] = _ratio(Decimal(str(inside)) / Decimal(str(span))) if span > 0 else unavailable("ZERO_LENGTH_SPAN")
    return out


def classify(nets):
    """(winning nets, losing nets, number of breakeven trades) for completed trades' net P&L; a trade is a win or a loss only if its net is not exactly zero."""
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    return wins, losses, len(nets) - len(wins) - len(losses)


def compute_metrics(report):
    rows, summary = report["rows"], report["summary"]
    start = _d(report["account"]["starting_balance"])
    done, still_open, fills = _trades(rows)
    points = _curve(rows)
    final = points[-1]
    realized_total = _d(summary["rebuilt_realized_total"])
    realized_done = sum((t["net"] for t in done), Decimal("0"))
    realized_open = sum((t["net"] for t in still_open.values()), Decimal("0"))
    if realized_done + realized_open != realized_total:
        raise EvaluationError("trade-level realised P&L does not add up to the audit's account-level total")
    last = next(r for r in reversed(rows) if r["type"] == "CHECKPOINT")
    open_fees = sum((_d(p["buy_fees"]) for p in last["positions_after"].values()), Decimal("0"))
    total_fees = sum((f["fee"] for f in fills), Decimal("0"))
    realized_fees = sum((t["gross"] - t["net"] for t in list(done) + list(still_open.values())), Decimal("0"))
    if realized_fees + open_fees != total_fees or total_fees != _d(summary["rebuilt_fees_total"]):
        raise EvaluationError("fee allocation does not add up to the audit's total fees")
    mark = last["unrealized_pnl"]
    unrealized = unavailable(mark.get("reason", "UNAVAILABLE")) if mark.get("status") != "AVAILABLE" else _m(_d(mark["value"]))
    if final["equity"] is not None:
        if final["equity"] != start + realized_total + _d(mark["value"]) - open_fees:
            raise EvaluationError("final equity does not equal starting balance + realised + unrealised - fees carried by open positions")
        equity_final = _m(final["equity"])
        change = _m(final["equity"] - start)
    else:
        equity_final = change = unavailable("OPEN_POSITION_HAS_NO_USABLE_MARK")
    n_done = len(done)
    wins, losses, even = classify([t["net"] for t in done])
    if n_done:
        win_rate = {"value": _ratio(Decimal(len(wins)) / n_done), "wins": len(wins), "denominator": n_done, "denominator_meaning": "completed trades"}
        avg_win = _m(sum(wins) / len(wins)) if wins else unavailable("NO_WINNING_TRADES")
        avg_loss = _m(sum(losses) / len(losses)) if losses else unavailable("NO_LOSING_TRADES")
        if losses:
            profit_factor = _ratio(sum(wins, Decimal("0")) / abs(sum(losses)))
        else:
            profit_factor = unavailable("NO_LOSING_TRADES")
        durations = [(_when(t["closed_at"]) - _when(t["opened_at"])).total_seconds() for t in done]
        average_hold = format(Decimal(str(sum(durations))) / n_done, "f")
    else:
        win_rate = unavailable("NO_COMPLETED_TRADES")
        avg_win = avg_loss = profit_factor = average_hold = unavailable("NO_COMPLETED_TRADES")
    slippage = sum((f["slippage"] for f in fills), Decimal("0"))
    return {
        "equity": {"initial": _m(start), "final": equity_final, "change": change, "sampled_points": len(points),
                   "points_without_a_mark": sum(1 for p in points if p["equity"] is None),
                   "reconciliation": "final equity = starting balance + realised P&L + unrealised P&L - buy fees carried by open positions (checked)"},
        "pnl": {"realized_net": _m(realized_total), "realized_on_completed_trades": _m(realized_done), "realized_on_open_trades": _m(realized_open),
                "unrealized": unrealized, "unrealized_basis": mark.get("note") or mark.get("basis") or mark.get("reason"),
                "gross_realized_before_fees": _m(sum((t["gross"] for t in list(done) + list(still_open.values())), Decimal("0"))) if fills else unavailable("NO_FILLS"),
                "gross_basis": "sale proceeds minus the premium paid for the quantity sold, after modelled slippage, before fees"},
        "costs": {"fees_total": _m(total_fees) if fills else unavailable("NO_FILLS"), "fees_on_realized": _m(realized_fees) if fills else unavailable("NO_FILLS"),
                  "fees_carried_by_open_positions": _m(open_fees) if fills else unavailable("NO_FILLS"),
                  "modeled_slippage_total": _m(slippage) if fills else unavailable("NO_FILLS"),
                  "slippage_note": "slippage is already inside the fill prices (informational; not deducted again)"},
        "trades": {"completed": n_done, "open": len(still_open), "winning": len(wins) if n_done else unavailable("NO_COMPLETED_TRADES"),
                   "losing": len(losses) if n_done else unavailable("NO_COMPLETED_TRADES"), "breakeven": even if n_done else unavailable("NO_COMPLETED_TRADES"),
                   "win_rate": win_rate, "average_win": avg_win, "average_loss": avg_loss, "profit_factor": profit_factor,
                   "profit_factor_definition": "sum of winning trades / absolute sum of losing trades; undefined without a losing trade",
                   "average_holding_seconds": average_hold,
                   "completed_list": [{"contract": t["contract"], "opened_at": t["opened_at"], "closed_at": t["closed_at"], "fills": t["fills"], "gross": _m(t["gross"]),
                                       "net": _m(t["net"])} for t in done],
                   "open_list": [{"contract": t["contract"], "opened_at": t["opened_at"], "fills": t["fills"], "realized_so_far": _m(t["net"])}
                                 for t in still_open.values()]},
        "drawdown": _drawdown(points),
        "exposure": _exposure(points),
        "intents": _intents(rows),
        "signals": {"generated": unavailable("NOT_RECORDED_IN_EVENT_LOG"), "no_signal_decisions": unavailable("NOT_RECORDED_IN_EVENT_LOG")},
        "equity_curve": [{"clock": p["clock"], "event": p["type"], "equity": _m(p["equity"]) if p["equity"] is not None else unavailable(p["why"])} for p in points],
    }


def baseline(report, metrics):
    start = metrics["equity"]["initial"]
    out = {"kind": "NO_TRADE", "starting_equity": start, "final_equity": start, "same_window": report["inputs_identity"]["claimed"]["window"],
           "valuation": "cash only, no interest; the paper model holds nothing and pays no fees in this baseline",
           "benchmark": unavailable("NO_COMPARABLE_BENCHMARK_IN_SAVED_ARTIFACTS")}
    final = metrics["equity"]["final"]
    out["strategy_minus_baseline"] = _m(_d(final) - _d(start)) if not isinstance(final, dict) else unavailable("FINAL_EQUITY_UNAVAILABLE")
    return out


# ---- the report ---------------------------------------------------------------------------------------------------------------------------
def evaluate(config, strategy_name="index_sma_trend", strategy_version="1.0.0"):
    """Run the existing audit for `config` (an audit.AuditConfig) and build the evaluation report. Reads only."""
    audited = audit.build_report(config)
    reasons = refusal_reasons(audited, strategy_name, strategy_version)
    claimed = audited["inputs_identity"]["claimed"]
    claimed = claimed if isinstance(claimed, dict) and "digest" in claimed else {}
    plan = audited["inputs_identity"].get("plan", {})
    body = {
        "report_version": REPORT_VERSION, "kind": "STRATEGY_EVALUATION", "simulated": True, "synthetic_data": True, "notice": NOTICE,
        "strategy": {"name": strategy_name, "version": strategy_version, "declared_by": "caller (not recorded in the replay)"},
        "identity": {"source_id": claimed.get("source_id", unavailable("NOT_ESTABLISHED")), "account": claimed.get("account", unavailable("NOT_ESTABLISHED")),
                     "window": claimed.get("window", unavailable("NOT_ESTABLISHED")), "input_identity_digest": claimed.get("digest", unavailable("NOT_ESTABLISHED")),
                     "account_binding": claimed.get("account_binding", unavailable("NOT_ESTABLISHED")),
                     "manifest_sha256": claimed.get("manifest_sha256", unavailable("NOT_ESTABLISHED")), "plan_digest": plan.get("claimed_digest", unavailable("NOT_ESTABLISHED")),
                     "planned_intents": plan.get("claimed_intents", unavailable("NOT_ESTABLISHED"))},
        "evidence": {"audit_verdict": audited["verdict"], "audit_coverage": audited["coverage"], "audit_digest": audited["digest"], "chain_status": audited["chain"]["status"],
                     "chain_events": audited["chain"]["events_verified"], "chain_head": audited["chain"]["head"], "anchor_status": audited["anchor"]["status"],
                     "anchor_protection": audited["anchor"].get("protection"), "pairing": audited["pairing"]["status"],
                     "input_identity": audited["inputs_identity"]["status"], "plan": plan.get("status"),
                     "events_file": audited["inputs"]["events"], "state_file": audited["inputs"]["state"],
                     "reading": "Consistency of the supplied files with each other and the stated rules. An unanchored or locally anchored chain detects edits by others "
                                "only if the head or anchor is held where the writer cannot change it; nothing here shows the events are genuine."},
        "limitations": list(LIMITATIONS),
    }
    if reasons:
        body.update(status="REFUSED", refusal_reasons=reasons, metrics=unavailable("EVALUATION_REFUSED"), baseline=unavailable("EVALUATION_REFUSED"))
    else:
        try:
            metrics = compute_metrics(audited)
        except (EvaluationError, KeyError, TypeError, ValueError, StopIteration) as error:
            body.update(status="REFUSED", refusal_reasons=[f"the audited rows could not be evaluated safely ({error.__class__.__name__}: {error})"],
                        metrics=unavailable("EVALUATION_REFUSED"), baseline=unavailable("EVALUATION_REFUSED"))
        else:
            body.update(status="EVALUATED", refusal_reasons=[], metrics=metrics, baseline=baseline(audited, metrics))
    body["digest"] = report_digest(body)
    return body


def report_digest(body):
    plain = {k: v for k, v in body.items() if k != "digest"}
    return hashlib.sha256(DIGEST_DOMAIN + eventchain.canonical(plain).encode("ascii")).hexdigest()
