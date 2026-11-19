"""Manual, read-only smoke test for the FYERS adapter. You run it yourself after setting the two environment variables; automated tests never do.

It makes ONE quotes request (and, if an option is requested, one download of the public symbol master). It prints states, reasons and prices,
never the credentials, the Authorization header or the request. Exit codes: 0 data was read, 1 no usable data, 2 bad arguments or no credentials,
3 the option symbol could not be resolved.
"""
import argparse
import sys
from datetime import date, datetime, timedelta, timezone

from ..dashboard.server import App
from ..dashboard.status import VALUE_STATES
from .fyers_http import TransportError, urllib_transport
from .fyers_provider import ConfigError, build_provider, credentials_summary, load_credentials, resolve_option_online, utc_now
from .fyers_symbols import SymbolError

IST = timezone(timedelta(hours=5, minutes=30))


def main(argv=None, *, env=None, transport=urllib_transport, clock=utc_now, out=print):
    parser = argparse.ArgumentParser(prog="fyers_smoke", description="Read-only FYERS smoke test (one quotes request).")
    parser.add_argument("--option-expiry", help="YYYY-MM-DD")
    parser.add_argument("--option-strike", type=int)
    parser.add_argument("--option-type", choices=("CE", "PE"))
    args = parser.parse_args(argv)
    given = [v is not None for v in (args.option_expiry, args.option_strike, args.option_type)]
    if any(given) and not all(given):
        out("error: --option-expiry, --option-strike and --option-type must be given together")
        return 2
    out(credentials_summary(env))
    try:
        load_credentials(env)
    except ConfigError:
        return 2
    option = None
    if all(given):
        try:
            option = resolve_option_online(date.fromisoformat(args.option_expiry), args.option_strike, args.option_type,
                                           clock().astimezone(IST).date(), transport)
        except ValueError:
            out("error: --option-expiry must look like 2026-11-03")
            return 2
        except (SymbolError, TransportError) as error:
            out(f"option symbol could not be resolved ({error.code}); nothing was guessed")
            return 3
        out(f"option resolved from the official symbol master: {option.symbol} (lot {option.lot_size}, tick {option.tick_size})")
    provider = build_provider(option, env=env, transport=transport, clock=clock)
    health, snapshot, _ = App(provider, clock=clock, mode="provider").view()
    out(f"state: {health.state.value}")
    out(f"reason: {health.reason}")
    if snapshot is not None:
        out(f"evidence: {snapshot.evidence}   (PROVIDER_RESPONSE = the real HTTPS client; FIXTURE_TRANSPORT = a test transport, not a provider response)")
        out(f"received (local clock): {health.received_at.isoformat() if health.received_at else '-'}")
        out(f"provider time field, raw: {health.provider_time_raw!r}   (meaning not established; not used as quote time)")
        out(f"exchange time: {snapshot.provider_time.isoformat() if snapshot.provider_time else 'NOT ESTABLISHED (no documented exchange-time field is used)'}")
        out(f"NIFTY index last price: {snapshot.index_value}   change %: {snapshot.pct_change}")
        for row in snapshot.chain:
            out(f"option {row.symbol}: last {row.price}  bid {row.bid}  ask {row.ask}")
        for note in snapshot.notes:
            out(f"note: {note}")
    out("Reminder: a successful response does not prove the price is live; UNVERIFIED is the expected result.")
    return 0 if health.state in VALUE_STATES else 1


if __name__ == "__main__":
    sys.exit(main())
