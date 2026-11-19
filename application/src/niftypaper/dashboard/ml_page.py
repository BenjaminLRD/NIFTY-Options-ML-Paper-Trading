"""The ML page (/ml): market and data-source status, predictions and explanations, risk limits and refusals, simulator assumptions, model performance, portfolio history
and simulated trades, from a saved replay of INVENTED fixture data (see niftypaper.ml.session). Read-only: the page only reads a run's summary.json and decisions.jsonl; it
never runs a model, never calls a provider and never trades. Every section says what kind of data it shows. A saved replay that is not labelled as invented fixture data
with no provider is refused, not shown.
"""
import json
import re
from decimal import Decimal
from html import escape
from pathlib import Path

from ..ml import runs
from .render import _CSS, _CSS_WORKSPACE

DATA_BANNER = "HISTORICAL REPLAY OF INVENTED FIXTURE DATA — NOT MARKET DATA — NO PROVIDER CONNECTED"
EMPTY_COMMAND = "python run_ml_demo.py"
MAX_BYTES = 8_000_000
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


def _card(key, value, sub="", tone=""):
    return f'<div class="card {tone}"><div class="k">{escape(key)}</div><div class="v sm">{escape(str(value))}</div><div class="s">{escape(sub)}</div></div>'


def _fmt(text):
    try:
        return f"₹{Decimal(text):,.2f}"
    except Exception:
        return str(text)


def _intro():
    return ('<section class="panel primary" aria-labelledby="intro-h"><h2 id="intro-h">What am I looking at?</h2>'
            "<p>A <strong>local paper-trading demonstration</strong>. A prediction model scores two option contracts each day, a decision layer says BUY, SELL or NO TRADE, and a "
            "simulated account (virtual money, illustrative fees) acts on it under risk limits. <strong>No real order exists or can be sent, and no real market data is used.</strong> "
            "The 40 trading days are invented (dated 2031), so nothing here shows that any model makes money.</p>"
            '<ol class="steps"><li><strong>System health</strong>: is the replay consistent, which models may trade?</li><li><strong>Predictions and decisions</strong>: what each model said '
            "and why the decision followed.</li><li><strong>Risk limits and refusals</strong>: what the account allows and what it refused.</li><li><strong>Model performance</strong>, "
            "<strong>portfolio history</strong> and <strong>simulated trades</strong>: what happened.</li><li><strong>Simulator assumptions</strong> and <strong>limitations</strong>: "
            "what is simplified.</li></ol></section>")


def _modes_strip():
    return ('<section class="panel" aria-labelledby="modes-h"><h2 id="modes-h">Market and data-source status</h2><div class="cards">'
            + _card("This page", "HISTORICAL REPLAY", "of INVENTED fixtures (2031 dates). Not market data, not a quote.", "tone-accent")
            + _card("Main page, Synthetic Mode", "SYNTHETIC", "generated quotes, no history, no models")
            + _card("Live or delayed provider quotes", "NOT CONNECTED", "no provider is approved; nothing here uses one") + "</div>"
            '<p class="note">Three kinds of data are kept apart. A replay is never shown as live or delayed, and the main page never shows model predictions.</p></section>')


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
    return (f'<details{" open" if opened else ""}><summary>Saved replays (each run is kept; none is ever overwritten)</summary><div class="scroll"><table><caption class="sr">Saved replays</caption>'
            '<thead><tr><th class="l">Run</th><th class="l">Model</th><th>BUY / SELL / NO TRADE</th><th>Fills</th><th class="l">Audit</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div><p class=\"note\">Make another with <code>python run_ml_demo.py --new</code>; list them with <code>python run_ml_demo.py --list</code>.</p></details>")


def _health(s):
    h = s["health"]
    audit_ok = h.get("audit_verdict") == "CONSISTENT"
    held = ", ".join(f'{k.split("|")[-1]} {v}' for k, v in h["open_positions"].items()) or "none"
    cards = (_card("Data origin", h["data_origin"], "the only origin this page accepts", "tone-accent")
             + _card("Provider", "none connected", h["provider_note"])
             + _card("Replay audit", f'{h.get("audit_verdict")} / {h.get("audit_coverage")}', "events, saved trades and fixtures rebuilt independently", "tone-pos" if audit_ok else "tone-neg")
             + _card("Decisions / fills", f'{h["decisions"]} / {h["fills"]}', f'{h["events"]} events in the hash-chained log')
             + _card("Virtual cash", _fmt(h["final_cash"]), f'start {_fmt(h["starting_balance"])}')
             + _card("Open positions", held, "units; none are marked here without a quote"))
    models = "".join(f'<tr><td class="l">{escape(m["model_id"])}</td><td class="l">{escape(m["standing"])}</td><td class="l">{escape(m["params_status"])}</td>'
                     f'<td class="l" style="white-space:normal">{escape(m["params_note"] or "-")}</td></tr>' for m in s["models"])
    return ('<section class="panel" aria-labelledby="health-h"><h2 id="health-h">System health and current account</h2><div class="cards">' + cards + "</div>"
            '<div class="scroll"><table><caption>Model registry: a model can trade only if its standing allows it (research models never can)</caption>'
            '<thead><tr><th class="l">Model</th><th class="l">Standing</th><th class="l">Parameters</th><th class="l">Note</th></tr></thead>'
            f"<tbody>{models}</tbody></table></div>"
            f'<p class="note">Event log head {escape(h["event_log_head"][:16])}…, decisions file SHA-256 {escape(h["decisions_sha256"][:16])}… '
            "(local integrity only; these hashes prove the files did not change, not that the inputs are genuine).</p></section>")


def _pcell(p):
    if p["probability"] is None:
        return f'<span class="neg" title="{escape(p["reasons"][0][0] if p["reasons"] else "")}">none</span>'
    return f'{Decimal(p["probability"]):.3f} <span class="tag">{escape(p["status"])}</span>'


def _decisions(s, decisions):
    rows = []
    for d in reversed(decisions):
        preds = d["inputs"].get("predictions", {})
        by_type = {k.split("|")[-1]: v for k, v in preds.items()}
        tone = {"BUY": "pos", "SELL": "neg"}.get(d["label"], "")
        result = d.get("engine_result") or {}
        outcome = result.get("event") or ("no action" if d["label"] == "NO TRADE" else d["outcome"])
        if result.get("code"):
            outcome = f'REFUSED at {result["stage"]}: {result["code"]}'
        rows.append(f'<tr><td class="l">{escape(d["decision_time"][:16].replace("T", " "))}</td><td class="l"><strong class="{tone}">{escape(d["label"])}</strong></td>'
                    f'<td>{_pcell(by_type["CE"]) if "CE" in by_type else "-"}</td><td>{_pcell(by_type["PE"]) if "PE" in by_type else "-"}</td>'
                    f'<td class="l">{escape(outcome)}</td><td class="l" style="white-space:normal;min-width:240px">{escape(_short(d["explanation"]))}</td></tr>')
    counts = s["decisions"]
    return ('<section class="panel" aria-labelledby="dec-h"><h2 id="dec-h">Predictions and decisions (invented replay data)</h2>'
            f'<p class="note">Model <strong>{escape(s["model_id"])}</strong>. BUY {counts["BUY"]}, SELL {counts["SELL"]}, NO TRADE {counts["NO TRADE"]}. Each probability is the chance that the '
            "contract&rsquo;s next settlement is above the current one. A decision is BUY or SELL only when the prediction is ELIGIBLE and crosses the rule&rsquo;s threshold (buy at "
            "0.55 or more, sell below 0.50); the paper engine&rsquo;s own limits then apply and may refuse it. &ldquo;none&rdquo; means the model could not predict (hover for the code); "
            "the explanation says why. Newest first.</p>"
            '<div class="scroll"><table><caption class="sr">Decisions</caption><thead><tr><th class="l">Decision time (IST)</th><th class="l">Decision</th><th>Call P(up)</th><th>Put P(up)</th>'
            '<th class="l">Result</th><th class="l">Recorded explanation</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div></section>')


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
    return ('<section class="panel" aria-labelledby="risk-h"><h2 id="risk-h">Risk limits and refusals</h2><div class="cols"><div><h3 class="mini">Limits in force</h3>' + limits +
            '<p class="note">These limit NEW risk. They do not guarantee the day&rsquo;s loss stays within the budget, and nothing is sold automatically.</p></div>'
            '<div><h3 class="mini">Attempts the engine or data checks refused</h3><div class="scroll"><table><caption class="sr">Refusals</caption>'
            '<thead><tr><th class="l">Stage</th><th class="l">Code</th><th>Count</th><th class="l">Meaning</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div><p class="note">A refused attempt changes nothing: no fill, no cash movement, no position. Refusals are normal behaviour, not errors.</p></div></div></section>')


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
    return ('<section class="panel" aria-labelledby="asm-h"><h2 id="asm-h">Simulator assumptions</h2><ul class="steps">' + "".join(f"<li>{escape(t)}</li>" for t in items) + "</ul></section>")


def _performance(s):
    p = s["performance"]
    if p.get("scored"):
        mine = (f'<dl class="kv"><dt>Probabilities scored</dt><dd>{p["scored"]}</dd><dt>Brier score, model</dt><dd>{p["brier_model"]}</dd><dt>Brier score, prior (earlier labels only)</dt>'
                f'<dd>{p["brier_prior"]}</dd><dt>Difference (model - prior; below 0 is better)</dt><dd>{p["brier_difference"]}</dd><dt>Direction hit rate</dt><dd>{p["hit_rate"]}</dd></dl>'
                f'<p class="note">{escape(p["note"])}</p>')
    else:
        mine = f'<p class="note">{escape(p.get("note", "nothing scored"))}</p>'
    research = "".join(f'<tr><td class="l">{escape(r["option"])}</td><td class="l">{escape(r["model"])}</td><td>{r["prior_brier"]}</td><td>{r["model_brier"]}</td>'
                       f'<td>{r["difference"]}</td><td>{r["interval"]}</td><td class="l">{"supported" if r["supported"] else "NOT SUPPORTED"}</td></tr>' for r in s["research"]["rows"])
    return ('<section class="panel" aria-labelledby="perf-h"><h2 id="perf-h">Model performance</h2><div><h3 class="mini">On this invented replay</h3>' + mine + "</div>"
            '<div><h3 class="mini">Research record (real historical data; copied, not recomputed here)</h3><div class="scroll"><table><caption class="sr">V5.1 research result</caption>'
            '<thead><tr><th class="l">Option</th><th class="l">Model</th><th>Prior Brier</th><th>Model Brier</th><th>Diff</th><th>97.5% interval</th><th class="l">Verdict</th></tr></thead>'
            f'<tbody>{research}</tbody></table></div><p class="note">{escape(s["research"]["source"])}. No research model was found better than the prior, and none is claimed '
            "profitable. They cannot trade in this program (standing NONE).</p></div></section>")


def _equity(s):
    points = s["equity"]
    if len(points) < 2:
        return '<section class="panel"><h2>Portfolio history</h2><p class="note">Not enough decision points to draw a history.</p></section>'
    values = [Decimal(p["equity"]) for p in points]
    lo, hi = min(values), max(values)
    span = (hi - lo) or Decimal(1)
    width, height, pad = 640, 170, 12
    xy = [(pad + i * (width - 2 * pad) / (len(values) - 1), height - pad - float((v - lo) / span) * (height - 2 * pad)) for i, v in enumerate(values)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    change = values[-1] - values[0]
    tone = "pos" if change > 0 else "neg" if change < 0 else ""
    table = "".join(f'<tr><td class="l">{escape(p["at"][:16].replace("T", " "))}</td><td>{_fmt(p["cash"])}</td><td>{_fmt(p["positions_value"])}</td><td>{_fmt(p["equity"])}</td>'
                    f'<td>{p["open_quantity"]}</td></tr>' for p in reversed(points))
    return ('<section class="panel" aria-labelledby="eq-h"><h2 id="eq-h">Portfolio history (simulated, invented data)</h2>'
            f'<p class="note">Equity = virtual cash + open positions marked at the bid, at each decision time. Change over the replay: <strong class="{tone}">{_fmt(change)}</strong> '
            "on invented data: this is a pipeline check, not a performance claim.</p>"
            f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Simulated equity at each decision time, invented data" style="width:100%;height:auto;max-height:220px;background:var(--bg2);border:1px solid var(--line);border-radius:10px">'
            f'<polyline points="{line}" fill="none" style="stroke:var(--accent)" stroke-width="2"/></svg>'
            f'<p class="note">Low {_fmt(lo)}, high {_fmt(hi)}.</p>'
            '<details><summary>Equity at each decision time</summary><div class="scroll"><table><caption class="sr">Equity history</caption><thead><tr><th class="l">Time (IST)</th>'
            f'<th>Cash</th><th>Positions</th><th>Equity</th><th>Open qty</th></tr></thead><tbody>{table}</tbody></table></div></details></section>')


def _fills(s):
    fills = "".join(f'<tr><td>{f["seq"]}</td><td class="l">{escape(f["engine_time"][:16].replace("T", " "))}</td><td class="l">{escape(f["side"])}</td>'
                    f'<td class="l">{escape(f["symbol"].split("|")[-1])} {escape(f["symbol"].split("|")[2])}</td><td>{f["quantity"]}</td><td>{escape(f["quote_bid"])} / {escape(f["quote_ask"])}</td>'
                    f'<td>{escape(f["fill_price"])}</td><td>{escape(f["cost"])}</td><td>{escape(str(f["realized_pnl"] or "-"))}</td></tr>' for f in reversed(s["fills"]))
    fills = fills or '<tr><td class="empty" colspan="9">No simulated trades were made in this replay.</td></tr>'
    return ('<section class="panel" aria-labelledby="fill-h"><h2 id="fill-h">Simulated trades (trade history)</h2><div class="scroll"><table><caption class="sr">Simulated fills</caption>'
            '<thead><tr><th>#</th><th class="l">Time (IST)</th><th class="l">Side</th><th class="l">Contract</th><th>Qty (units)</th><th>Quote bid / ask</th><th>Fill</th><th>Cost</th><th>Realised P&amp;L</th></tr></thead>'
            f'<tbody>{fills}</tbody></table></div>'
            '<p class="note">Every trade is virtual; no real order exists or can be sent. Only accepted trades appear here; refused attempts are listed under risk limits and refusals. '
            "Realised P&amp;L is shown on a sale, after fees.</p></section>")


def _limits(s):
    items = "".join(f"<li>{escape(t)}</li>" for t in s["limitations"])
    return f'<section class="panel" aria-labelledby="lim-h"><h2 id="lim-h">Limitations</h2><ul class="steps">{items}</ul></section>'


def _page(body, banner=DATA_BANNER, run_label=""):
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>NIFTY Paper Trading: ML replay</title><style>{_CSS}{_CSS_WORKSPACE}</style></head><body>"
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
    body = (_intro() + _modes_strip() + _run_picker(all_runs, chosen) + _health(summary) + _decisions(summary, decisions) + _risk(summary) + _performance(summary)
            + _equity(summary) + _fills(summary) + _assumptions(summary) + _limits(summary))
    return 200, _page(body, run_label=f"replay {chosen}")
