# FYERS: written confirmation and applicable terms (local record)

Purpose: keep, next to the code, the permission and the terms this project relies on for reading FYERS market data. Nothing here is a secret.
Do not paste access tokens, app secrets or full account numbers into this file.

## 1. FYERS support's written confirmation (to be completed by the project owner)
The project owner reports that FYERS support confirmed in writing that this project's small, private paper-trading use is acceptable.
**The assistant that wrote this file has not seen that message**, so its wording is not recorded here and has not been verified. Please paste it
(remove personal details such as your name, client ID, e-mail address and ticket links if you wish to keep them private, but keep the wording that grants permission):

| Item | Value |
|---|---|
| Date received | _fill in_ |
| Channel (e-mail, ticket, chat export) | _fill in_ |
| Reference / ticket ID (optional) | _fill in_ |
| Question you asked (summary) | _fill in_ |
| FYERS's answer, exact wording | _paste here_ |
| Conditions or limits FYERS stated (plan, personal use only, no redistribution, data-only app, etc.) | _fill in_ |

Keep the original message in your own mailbox or files as well. Until this table is filled in, the repository does not document the permission.

## 2. Applicable FYERS API Terms and Conditions (excerpts, retrieved 2026-10-26)
Source: https://fyers.in/terms-and-conditions-api . The page shows no version or last-updated date. The raw HTML retrieved that day has SHA-256
`44b130856a96579e7adb3e43ed2dda1d3acbd008b60e3c89f8db1f3a6b2f733c` (the file itself is not stored in the repository). Re-read the live page; it may have changed.
- Section 1: access to certain APIs requires a valid FYERS account.
- Section 2: personal use, as a private interface for customising personal trading and investment experience, is allowed.
- Section 11 (Prohibitions), last bullet, short quotation: "The said data shall not be used for the development of different charting, technical tools, paper trading, gaming, and virtual trading purposes."
  This is the clause the written confirmation in section 1 is meant to cover. If the confirmation does not clearly cover it, treat the adapter as not permitted and stop using it.
- Section 12 (Confidentiality): credentials are for the Client only and must be kept secure; copying, caching for redistribution, public display or sub-licensing of API content needs FYERS's written consent. This project does not redistribute or publicly display the data.

## 3. Plan and access facts relied on (retrieved 2026-10-26)
- Plans & Limits (page last updated 14 Oct 2026): the Standard plan is free; Data API limits 5 requests/s, 50/min, 5,000/day; quotes up to 50 symbols per request; data socket 100 symbols. https://fyers.in/api-docs/v3/getting-started/plans-and-limits/
- Create an App: a non-trading (data-only) app needs no static IP and is not granted order placement; creating one means accepting the API Terms. https://fyers.in/api-docs/v3/getting-started/create-an-app/individual-app/
- Authentication: access tokens expire daily at 6:30 AM. https://fyers.in/api-docs/v3/api/authentication/
- Quotes: the app needs the Market Data permission. https://fyers.in/api-docs/v3/api/market-data/quotes/

## 4. What this means for the code
- The adapter is read-only, uses a data-only app, shows the data only on `127.0.0.1`, and does not store or redistribute it.
- Whether the Standard plan's feed is real-time is not stated in FYERS's documentation, and a written confirmation of permitted use is not a statement about timing. The dashboard therefore shows `UNVERIFIED`, not `LIVE`.
