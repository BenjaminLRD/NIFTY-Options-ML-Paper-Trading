# FYERS provider verification (V6-0.3 milestone record)

Official pages were read on 2026-10-29 (read-only page fetches; no login, no API call, no credentials used). **No request was made to any FYERS API endpoint**, because the preconditions below are not met. Nothing here shows that FYERS data is real-time, permitted for this use, or correct. Fixtures and test transports are never provider data.

## 1. Verified from the official documentation (as read on that date)

| Topic | What the official page says | Source page |
|---|---|---|
| Quotes endpoint | `GET /data/quotes` (up to 50 symbols, `symbols` string) and `POST /data/quotes` (array; 1,000 symbols on Prime, 50 on Standard). Header `Authorization: app_id:access_token`. The app needs the **Market Data** permission. | api-docs/v3/api/market-data/quotes |
| Host | The only host shown is in the POST cURL example (the one `DATA_HOST` in `providers/fyers_http.py` uses); the page names no host for GET. | same |
| Fields in `v` | `lp` last traded price, `ch`, `chp`, `bid`, `ask`, `spread`, `open_price`, `high_price`, `low_price`, `prev_close_price`, `atp`, `volume`, `symbol`, `fyToken`, ... and `tt`. | same |
| Timestamp | The only time field is `tt`, documented as "Today's time", typed int but shown as quoted strings in samples, no timezone, no unit. **No last-traded-time or exchange-feed-time field is documented.** | same |
| Real-time or delayed | Not stated for REST quotes (the page points streaming users to the Data Socket for "live" updates). Also not stated on the Plans page. | same; plans-and-limits |
| Plans | Standard: free, with limits. Prime: paid subscription (no price on the page). | getting-started/plans-and-limits |
| Data API limits | Standard 5/s, 50/min, 5,000/day; Prime 10/s, 500/min, 500,000/day. Quotes per request: 50 (Standard) / 1,000 (Prime). | same |
| Authentication | Log in to obtain an `access_token`; it expires daily at 06:30; expired or invalid tokens give codes -8, -15, -16, -17. | api/authentication |
| Option chain | An SDK method `optionchain` with `symbol`, `strikecount` (max 50), `timestamp`, `greeks`; fields include `ltp`, `bid`, `ask`, `oi`. The page gives **no host/path, no update frequency, no real-time statement**, and the Plans page does not list it. | api/market-data/option-chain |
| Terms of use (no version or date shown) | Section 2: personal use allowed. **Section 11: exchange data may not be used for "charting or technical tools" or for "paper trading, gaming, and virtual trading purposes."** Section 12: credentials are the client's alone; scraping, building databases or keeping copies for redistribution needs FYERS's written consent. No API or data fee is stated. | fyers.in/terms-and-conditions-api |

## 2. Not verified (still uncertain)

- Whether Standard-plan REST quotes are real-time, delayed or snapshots, and how often they update.
- What `tt` means (timezone, unit, whether it is an exchange time) and whether it ever carries a time of day.
- The official host for the GET quotes endpoint (this project uses the host from the POST example, `DATA_HOST`).
- Whether the quotes endpoint accepts the index symbol `NSE:NIFTY50-INDEX` and whether option `bid`/`ask` are populated for NIFTY options on Standard.
- The official host/path and plan entitlement for the option chain (a community post cites `/data/options-chain-v3`; that is not official and is **not used**).
- NIFTY lot size and tick size against an exchange circular (the adapter takes them from FYERS's public symbol master; `config/contract_specs.json` stays `UNVERIFIED_OBSERVED`).
- Prime's price.
- Whether FYERS's written permission covers this project's use (section 11). The confirmation table in `docs/provider_terms/FYERS_TERMS_AND_CONFIRMATION.md` is still blank.

## 3. Why no request was made (unresolved requirements)

1. **Credentials** `FYERS_APP_ID` / `FYERS_ACCESS_TOKEN` are not set in the environment (checked for presence only; no value read or printed). You must create a data-only app and set them locally.
2. **Recorded permission.** `config/local/provider_permissions.json` does not exist and the confirmation record is blank; section 11 prohibits paper/virtual trading with FYERS data unless FYERS allows it.
3. **Real-time evidence and timestamp meaning** (section 2) are undocumented, so FYERS data would be `UNVERIFIED` and could never be traded even with the two items above.
4. The GET host, option-chain access and contract specifications (section 2) remain open.

When 1 and 2 are done by you, the single manual check is `python run_fyers_smoke.py` (one quotes request; it prints only states, reasons, prices, the evidence label and both timestamps, never credentials). Its expected honest result is `UNVERIFIED`.

## 4. What the code now guarantees (all tested with fixtures only)

- **Evidence label.** Every snapshot says where it came from: `PROVIDER_RESPONSE` (only when the real HTTPS client is the transport), `FIXTURE_TRANSPORT` (the real adapter code reading a test transport), `SYNTHETIC_GENERATOR`, or `UNSPECIFIED` (a hand-built test provider). It is shown on the page, in `/api/snapshot` and in the smoke output, and it never upgrades a data state.
- **Two clocks, kept apart.** `received_at` is our own clock when the response arrived; `provider_time_raw` is the provider's field exactly as received; `exchange_time` is `null` with status `NOT_ESTABLISHED` unless a snapshot declares a documented quote time. FYERS snapshots never do.
- **Freshness and eligibility.** Age and the stale limit (30 s) are on the page; the strip states `Execution prices: ELIGIBLE` or `NOT ELIGIBLE` with the reason. Provider data is at best `UNVERIFIED`; only `LIVE` data with a recorded permission can ever be traded.
- **Fail closed.** Missing permission, unverified metadata, missing or stale quotes, crossed/locked/zero/off-tick bid and ask, unknown lot or tick size, and no data all leave the ledger unchanged.
- **No order path.** The HTTP client can name only two read-only GET endpoints (quotes, public symbol master); tests show order-looking URLs, plain HTTP, ports and fragments are refused.
- **Charting.** The dashboard chart draws only synthetic history. Section 11 of the terms prohibits charting FYERS exchange data, so provider data is never charted (also enforced by tests).
- **Unpriced positions.** See `docs/STALE_DATA_RECOVERY.md`.
