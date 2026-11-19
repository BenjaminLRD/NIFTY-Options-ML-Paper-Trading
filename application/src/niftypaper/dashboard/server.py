"""Local HTTP server for the dashboard. Loopback only; no outbound connections.

GET and HEAD show the page and JSON. When the app is interactive it also accepts exactly two POSTs, both protected by a per-run form key, a Host check and
an Origin check: /mode (choose Synthetic or Real-Time mode) and /paper/trade (a VIRTUAL trade in the engine). There is no real-order endpoint.
This is the only module in the package allowed to import `http` (tests/test_structure.py enforces that).
"""
import hmac
import json
import os
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from ..paper.engine import TradeRequest
from .ml_page import load_replay, render_ml_page
from .render import SCRIPT_CSP_HASH, render_page, snapshot_json
from .status import DataState, PaperAccount, assess
from .trading import build_ui, build_views, position_status

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MAX_AGE = timedelta(seconds=30)
_LOOPBACK = {"127.0.0.1": "127.0.0.1", "localhost": "127.0.0.1"}
POST_PATHS = ("/mode", "/paper/trade")
MAX_FORM_BYTES = 2048
_PAIR = re.compile(rb"([A-Za-z_]{1,20})=([A-Za-z0-9_.:+%\-]{0,200})")


def utc_now():
    return datetime.now(timezone.utc)


def parse_form(raw):
    """Strict application/x-www-form-urlencoded parser: short ASCII keys and values, no repeated keys. Raises ValueError otherwise."""
    form = {}
    for part in raw.split(b"&"):
        match = _PAIR.fullmatch(part)
        if not match or match.group(1).decode() in form:
            raise ValueError("bad form")
        value = re.sub(rb"%([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), match.group(2).replace(b"+", b" "))
        form[match.group(1).decode()] = value.decode("ascii")
    return form


def _risk_json(report):
    out = {name: (str(value) if not isinstance(value, (bool, type(None))) else value) for name, value in report.__dict__.items()}
    out["note"] = "limits new risk only; it is not a guarantee that realised loss stays below the budget"
    return out


class _Unavailable:
    """Stands in for a provider that could not be started: always no data, with a fixed reason. Never synthetic data."""

    def __init__(self, reason):
        self.last_error = reason

    def __call__(self, now=None):
        return None


class App:
    def __init__(self, provider, account=None, max_age=DEFAULT_MAX_AGE, clock=utc_now, mode="synthetic", *, provider_id=None, factories=None,
                 engine_factory=None, permissions=None, env=None, ml_dir=None):
        if mode not in ("synthetic", "provider"):
            raise ValueError("mode must be 'synthetic' or 'provider'")
        self.mode = mode
        self.provider = provider
        self.provider_id = provider_id or ("synthetic" if mode == "synthetic" else "unknown")
        self.factories = factories              # {provider id: callable returning a provider}; None: mode switching is off
        self.engine_factory = engine_factory    # callable(account key) -> PaperEngine; None: virtual trading is off
        self._engines = {}
        self.permissions = permissions or {}    # {provider id: (permitted, note)}
        self.env = env
        self._open_nonces = []                 # one-time form_ids of order forms that were shown and not yet submitted (see issue_nonce)
        self.ml_dir = ml_dir                    # folder of a saved ML replay (invented data) for the /ml page; None: the page says there is none
        self.account = account if account is not None else PaperAccount()
        self.max_age = max_age
        self.clock = clock
        self.csrf = os.urandom(24).hex()
        self.notice = None
        self.notice_code = None     # the engine's refusal code for a refused trade, so the page can say which kind of rule refused it
        self.notice_kind = None     # "mode", "trade_ok" or "trade_refused": where and how the page shows the one-time notice

    @property
    def interactive(self):
        return self.factories is not None or self.engine_factory is not None

    @property
    def account_key(self):
        """Which virtual account the current data belongs to: "synthetic", or the provider's id. Accounts never share cash, positions, risk or a ledger."""
        return "synthetic" if self.mode == "synthetic" else (self.provider_id or "unknown")

    @property
    def engine(self):
        if self.engine_factory is None:
            return None
        key = self.account_key
        if key not in self._engines:
            self._engines[key] = self.engine_factory(key)
        return self._engines[key]

    def issue_nonce(self):
        """A one-time id for one rendering of the order form. Submitting the same form twice (double click, back button, resubmit) is then recognised and not executed twice."""
        form_id = os.urandom(12).hex()
        self._open_nonces.append(form_id)
        del self._open_nonces[:-256]
        return form_id

    def take_nonce(self, form_id):
        if form_id in self._open_nonces:
            self._open_nonces.remove(form_id)
            return True
        return False

    def close(self):
        """Release every account's single-writer lock (also released by the system if the process dies)."""
        for engine in self._engines.values():
            engine.close()

    def permission(self):
        if self.mode == "synthetic":
            return True, "not needed: synthetic data"
        return self.permissions.get(self.provider_id, (False, "no permission recorded for this provider"))

    def view(self):
        """(health, snapshot, now). A failing provider means UNAVAILABLE; there is no fallback to other data."""
        now = self.clock()
        try:
            snapshot = self.provider(now)
        except Exception:                      # the message is dropped on purpose: it could contain request details
            snapshot = None
        if snapshot is not None and self.mode == "provider" and getattr(snapshot, "synthetic", None) is not False:
            # structural guard against a silent fallback: a provider-mode dashboard never displays synthetic or unclassified data
            health = assess(None, now, self.max_age)
            return replace(health, reason="refused: provider mode received synthetic or unclassified data"), None, now
        health = assess(snapshot, now, self.max_age)
        reason = getattr(self.provider, "last_error", None)
        if snapshot is None and health.state is DataState.UNAVAILABLE and isinstance(reason, str) and reason:
            health = replace(health, reason=reason)
        return health, snapshot, now

    def switch(self, ui_mode, provider_id=None):
        """Choose Synthetic Mode or Real-Time Mode. Only an explicit call changes the mode; a failing provider never causes a switch."""
        if self.factories is None:
            return "Mode switching is not configured for this session."
        if ui_mode == "synthetic":
            key, mode = "synthetic", "synthetic"
        elif ui_mode == "realtime" and provider_id in self.factories and provider_id != "synthetic":
            key, mode = provider_id, "provider"
        else:
            return "That mode or provider is not supported."
        try:
            provider = self.factories[key]()
        except Exception:
            provider = _Unavailable("the data provider could not be started")
        self.provider, self.mode, self.provider_id = provider, mode, key
        return ("Switched to Synthetic Mode: simulated data, no provider connection." if mode == "synthetic"
                else f"Switched to Real-Time Mode with {key}. The status below shows whether the data can be trusted.")

    def handle_mode(self, form):
        self.notice, self.notice_kind = self.switch(form.get("mode"), form.get("provider")), "mode"

    def handle_trade(self, form):
        engine = self.engine
        nonce = form.get("nonce")
        if nonce is not None and not self.take_nonce(nonce):          # a form posted twice, or one that is out of date: do nothing, say so
            self.notice_kind, self.notice_code = "trade_refused", "DUPLICATE_SUBMISSION"
            self.notice = ("This order form was already submitted or is out of date, so nothing was done. Check the activity list below, then submit again from the reloaded page if you still want it.")
            return
        if engine is None:
            self.notice, self.notice_kind = "Virtual trading is not available in this session.", "trade_refused"
            return
        try:
            lots = int(form.get("lots", ""))
        except ValueError:
            lots = 0
        health, snapshot, _ = self.view()
        views = build_views(health, snapshot, self.permission()[0])
        result = engine.simulate(TradeRequest(form.get("symbol", ""), form.get("side", ""), lots), views.get(form.get("symbol")), marks=views)
        self.notice_kind, self.notice_code = ("trade_ok" if result.accepted else "trade_refused"), (None if result.accepted else result.code)
        self.notice = result.message if not result.accepted else (
            f"{result.message} {result.record.side} {result.record.quantity} {result.record.symbol} at {result.record.fill_price} "
            f"(quote {result.record.quote_bid} / {result.record.quote_ask}).")


class Handler(BaseHTTPRequestHandler):
    server_version = "niftypaper-dashboard"
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def _origins(self):
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _host_ok(self):
        # refuse any call whose Host header is not our own loopback address (guards against DNS rebinding from a web page)
        return self.headers.get("Host", "") in self._origins()

    def _send(self, status, body, content_type, head_only=False, synthetic="none", extra=None):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", f"default-src 'none'; style-src 'unsafe-inline'; script-src {SCRIPT_CSP_HASH}; base-uri 'none'; form-action 'self'")
        self.send_header("X-Data-Synthetic", synthetic)       # "true" synthetic values, "false" provider values, "none" no values
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def _get(self, head_only):
        if not self._host_ok():
            return self._send(403, "forbidden host\n", "text/plain; charset=utf-8", head_only)
        path, _, query = self.path.partition("?")
        if path not in ("/", "/api/snapshot", "/api/ledger", "/ml", "/api/ml"):
            return self._send(404, "not found\n", "text/plain; charset=utf-8", head_only)
        app = self.server.app
        run = re.search(r"(?:^|&)run=([A-Za-z0-9_]{1,12})(?:&|$)", query)
        run_id = run.group(1) if run else None
        if path == "/ml":
            status, page = render_ml_page(app.ml_dir, run_id)
            return self._send(status, page, "text/html; charset=utf-8", head_only, "true")
        if path == "/api/ml":
            summary, _, problem = load_replay(app.ml_dir, run_id)
            body = json.dumps(summary if summary is not None else {"replay": None, "problem": problem}, indent=2, sort_keys=True) + "\n"
            return self._send(404 if problem and problem.startswith("there is no replay") else 200, body, "application/json; charset=utf-8", head_only, "true")
        health, snapshot, now = app.view()
        synthetic = "true" if getattr(snapshot, "synthetic", None) is True else "false" if snapshot is not None else "none"
        if path == "/api/ledger":
            engine = app.engine
            risk = _risk_json(engine.risk_report(build_views(health, snapshot, app.permission()[0]))) if engine else None
            views = build_views(health, snapshot, app.permission()[0])
            positions = position_status(engine, views) if engine else []
            body = json.dumps({"mode": app.mode, "simulated": True, "data_state": health.state.value, "data_reason": health.reason, "risk": risk,
                               "positions": positions, "unpriced_positions": sum(1 for p in positions if not p["priced"]),
                               "trades": [r.to_dict() for r in (engine.ledger if engine else [])]}, indent=2) + "\n"
            return self._send(200, body, "application/json; charset=utf-8", head_only, synthetic)
        if path == "/":
            ui = build_ui(app, health, snapshot, now) if app.interactive and not head_only else None
            return self._send(200, render_page(health, snapshot, app.account, now, app.mode, ui), "text/html; charset=utf-8", head_only, synthetic)
        body = json.dumps(snapshot_json(health, snapshot, app.account, now, app.mode), indent=2) + "\n"
        return self._send(200, body, "application/json; charset=utf-8", head_only, synthetic)

    def _safe_get(self, head_only):
        try:
            self._get(head_only)
        except Exception:          # never leave the browser with an empty reply, and never put details (paths, values) into the page
            self._send(500, "internal error: this page could not be built. Nothing was changed; reload, or check the terminal where the dashboard runs.\n", "text/plain; charset=utf-8", head_only)

    def do_GET(self):
        self._safe_get(False)

    def do_HEAD(self):
        self._safe_get(True)

    def _read_body(self):
        """Read the request body (at most MAX_FORM_BYTES + 1 bytes) BEFORE answering. Answering and closing with unread data makes some
        systems reset the connection, so the client could miss the response."""
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return b"", None
        return self.rfile.read(min(max(size, 0), MAX_FORM_BYTES + 1)), size

    def do_POST(self):
        app = self.server.app
        path = self.path.split("?", 1)[0]
        raw, size = self._read_body()
        if not app.interactive or path not in POST_PATHS:
            return self._refuse(body_read=True)
        origin = self.headers.get("Origin")
        if not self._host_ok() or (origin is not None and origin not in {f"http://{h}" for h in self._origins()}):
            return self._send(403, "forbidden origin\n", "text/plain; charset=utf-8")
        try:
            if size is None or not 0 < size <= MAX_FORM_BYTES or not self.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
                raise ValueError
            form = parse_form(raw)
        except ValueError:
            return self._send(400, "bad request\n", "text/plain; charset=utf-8")
        if not hmac.compare_digest(form.get("csrf", "").encode(), app.csrf.encode()):
            return self._send(403, "forbidden: missing or wrong form key\n", "text/plain; charset=utf-8")
        (app.handle_mode if path == "/mode" else app.handle_trade)(form)
        self._send(303, "", "text/plain; charset=utf-8", extra={"Location": "/"})

    def _refuse(self, body_read=False):
        if not body_read:
            self._read_body()
        self._send(405, "read-only dashboard: GET and HEAD only\n", "text/plain; charset=utf-8")

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse


def create_server(app, host=DEFAULT_HOST, port=DEFAULT_PORT):
    """Bind to loopback only. Any other host (0.0.0.0, a LAN address, '') raises ValueError before anything is opened."""
    if host not in _LOOPBACK:
        raise ValueError(f"refusing to bind to {host!r}: the dashboard is local-only (use 127.0.0.1)")
    server = HTTPServer((_LOOPBACK[host], port), Handler)
    server.app = app
    return server
