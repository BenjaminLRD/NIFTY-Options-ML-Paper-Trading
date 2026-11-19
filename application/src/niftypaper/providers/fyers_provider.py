"""Read-only FYERS provider for the dashboard: NIFTY index quote and, optionally, one verified NIFTY option quote.

One quotes request per refresh (at most MIN_INTERVAL apart; failures are rate-limited the same way). The result is a Snapshot that is
ALWAYS marked time_semantics=UNKNOWN and latency_class=UNKNOWN, because FYERS's documentation does not establish that the feed is real-time
for this plan nor what the `tt` field means. So the dashboard can show it as UNVERIFIED (or STALE / UNAVAILABLE) but never LIVE.
`received_at` is our own clock when the response arrived; it does not say when the market observation was made.
A failure returns None with a short `last_error`; this module never produces synthetic data.

Credentials come only from the environment: FYERS_APP_ID and FYERS_ACCESS_TOKEN. They are held in an object whose repr is redacted.
"""
import os
import re
from datetime import datetime, timedelta, timezone

from ..dashboard.status import EVIDENCE_FIXTURE_TRANSPORT, EVIDENCE_PROVIDER_RESPONSE, LATENCY_UNKNOWN, TIME_UNKNOWN, OptionRow, Snapshot
from .fyers_http import QuotesClient, TransportError, fetch_symbol_master, urllib_transport
from .fyers_quotes import DESCRIPTIONS, INDEX_SYMBOL, QuoteError, QuoteSpec, parse_quotes
from .fyers_symbols import resolve_nifty_option

ENV_APP_ID = "FYERS_APP_ID"
ENV_ACCESS = "FYERS_ACCESS_TOKEN"
SOURCE = "FYERS_REST_QUOTES"
MIN_INTERVAL = timedelta(seconds=5)       # free Standard plan: 5 requests/s, 50/min, 5,000/day (Plans & Limits page); this keeps well under
BACKOFF = timedelta(seconds=60)
TIME_NOTE = ("The provider time field `tt` is documented only as 'today's time'. It is shown exactly as received and is not used as a quote time "
             "or to judge freshness.")
_APP_ID = re.compile(r"[A-Za-z0-9_-]{3,40}")
_SECRET = re.compile(r"[A-Za-z0-9._~+/=-]{20,4096}")


class ConfigError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Credentials:
    __slots__ = ("_app_id", "_secret")

    def __init__(self, app_id, secret):
        if not (isinstance(app_id, str) and _APP_ID.fullmatch(app_id) and isinstance(secret, str) and _SECRET.fullmatch(secret)):
            raise ConfigError("CREDENTIALS_INVALID")      # the values are deliberately not included
        self._app_id, self._secret = app_id, secret

    def header_value(self):
        return f"{self._app_id}:{self._secret}"

    def __repr__(self):
        return "Credentials(<redacted>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("credentials cannot be pickled")


def load_credentials(env=None):
    env = os.environ if env is None else env
    app_id, secret = env.get(ENV_APP_ID), env.get(ENV_ACCESS)
    if not app_id or not secret:
        raise ConfigError("CREDENTIALS_MISSING")
    return Credentials(app_id.strip(), secret.strip())


def utc_now():
    return datetime.now(timezone.utc)


class FyersProvider:
    origin = "FYERS"

    def __init__(self, client, *, option=None, clock=utc_now, min_interval=MIN_INTERVAL, backoff=BACKOFF, config_error=None, error_detail=None,
                 evidence=EVIDENCE_FIXTURE_TRANSPORT):
        self._client, self._option, self._clock = client, option, clock
        self._evidence = evidence                 # PROVIDER_RESPONSE only when build_provider was given the real HTTPS transport; anything else is a fixture
        self._min_interval, self._backoff = min_interval, backoff
        self._cached = None
        self._next_attempt = None
        self.last_error = DESCRIPTIONS[config_error] + (f" ({error_detail})" if error_detail else "") if config_error else None
        self._config_error = config_error

    def __call__(self, now=None):
        if self._config_error:
            return None
        started = self._clock()
        if self._next_attempt is not None and started < self._next_attempt:
            return self._cached                       # cached success or failure; no new request inside the interval
        self._next_attempt = started + self._min_interval
        try:
            self._cached = self._fetch()
            self.last_error = None
        except (QuoteError, TransportError) as error:
            self._fail(error.code, started)
        except Exception:                             # nothing from the exception is kept: it could carry request details
            self._fail("INTERNAL_ERROR", started)
        return self._cached

    def _fail(self, code, started):
        self._cached = None
        self.last_error = DESCRIPTIONS.get(code, DESCRIPTIONS["API_ERROR"])
        if code == "RATE_LIMITED":
            self._next_attempt = started + self._backoff

    def _fetch(self):
        specs = {INDEX_SYMBOL: QuoteSpec(False)}
        if self._option is not None:
            specs[self._option.symbol] = QuoteSpec(True, self._option.tick_size)
        payload = self._client.get_quotes(tuple(specs))
        received = self._clock()
        results = parse_quotes(payload, specs)
        index = results[INDEX_SYMBOL]
        if index.quote is None:
            raise QuoteError(index.error)
        notes = [TIME_NOTE]
        chain = ()
        if self._option is not None:
            option = results[self._option.symbol]
            if option.quote is None:
                notes.append(f"Option quote for {self._option.symbol} was rejected: {DESCRIPTIONS.get(option.error, option.error)}.")
            else:
                q = option.quote
                chain = (OptionRow(self._option.symbol, self._option.option_type, self._option.strike, q.last_price, q.bid, q.ask,
                                   lot_size=self._option.lot_size, tick=self._option.tick_size),)
        return Snapshot(source=SOURCE, synthetic=False, as_of=received, index_value=index.quote.last_price, pct_change=index.quote.pct_change,
                        chain=chain, received_at=received, provider_time_raw=index.quote.provider_time_raw, provider_time=None,
                        time_semantics=TIME_UNKNOWN, latency_class=LATENCY_UNKNOWN, notes=tuple(notes), evidence=self._evidence)


def build_provider(option=None, *, env=None, transport=urllib_transport, clock=utc_now, min_interval=MIN_INTERVAL):
    """A provider that reads credentials from the environment. Missing or malformed credentials give a provider that always reports UNAVAILABLE."""
    try:
        credentials = load_credentials(env)
    except ConfigError as error:
        return FyersProvider(None, option=option, clock=clock, config_error=error.code)
    evidence = EVIDENCE_PROVIDER_RESPONSE if transport is urllib_transport else EVIDENCE_FIXTURE_TRANSPORT
    return FyersProvider(QuotesClient(credentials, transport), option=option, clock=clock, min_interval=min_interval, evidence=evidence)


def credentials_summary(env=None):
    """One line for startup messages: whether credentials look usable. Never shows a value."""
    try:
        load_credentials(env)
        return "credentials: found in the environment (values not shown)"
    except ConfigError as error:
        return f"credentials: {DESCRIPTIONS[error.code]}"


def resolve_option_online(expiry, strike, option_type, today, transport=urllib_transport):
    """Download the public symbol master and resolve one NIFTY option; raises SymbolError / TransportError, never guesses."""
    return resolve_nifty_option(fetch_symbol_master(transport), expiry=expiry, strike=strike, option_type=option_type, today=today)
