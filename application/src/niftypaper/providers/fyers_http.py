"""The only module in this project that opens an outbound connection. GET only, HTTPS only, two allow-listed (host, path) pairs, no redirects.

Allowed: the FYERS quotes endpoint (needs the credentials header) and the public NSE_FO symbol master file (no credentials).
Nothing else can be requested: the client has no way to name any other endpoint.
Credentials appear only in the request header; they are never put in a URL, an exception, a log line or a return value.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from .fyers_quotes import QuoteError, code_for_api_code, code_for_http_status

DATA_HOST = "api-t1.fyers.in"      # host shown in FYERS's own curl example for the quotes endpoint; the GET example does not name a host
QUOTES_PATH = "/data/quotes"
MASTER_HOST = "public.fyers.in"
MASTER_PATH = "/sym_details/NSE_FO.csv"
ALLOWED = frozenset({(DATA_HOST, QUOTES_PATH), (MASTER_HOST, MASTER_PATH)})
MAX_SYMBOLS = 50                   # documented limit of this endpoint, also the free Standard plan's per-request limit
MAX_QUOTES_BYTES = 1_000_000
MAX_MASTER_BYTES = 64_000_000


class TransportError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def check_url(url):
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != "https" or parts.username or parts.password or parts.port or parts.fragment
            or (parts.hostname, parts.path) not in ALLOWED):
        raise TransportError("NETWORK_ERROR")
    return parts


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def urllib_transport(url, headers, timeout, max_bytes):
    """GET `url` and return (status, body). Never follows redirects. Raises TransportError with a short code only."""
    check_url(url)
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            status, body = response.status, response.read(max_bytes + 1)
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400:
            raise TransportError("REDIRECT_REFUSED") from None
        status = error.code
        body = error.read(max_bytes + 1)
    except TimeoutError:
        raise TransportError("TIMEOUT") from None
    except (urllib.error.URLError, OSError):
        raise TransportError("NETWORK_ERROR") from None
    if len(body) > max_bytes:
        raise TransportError("RESPONSE_TOO_LARGE")
    return status, body


def _reject_constant(name):
    raise ValueError(name)           # json.loads would otherwise accept NaN and Infinity


class QuotesClient:
    def __init__(self, credentials, transport=urllib_transport, timeout=10.0):
        self._credentials = credentials
        self._transport = transport
        self._timeout = timeout

    def get_quotes(self, symbols):
        """Return the decoded quotes response. Raises QuoteError or TransportError carrying a short code only."""
        symbols = tuple(symbols)
        if not 1 <= len(symbols) <= MAX_SYMBOLS or any(not _symbol_ok(s) for s in symbols):
            raise QuoteError("INVALID_SYMBOL")
        url = f"https://{DATA_HOST}{QUOTES_PATH}?symbols=" + urllib.parse.quote(",".join(symbols), safe=",")
        headers = {"Authorization": self._credentials.header_value(), "Accept": "application/json"}
        status, body = self._transport(url, headers, self._timeout, MAX_QUOTES_BYTES)
        payload = _decode(body)
        failure = code_for_http_status(status)
        if failure is not None:
            api_code = payload.get("code") if isinstance(payload, dict) else None
            mapped = code_for_api_code(api_code)
            raise QuoteError(mapped if mapped != "API_ERROR" else failure)
        if payload is None:
            raise QuoteError("MALFORMED_RESPONSE")
        return payload


def fetch_symbol_master(transport=urllib_transport, timeout=60.0):
    """Download the public NSE_FO symbol master as text (no credentials are sent)."""
    status, body = transport(f"https://{MASTER_HOST}{MASTER_PATH}", {"Accept": "text/plain"}, timeout, MAX_MASTER_BYTES)
    if status != 200:
        raise TransportError("NETWORK_ERROR")
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        raise TransportError("NETWORK_ERROR") from None


def _symbol_ok(symbol):
    return (isinstance(symbol, str) and 3 <= len(symbol) <= 64 and symbol.startswith("NSE:")
            and all(c.isascii() and (c.isalnum() or c in ":-_&") for c in symbol))


def _decode(body):
    try:
        return json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError):
        return None
