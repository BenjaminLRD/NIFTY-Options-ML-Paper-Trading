"""Builds the selectable data sources. Nothing is created, read or contacted until the user chooses it: Synthetic Mode never touches the network
or the credential variables, and the FYERS provider (including its optional symbol-master download) is only built when Real-Time Mode with FYERS is chosen.
"""
from datetime import datetime, timedelta, timezone

from .dashboard.sample_data import SyntheticProvider
from .providers.fyers_http import TransportError, urllib_transport
from .providers.fyers_provider import FyersProvider, build_provider, resolve_option_online, utc_now
from .providers.fyers_symbols import SymbolError

IST = timezone(timedelta(hours=5, minutes=30))


def default_factories(*, scenario="simulated", option_request=None, env=None, transport=urllib_transport, clock=utc_now):
    """{provider id: factory}. option_request is (expiry date, strike, 'CE'|'PE') or None."""

    def synthetic():
        return SyntheticProvider(scenario)

    def fyers():
        option = None
        if option_request is not None:
            try:
                option = resolve_option_online(*option_request, datetime.now(IST).date(), transport)
            except (SymbolError, TransportError) as error:       # fail closed: no instrument is guessed
                return FyersProvider(None, clock=clock, config_error="OPTION_UNRESOLVED", error_detail=error.code)
        return build_provider(option, env=env, transport=transport, clock=clock)

    return {"synthetic": synthetic, "fyers": fyers}
