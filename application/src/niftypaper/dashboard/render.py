"""Turns a (health, snapshot, account) view into an HTML page and a JSON document. Pure functions; the only file read is this package's own chart.js, once, at import. No inputs from the network.

Values are shown only in the states SIMULATED, UNVERIFIED, LIVE and DELAYED; for STALE and UNAVAILABLE no price is rendered at all.
Synthetic values are tagged SYN (data-synthetic="true"). Provider values are tagged with their state name (data-synthetic="false"), so
UNVERIFIED provider data is never shown as live. The page has no font, image or link to anything outside this server: it is one document with an inline
style sheet and one local script (chart.js, allowed by the server's content-security policy only by its SHA-256 hash; the data it draws sits in a JSON block). Its only forms (when modes and trading are enabled) post to the dashboard's own /mode and /paper/trade, with the per-run form key.

Layout: a compact top bar (product, mode, status, virtual cash), the data cards, the trading workspace beside the account and risk cards, positions, the option
chain, recent activity, and a collapsed "Details" area for everything long (data health, provider setup and terms, risk assumptions, persistence).
"""
import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from html import escape
from pathlib import Path

from .status import VALUE_STATES, DataState, Snapshot

BANNER = "SIMULATED DATA — NOT LIVE MARKET DATA"
PROVIDER_BANNERS = {
    DataState.UNVERIFIED: "PROVIDER DATA — TIMING NOT VERIFIED — NOT CONFIRMED LIVE",
    DataState.LIVE: "PROVIDER DATA — LIVE STATUS DOCUMENTED",
    DataState.DELAYED: "PROVIDER DATA — DELAYED",
    DataState.STALE: "PROVIDER DATA STALE — NO VALUES SHOWN",
    DataState.UNAVAILABLE: "PROVIDER DATA UNAVAILABLE — NO VALUES SHOWN",
}
IST = timezone(timedelta(hours=5, minutes=30))
CENT = Decimal("0.01")
_JS = Path(__file__).with_name("chart.js").read_text(encoding="utf-8")
SCRIPT_CSP_HASH = "'sha256-" + base64.b64encode(hashlib.sha256(_JS.encode("utf-8")).digest()).decode("ascii") + "'"
# a symbol next to every state so that the state never depends on colour alone
STATE_MARK = {"SIMULATED": "◆", "LIVE": "●", "DELAYED": "◐", "UNVERIFIED": "?", "STALE": "!", "UNAVAILABLE": "✕"}

_CSS = """
:root{color-scheme:dark;--bg:#0a0e14;--bg2:#0d121a;--panel:#121923;--panel2:#18212d;--panel3:#1d2836;--line:#243040;--line2:#2f3d51;--fg:#e9edf3;--muted:#93a0b2;--dim:#6f7c8f;
--accent:#1de9a0;--accent2:#6ff2c8;--pos:#3fd68f;--neg:#ff6b6b;--warn:#f0bf4c;--bannerbg:#33250a;--bannerfg:#ffd88a;--r:12px;--shadow:0 1px 0 rgba(255,255,255,.03) inset,0 8px 24px rgba(0,0,0,.28)}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:radial-gradient(1200px 500px at 50% -200px,#13202f 0,var(--bg) 70%) no-repeat,var(--bg);color:var(--fg);
font:15px/1.5 "Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,Roboto,sans-serif;font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased}
.banner{background:var(--bannerbg);color:var(--bannerfg);font-weight:700;font-size:.78rem;letter-spacing:.08em;text-align:center;padding:6px 12px;border-bottom:1px solid #5e4510}
header.top{display:flex;flex-wrap:wrap;align-items:center;gap:10px 14px;padding:12px 24px;background:rgba(18,25,35,.92);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5;backdrop-filter:blur(6px)}
header.top h1{font-size:1.05rem;margin:0 8px 0 0;letter-spacing:.04em;font-weight:700}
header.top h1::before{content:"";display:inline-block;width:10px;height:10px;border-radius:3px;background:var(--accent);margin-right:9px;vertical-align:baseline}
.chip{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line2);border-radius:999px;padding:3px 11px;font-size:.76rem;font-weight:600;background:var(--panel2);color:var(--fg);white-space:nowrap}
.chip.virtual{border-color:#1f6b55;color:var(--accent2);background:#0f2a23}
.state-SIMULATED,.state-LIVE{color:var(--pos)}.state-STALE,.state-UNAVAILABLE,.state-UNVERIFIED,.state-DELAYED{color:var(--warn)}
.spacer{flex:1}
.mode{display:inline-flex;flex-wrap:wrap;align-items:center;gap:10px;margin:0}
.seg{display:inline-flex;border:1px solid var(--line2);border-radius:9px;overflow:hidden;background:var(--panel2)}
.seg button{font:inherit;font-size:.84rem;font-weight:600;padding:7px 14px;border:0;background:transparent;color:var(--muted);cursor:pointer;transition:background .12s,color .12s}
.seg button:hover{background:var(--panel3);color:var(--fg)}
.seg button+button{border-left:1px solid var(--line2)}
.seg button[aria-pressed=true]{background:var(--accent);color:#03170f}
main{max-width:1160px;margin:0 auto;padding:22px 24px 8px;display:grid;gap:22px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px}
.card{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line);border-top:2px solid var(--line2);border-radius:var(--r);padding:14px 16px 13px;min-width:0;box-shadow:var(--shadow)}
.card.tone-pos{border-top-color:var(--pos)}.card.tone-neg{border-top-color:var(--neg)}.card.tone-accent{border-top-color:var(--accent)}
.card .k{color:var(--muted);font-size:.7rem;font-weight:600;text-transform:uppercase;letter-spacing:.09em}
.card .v{font-size:clamp(1.25rem,2.4vw,1.55rem);font-weight:700;margin-top:4px;line-height:1.2;letter-spacing:-.01em;white-space:nowrap}
.card .v.sm{font-size:1rem;font-weight:650;white-space:normal;overflow-wrap:anywhere}
.card .s{color:var(--dim);font-size:.78rem;margin-top:4px}
.card.big .v{font-size:clamp(1.5rem,3.4vw,2.05rem)}
.cols{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(0,1fr);gap:22px;align-items:start}
.stack{display:grid;gap:22px;min-width:0}
@media (max-width:860px){.cols{grid-template-columns:minmax(0,1fr)}header.top{padding:10px 14px;position:static}main{padding:14px 14px 6px;gap:16px}}
section.panel{background:linear-gradient(180deg,var(--panel),var(--bg2));border:1px solid var(--line);border-radius:var(--r);padding:18px 20px;min-width:0;box-shadow:var(--shadow)}
section.panel>h2{font-size:.78rem;margin:0 0 12px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:700}
.primary{border-color:#1f6b55;box-shadow:var(--shadow),0 0 0 1px rgba(29,233,160,.12)}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:.88rem}
caption{text-align:left;color:var(--muted);font-size:.78rem;padding-bottom:6px}
th,td{padding:8px 12px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{color:var(--dim);font-weight:700;font-size:.68rem;text-transform:uppercase;letter-spacing:.08em;background:rgba(255,255,255,.015)}
th.l,td.l{text-align:left}
tbody tr:nth-child(even){background:rgba(255,255,255,.02)}
tbody tr:hover{background:var(--panel3)}
td.empty{text-align:center;color:var(--muted);padding:22px 12px;white-space:normal}
.tag{font-size:.6rem;color:var(--dim);border:1px solid var(--line2);border-radius:4px;padding:0 4px;margin-left:6px;vertical-align:middle;font-weight:600;letter-spacing:.05em;white-space:nowrap}
.note{color:var(--muted);font-size:.82rem;margin:8px 0 0}
.pos{color:var(--pos)}.neg{color:var(--neg)}
.notice{border:1px solid var(--line2);border-left:4px solid var(--accent);background:var(--panel3);border-radius:10px;padding:11px 15px;font-weight:600;margin:0 0 14px;box-shadow:var(--shadow)}
.notice.ok{border-left-color:var(--pos);background:#0f2a21}.notice.refused{border-left-color:var(--neg);background:#2c1519}
.notice span{display:block;font-size:.72rem;letter-spacing:.08em;text-transform:uppercase;margin-bottom:3px}
.notice.ok span{color:var(--pos)}.notice.refused span{color:var(--neg)}
.blocked{border:1px dashed var(--warn);border-radius:10px;padding:12px 15px;color:var(--warn);font-weight:600;margin:0 0 10px;background:rgba(240,191,76,.06)}
form.trade{display:grid;gap:14px;margin-top:14px}
.field{display:grid;gap:5px}
.field label,.field .lab{font-size:.7rem;color:var(--muted);text-transform:uppercase;letter-spacing:.09em;font-weight:600}
select,input[type=text],input:not([type]){font:inherit;color:var(--fg);background:var(--bg2);border:1px solid var(--line2);border-radius:9px;padding:10px 12px;width:100%}
select:hover,input:hover{border-color:#41546d}
.mode select{width:auto;padding:5px 8px;font-size:.84rem}
.side{display:grid;grid-template-columns:1fr 1fr;border:1px solid var(--line2);border-radius:10px;overflow:hidden;position:relative;background:var(--bg2)}
.side input{position:absolute;opacity:0;pointer-events:none}
.side label{padding:11px 12px;text-align:center;cursor:pointer;background:transparent;color:var(--muted);font-weight:800;text-transform:none;letter-spacing:.06em;font-size:.95rem;transition:background .12s,color .12s}
.side label:hover{background:var(--panel3);color:var(--fg)}
.side input#side-buy:checked+label{background:var(--pos);color:#04180f}
.side input#side-sell:checked+label{background:var(--neg);color:#2a0709}
.side input:focus-visible+label{outline:2px solid var(--fg);outline-offset:-4px}
button.go{font:inherit;font-weight:800;letter-spacing:.03em;padding:13px 18px;border:0;border-radius:10px;background:linear-gradient(180deg,#4af0b8,var(--accent));color:#03170f;cursor:pointer;box-shadow:0 6px 16px rgba(79,157,255,.22)}
button.go:hover{filter:brightness(1.08)}button.go:active{transform:translateY(1px)}
:focus-visible{outline:2px solid var(--accent2);outline-offset:2px}
progress{-webkit-appearance:none;appearance:none;width:100%;height:12px;border:0;border-radius:999px;background:var(--panel3);overflow:hidden;accent-color:var(--accent);display:block;margin:8px 0}
progress::-webkit-progress-bar{background:var(--panel3);border-radius:999px}
progress::-webkit-progress-value{background:var(--accent);border-radius:999px}
progress::-moz-progress-bar{background:var(--accent);border-radius:999px}
progress.hot::-webkit-progress-value{background:var(--warn)}progress.hot::-moz-progress-bar{background:var(--warn)}progress.hot{accent-color:var(--warn)}
progress.full::-webkit-progress-value{background:var(--neg)}progress.full::-moz-progress-bar{background:var(--neg)}progress.full{accent-color:var(--neg)}
.risk-row{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;margin:6px 0}
.risk-row .big{font-size:1.7rem;font-weight:800;letter-spacing:-.01em}
.risk-status{font-size:.82rem;font-weight:700}
.pct{color:var(--muted);font-size:.78rem;display:flex;justify-content:space-between}
dl.kv{display:grid;grid-template-columns:minmax(0,max-content) minmax(0,1fr);gap:6px 18px;margin:8px 0 0}dl.kv dt{color:var(--muted)}dl.kv dd{margin:0;overflow-wrap:anywhere}
@media (max-width:520px){dl.kv{grid-template-columns:minmax(0,1fr)}dl.kv dd{margin-bottom:6px}.card .v{font-size:1.35rem}}
details{border:1px solid var(--line);border-radius:10px;background:var(--panel);padding:0 16px;margin:10px 0}
details>summary{cursor:pointer;padding:11px 0;font-weight:650;font-size:.88rem;color:var(--fg);list-style-position:inside}
details>summary:hover{color:var(--accent2)}
details[open]>summary{border-bottom:1px solid var(--line);margin-bottom:12px}
details>:last-child{margin-bottom:14px}
ol.steps{margin:6px 0 0;padding-left:22px}ol.steps li{margin:6px 0}
footer.foot{color:var(--dim);font-size:.76rem;text-align:center;padding:6px 0 26px}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""


def _money(value, places=2):
    return f"{Decimal(value):,.{places}f}"


def _rupee(value):
    return f"&#8377;{escape(_money(value))}"


def _signed(value):
    value = Decimal(value)
    return ("&#9650; +" if value > 0 else "&#9660; -" if value < 0 else "&#9679; ") + _rupee(abs(value))


def _tone(value):
    value = Decimal(value)
    return "tone-pos" if value > 0 else "tone-neg" if value < 0 else ""


def _signed_class(value):
    value = Decimal(value)
    return "pos" if value > 0 else "neg" if value < 0 else ""


def _provider_mode(snapshot, mode):
    """Provider data is recognised from the snapshot itself, so a mislabelled mode cannot make it look synthetic."""
    if isinstance(snapshot, Snapshot):
        return snapshot.synthetic is False
    return mode == "provider"


def _tag(health, snapshot):
    return ("SYN", "true") if snapshot.synthetic is True else (health.state.value, "false")


def _cell(health, snapshot, text, tag="td"):
    label, flag = _tag(health, snapshot)
    return f'<{tag} class="num" data-synthetic="{flag}">{escape(text)}<span class="tag">{escape(label)}</span></{tag}>'


def _time(value):
    return value.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S IST") if value is not None else "-"


def _short_time(value):
    return value.astimezone(IST).strftime("%H:%M:%S") if value is not None else "-"


def _short_time_text(iso):
    try:
        return datetime.fromisoformat(iso).astimezone(IST).strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return "-"


def banner_text(health, snapshot, mode="synthetic"):
    if not _provider_mode(snapshot, mode):
        return BANNER
    return PROVIDER_BANNERS[health.state]


def _status_chip(state):
    return f'<span class="chip state-{escape(state)}"><span aria-hidden="true">{STATE_MARK.get(state, "?")}</span> {escape(state)}</span>'


# ---- top bar ----------------------------------------------------------------------------------------------------------------------------
def _mode_form(ui):
    synthetic = ui["ui_mode"] == "synthetic"
    if not ui["can_switch"]:
        return '<span class="note">Mode switching is not configured for this session.</span>'
    options = "".join(f'<option value="{escape(i)}"{" selected" if i == ui["provider_id"] else ""}>{escape(name)}</option>' for i, name in ui["real_providers"])
    return (f'<form method="post" action="/mode" class="mode"><input type="hidden" name="csrf" value="{escape(ui["csrf"])}">'
            '<span class="seg" role="group" aria-label="Market data mode">'
            f'<button name="mode" value="synthetic" aria-pressed="{"true" if synthetic else "false"}">Synthetic Mode (Free)</button>'
            f'<button name="mode" value="realtime" aria-pressed="{"false" if synthetic else "true"}">Real-Time Mode (requires provider access)</button></span>'
            f'<label class="note">Provider <select name="provider" aria-label="Provider for Real-Time Mode">{options}</select></label></form>')


def _top_bar(health, ui, provider):
    mode_text = "Real-Time" if provider else "Synthetic"
    cash = ""
    if ui and ui["summary"] is not None:
        cash = f'<span class="chip">Virtual cash {_rupee(ui["summary"]["cash"])}</span>'
    form = _mode_form(ui) if ui else ""
    link = '<a class="chip" href="/ml">ML replay (invented data)</a>' if ui else ""      # the only link on the page: same-origin, read-only
    return ('<header class="top"><h1>NIFTY Paper Trading</h1>'
            f'<span class="chip">Mode: {escape(mode_text)}</span>{_status_chip(health.state.value)}'
            f'<span class="chip virtual">All trades are virtual</span>{link}<span class="spacer"></span>{cash}{form}</header>')


# ---- market data -------------------------------------------------------------------------------------------------------------------------
def _freshness(health, now, limit):
    """How old the data is and the limit it is judged by. A provider's age is measured from OUR receive time, which says nothing about when the market observation was made."""
    ref = health.received_at or health.as_of
    if ref is None or now is None:
        return "age unknown"
    age = int((now - ref).total_seconds())
    basis = "since the response was received" if health.received_at else "since the data was produced"
    return f"{max(age, 0)} s old {basis}; treated as stale after {limit} s" if limit else f"{max(age, 0)} s old {basis}"


def _market(health, snapshot, provider, now=None, ui=None):
    shown = health.state in VALUE_STATES
    kind = "provider data" if provider else "SIMULATED"
    if shown:
        pct = snapshot.pct_change
        change = "-" if pct is None else f"{Decimal(pct):+.2f}%"
        arrow = "" if pct is None else ("&#9650; " if Decimal(pct) > 0 else "&#9660; " if Decimal(pct) < 0 else "&#9679; ")
        tone = "" if pct is None else _signed_class(pct)
        index = f'<div class="v">{_cell(health, snapshot, _money(snapshot.index_value), "span")}</div>'
        move = f'<div class="v {tone}">{arrow}{_cell(health, snapshot, change, "span")}</div>'
        gap = ""
    else:
        index = move = '<div class="v">&mdash;</div>'
        gap = f'<div class="s">No value shown: {escape(health.state.value)}</div>'
    extra = ""
    if provider:
        exchange = _time(snapshot.provider_time) if shown and snapshot.provider_time else "not established"
        extra = (f'<div class="s">Received {escape(_short_time(health.received_at))} (local clock); provider time field {escape(repr(health.provider_time_raw))}</div>'
                 f'<div class="s">Exchange time: {escape(exchange)}</div>')
    return ('<section aria-label="Market overview" class="cards">'
            f'<div class="card big tone-accent"><div class="k">NIFTY index ({escape(kind)})</div>{index}{gap}</div>'
            f'<div class="card"><div class="k">Change ({escape(kind)})</div>{move}</div>'
            f'<div class="card"><div class="k">Data source</div><div class="v sm">{escape(health.source)}</div>'
            f'<div class="s">Status <span class="state-{escape(health.state.value)}">{STATE_MARK.get(health.state.value, "?")} <strong>{escape(health.state.value)}</strong></span></div>'
            f'<div class="s">Evidence: {escape(snapshot.evidence) if shown else "none (no data shown)"}</div></div>'
            f'<div class="card"><div class="k">Last update</div><div class="v sm">{escape(_time(health.as_of))}</div>'
            f'<div class="s">{escape(_freshness(health, now, ui["max_age_seconds"] if ui else None))}</div>{extra}</div></section>{_mode_strip(health, snapshot, provider, ui)}')


def _mode_strip(health, snapshot, provider, ui):
    """What the numbers are and what the app does with them, in one line: data mode, source and state, and that quotes are not trading."""
    if ui is None:
        return ""
    if provider:
        mode = f"Real-Time Mode &mdash; provider data from {escape(health.source)}, state {escape(health.state.value)}"
    else:
        mode = f"Synthetic Mode &mdash; invented numbers from {escape(health.source)}, state {escape(health.state.value)}"
    verdict = "ELIGIBLE" if ui.get("eligible") else "NOT ELIGIBLE"
    return ('<p class="strip" role="note"><strong>Data mode:</strong> ' + mode + f'. <strong>Execution prices: {verdict}</strong> &mdash; {escape(ui.get("eligibility_text", ""))} '
            '<strong>Quotes are not trading:</strong> "live" would describe quote data only; '
            "this app has no broker connection, every trade is virtual and no real order can be sent.</p>")


def _chain(health, snapshot, provider):
    title = "Selected option contract (provider data)" if provider else "Sample option chain &mdash; SYNTHETIC"
    head = f'<section class="panel" aria-labelledby="chain-h"><h2 id="chain-h">{title}</h2>'
    if health.state not in VALUE_STATES:
        return head + f'<p class="note">UNAVAILABLE &mdash; no option values shown ({escape(health.state.value)}).</p></section>'
    if not snapshot.chain:
        return head + '<p class="note">No option contract is shown for this snapshot.</p></section>'
    rows = "".join(f'<tr><td class="l">{escape(r.symbol)}</td><td class="l">{escape(r.kind)}</td><td>{r.strike}</td>'
                   f"{_cell(health, snapshot, _money(r.bid))}{_cell(health, snapshot, _money(r.ask))}{_cell(health, snapshot, _money(r.price))}</tr>"
                   for r in snapshot.chain)
    note = ("Prices come from the provider. Their timing is not verified; do not treat them as live or as fillable."
            if provider else "Invented numbers and invented labels. Not NSE quotes. Not market conditions.")
    return (head + f'<p class="note">{escape(note)}</p><div class="scroll"><table><caption class="sr">Option chain</caption>'
            '<thead><tr><th class="l">Instrument</th><th class="l">Type</th><th>Strike</th><th>Bid</th><th>Ask</th><th>Price</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div></section>")


# ---- details (collapsed) -------------------------------------------------------------------------------------------------------------------
def _health_details(health, snapshot, provider):
    notes = ""
    if provider:
        items = "".join(f"<li>{escape(n)}</li>" for n in (snapshot.notes if isinstance(snapshot, Snapshot) else ()))
        notes = ('<p class="note">A received response proves neither that the price is current nor when the market observation was made. '
                 "The provider time field is shown exactly as received and is not used to judge freshness.</p>" + (f"<ul>{items}</ul>" if items else ""))
    return ('<details><summary>Data health and eligibility</summary>'
            f'<dl class="kv"><dt>Source status</dt><dd>{_status_chip(health.state.value)}</dd>'
            f"<dt>Reason</dt><dd>{escape(health.reason)}</dd>"
            f"<dt>Last update</dt><dd>{escape(_time(health.as_of))}</dd>"
            f"<dt>Stale or unavailable</dt><dd>{'yes' if health.state in (DataState.STALE, DataState.UNAVAILABLE) else 'no'}</dd></dl>"
            '<p class="note">States: LIVE and DELAYED need explicit provider evidence; UNVERIFIED is well-formed provider data with timing not established; '
            "SIMULATED is synthetic; STALE and UNAVAILABLE show no values. Unavailable data is never replaced by synthetic data. "
            f"Only synthetic data in its fresh state, or LIVE provider data with recorded terms permission, can be traded.</p>{notes}</details>")


def _requirements(ui):
    info = ui["info"]
    if info is None or not info.needs_access:
        from ..providers.registry import PROVIDERS, real_provider_ids
        ids = real_provider_ids()
        if not ids:
            return ""
        info, lead = PROVIDERS[ids[0]], "To use Real-Time Mode you need provider access, for example:"
    else:
        lead = "Requirements for the selected provider:"
    names = ui["credentials"] if ui["credentials"] else [(n, None) for n in info.credential_env]
    flags = "".join(f"<li>{escape(n)}: {'needed' if v is None else 'set' if v else 'NOT set'} (value never shown)</li>" for n, v in names)
    steps = "".join(f"<li>{escape(t)}</li>" for t in info.setup_steps)
    return (f"<p>{escape(lead)} <strong>{escape(info.display_name)}</strong></p><ul><li>Subscription: {escape(info.subscription_note)}</li>"
            f"<li>Permissions: {escape(info.permission_note)}</li><li>Terms: {escape(info.terms_note)}</li>{flags}</ul>"
            f'<p><strong>Setup steps</strong></p><ol class="steps">{steps}</ol>')


def _provider_details(ui):
    synthetic = ui["ui_mode"] == "synthetic"
    permission = "" if synthetic else (f"<dt>Terms permission for virtual trading</dt><dd>{'recorded' if ui['permitted'] else 'NOT recorded'}: {escape(ui['permission_note'])}</dd>")
    return ('<details><summary>Provider connection and setup</summary>'
            f'<dl class="kv"><dt>Selected provider</dt><dd>{escape(ui["provider_name"])}</dd>'
            f'<dt>Connection status</dt><dd><span class="state-{escape(ui["state"])}">{escape(ui["connection"])}</span> (data state {escape(ui["state"])})</dd>{permission}</dl>'
            '<p class="note">CONNECTED: the provider answered and its timing is established. UNVERIFIED: it answered but real-time status or the meaning of its time field is not established. '
            "STALE: the last answer is too old. UNAVAILABLE: no usable answer. A last-trade time is not treated as a quote time unless the provider documents it so. "
            "Real-Time Mode means a real provider is used; it does not mean the data is verified real-time. "
            "To return to synthetic data, press Synthetic Mode (Free) at the top: unavailable real data is never replaced by synthetic data on its own.</p>"
            f"{_requirements(ui)}</details>")


def _risk_details(ui):
    r, limits, costs = ui["risk"], ui["limits"], ui["costs"]
    if r is None or limits is None:
        return ""
    marks = "" if r.marks_known else " (some positions have no verified price, so their loss so far is not yet counted; the committed total is unaffected)"
    return ('<details><summary>Risk details and assumptions</summary>'
            '<dl class="kv">'
            f"<dt>Trading day</dt><dd>{r.day.isoformat()} (India time; a new day starts at 00:00 IST)</dd>"
            f"<dt>Budget</dt><dd>&#8377;{escape(_money(r.budget))} ({escape(str(limits.daily_loss_fraction * 100))}% of the configured starting balance)</dd>"
            f"<dt>Realised today (after fees)</dt><dd>&#8377;{escape(_money(r.realized_today))}</dd>"
            f"<dt>Unrealised loss on open positions</dt><dd>&#8377;{escape(_money(r.unrealized_loss))}{escape(marks)}</dd>"
            f"<dt>Exit slippage allowance</dt><dd>{r.exit_slippage_ticks} tick(s) per unit off a marked position's value when judging the loss so far "
            f"(&#8377;{escape(_money(r.exit_slippage_allowance))} of the unrealised loss above); not added to the committed worst case, which already assumes the whole premium can be lost</dd>"
            f"<dt>Most the open positions could still lose</dt><dd>&#8377;{escape(_money(r.remaining_max_loss))}</dd>"
            f"<dt>Buy fees on open positions</dt><dd>&#8377;{escape(_money(r.open_buy_fees))}</dd>"
            f"<dt>Estimated fees for exiting them (one flat fee per lot)</dt><dd>&#8377;{escape(_money(r.exit_cost_estimate))}</dd>"
            f"<dt>Loss counted so far (estimate, not realised)</dt><dd>&#8377;{escape(_money(r.incurred))} = realised loss today (&#8377;{escape(_money(max(-r.realized_today, 0)))}) + marked loss on open positions (&#8377;{escape(_money(r.unrealized_loss))}, including the exit-slippage allowance, gains never offset) + buy fees on open positions (&#8377;{escape(_money(r.open_buy_fees))}); a profit today would reduce it</dd>"
            f"<dt>Committed worst case</dt><dd>&#8377;{escape(_money(r.committed))}</dd>"
            f"<dt>Remaining budget for new risk</dt><dd>&#8377;{escape(_money(r.headroom))} (a new BUY needs its premium + buy fee + estimated exit cost to fit)</dd></dl>"
            '<p class="note">This limits NEW risk. It does not guarantee that the day\'s loss stays within the budget: price gaps, thin or missing liquidity, bad prices or an exit that '
            "cannot be made can lose more, and nothing is ever sold automatically. Exits are always allowed.</p>"
            f'<p class="note">Limits (illustrative): {limits.max_lots_per_trade} lots per trade, {limits.max_open_positions} open positions, open premium (premium paid for open positions, at fill prices, without fees) '
            f"up to {escape(str(limits.max_exposure_fraction * 100))}% of the starting balance; a BUY is refused if the worst-case loss (realised loss today, plus all open premium and buy fees, plus this trade's premium and fee) "
            f"could pass {escape(str(limits.daily_loss_fraction * 100))}% of the starting balance. Costs ({escape(costs.label)}): {escape(str(costs.fee_per_trade))} per trade plus "
            f"{escape(str(costs.turnover_fraction * 100))}% of turnover. Missing or crossed bid/ask means no fill; displayed size is checked only when the provider supplies it.</p></details>")


def _account_details(ui):
    summary = ui["summary"]
    if summary is None:
        return ""
    saved = summary["state_file"]
    where = (f"account {escape(str(summary['account']))}, saved to {escape(saved)}" if saved else "NOT saved: held in memory only, so a restart would reset it")
    return ('<details><summary>Account and persistence</summary><dl class="kv">'
            f"<dt>Account</dt><dd>{where}</dd>"
            f"<dt>Trades in this account now</dt><dd>{summary['trades']}</dd>"
            f"<dt>Trades restored when the session opened</dt><dd>{summary['restored_trades']} (counted once, when the saved file was read; not the current count)</dd>"
            f"<dt>Starting virtual balance</dt><dd>&#8377;{escape(_money(summary['starting_balance']))} (configurable example)</dd></dl>"
            '<p class="note">Trades are appended to the file as they happen, so a restart keeps positions, cash and the day\'s risk budget. A damaged or foreign file locks trading instead of starting blank; '
            "move or delete the file yourself to begin a new virtual account.</p></details>")


# ---- trading workspace -------------------------------------------------------------------------------------------------------------------
# which kind of rule refused an order, by the engine's refusal code (the order is only "Recorded" when a trade was actually saved)
RISK_LIMIT_CODES = {"RISK_DAILY_LOSS", "RISK_DAILY_LIMIT_REACHED"}
ORDER_RULE_CODES = {"BAD_REQUEST", "RISK_MAX_LOTS", "RISK_MAX_POSITIONS", "RISK_EXPOSURE", "INSUFFICIENT_BALANCE", "NO_POSITION", "SIZE_EXCEEDS_POSITION",
                    "EXCEEDS_DISPLAYED_SIZE", "LOT_SIZE_CHANGED"}
ACCOUNT_CODES = {"STATE_UNAVAILABLE", "PERSISTENCE_FAILED", "ACCOUNT_MISMATCH"}


def _refusal_heading(code):
    if code == "DUPLICATE_SUBMISSION":
        return "Not submitted: this form was already used"
    if code in RISK_LIMIT_CODES:
        return "Refused by the daily risk limit"
    if code in ORDER_RULE_CODES:
        return "Order refused (order rule; the quotes were not the problem)"
    if code in ACCOUNT_CODES:
        return "Refused: account state problem"
    return "Refused: quote or data check failed"


def _notice(ui, kinds):
    if not ui["notice"] or ui["notice_kind"] not in kinds:
        return ""
    cls, mark = {"trade_ok": ("ok", "&#10003; Recorded"), "trade_refused": ("refused", "&#10007; " + _refusal_heading(ui.get("notice_code")))}.get(ui["notice_kind"], ("", "&#9432; Mode"))
    return f'<p class="notice {cls}" role="status"><span>{mark}: </span>{escape(ui["notice"])}</p>'


def _estimates(ui):
    est = ui["estimates"]
    if not est:
        return ""
    rows = "".join(f'<tr><td class="l">{escape(e["symbol"])}</td><td>{e["lot_size"]}</td><td>{escape(_money(e["premium"]))}</td><td>{escape(_money(e["fee"]))}</td><td>{escape(_money(e["slippage"]))}</td><td>{escape(_money(e["risk_added"]))}</td></tr>'
                   for e in est)
    return ('<details><summary>Estimates for one lot (BUY)</summary><div class="scroll"><table><caption class="sr">Per-lot estimates</caption>'
            '<thead><tr><th class="l">Instrument</th><th>Units per lot</th><th>Est. order value (&#8377;)</th><th>Est. fee (&#8377;)</th><th>Slippage allowance (&#8377;)</th><th>Counts against risk budget (&#8377;)</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div><p class="note">Order value is the current ask plus the slippage allowance, times the units in one lot. The fee uses the illustrative cost model. The last column is what a one-lot BUY adds to the committed worst case: order value + buy fee + estimated exit fee (the same sum the daily risk check uses). '
            "The actual simulated fill uses the price at the moment you press the button, so it can differ.</p></details>")


def _trade_form(ui):
    est = {e["symbol"]: e for e in ui["estimates"]}
    w = ui["workspace"]
    det = w["details"] if w.get("chart") else {}
    options = "".join(f'<option value="{escape(s)}">{escape(s)}' + (f' &mdash; {escape(det[s]["kind_text"])} &middot; strike {escape(det[s]["strike_text"])} &middot; premium {_fmt_money(det[s]["premium"])}' if s in det else
                                                                    (f' &middot; est. &#8377;{escape(_money(est[s]["premium"]))}/lot' if s in est else "")) + "</option>" for s in ui["instruments"])
    sizes = sorted({e["lot_size"] for e in ui["estimates"]})
    unit = (f"1 lot = {sizes[0]} units in this simulator" if len(sizes) == 1 else "Units per lot differ by instrument (see the estimates)") if sizes else "Units per lot are set by the instrument"
    return ('<form method="post" action="/paper/trade" class="trade">'
            f'<input type="hidden" name="csrf" value="{escape(ui["csrf"])}"><input type="hidden" name="nonce" value="{escape(ui["nonce"])}">'
            f'<div class="field"><label for="symbol">Instrument</label><select id="symbol" name="symbol">{options}</select></div>'
            '<div class="field"><span class="lab" id="sidelab">Side</span><span class="side" role="radiogroup" aria-labelledby="sidelab">'
            '<input type="radio" id="side-buy" name="side" value="BUY" checked><label for="side-buy">BUY</label>'
            '<input type="radio" id="side-sell" name="side" value="SELL"><label for="side-sell">SELL</label></span></div>'
            '<div class="field"><label for="lots">Quantity (lots)</label><input id="lots" name="lots" value="1" size="3" inputmode="numeric" pattern="[0-9]{1,3}" aria-describedby="lotnote"></div>'
            f'<p class="note" id="lotnote">{escape(unit)}. This is a synthetic contract size, <strong>not a verified exchange lot size</strong>; quantities are whole lots and orders are never split.</p>'
            f'{_static_summary(w)}<button class="go">Simulate virtual trade</button></form>')


def _trading_panel(ui):
    summary = ui["summary"]
    head = '<section class="panel primary" aria-labelledby="trade-h"><h2 id="trade-h">Paper-trading workspace &mdash; virtual trades only</h2>'
    if summary is None:
        return head + f'<p class="blocked">{escape(ui["trade_note"])}</p></section>'
    body = _notice(ui, ("trade_ok", "trade_refused"))
    if ui["trade_allowed"]:
        body += f'<p class="note"><strong>Quote status:</strong> {escape(ui["trade_note"])}</p>{_trade_form(ui)}{_estimates(ui)}'
    else:
        body += (f'<p class="blocked">Virtual trading is blocked. {escape(ui["trade_note"])}</p>'
                 '<p class="note">To trade without a provider, choose Synthetic Mode (Free) in the top bar.</p>')
    return head + body + "</section>"


# ---- account, risk, positions, activity ----------------------------------------------------------------------------------------------------
def _account_cards(ui):
    summary = ui["summary"]
    if summary is None:
        return ""
    marks = [row["pnl"] for row in ui["position_rows"]]
    if not marks:
        unreal = '<div class="v">&#8377;0.00</div><div class="s">no open positions</div>'
    elif any(m is None for m in marks):
        unreal = '<div class="v">unknown</div><div class="s">some open positions have no verified price</div>'
    else:
        total = sum(marks, Decimal("0"))
        unreal = f'<div class="v {_signed_class(total)}">{_signed(total)}</div><div class="s">marked at the bid, before fees</div>'
    realised = Decimal(summary["realized_pnl"])
    unreal_tone = _tone(total) if marks and not any(m is None for m in marks) else ""
    return ('<section class="cards" aria-label="Account">'
            f'<div class="card"><div class="k">Virtual cash</div><div class="v">{_rupee(summary["cash"])}</div><div class="s">starting {_rupee(summary["starting_balance"])} (example)</div></div>'
            f'<div class="card {_tone(realised)}"><div class="k">Realised P&amp;L</div><div class="v {_signed_class(realised)}">{_signed(realised)}</div><div class="s">after fees</div></div>'
            f'<div class="card {unreal_tone}"><div class="k">Unrealised P&amp;L</div>{unreal}</div>'
            f'<div class="card"><div class="k">Open positions</div><div class="v">{len(ui["position_rows"])}</div><div class="s">{summary["trades"]} simulated trade(s) in this account</div></div></section>')


def _risk_card(ui):
    r = ui["risk"]
    if r is None:
        return ""
    near = r.budget > 0 and r.committed >= r.budget * Decimal("0.8")
    if r.locked:
        status = "&#10007; LOCKED: no trades are accepted"
    elif r.limit_reached:
        status = "&#10007; LIMIT REACHED: no new risk"
    elif r.headroom <= 0:
        status = "&#9888; FULL: no room for new risk"
    elif near:
        status = "&#9888; OPEN: little room left"
    else:
        status = "&#10003; OPEN: room for new risk"
    cls = "full" if (r.limit_reached or r.headroom <= 0) else "hot" if near else ""
    budget = r.budget.quantize(CENT, ROUND_HALF_UP)
    pct = f"{(min(max(r.committed, Decimal('0')), r.budget) / r.budget * 100):.0f}" if r.budget > 0 else "0"
    value = min(max(r.committed, Decimal("0")), r.budget).quantize(CENT, ROUND_HALF_UP) if r.budget > 0 else Decimal("0.00")
    return ('<section class="panel" aria-labelledby="risk-h"><h2 id="risk-h">Daily risk budget</h2>'
            f'<div class="risk-row"><span>Committed worst case <strong>{_rupee(r.committed)}</strong> of {_rupee(r.budget)}</span><span class="risk-status">{status}</span></div>'
            f'<progress class="{cls}" max="{budget}" value="{value}" aria-label="Committed risk against the daily budget"></progress>'
            f'<div class="pct"><span>{pct}% of the daily budget committed</span><span>limit {_rupee(r.budget)}</span></div>'
            f'<div class="risk-row"><span>Remaining budget for new risk</span><span class="big">{_rupee(max(r.headroom, Decimal("0")))}</span></div>'
            f'<dl class="kv"><dt>Open-position exposure (most they could still lose)</dt><dd>{_rupee(r.remaining_max_loss)}</dd>'
            f'<dt>Estimated exit costs</dt><dd>{_rupee(r.exit_cost_estimate)}</dd>'
            f'<dt>Loss counted so far (estimate, not realised)</dt><dd>{_rupee(r.incurred)}</dd></dl>'
            '<p class="note">Counted so far = realised loss today + marked loss on open positions (losses only, valued at the bid less an exit-slippage allowance) + buy fees on open positions. It is not a realised loss.</p>'
            '<p class="note">A limit on new risk, not a guarantee: losses can exceed the budget. See Risk details below.</p></section>')


def _unpriced_alert(ui):
    """Open positions that have no verified price right now: said plainly, with the recovery steps and the promise that nothing is priced, filled or closed for them."""
    rows = ui["unpriced"]
    if not rows:
        return ""
    names = ", ".join(escape(r["symbol"]) for r in rows)
    return ('<section class="panel alert" role="alert" aria-labelledby="unpriced-h">'
            f'<h2 id="unpriced-h">&#9888; UNPRICED POSITION: {len(rows)} open position{"s" if len(rows) != 1 else ""} without a verified price</h2>'
            f'<p><strong>{names}</strong>. Data state: <strong>{escape(ui["data_state_text"])}</strong> &mdash; {escape(ui["data_reason"])}. Their profit or loss is unknown. '
            'Nothing has been priced, filled or closed on their behalf, and this app never will: no price or exit is invented.</p>'
            '<p class="note">The risk budget still counts their whole premium, and new orders (entries and exits) stay blocked while no verified price exists.</p>'
            '<h3 class="mini">Recovery</h3><ol class="steps">'
            '<li>Leave the positions as they are. Nothing is closed automatically.</li>'
            '<li>Check the data status at the top. For stale or unavailable data, wait for a fresh answer and reload this page; provider data is requested at most every 5 seconds.</li>'
            '<li>Each mode has its own account: switching modes does not move positions. If Real-Time Mode stays unavailable, see Details &rsaquo; Provider connection and setup; unverified provider data can never price a position for trading.</li>'
            '<li>When a fresh, verified price returns, the row re-prices by itself and a SELL can be made at that quote, never at an older one.</li>'
            '<li>If verified data cannot return, the positions stay open and unpriced in the ledger. There is deliberately no &ldquo;close without a price&rdquo; action. '
            'Note the incident yourself; the full procedure is in docs/STALE_DATA_RECOVERY.md.</li></ol></section>')


def _positions(ui):
    rows = ""
    for p in ui["position_rows"]:
        pnl = "<strong>UNPRICED</strong> &mdash; P&amp;L unknown (price not verified)" if p["pnl"] is None else f'<span class="{_signed_class(p["pnl"])}">{_signed(p["pnl"])}</span>'
        bid = "-" if p["bid"] is None else escape(_money(p["bid"]))
        rows += f'<tr><td class="l">{escape(p["symbol"])}</td><td>{p["quantity"]}</td><td>{p["lots"]}</td><td>{escape(_money(p["average"]))}</td><td>{bid}</td><td>{pnl}</td></tr>'
    rows = rows or '<tr><td class="empty" colspan="6">No open positions yet. Pick an instrument and press Simulate virtual trade.</td></tr>'
    return ('<section class="panel" aria-labelledby="pos-h"><h2 id="pos-h">Open virtual positions</h2><div class="scroll"><table><caption class="sr">Open positions</caption>'
            '<thead><tr><th class="l">Instrument</th><th>Qty (units)</th><th>Lots</th><th>Avg fill</th><th>Current bid</th><th>Open P&amp;L (before fees)</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div></section>")


def _activity(ui):
    if ui["summary"] is None:
        return ""
    ledger = ui["ledger"]
    recent = "".join(f'<tr><td>{r["seq"]}</td><td class="l">{escape(_short_time_text(r["engine_time"]))}</td><td class="l">{escape(r["side"])}</td><td class="l">{escape(r["symbol"])}</td>'
                     f'<td>{r["quantity"]}</td><td>{escape(r["fill_price"])}</td><td class="l">{escape(r["data_origin"])}</td></tr>' for r in reversed(ledger[-8:]))
    recent = recent or '<tr><td class="empty" colspan="7">No activity yet: no virtual trades yet.</td></tr>'
    full = "".join(
        f"<tr><td>{r['seq']}</td><td class=\"l\">{escape(r['side'])}</td><td class=\"l\">{escape(r['symbol'])}</td><td>{r['quantity']}</td><td>{escape(r['quote_bid'])} / {escape(r['quote_ask'])}</td>"
        f"<td>{escape(r['fill_price'])}</td><td>{escape(r['cost'])}</td><td class=\"l\">{escape(r['data_origin'])} / {escape(r['provider'])}</td><td class=\"l\">{escape(r['data_state'])}</td></tr>"
        for r in ledger) or '<tr><td class="l" colspan="9">no virtual trades yet</td></tr>'
    return ('<section class="panel" aria-labelledby="act-h"><h2 id="act-h">Recent activity</h2><div class="scroll"><table><caption class="sr">Recent simulated trades, newest first</caption>'
            '<thead><tr><th>#</th><th class="l">Time</th><th class="l">Side</th><th class="l">Instrument</th><th>Qty (units)</th><th>Simulated fill</th><th class="l">Source</th></tr></thead>'
            f"<tbody>{recent}</tbody></table></div>"
            '<details><summary>Simulated trade ledger (latest 20; full list at /api/ledger)</summary><div class="scroll"><table><caption class="sr">Detailed ledger</caption>'
            '<thead><tr><th>#</th><th class="l">Side</th><th class="l">Instrument</th><th>Qty (units)</th><th>Quote bid / ask</th><th>Simulated fill</th><th>Cost</th>'
            '<th class="l">Data origin / source</th><th class="l">Data state</th></tr></thead>'
            f"<tbody>{full}</tbody></table></div>"
            f'<p class="note">Every trade is virtual; no real order exists or can be sent. A quote is what the market showed; a fill is simulated at the touch plus {ui["slippage_ticks"]} tick(s) of slippage.</p></details></section>')


def _paper(account):
    positions = "none" if not account.positions else f"{len(account.positions)}"
    return ('<section class="panel" aria-labelledby="paper-h"><h2 id="paper-h">Paper-trading summary</h2><dl class="kv">'
            f"<dt>Starting paper balance</dt><dd>&#8377;{escape(_money(account.starting_balance))} (configurable example)</dd>"
            f"<dt>Simulated positions</dt><dd>{escape(positions)}</dd>"
            f"<dt>Simulated P&amp;L</dt><dd>&#8377;{escape(_money(account.pnl))}</dd>"
            f"<dt>Orders executed</dt><dd>{int(account.orders_executed)}</dd></dl>"
            '<p class="note">No trade or fill has occurred. This page is read-only and has no order entry.</p></section>')


_CSS_WORKSPACE = """
.work{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(300px,1fr);gap:22px;align-items:start}
.work>div{display:grid;gap:22px;min-width:0}
@media (max-width:980px){.work{grid-template-columns:minmax(0,1fr)}}
.chart-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px 14px;margin-bottom:8px}
.chart-head h3{margin:0;font-size:1rem;font-weight:700}
.badge{display:inline-flex;align-items:center;border:1px solid #6b4d12;background:#33250a;color:var(--bannerfg);border-radius:6px;padding:2px 9px;font-size:.68rem;font-weight:800;letter-spacing:.1em}
.winbar{display:inline-flex;border:1px solid var(--line2);border-radius:8px;overflow:hidden;margin-left:auto}
.winbar button{font:inherit;font-size:.78rem;font-weight:700;padding:5px 12px;border:0;background:transparent;color:var(--muted);cursor:pointer}
.winbar button+button{border-left:1px solid var(--line2)}
.winbar button:hover{background:var(--panel3);color:var(--fg)}
.winbar button[aria-current=true]{background:var(--accent);color:#03170f}
.chartbox{background:var(--bg2);border:1px solid var(--line);border-radius:10px;padding:6px}
svg#chart-svg{display:block;width:100%;touch-action:pan-y;cursor:crosshair}
svg#chart-svg .grid{stroke:var(--line);stroke-width:1}
svg#chart-svg .axis{fill:var(--dim);font-size:11px}
svg#chart-svg .wick.up{stroke:var(--pos)}svg#chart-svg .wick.dn{stroke:var(--neg)}
svg#chart-svg .body.up{fill:var(--pos)}svg#chart-svg .body.dn{fill:var(--neg)}
svg#chart-svg .cl{stroke:var(--accent2);stroke-width:1;stroke-dasharray:4 3}
svg#chart-svg:focus-visible{outline:2px solid var(--accent2);outline-offset:2px}
.readout{margin:8px 0 0;font-size:.82rem;color:var(--fg);min-height:2.6em}
.chart-note{color:var(--muted);font-size:.78rem;margin:6px 0 0}
.nochart{border:1px dashed var(--line2);border-radius:10px;padding:16px;color:var(--muted)}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-top:4px}
.fact{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:10px 12px;min-width:0}
.fact .k{color:var(--muted);font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em}
.fact .v{font-size:1.08rem;font-weight:700;margin-top:2px}
.fact .h{color:var(--dim);font-size:.74rem;margin-top:3px}
.quote-box{border-left:3px solid var(--accent);padding-left:12px}
.watch{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px}
.wl-item{display:flex;justify-content:space-between;gap:8px;width:100%;text-align:left;font:inherit;font-size:.86rem;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--panel2);color:var(--fg);cursor:pointer}
.wl-item:hover{background:var(--panel3)}
.wl-item[aria-current=true]{border-color:var(--accent);background:#10233a}
.wl-item small{color:var(--muted);display:block}
.needs-js{display:none}.js .needs-js{display:block}.js .winbar.needs-js{display:inline-flex}
#order-summary{border:1px solid var(--line);border-radius:10px;background:var(--bg2);padding:10px 12px;display:grid;gap:4px;font-size:.88rem}
.sum-row{display:flex;justify-content:space-between;gap:12px}.sum-row span{color:var(--muted)}.sum-row.total{border-top:1px solid var(--line);padding-top:6px;margin-top:2px}.sum-row.total strong{font-size:1.05rem}
.sum-good{color:var(--pos);font-weight:700;margin:4px 0 0}.sum-bad{color:var(--neg);font-weight:700;margin:4px 0 0}
.hint{color:var(--dim);font-size:.76rem;margin:2px 0 0}
.panel.alert{border-color:var(--warn);background:linear-gradient(180deg,#2a2208,var(--bg2))}.panel.alert h2{color:var(--warn)}h3.mini{margin:10px 0 0;font-size:.78rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.strip{margin:0;padding:9px 14px;border:1px solid var(--line);border-radius:10px;background:var(--panel);color:var(--muted);font-size:.82rem}.strip strong{color:var(--fg)}
"""


def _fmt_money(text):
    whole, _, frac = str(text).partition(".")
    return f"&#8377;{int(whole):,}" + (f".{frac}" if frac else "")


def _chart_panel(w):
    d = w["details"][w["default"]]
    current = ' aria-current="true"'
    buttons = "".join(f'<button type="button" class="win" data-window="{escape(label)}"{current if label == "1h" else ""}>{escape(label)}</button>' for label in w["windows"])
    return ('<section class="panel" aria-labelledby="chart-h"><h2 id="chart-h">Price chart</h2>'
            f'<div class="chart-head"><h3 id="chart-title">{escape(d["symbol"])} &mdash; {escape(d["title"])}</h3><span class="badge">SIMULATED HISTORY</span>'
            f'<span class="winbar needs-js" role="group" aria-label="Chart time window">{buttons}</span></div>'
            '<div class="chartbox"><svg id="chart-svg" viewBox="0 0 760 320" role="img" tabindex="0" '
            'aria-label="Simulated history candlestick chart. Use the left and right arrow keys to move the crosshair, Home and End to jump."></svg>'
            '<noscript><p class="chart-note">The chart needs JavaScript. It is a local script: nothing is loaded from the internet.</p></noscript></div>'
            '<p class="readout" id="chart-readout" aria-live="polite">Move over the chart, or focus it and use the arrow keys, to read a candle.</p>'
            '<p class="chart-note">SIMULATED HISTORY: made up for visualisation from a fixed seed, so it is the same on every refresh. It is not market history, not a quote and is '
            "never used by the paper engine. It is not a forecast.</p></section>")


def _contract_panel(w):
    d, desc = w["details"][w["default"]], w["descriptions"]
    return ('<section class="panel" aria-labelledby="contract-h"><h2 id="contract-h">Selected contract</h2><div class="facts">'
            f'<div class="fact"><div class="k">Type</div><div class="v" id="d-kind">{escape(d["kind_text"])}</div><div class="h">{escape(desc["kind"])}</div></div>'
            f'<div class="fact"><div class="k">Strike</div><div class="v" id="d-strike">{escape(d["strike_text"])}</div><div class="h">{escape(desc["strike"])}</div></div>'
            f'<div class="fact"><div class="k">Quantity</div><div class="v" id="d-lot">1 synthetic lot = {d["lot_size"]} simulator units</div><div class="h">{escape(desc["lot"])}</div></div></div>'
            '<div class="quote-box"><h3 class="sr">Latest simulated quote</h3><p class="hint">Latest simulated quote &mdash; separate from the chart history above.</p><div class="facts">'
            f'<div class="fact"><div class="k">Premium</div><div class="v" id="d-premium">{_fmt_money(d["premium"])}</div><div class="h">{escape(desc["premium"])}</div></div>'
            f'<div class="fact"><div class="k">Bid</div><div class="v" id="d-bid">{_fmt_money(d["bid"])}</div><div class="h">{escape(desc["bid"])}</div></div>'
            f'<div class="fact"><div class="k">Ask</div><div class="v" id="d-ask">{_fmt_money(d["ask"])}</div><div class="h">{escape(desc["ask"])}</div></div></div></div>'
            f'<p class="chart-note">{escape(w["not_real"])}</p></section>')


def _watchlist(w):
    items = "".join(f'<li><button type="button" class="wl-item" data-symbol="{escape(s)}"><span>{escape(w["details"][s]["kind"])} {escape(w["details"][s]["strike_text"])}'
                    f'<small>{escape(s)}</small></span><span>{_fmt_money(w["details"][s]["premium"])}</span></button></li>' for s in w["symbols"])
    return ('<section class="panel needs-js" aria-labelledby="wl-h"><h2 id="wl-h">Watchlist (synthetic)</h2>'
            f'<ul class="watch">{items}</ul><p class="hint">Press a contract to chart it and fill the order form.</p></section>')


def _no_chart(w):
    return f'<section class="panel" aria-labelledby="chart-h"><h2 id="chart-h">Price chart</h2><p class="nochart">{escape(w["reason"])}</p></section>'


def _static_summary(w):
    """The order summary a reader without scripts gets: BUY of one lot of the first contract (the script replaces it whenever the selection changes)."""
    order = w.get("order") if w else None
    if not order:
        return ""
    d, t = w["details"][w["default"]], order["symbols"][w["default"]]
    e = t["buy"]["1"]
    status = ("&#10003; Appears to fit the daily risk budget." if e["fits"] and e["exposure_ok"] and e["cash_ok"] and not t["new_position_blocked"]
              else f"&#10007; Over a limit (budget excess {_fmt_money(e['excess'])}): the engine would refuse this.")
    return ('<div id="order-summary"><div class="sum-row"><span>BUY 1 lot</span><strong>' + f'{e["units"]} units of {escape(d["title"])}</strong></div>'
            f'<div class="sum-row"><span>Estimated order value</span><strong>{_fmt_money(e["value"])}</strong></div>'
            f'<div class="sum-row"><span>Estimated fee</span><strong>{_fmt_money(e["fee"])}</strong></div>'
            f'<div class="sum-row"><span>Estimated exit fee</span><strong>{_fmt_money(e["exit_fee"])}</strong></div>'
            f'<div class="sum-row total"><span>Counts against the risk budget</span><strong>{_fmt_money(e["risk"])}</strong></div>'
            f'<div class="sum-row"><span>Remaining daily risk budget</span><strong>{_fmt_money(order["headroom"])}</strong></div><p class="hint">{status}</p></div>')


def _workspace_data(w):
    payload = {k: w[k] for k in ("symbols", "details", "history", "windows", "default", "order")}
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).replace("</", "<\\/")


def _work_area(ui, trading_panel):
    w = ui["workspace"]
    if not w["chart"]:
        return f'<div class="work"><div>{_no_chart(w)}</div><div>{trading_panel}</div></div>'
    data = f'<script type="application/json" id="workspace-data">{_workspace_data(w)}</script><script>{_JS}</script>'
    return (f'<div class="work"><div>{_chart_panel(w)}{_watchlist(w)}{_contract_panel(w)}</div><div>{trading_panel}</div></div>{data}')


def render_page(health, snapshot, account, now, mode="synthetic", ui=None):
    provider = _provider_mode(snapshot, mode)
    if ui:
        workspace = (f'{_work_area(ui, _trading_panel(ui))}<div class="cols"><div class="stack">{_account_cards(ui)}</div>{_risk_card(ui)}</div>'
                     f"{_unpriced_alert(ui)}{_positions(ui)}{_chain(health, snapshot, provider)}{_activity(ui)}")
        details = (f'<section aria-label="Details"><h2 class="sr">Details</h2>{_health_details(health, snapshot, provider)}{_provider_details(ui)}'
                   f"{_risk_details(ui)}{_account_details(ui)}</section>")
        body = f"{_notice(ui, ('mode',))}{_market(health, snapshot, provider, now, ui)}{workspace}{details}"
    else:
        body = f"{_market(health, snapshot, provider, now)}{_chain(health, snapshot, provider)}{_paper(account)}{_health_details(health, snapshot, provider)}"
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>NIFTY Paper Trading</title>"
            f"<style>{_CSS}{_CSS_WORKSPACE}</style></head><body>"
            f"<div class=\"banner\" role=\"alert\">{escape(banner_text(health, snapshot, mode))}</div>"
            f"{_top_bar(health, ui, provider)}<main>{body}"
            f"<footer class=\"foot\">Page generated {escape(_time(now))}. No predictions or trading signals are shown.</footer>"
            "</main></body></html>")


def snapshot_json(health, snapshot, account, now, mode="synthetic"):
    shown = health.state in VALUE_STATES
    provider = _provider_mode(snapshot, mode)
    synthetic = bool(shown and snapshot.synthetic is True)
    return {
        "banner": banner_text(health, snapshot, mode),
        "mode": "provider" if provider else "synthetic",
        "data_state": health.state.value,
        "reason": health.reason,
        "source": health.source,
        "as_of": health.as_of.isoformat() if health.as_of is not None else None,
        "received_at": health.received_at.isoformat() if health.received_at is not None else None,
        "provider_time_raw": health.provider_time_raw,
        "exchange_time": snapshot.provider_time.isoformat() if isinstance(snapshot, Snapshot) and snapshot.provider_time is not None else None,
        "exchange_time_status": "ESTABLISHED" if isinstance(snapshot, Snapshot) and snapshot.provider_time is not None else "NOT_ESTABLISHED",
        "evidence": snapshot.evidence if isinstance(snapshot, Snapshot) and shown else None,
        "time_semantics": snapshot.time_semantics if isinstance(snapshot, Snapshot) else None,
        "latency_class": snapshot.latency_class if isinstance(snapshot, Snapshot) else None,
        "notes": list(snapshot.notes) if isinstance(snapshot, Snapshot) else [],
        "generated_at": now.isoformat(),
        "synthetic": synthetic,                  # True only when synthetic values are present; values are withheld otherwise
        "market": {"index_value": str(snapshot.index_value), "pct_change": None if snapshot.pct_change is None else str(snapshot.pct_change),
                   "synthetic": snapshot.synthetic is True} if shown else None,
        "chain": [{"symbol": r.symbol, "kind": r.kind, "strike": r.strike, "price": str(r.price), "bid": str(r.bid), "ask": str(r.ask),
                   "synthetic": snapshot.synthetic is True} for r in snapshot.chain] if shown else [],
        "paper_account": {"starting_balance": str(account.starting_balance), "starting_balance_is_example": True,
                          "positions": len(account.positions), "pnl": str(account.pnl), "orders_executed": account.orders_executed},
    }
