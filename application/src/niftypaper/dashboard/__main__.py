"""Command line entry: `python run_dashboard.py` (or `python -m niftypaper.dashboard` with PYTHONPATH=src). Serves on 127.0.0.1 only.

The dashboard starts in Synthetic Mode (free, offline) unless `--mode realtime` is given; the mode can be switched on the page.
"""
import argparse
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..assembly import default_factories
from ..paper.engine import ORIGIN_PROVIDER, ORIGIN_SYNTHETIC, PaperEngine
from ..paper.store import JsonlStore
from ..providers.permissions import DEFAULT_PATH, load_permissions
from ..providers.registry import real_provider_ids
from .sample_data import SCENARIOS
from .server import DEFAULT_HOST, DEFAULT_PORT, App, create_server
from .status import PaperAccount


# Anchored to the repository, not to the current folder: starting the app from another folder must not silently begin a new, empty virtual account.
DEFAULT_STATE_DIR = Path(__file__).resolve().parents[3] / "data" / "paper_state"
DEFAULT_ML_DIR = Path(__file__).resolve().parents[3] / "data" / "ml_replay"      # folder of saved replays run1, run2, ... of invented fixtures (see run_ml_demo.py)


def build_parser():
    parser = argparse.ArgumentParser(prog="niftypaper.dashboard", description="Local dashboard (127.0.0.1 only).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--mode", choices=("synthetic", "realtime"), default="synthetic",
                        help="starting mode; switchable on the page. realtime needs provider access (see the page or README)")
    parser.add_argument("--realtime-provider", choices=real_provider_ids(), default=real_provider_ids()[0], help="provider used when Real-Time Mode is chosen")
    parser.add_argument("--scenario", choices=SCENARIOS, default="simulated", help="synthetic demo state: simulated, stale or unavailable")
    parser.add_argument("--option-expiry", help="YYYY-MM-DD, with --option-strike and --option-type: one NIFTY option for Real-Time Mode (resolved only when that mode is chosen)")
    parser.add_argument("--option-strike", type=int)
    parser.add_argument("--option-type", choices=("CE", "PE"))
    parser.add_argument("--starting-balance", default="1000000", help="example virtual balance in rupees")
    parser.add_argument("--permissions-file", default=str(DEFAULT_PATH), help="local record of provider terms permissions")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR),
                        help="where each mode's trades are saved so the daily risk budget survives a restart (default: data/paper_state inside this repository, "
                             "whatever the current folder is; delete a file yourself to start a new virtual account)")
    parser.add_argument("--ml-dir", default=str(DEFAULT_ML_DIR), help="folder of a saved ML replay shown on /ml (invented data; created by run_ml_demo.py)")
    return parser


def _fail(message, code):
    print(f"error: {message}", file=sys.stderr)
    return code


def _engine_for(key, balance, state_dir):
    """One virtual account per data source: its own file, its own cash, positions, risk budget and ledger, and it only accepts that source's prices."""
    if key != "synthetic" and key not in real_provider_ids():
        raise ValueError(f"no virtual account for {key!r}")
    return PaperEngine(balance, origin=ORIGIN_SYNTHETIC if key == "synthetic" else ORIGIN_PROVIDER,
                       store=JsonlStore(state_dir / f"paper_{key}.jsonl", balance, account=key))


def _build_app(args, balance):
    option_args = (args.option_expiry, args.option_strike, args.option_type)
    option_request = None
    if any(v is not None for v in option_args):
        if any(v is None for v in option_args):
            raise ValueError("--option-expiry, --option-strike and --option-type must be given together")
        option_request = (date.fromisoformat(args.option_expiry), args.option_strike, args.option_type)
    factories = default_factories(scenario=args.scenario, option_request=option_request)
    permissions, permission_note = load_permissions(Path(args.permissions_file))
    provider_id = "synthetic" if args.mode == "synthetic" else args.realtime_provider
    app = App(factories[provider_id](), PaperAccount(starting_balance=balance), mode="synthetic" if args.mode == "synthetic" else "provider",
              provider_id=provider_id, factories=factories, engine_factory=lambda key: _engine_for(key, balance, Path(args.state_dir)), permissions=permissions,
              ml_dir=Path(args.ml_dir))
    return app, permission_note


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        balance = Decimal(args.starting_balance)
        if not balance.is_finite() or balance < 0:
            raise InvalidOperation
    except InvalidOperation:
        return _fail("--starting-balance must be a non-negative number", 2)
    try:
        app, permission_note = _build_app(args, balance)
    except ValueError as exc:
        return _fail(str(exc), 2)
    try:
        server = create_server(app, DEFAULT_HOST, args.port)
    except OSError as exc:
        return _fail(f"cannot listen on {DEFAULT_HOST}:{args.port}: {exc}", 1)
    print(f"Starting mode: {args.mode}. Permissions: {permission_note}.", flush=True)
    print(f"Virtual accounts are saved in: {Path(args.state_dir).resolve()}", flush=True)
    print("SIMULATED DATA - NOT LIVE MARKET DATA" if args.mode == "synthetic" else "Provider data: timing is not verified; not confirmed live.", flush=True)
    print(f"Open http://{DEFAULT_HOST}:{server.server_address[1]}/   (stop with Ctrl+C)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()
        print("dashboard stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
