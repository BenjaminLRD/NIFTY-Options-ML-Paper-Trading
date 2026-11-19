# Safety boundaries and limitations (scaffold V6-0.1)

This document states what the scaffold protects against, how, and, just as importantly, what it does **not** protect against.
Nothing here proves the system is secure against every possible bypass.

## 1. Properties the scaffold aims to keep
1. **No executable price comes from non-executable data.** End-of-day, settlement-only, last-traded, delayed and unknown prices cannot become a fill price.
2. **Refusal is a hard stop.** An ineligible quote gives an exception or an explicit `eligible=False` result with reason codes. There is no warning-only path and no fallback to another price.
3. **No order-placement capability exists** in the source: no broker SDK, no order-placement/modification/cancellation function, no live-order endpoint, no outbound network code. The one network module is the V6-0.2 dashboard server (`http.server`), bound to 127.0.0.1, answering GET/HEAD only (section 5).
4. **No secrets in the repository.** No credentials are used by any code or test. Test "secrets" are synthetic strings assembled at run time.

## 2. Mechanisms
| Property | Mechanism | Test evidence |
|---|---|---|
| 1, 2 | `eligibility.check_fill_eligibility` (never raises; any exception becomes `INTERNAL_ERROR`), `require_fillable` (raises `IneligibleQuoteError`), `FILLABLE_TYPES = {LIVE_QUOTE}` | `tests/test_eligibility.py`: every prohibited case asserts `eligible is False`, the specific reason, an exception from `require_fillable`, and that no warning was issued |
| 1 | `fill_price` accepts only an `ExecutableQuote`; its constructor needs a module-private token, so it cannot be built directly | direct-construction, wrong-argument and immutability tests |
| 1 | `Quote.digest` recomputed at check time: fields changed after construction are rejected (`TAMPERED`); subclasses are rejected (exact `type(...) is Quote`) | bypass-attempt tests |
| 2 | `EligibilityResult.__bool__` raises, so `if check(...):` cannot be written by mistake | boolean-misuse test |
| 2 | `EligibilityPolicy` has no defaults and no way to allow a prohibited type | policy tests |
| 3 | No such code exists; AST tests check imports (no network modules, standard library only), names (no order/broker identifiers), `pyproject.toml` (no dependencies) and that refusals are not warnings; the safety scan checks file text | `tests/test_structure.py`, `tests/test_safety_scan.py` |
| 4 | `niftypaper.safety.scan` over git-listed files, `.gitignore` requirements, optional pre-commit hook | synthetic-example tests; the repository scans clean |

## 3. Limitations of the eligibility boundary
- **In-process guard only.** Python cannot stop code in the same process from importing private names, monkeypatching `check_fill_eligibility`, editing `FILLABLE_TYPES`, or rebuilding objects. The digest detects accidental or naive changes; code that recomputes it, or builds a fresh `Quote`, is not stopped.
- **Label trust.** A quote is only as honest as the adapter that labelled it. If a future adapter marks end-of-day data as `LIVE_QUOTE`, this boundary will accept it. Each adapter needs its own tests proving its output type (planned for the EOD adapter: all rows non-fillable).
- **Timestamp trust.** The boundary checks that a timestamp exists, is timezone-aware, is not too old and not in the future; it cannot check that the source's clock was right, or distinguish exchange time from receive time.
- **Not yet checked:** trading-session hours and holidays, order size against displayed size/depth, price bands, instrument existence, lot-size consistency, quote sequence/duplicates, latency. These belong to later phases.
- **Locked markets are rejected** (bid equal to ask) as a conservative choice; this may reject some genuine quotes.
- **Delayed quotes are not supported** as a fill source in this version, by design.
- **Contract specs are unverified.** The lot size and tick size shipped in `config/contract_specs.json` come from sample data in the earlier research project and are marked `UNVERIFIED_OBSERVED`.

## 4. Limitations of the safety scan
- **Pattern matching on text.** It cannot see obfuscated, split, encoded or encrypted values (the tests document examples), values built at run time, or secrets stored outside the files.
- **Scope.** Default: files known to git (tracked or staged). `--include-untracked` adds untracked, non-ignored files. It does not read git history, other branches, stashes, ignored files, or anything outside the repository.
- **Lists are non-exhaustive and unverified.** Broker hosts and SDK names come from memory and may be incomplete or out of date. Order-verb and endpoint rules can be evaded by different naming.
- **False positives and negatives.** It flags some harmless text (for example a comment using a listed verb) and will miss anything the rules do not describe. Credential-like assignments need a long mixed-character value; short or low-variety secrets and values containing words such as "example" or "your" are ignored on purpose.
- **Incomplete scans fail closed:** unreadable, oversized, binary, non-UTF-8 or non-regular files are listed and make the exit code non-zero. It also needs `git`; without it, it errors instead of passing.
- **Not a replacement** for a dedicated secret scanner, code review, or keeping credentials out of the working tree altogether.

## 5. Dashboard (V6-0.2)
- Synthetic values are tagged `SYN` in the page (`data-synthetic="true"`) and `"synthetic": true` in the JSON, and the response carries `X-Data-Synthetic: true`.
- Stale, missing, erroring, mistimed (naive or future) or non-synthetic data gives `STALE` or `UNAVAILABLE` and **no values**. The provider is never swapped for synthetic data automatically.
- With modes and trading off (a bare `App`), the page has no form, button, input or script; with them on, the only forms post to `/mode` and `/paper/trade` (section 7). Every other method gets 405; unknown paths (including order-like ones) get 404; a foreign `Host` header gets 403.
- The server refuses to bind to anything but 127.0.0.1/localhost. Tests show this for the code as written.
- The dashboard does not import the quote model or the eligibility boundary, and its synthetic rows are not `Quote` objects, so they cannot become a fill source.
- **Limits:** any program or user on this computer can reach the port (no authentication); loopback binding does not protect against another local account or malware; the Host-header check and CSP are basic hardening, not a security review; the structure tests are static checks of this source tree.

## 6. Read-only FYERS adapter (V6-0.5)
- **Network:** `providers/fyers_http.py` is the only module that opens an outbound connection (`urllib`). It sends GET requests only, over HTTPS, to two allow-listed (host, path) pairs: the quotes endpoint and the public NSE_FO symbol master. It follows no redirects. There is no way to name another endpoint, and no order, position or account endpoint exists in the source. The quotes host is the one shown in FYERS's own curl example; the GET example does not name a host, so the first manual smoke test is also the first confirmation of it.
- **Credentials:** read only from the environment (`FYERS_APP_ID`, `FYERS_ACCESS_TOKEN`), held in an object whose `repr`/`str` are redacted and that cannot be pickled, sent only in the `Authorization` header, and never placed in a URL, exception, log line, page or JSON. Failures carry short fixed codes, not response or exception text. Tests check that a fake secret appears nowhere the user can see.
- **No silent fallback:** a failed provider gives `UNAVAILABLE` with no prices. A provider-mode dashboard refuses a synthetic or unclassified snapshot, and the banner and tags are chosen from the snapshot itself, so provider data can never carry the simulated label (or the reverse).
- **No unearned `LIVE`:** the adapter always reports timestamp meaning and latency as unknown, so the state is at best `UNVERIFIED`. `tt` is kept exactly as received and not used to judge freshness; the local receive time is separate and proves nothing about when the observation was made. Only the local receive age (30 s limit) can turn a snapshot `STALE`.
- **Quote checks:** response status, symbol identity, finite positive numbers, and for options a non-empty, uncrossed, on-tick bid/ask. The option symbol comes from the official symbol master and is accepted only on an exact, internally consistent single match.
- **The V6-0.1 eligibility boundary is untouched and not used.** Provider snapshots are not `Quote` objects and cannot become a fill source; feeding it would need a verified quote time and a separate, approved design.
- **Scanner exemption:** rule B3 (broker host) is waived for exactly two files, the client module and the implementation record, because they must name the one data host; every other rule still applies to them and B3 applies everywhere else.
- **Limits:** the dashboard polls on page load (at most one upstream request per 5 s); anyone on this computer who opens the page sees the data; the Standard plan allows 5,000 data requests a day; the first real index request is unverified until you run the smoke test; tests use made-up responses, so they cannot tell you how FYERS actually behaves.

## 7. Dual-mode dashboard and virtual trading
- **Mode switching** is a POST to `/mode`; virtual trades are a POST to `/paper/trade`. These are the only POST endpoints, and they exist only when the app is run with modes or trading enabled (the launcher does). Both need the per-run form key embedded in the page, a loopback `Host`, and (if sent) a loopback `Origin`, a form content type and a body under 2 KB with strict parsing. Other methods and paths get 405. There is no real-order endpoint, and the engine imports no provider, dashboard or network code (tested).
- **Synthetic Mode** reads no credentials and makes no network request; the real provider is built only when Real-Time Mode is chosen (tested with a spy environment, a spy transport and a connection guard).
- **No silent fallback:** only an explicit mode switch changes the source. A failed, missing-credential, rate-limited or unstartable real provider shows `UNAVAILABLE`; the app stays in Real-Time Mode. A provider-mode app also refuses synthetic data.
- **Trading gate:** a virtual trade needs verified current prices (`SIMULATED` for synthetic data, `LIVE` for provider data) and, for provider data, the locally recorded terms permission. `LIVE` needs documented real-time delivery plus a documented quote time; provider quotes must then pass the V6-0.1 eligibility boundary (quote time, displayed sizes, tick, freshness). The engine rechecks everything itself and never guesses a spread or a fill price.
- **Terms:** the permission file is a statement by the user, not a verification. Without a valid entry (`virtual_trading_permitted` exactly true, an evidence note, a non-future date) virtual trading with provider data is refused. Viewing provider data does not need it.
- **Limits:** the form key and Host/Origin checks stop other web pages from driving the dashboard, not other programs or users on this computer; the ledger and positions are in memory; costs, slippage and limits are illustrative defaults, not actual charges or a risk model; fills assume the whole quantity at the touch.

## 7a. Daily risk-entry limit and saved paper state
- A BUY is refused if the projected worst-case loss for the IST trading day would pass 2% of the starting balance (formula in `docs/PAPER_ACCOUNTING.md`). This limits new risk only: it is not a guarantee against gaps, illiquidity, bad prices or impossible exits, and nothing is sold automatically. Exits are never blocked.
- Accepted trades are appended to a local file and replayed on start. A file that cannot be trusted locks all trading (never silently reset); it is never rewritten by the program. The file is not tamper-proof and is meant for one process at a time.
- Refusals are atomic: no cash, position, fee, P&L, ledger, day or file change.
- One writer per state file: the account holds an OS lock on `<file>.lock` (released if the process dies). A second dashboard on the same directory locks that account with `IN_USE` and writes nothing; a file changed by something else mid-run refuses the trade and locks the account. Tested with separate processes on Windows; the lock is advisory.
- One virtual account per data source (synthetic, and each provider): separate file, cash, positions, ledger and risk budget; an account refuses prices of the other kind; a file copied to another account's name is rejected. Switching modes never merges or resets accounts.
- The daily risk estimate allows one exit fee per lot and an exit-slippage allowance (default 2 ticks) when judging the loss already incurred; the allowance is not added to the worst case, which already assumes the whole premium is lost.

## 8. Handling real credentials
The only credentials the code handles are the two FYERS environment variables (section 6). Keys must live outside the repository (environment variables or the
operating-system credential store), `.gitignore` already excludes common secret file names, and logs must not print them. Read-only data access
must not require, and the code must not contain, any order-placement scope. The FYERS access token expires daily, so it is best set per terminal session rather than stored.
