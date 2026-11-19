"""The ML page (/ml): a saved replay of INVENTED fixture data, shown in layers (see niftypaper.ml.session). Read-only: the page only reads a run's summary.json and
decisions.jsonl; it never runs a model, never calls a provider and never trades. Every section says what kind of data it shows. A saved replay that is not labelled as
invented fixture data with no provider is refused, not shown.

Layout (most important first, advanced information folded away; the page is plain HTML and CSS, with no script, form or button):
  1. summary        one plain-English sentence, the replay-check verdict, and the next step
  2. key numbers    account value, change, decisions, trades, data kind
  3. portfolio history chart
  4. latest decisions and latest trades (the full tables are one click away)
  5. "More detail" folds, all closed: risk limits and refusals, model performance and the research record, system health and the model registry,
     simulator assumptions, limitations, saved replays, how to read this page
"""
import json
import re
from decimal import Decimal
from html import escape
from pathlib import Path

from ..ml import runs
from .render import _CSS, _CSS_UX, _CSS_WORKSPACE

DATA_BANNER = "HISTORICAL REPLAY OF INVENTED FIXTURE DATA — NOT MARKET DATA — NO PROVIDER CONNECTED"
EMPTY_COMMAND = "python run_ml_demo.py"
MAX_BYTES = 8_000_000
RECENT = 6
_CONTRACT = re.compile(r"NIFTY\|[0-9-]+\|([0-9.]+)\|(CE|PE)")


def _short(text):
    """Display only: 'NIFTY|2031-05-29|25000.00|PE' reads as 'PE 25000.00'."""
    return _CONTRACT.sub(lambda m: f"{m.group(2)} {m.group(1)}", text)


def _read_decisions(folder):
    path = Path(folder) / "decisions.jsonl"
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("too large")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def resolve(root, run_id=None):
    """(summary, decisions, run_id, all_runs, problem, status). `root` is either one run's folder (it holds summary.json) or a folder of runs run1, run2, ...
    Without a run id the newest run is shown. An unknown run id is a 404, never a fallback to another run."""
    if root is None:
        return None, None, None, [], "no replay folder configured", 200
    root = Path(root)
    if (root / "summary.json").exists():
        folder, all_runs, chosen = root, [], root.name
    else:
        all_runs = runs.list_runs(root)
        if run_id is not None and run_id not in {r["id"] for r in all_runs}:
            return None, None, run_id, all_runs, f"there is no replay named {run_id}", 404
        readable = [r for r in all_runs if r["summary"] is not None]
        pick = next((r for r in all_runs if r["id"] == run_id), (readable or all_runs or [None])[-1])
        if pick is None:
            return None, None, None, [], "no replay has been run yet", 200
        folder, chosen = pick["path"], pick["id"]
    summary, problem = runs.read_summary(folder)
    if summary is None:
        return None, None, chosen, all_runs, problem, 200
    try:
        decisions = _read_decisions(folder)
    except (OSError, ValueError):
        return None, None, chosen, all_runs, "the saved replay could not be read", 200
    return summary, decisions, chosen, all_runs, None, 200


def load_replay(directory, run_id=None):
    """(summary, decisions, problem) for the JSON route and tests."""
    summary, decisions, _, _, problem, _ = resolve(directory, run_id)
    return summary, decisions, problem


# ---- small building blocks -----------------------------------------------------------------------------------------------------------------
def _tip(text, explanation):
    """A term with a short explanation on hover, keyboard focus or touch."""
    return f'<span class="tip" tabindex="0" data-tip="{escape(explanation)}">{escape(text)}</span>'


def _card(key, value, sub="", tone="", tip=""):
    label = _tip(key, tip) if tip else escape(key)
    return f'<div class="card {tone}"><div class="k">{label}</div><div class="v sm">{escape(str(value))}</div><div class="s">{escape(sub)}</div></div>'


def _kpi(key, value, sub="", tone="", value_class="", tip=""):
    label = _tip(key, tip) if tip else escape(key)
    return f'<div class="kpi {tone}"><div class="k">{label}</div><div class="v {value_class}">{escape(str(value))}</div><div class="s">{escape(sub)}</div></div>'


def _fmt(text):
    try:
        return f"₹{Decimal(text):,.2f}"
    except Exception:
        return str(text)


def _signed(value):
    value = Decimal(value)
    sign = "+" if value > 0 else "−" if value < 0 else ""
    return f"{sign}₹{abs(value):,.2f}"


def _fold(title, hint, inner, extra=""):
    """A closed expandable section: a plain title and a one-line hint of what is inside."""
    return (f'<details class="fold {extra}"><summary><span class="ft">{title}</span><span class="fh">{escape(hint)}</span></summary>'
            f'<div class="fb">{inner}</div></details>')


def _pill(label):
    cls = {"BUY": "buy", "SELL": "sell"}.get(label, "hold")
    return f'<span class="pill {cls}">{escape(label)}</span>'


def _outcome(d):
    """(plain text, tone, raw code for the tooltip)"""
    result = d.get("engine_result") or {}
    if result.get("code"):
        raw = f'REFUSED at {result["stage"]}: {result["code"]}'
        text = "Blocked by the daily loss limit" if result["code"] == "RISK_DAILY_LOSS" else "Blocked by a risk or order rule"
        return text, "neg", raw
    if result.get("event") == "FILL":
        return "Virtual trade made", "pos", "FILL"
    if d["label"] == "NO TRADE":
        return "No action", "", "no action"
    return str(d["outcome"]), "", str(d["outcome"])


# ---- layer 1: what am I looking at, and how did it go --------------------------------------------------------------------------------------
def _hero(s, chosen):
    h, counts, eq = s["health"], s["decisions"], s["equity"]
    start = Decimal(h["starting_balance"])
    end = Decimal(eq[-1]["equity"]) if eq else Decimal(h["final_cash"])
    change = end - start
    audit_ok = h.get("audit_verdict") == "CONSISTENT"
    check = ('<span class="status ok" title="The replay was rebuilt independently from its saved records and every number matched (CONSISTENT / FULL).">✓ Replay checks passed</span>'
             if audit_ok else
             f'<span class="status bad" title="Audit verdict: {escape(str(h.get("audit_verdict")))}">✗ Replay checks did not pass: do not rely on this run</span>')
    n = h["decisions"]
    sentence = (f"A practice run on <strong>invented data</strong>. Over {n} invented trading days (dated 2031) a demonstration model made "
                f"{counts['BUY']} BUY, {counts['SELL']} SELL and {counts['NO TRADE']} NO TRADE decisions, and a virtual account went from "
                f"{escape(_fmt(start))} to {escape(_fmt(end))} ({escape(_signed(change))}).")
    return ('<section class="panel hero primary" aria-labelledby="intro-h"><div class="hero-top"><h2 id="intro-h">What am I looking at?</h2>' + check + "</div>"
            f'<p class="lead">{sentence}</p>'
            '<p class="sub"><strong>No real order exists or can be sent, and no real market data is used.</strong> '
            "The result shows that the software works end to end. It says nothing about any real market, and it does not show that any model makes money.</p>"
            '<p class="next"><strong>Next:</strong> look at the latest decisions below, or open <a href="/">the main dashboard</a> to place a virtual trade yourself. '
            "Use the &ldquo;More detail&rdquo; sections at the bottom only when you want the evidence.</p></section>")


def _modes_inner():
    return ('<div class="cards">'
            + _card("This page", "HISTORICAL REPLAY", "of INVENTED fixtures (2031 dates). Not market data, not a quote.", "tone-accent")
            + _card("Main page, Synthetic Mode", "SYNTHETIC", "generated quotes, no history, no models")
            + _card("Live or delayed provider quotes", "NOT CONNECTED", "no provider is approved; nothing here uses one") + "</div>"
            '<p class="note">Three kinds of data are kept apart. A replay is never shown as live or delayed, and the main page never shows model predictions.</p>')


def _modes_strip():
    return '<section class="panel" aria-labelledby="modes-h"><h2 id="modes-h">Market and data-source status</h2>' + _modes_inner() + "</section>"


def _data_chips(compact=True):
    return ('<div class="chips" aria-label="Where the data comes from">'
            '<span class="chip virtual">Data: invented replay</span><span class="chip">Provider: NOT CONNECTED</span>'
            '<span class="chip">Main page: SYNTHETIC</span></div>')


def _kpis(s):
    h, counts, eq = s["health"], s["decisions"], s["equity"]
    start = Decimal(h["starting_balance"])
    end = Decimal(eq[-1]["equity"]) if eq else Decimal(h["final_cash"])
    change = end - start
    tone = "tone-pos" if change > 0 else "tone-neg" if change < 0 else ""
    cls = "pos" if change > 0 else "neg" if change < 0 else ""
    blocked = len(s["refused"])
    return ('<section class="kpis" aria-label="Key numbers">'
            + _kpi("Virtual account", _fmt(end), f"started at {_fmt(start)}", "", "", "Cash plus the value of anything still held. All virtual money.")
            + _kpi("Change", _signed(change), "on invented data: a pipeline check, not a result", tone, cls, "End value minus start value, after simulated fees.")
            + _kpi("Decisions", h["decisions"], f'{counts["BUY"]} buy · {counts["SELL"]} sell · {counts["NO TRADE"]} no trade', "", "",
                   "One decision per day at 10:15 IST: BUY, SELL, or NO TRADE (do nothing).")
            + _kpi("Virtual trades", h["fills"], f"{blocked} blocked by risk limits" if blocked else "none blocked", "", "",
                   "Trades the simulated account actually made. Blocked attempts change nothing.")
            + "</section>" + _data_chips())


# ---- layer 2: what happened ----------------------------------------------------------------------------------------------------------------
def _equity(s):
    points = s["equity"]
    if len(points) < 2:
        return '<section class="panel"><h2>Portfolio history</h2><p class="note">Not enough decision points to draw a history.</p></section>'
    values = [Decimal(p["equity"]) for p in points]
    start = Decimal(s["health"]["starting_balance"])
    lo, hi = min(values), max(values)
    span = (hi - lo) or Decimal(1)
    width, height, left, right, top, bottom = 640, 190, 14, 14, 16, 22
    plot_w, plot_h = width - left - right, height - top - bottom

    def y_of(v):
        return top + plot_h - float((v - lo) / span) * plot_h

    xy = [(left + i * plot_w / (len(values) - 1), y_of(v)) for i, v in enumerate(values)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    area = f"{xy[0][0]:.1f},{top + plot_h} " + line + f" {xy[-1][0]:.1f},{top + plot_h}"
    change = values[-1] - values[0]
    tone = "pos" if change > 0 else "neg" if change < 0 else ""
    base = ""
    if lo < start < hi:                         # at the very top or bottom the line would only repeat the high or low label
        base = (f'<line x1="{left}" x2="{width - right}" y1="{y_of(start):.1f}" y2="{y_of(start):.1f}" class="base"/>'
                f'<text x="{width - right}" y="{y_of(start) - 4:.1f}" text-anchor="end" class="ax">start {escape(_fmt(start))}</text>')
    first, last = points[0]["at"][:10], points[-1]["at"][:10]
    svg = (f'<svg viewBox="0 0 {width} {height}" role="img" class="eqchart" aria-label="Simulated account value at each decision time, invented data">'
           f'<polygon points="{area}" class="area"/>{base}<polyline points="{line}" fill="none" class="eqline"/>'
           f'<circle cx="{xy[-1][0]:.1f}" cy="{xy[-1][1]:.1f}" r="3.5" class="dot"/>'
           f'<text x="{left}" y="{height - 6}" class="ax">{escape(first)}</text><text x="{width - right}" y="{height - 6}" text-anchor="end" class="ax">{escape(last)}</text>'
           f'<text x="{left}" y="{top - 4}" class="ax">high {escape(_fmt(hi))}{" (= start)" if hi == start else ""}</text>'
           f'<text x="{width - right}" y="{top + plot_h - 5}" text-anchor="end" class="ax">low {escape(_fmt(lo))}</text></svg>')
    table = "".join(f'<tr><td class="l">{escape(p["at"][:16].replace("T", " "))}</td><td>{_fmt(p["cash"])}</td><td>{_fmt(p["positions_value"])}</td><td>{_fmt(p["equity"])}</td>'
                    f'<td>{p["open_quantity"]}</td></tr>' for p in reversed(points))
    return ('<section class="panel" aria-labelledby="eq-h"><h2 id="eq-h">Portfolio history (simulated, invented data)</h2>'
            f'<p class="note">Equity = virtual cash + open positions marked at the bid, at each decision time. Change over the replay: <strong class="{tone}">{escape(_signed(change))}</strong> '
            "on invented data: this is a pipeline check, not a performance claim.</p>"
            f'<div class="chartwrap">{svg}</div>'
            f'<p class="note">Low {_fmt(lo)}, high {_fmt(hi)}. The vertical scale starts at the low, so the line looks steeper than the money really moved.</p>'
            '<details class="inner"><summary>Equity at each decision time</summary><div class="scroll"><table><caption class="sr">Equity history</caption><thead><tr><th class="l">Time (IST)</th>'
            f'<th>Cash</th><th>Positions</th><th>Equity</th><th>Open qty</th></tr></thead><tbody>{table}</tbody></table></div></details></section>')


def _pcell(p):
    if p["probability"] is None:
        return f'<span class="neg" title="{escape(p["reasons"][0][0] if p["reasons"] else "")}">none</span>'
    return f'{Decimal(p["probability"]):.3f} <span class="tag">{escape(p["status"])}</span>'


def _decision_rows(decisions, detailed):
    rows = []
    for d in reversed(decisions):
        preds = d["inputs"].get("predictions", {})
        by_type = {k.split("|")[-1]: v for k, v in preds.items()}
        tone = {"BUY": "pos", "SELL": "neg"}.get(d["label"], "")
        when = escape(d["decision_time"][:16].replace("T", " "))
        why = escape(_short(d["explanation"]))
        if detailed:
            result = d.get("engine_result") or {}
            outcome = result.get("event") or ("no action" if d["label"] == "NO TRADE" else d["outcome"])
            if result.get("code"):
                outcome = f'REFUSED at {result["stage"]}: {result["code"]}'
            rows.append(f'<tr><td class="l">{when}</td><td class="l"><strong class="{tone}">{escape(d["label"])}</strong></td>'
                        f'<td>{_pcell(by_type["CE"]) if "CE" in by_type else "-"}</td><td>{_pcell(by_type["PE"]) if "PE" in by_type else "-"}</td>'
                        f'<td class="l">{escape(outcome)}</td><td class="l" style="white-space:normal;min-width:240px">{why}</td></tr>')
        else:
            text, otone, raw = _outcome(d)
            rows.append(f'<tr><td class="l">{when}</td><td class="l">{_pill(d["label"])}</td><td class="l" style="white-space:normal;min-width:220px">{why}</td>'
                        f'<td class="l"><span class="{otone}" title="{escape(raw)}">{escape(text)}</span></td></tr>')
    return rows


def _decisions(s, decisions):
    counts = s["decisions"]
    recent = _decision_rows(decisions, False)[:RECENT]
    full = _decision_rows(decisions, True)
    up = ("Chance the contract's next settlement price is above the current one, as the model sees it. 0.50 means no lean. "
          "ELIGIBLE = this model may trade on it; none = the model could not predict.")
    explain = ("<p class=\"note\">Each probability is the chance that the contract&rsquo;s next settlement is above the current one. A decision is BUY or SELL only when the prediction "
               "is ELIGIBLE and crosses the rule&rsquo;s threshold (buy at 0.55 or more, sell below 0.50); the paper engine&rsquo;s own limits then apply and may refuse it. "
               "&ldquo;none&rdquo; means the model could not predict (hover for the code); the explanation says why. Newest first.</p>")
    table_full = ('<div class="scroll"><table><caption class="sr">Decisions</caption><thead><tr><th class="l">Decision time (IST)</th><th class="l">Decision</th>'
                  f'<th>Call P(up)</th><th>Put P(up)</th><th class="l">Result</th><th class="l">Recorded explanation</th></tr></thead><tbody>{"".join(full)}</tbody></table></div>')
    return ('<section class="panel" aria-labelledby="dec-h"><h2 id="dec-h">Predictions and decisions (invented replay data)</h2>'
            f'<p class="note">Model <strong>{escape(s["model_id"])}</strong>: {counts["BUY"]} BUY, {counts["SELL"]} SELL, {counts["NO TRADE"]} NO TRADE. The {min(RECENT, len(decisions))} latest decisions, newest first:</p>'
            '<div class="scroll"><table><caption class="sr">Latest decisions</caption><thead><tr><th class="l">Day (IST)</th><th class="l">Decision</th><th class="l">Why</th><th class="l">What happened</th></tr></thead>'
            f'<tbody>{"".join(recent)}</tbody></table></div>'
            f'<details class="inner"><summary>All {len(decisions)} decisions, with the model&rsquo;s probabilities</summary>{explain}'
            f'<p class="note"><span class="tip" tabindex="0" data-tip="{escape(up)}">What does P(up) mean?</span></p>{table_full}</details></section>')


def _fill_rows(fills):
    return "".join(f'<tr><td>{f["seq"]}</td><td class="l">{escape(f["engine_time"][:16].replace("T", " "))}</td><td class="l">{escape(f["side"])}</td>'
                   f'<td class="l">{escape(f["symbol"].split("|")[-1])} {escape(f["symbol"].split("|")[2])}</td><td>{f["quantity"]}</td><td>{escape(f["quote_bid"])} / {escape(f["quote_ask"])}</td>'
                   f'<td>{escape(f["fill_price"])}</td><td>{escape(f["cost"])}</td><td>{escape(str(f["realized_pnl"] or "-"))}</td></tr>' for f in reversed(fills))


def _fills(s):
    fills = s["fills"]
    head = ('<thead><tr><th>#</th><th class="l">Time (IST)</th><th class="l">Side</th><th class="l">Contract</th><th>Qty (units)</th><th>Quote bid / ask</th><th>Fill</th><th>Cost</th>'
            "<th>Realised P&amp;L</th></tr></thead>")
    empty = '<tr><td class="empty" colspan="9">No simulated trades were made in this replay.</td></tr>'
    recent = _fill_rows(fills[-RECENT:]) or empty
    more = ""
    if len(fills) > RECENT:
        more = (f'<details class="inner"><summary>All {len(fills)} simulated trades</summary><div class="scroll"><table><caption class="sr">All simulated fills</caption>{head}'
                f"<tbody>{_fill_rows(fills)}</tbody></table></div></details>")
    return ('<section class="panel" aria-labelledby="fill-h"><h2 id="fill-h">Simulated trades (trade history)</h2>'
            f'<div class="scroll"><table><caption class="sr">Latest simulated fills</caption>{head}<tbody>{recent}</tbody></table></div>{more}'
            '<p class="note">Every trade is virtual; no real order exists or can be sent. Only accepted trades appear here; refused attempts are listed under risk limits and refusals. '
            "Realised P&amp;L is shown on a sale, after fees.</p></section>")


# ---- layer 3: the evidence, folded away -----------------------------------------------------------------------------------------------------
def _risk(s):
    sim = s["simulator"]
    limits = (f'<dl class="kv"><dt>Starting virtual balance</dt><dd>{_fmt(sim["starting_balance"])}</dd><dt>Max lots per trade</dt><dd>{sim["max_lots_per_trade"]}</dd>'
              f'<dt>Max open positions</dt><dd>{sim["max_open_positions"]}</dd><dt>Max open premium</dt><dd>{Decimal(sim["max_exposure_fraction"]) * 100:.0f}% of the starting balance</dd>'
              f'<dt>Daily loss budget</dt><dd>{_fmt(sim["daily_loss_budget"])} ({Decimal(sim["daily_loss_fraction"]) * 100:.0f}% of the starting balance), counted in the worst case '
              "(open premium and fees included)</dd></dl>")
    counts = {}
    for r in s["refused"]:
        counts.setdefault((r["stage"], r["code"], r["message"]), 0)
        counts[(r["stage"], r["code"], r["message"])] += 1
    rows = "".join(f'<tr><td class="l">{escape(st)}</td><td class="l">{escape(code)}</td><td>{n}</td><td class="l" style="white-space:normal">{escape(msg or "no description")}</td></tr>'
                   for (st, code, msg), n in sorted(counts.items())) or '<tr><td class="empty" colspan="4">Nothing was refused in this replay.</td></tr>'
    return ('<div class="cols"><div><h3 class="mini">Limits in force</h3>' + limits +
            '<p class="note">These limit NEW risk. They do not guarantee the day&rsquo;s loss stays within the budget, and nothing is sold automatically.</p></div>'
            '<div><h3 class="mini">Attempts the engine or data checks refused</h3><div class="scroll"><table><caption class="sr">Refusals</caption>'
            '<thead><tr><th class="l">Stage</th><th class="l">Code</th><th>Count</th><th class="l">Meaning</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div><p class="note">A refused attempt changes nothing: no fill, no cash movement, no position. Refusals are normal behaviour, not errors.</p></div></div>')


def _performance(s):
    p = s["performance"]
    if p.get("scored"):
        mine = (f'<dl class="kv"><dt>Probabilities scored</dt><dd>{p["scored"]}</dd>'
                f'<dt>{_tip("Brier score, model", "Average squared gap between each probability and what happened (1 for up, 0 for not up). Lower is better. Always guessing 50% scores 0.25.")}</dt><dd>{p["brier_model"]}</dd>'
                f'<dt>{_tip("Brier score, prior (earlier labels only)", "The same score for a naive guess: the share of earlier days that went up.")}</dt><dd>{p["brier_prior"]}</dd>'
                f'<dt>Difference (model - prior; below 0 is better)</dt><dd>{p["brier_difference"]}</dd>'
                f'<dt>{_tip("Direction hit rate", "How often the model leaned the right way (above 0.50 and the price rose, or the reverse). A coin flip gets about 0.50.")}</dt><dd>{p["hit_rate"]}</dd></dl>'
                f'<p class="note">{escape(p["note"])}</p>')
    else:
        mine = f'<p class="note">{escape(p.get("note", "nothing scored"))}</p>'
    research = "".join(f'<tr><td class="l">{escape(r["option"])}</td><td class="l">{escape(r["model"])}</td><td>{r["prior_brier"]}</td><td>{r["model_brier"]}</td>'
                       f'<td>{r["difference"]}</td><td>{r["interval"]}</td><td class="l">{"supported" if r["supported"] else "NOT SUPPORTED"}</td></tr>' for r in s["research"]["rows"])
    return ('<div><h3 class="mini">On this invented replay</h3>' + mine + "</div>"
            '<div><h3 class="mini">Research record (real historical data; copied, not recomputed here)</h3><div class="scroll"><table><caption class="sr">V5.1 research result</caption>'
            '<thead><tr><th class="l">Option</th><th class="l">Model</th><th>Prior Brier</th><th>Model Brier</th><th>Diff</th><th>97.5% interval</th><th class="l">Verdict</th></tr></thead>'
            f'<tbody>{research}</tbody></table></div><p class="note">{escape(s["research"]["source"])}. No research model was found better than the prior, and none is claimed '
            "profitable. They cannot trade in this program (standing NONE).</p></div>")


def _health(s):
    h = s["health"]
    audit_ok = h.get("audit_verdict") == "CONSISTENT"
    held = ", ".join(f'{k.split("|")[-1]} {v}' for k, v in h["open_positions"].items()) or "none"
    cards = (_card("Data origin", h["data_origin"], "the only origin this page accepts", "tone-accent")
             + _card("Provider", "none connected", h["provider_note"])
             + _card("Replay audit", f'{h.get("audit_verdict")} / {h.get("audit_coverage")}', "events, saved trades and fixtures rebuilt independently", "tone-pos" if audit_ok else "tone-neg",
                     "Checks that the log, the saved trades and the data files agree. CONSISTENT / FULL is the good result.")
             + _card("Decisions / fills", f'{h["decisions"]} / {h["fills"]}', f'{h["events"]} events in the hash-chained log')
             + _card("Virtual cash", _fmt(h["final_cash"]), f'start {_fmt(h["starting_balance"])}')
             + _card("Open positions", held, "units; none are marked here without a quote"))
    models = "".join(f'<tr><td class="l">{escape(m["model_id"])}</td><td class="l">{escape(m["standing"])}</td><td class="l">{escape(m["params_status"])}</td>'
                     f'<td class="l" style="white-space:normal">{escape(m["params_note"] or "-")}</td></tr>' for m in s["models"])
    standing = ("Permission to trade. NONE = display only, never trades. DEMONSTRATION_ONLY = may trade, but only on invented data.")
    return ('<div class="cards">' + cards + "</div>"
            '<div class="scroll"><table><caption>Model registry: a model can trade only if its '
            f'{_tip("standing", standing)} allows it (research models never can)</caption>'
            '<thead><tr><th class="l">Model</th><th class="l">Standing</th><th class="l">Parameters</th><th class="l">Note</th></tr></thead>'
            f"<tbody>{models}</tbody></table></div>"
            f'<p class="note">Event log head {escape(h["event_log_head"][:16])}…, decisions file SHA-256 {escape(h["decisions_sha256"][:16])}… '
            "(local integrity only; these hashes prove the files did not change, not that the inputs are genuine).</p>")


def _assumptions(s):
    sim = s["simulator"]
    items = [
        f"Every fill is simulated at the touch (ask for BUY, bid for SELL) plus {sim['slippage_ticks']} tick ({sim['tick_size']}) of adverse slippage; the quote and the fill are both recorded. No partial fills, no queue position, no shorting.",
        f"Costs are illustrative, not real charges: {sim['fee_per_trade']} per trade plus {Decimal(sim['turnover_fraction']) * 100}% of turnover. No taxes.",
        f"Quantities are whole lots; one lot is {sim['lot_size']} units here, an invented size (not a verified exchange lot size). Quotes must be at most {sim['max_quote_age_seconds']} seconds old.",
        f"The invented quotes have a fixed spread of {Decimal(sim['half_spread']) * 2}. Open positions are marked at the bid, only when a quote exists; otherwise they stay unpriced, never guessed.",
        "One decision per day at 10:15 IST; equity is sampled at those moments only. Positions close only when the model's exit rule fires, so some may remain open at the end.",
        "The invented index is a random walk and the option premiums a fixed linear function of it. It resembles no real market.",
    ]
    return '<ul class="steps">' + "".join(f"<li>{escape(t)}</li>" for t in items) + "</ul>"


def _limits(s):
    return '<ul class="steps">' + "".join(f"<li>{escape(t)}</li>" for t in s["limitations"]) + "</ul>"


def _guide():
    return ('<ul class="steps"><li><strong>Summary and key numbers</strong> (top): how the practice run ended.</li>'
            "<li><strong>Portfolio history</strong>: the virtual account over the 40 days.</li>"
            "<li><strong>Predictions and decisions</strong>: what the model said each day and why the rule followed it.</li>"
            "<li><strong>Simulated trades</strong>: the virtual trades that actually happened.</li>"
            "<li><strong>More detail</strong> (closed by default): risk limits, scoring, model registry, assumptions, limitations.</li></ul>"
            '<p class="note">A prediction model scores two option contracts each day, a decision layer says BUY, SELL or NO TRADE, and a simulated account (virtual money, illustrative fees) '
            "acts on it under risk limits. The 40 trading days are invented, so nothing here shows that any model makes money.</p>")


def _run_picker(all_runs, chosen, opened=False):
    if len(all_runs) < 1:
        return ""
    rows = ""
    for r in all_runs:
        s = r["summary"]
        link = r["id"] if r["id"] == chosen else f'<a href="/ml?run={escape(r["id"])}">{escape(r["id"])}</a>'
        if s is None:
            rows += f'<tr><td class="l">{link}</td><td class="l" colspan="4">not shown: {escape(r["problem"])}</td></tr>'
        else:
            d, h = s["decisions"], s["health"]
            rows += (f'<tr><td class="l">{link}{" (shown)" if r["id"] == chosen else ""}</td><td class="l">{escape(s["model_id"])}</td><td>{d["BUY"]} / {d["SELL"]} / {d["NO TRADE"]}</td>'
                     f'<td>{h["fills"]}</td><td class="l">{escape(str(h["audit_verdict"]))}</td></tr>')
    return (f'<details class="fold"{" open" if opened else ""}><summary><span class="ft">Saved replays</span><span class="fh">each run is kept; none is ever overwritten</span></summary>'
            '<div class="fb"><div class="scroll"><table><caption class="sr">Saved replays</caption>'
            '<thead><tr><th class="l">Run</th><th class="l">Model</th><th>BUY / SELL / NO TRADE</th><th>Fills</th><th class="l">Audit</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div><p class=\"note\">Make another with <code>python run_ml_demo.py --new</code>; list them with <code>python run_ml_demo.py --list</code>.</p></div></details>")


def _page(body, banner=DATA_BANNER, run_label=""):
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>NIFTY Paper Trading: ML replay</title><style>{_CSS}{_CSS_WORKSPACE}{_CSS_UX}</style></head><body>"
            f'<div class="banner" role="alert">{escape(banner)}</div>'
            '<header class="top"><h1>NIFTY Paper Trading</h1><nav aria-label="Pages" style="display:inline-flex;gap:8px"><a class="chip" href="/">Main dashboard</a>'
            '<span class="chip virtual" aria-current="page">ML replay</span></nav>'
            f'<span class="chip">{escape(run_label)}</span><span class="spacer"></span><span class="chip virtual">All trades are virtual</span></header>'
            f'<main>{body}<footer class="foot">No provider connected. Nothing on this page is a prediction about, or advice on, a real market, and no profitability is established.</footer></main></body></html>')


def _empty(problem, has_runs=False):
    if has_runs:
        return ('<section class="panel primary"><h2>No replay to show</h2>'
                f'<p>{escape(problem[0].upper() + problem[1:])}. Pick one of the saved replays listed below, or make a new one with <code>python run_ml_demo.py --new</code>.</p></section>')
    return ('<section class="panel primary"><h2>No replay to show</h2>'
            f'<p>{escape(problem[0].upper() + problem[1:])}. Run the demonstration once to create one (it uses invented data only, makes no network request and needs no credentials):</p>'
            f'<p><code>{escape(EMPTY_COMMAND)}</code></p><p class="note">Replays are saved under data/ml_replay/ (run1, run2, ...) and are never overwritten or deleted by the program.</p></section>')


def render_ml_page(directory, run_id=None):
    """(HTTP status, html). 404 only for a run id that does not exist."""
    summary, decisions, chosen, all_runs, problem, status = resolve(directory, run_id)
    if summary is None:
        body = _modes_strip() + _empty(problem, bool(all_runs)) + _run_picker(all_runs, chosen, opened=True)
        return status, _page(body, run_label="no replay shown")
    folds = (_fold("Risk limits and refusals", "what the account allows and what it refused", _risk(summary))
             + _fold("Model performance", "scoring on invented data, and the real research result", _performance(summary))
             + _fold("System health and current account", "audit verdict, model registry, data origin", _health(summary))
             + _fold("Market and data-source status", "which kinds of data are kept apart", _modes_inner())
             + _fold("Simulator assumptions", "how fills, fees and quotes are simplified", _assumptions(summary))
             + _fold("Limitations", "what these results cannot tell you", _limits(summary))
             + _run_picker(all_runs, chosen)
             + _fold("How to read this page", "a short guide", _guide()))
    body = (_hero(summary, chosen) + _kpis(summary) + _equity(summary) + _decisions(summary, decisions) + _fills(summary)
            + '<section class="more" aria-labelledby="more-h"><h2 id="more-h">More detail</h2><p class="note">Closed by default. Open a section when you want the evidence behind the numbers.</p>'
            + folds + "</section>")
    return 200, _page(body, run_label=f"replay {chosen}")
