"""What the user is told about each selectable data source: cost, credentials, permissions, terms. Descriptions only: no network, no secrets.

To add a provider later: write its adapter (a callable `provider(now) -> Snapshot | None` with a `last_error`), add a ProviderInfo here, add a factory in
niftypaper.assembly, and record its terms permission in config/local/provider_permissions.json. The dashboard needs no other change.
"""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderInfo:
    id: str
    display_name: str
    needs_access: bool                 # True: needs an account, credentials and permissions; False: free, offline
    credential_env: tuple              # names of environment variables (never their values)
    subscription_note: str
    permission_note: str
    terms_note: str
    setup_steps: tuple


PROVIDERS = {
    "synthetic": ProviderInfo(
        "synthetic", "Synthetic generator", False, (),
        "Free. No account or subscription.", "None needed.",
        "Invented numbers made by this program. Not NSE quotes and not market conditions; for testing the application and the virtual-trading engine.",
        ("Choose Synthetic Mode. Nothing else is needed and no network request is made.",)),
    "fyers": ProviderInfo(
        "fyers", "FYERS (read-only quotes)", True, ("FYERS_APP_ID", "FYERS_ACCESS_TOKEN"),
        "Needs a FYERS account and a data-only API app. The free Standard plan allows market data with limits (5 requests/s, 5,000/day); paid Prime raises them. "
        "FYERS does not state that the free feed is real-time.",
        "The API app needs the Market Data permission. The access token expires every day at 6:30 AM.",
        "FYERS API Terms Section 11 says exchange data may not be used for paper trading unless FYERS allows it. Virtual trading with FYERS data stays "
        "disabled until FYERS's written permission is recorded in config/local/provider_permissions.json (see docs/provider_terms/).",
        ("Terms first: FYERS's written confirmation that covers paper trading must be recorded in docs/provider_terms/FYERS_TERMS_AND_CONFIRMATION.md. The program cannot check it.",
         "In the FYERS API dashboard create a non-trading (data-only) app with the Market Data permission and complete FYERS's own login to get today's access token. "
         "This program never does the login and never sees your app secret.",
         'In the PowerShell window that will run the dashboard, BEFORE starting it, set the two variables for that window only: $env:FYERS_APP_ID = "<your app id>" and '
         '$env:FYERS_ACCESS_TOKEN = "<current access token>". Repeat each day (the token expires at 6:30 AM). Never paste real values into files or chat.',
         "To allow virtual trading with FYERS data, copy config/provider_permissions.example.json to config/local/provider_permissions.json, set virtual_trading_permitted to true "
         "only if FYERS's written permission covers it, and fill in evidence and confirmed_on. The file is read when the dashboard starts, so create it before running python run_dashboard.py.",
         "Start the dashboard, then press Real-Time Mode and choose FYERS. Data shows as UNVERIFIED (timing not established), so trading stays blocked until FYERS documents real-time "
         "delivery and the meaning of its time field. Press Synthetic Mode (Free) at any time to return to simulated data.")),
}


def real_provider_ids():
    return tuple(i for i, info in PROVIDERS.items() if info.needs_access)


def credential_flags(info, env=None):
    """[(variable name, is it set)]: whether each variable has a value, never the value."""
    env = os.environ if env is None else env
    return [(name, bool(env.get(name))) for name in info.credential_env]
