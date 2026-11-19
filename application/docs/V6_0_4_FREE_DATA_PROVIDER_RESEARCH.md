# V6-0.4 Free market-data provider research: FYERS, compared with Groww

> **Attribution note.** This record was written by the Claude Code assistant during Claude-assisted development. "The assistant" (or "I", where it remains) means that assistant; "you" means the project owner. It records what the assistant did and found. It does not describe the owner's own contribution, which is not documented here.

Date checked: 2026-10-26. Research only. No account was opened, no credential created, no API called, no package installed, nothing
subscribed to or accepted, and no code changed. The V6-0.1 and V6-0.2 code, the ML project and the V5.1 frozen artifacts were not touched.
The Groww findings are from `docs/V6-0.3_GROWW_FEASIBILITY.md` and were not re-checked.

## 1. Conclusion
**No verified free source meets our requirements today.**
- **FYERS Standard (free)** appears technically close: the documented market-data endpoints include bid/ask, five-level depth, an option chain
  with bid/ask, and streaming. But FYERS's own API Terms (Section 11) say exchange data "shall not be used for the development of ...
  paper trading, gaming, and virtual trading purposes". Paper trading is exactly what this project does. Until FYERS confirms in
  writing that this use is permitted, FYERS must not be integrated. This is a legal/terms blocker, not a technical one.
- **Groww** documents live data as excluded from its free plan (paid: ₹499 + taxes per month, or ₹4,999 per year).
- Neither provider documents a true quote timestamp or states whether the data is real-time or delayed on your tier.

Lowest-cost practical path (section 6): spend ₹0 on one written question to FYERS before anything else, and do not subscribe to anything
until that answer and the timestamp questions are settled.

## 2. How this was read
Pages were fetched with a tool that returns a model-written summary of each page, except the FYERS API Terms, which the assistant downloaded as raw HTML
(one public page, saved only to a scratch folder) and searched for the exact clause. "[doc]" means a FYERS page states it. Page text changes;
re-read the links before relying on a detail. Other brokers and data vendors were **not** examined, so this report does not say none exist.

## 3. FYERS: verified from official pages
| Topic | Finding | Source |
|---|---|---|
| Free plan | **Standard is free** with usage limits; **Prime** is paid and raises them. Prime prices are not listed on the docs page. "Market data is accessible on the Standard plan, but with the restrictions above." Page last updated 14 Oct 2026. | [Plans & Limits](https://fyers.in/api-docs/v3/getting-started/plans-and-limits/) |
| Cost statement | A support article says datafeeds are "available to all our clients at absolutely zero fees" (undated; no real-time/delayed distinction). | [Support](https://support.fyers.in/portal/en/kb/articles/do-i-need-to-pay-for-datafeeds) |
| Account | API Terms Section 1: the Client must "hold a valid FYERS account" (an account with a FYERS-assigned client code). A FYERS account is therefore required. Whether you hold one is unknown to me. | [API Terms](https://fyers.in/terms-and-conditions-api) |
| App registration | Created in the FYERS API dashboard. An *Individual app* has one trading app (static IP required) and any number of **non-trading (data-only) apps**, which need no static IP and are not granted order placement. A non-trading app needs an app name and redirect URL, and you accept the API Terms and Conditions when creating it. The App ID and App Secret are shown in the dashboard. | [Create an App](https://fyers.in/api-docs/v3/getting-started/create-an-app/), [Individual app](https://fyers.in/api-docs/v3/getting-started/create-an-app/individual-app/) |
| Authentication | Browser login to get an auth code, then a POST of `appIdHash` (SHA-256 of app ID and secret) and the code to get an access token. Requests send `app_id:access_token` in the `Authorization` header. **Tokens expire daily at 6:30 AM.** Refresh-token validity, PIN/TOTP requirements and whether login can be automated were not found. | [Authentication](https://fyers.in/api-docs/v3/api/authentication/), [Flow](https://fyers.in/api-docs/v3/api/authentication/individual-app/authentication-flow/) |
| Permission | Quotes and Market Depth pages say the app needs the "Market Data" permission. | [Quotes](https://fyers.in/api-docs/v3/api/market-data/quotes/) |
| Rate limits (Data APIs) | Standard: 5/s, 50/min, 5,000/day. Prime: 10/s, 500/min, 500,000/day. Over a limit returns HTTP 429; more than 3 per-minute breaches in a day blocks you for that day. | [Plans & Limits](https://fyers.in/api-docs/v3/getting-started/plans-and-limits/) |
| Quotes | `GET` quotes: up to 50 symbols (POST: 1,000 for Prime, 50 for Standard). Fields include `lp`, **`bid`, `ask`**, `spread`, OHLC, `volume`, `tt`. No open interest field on this page. The page documents only an equity symbol example; the NIFTY index and option symbol formats are not on it. | [Quotes](https://fyers.in/api-docs/v3/api/market-data/quotes/) |
| Market depth (REST) | One symbol per request; **5 bid and 5 ask levels** (price, volume, orders) in the example, plus `ltp`, `ltq`, `ltt`, `oi`. | [Market Depth](https://fyers.in/api-docs/v3/api/market-data/market-depth/) |
| Option chain | Parameters `symbol`, `strikecount` (max 50 each side), `timestamp`, `greeks`. Each option has `ltp`, **`bid`, `ask`**, `oi`, `volume`, option type, strike; expiry list included. Examples use stocks; NIFTY is not named. No per-option timestamp. | [Option Chain](https://fyers.in/api-docs/v3/api/market-data/option-chain/) |
| Streaming | Data socket: symbol updates (`ltp`, `bid_price`, `ask_price`, sizes, `last_traded_time`), index updates (`NSE:NIFTY50-INDEX` is the documented example; index messages carry no bid/ask), 5-level depth updates. Subscription cap: **100 symbols on Standard**, 5,000 on Prime, per user. Tick-by-tick socket: 1 symbol on Standard. | [Overview](https://fyers.in/api-docs/v3/api/websockets/data-socket/overview/), [Symbol](https://fyers.in/api-docs/v3/api/websockets/data-socket/data-socket-symbol-update/), [Index](https://fyers.in/api-docs/v3/api/websockets/data-socket/data-socket-indices-update/), [Depth](https://fyers.in/api-docs/v3/api/websockets/data-socket/data-socket-depth-update/) |
| Historical | Candles (open, high, low, close, volume, optional OI) only, no bid/ask history. Standard: daily candles for the last 5 years (from 2021), intraday for the last 1 year (from 2025), no seconds data, expired F&O daily data for 2 years (from 2024), expired F&O intraday 10 requests per day. Index symbols are not mentioned on the page. | [History](https://fyers.in/api-docs/v3/api/market-data/historical-data/), [Plans](https://fyers.in/api-docs/v3/getting-started/plans-and-limits/) |
| Changelog | Newest entry 16 Oct 2026. Symbol limit raised 200 to 5,000 (26 Mar 2024); daily request cap raised to 100,000 (22 Aug 2024; the current plans page now shows 5,000/day for Standard Data APIs). | [Changelog](https://fyers.in/api-docs/v3/changelog/) |

### The terms
API Terms Section 11 (Prohibitions), final bullet, raw text: "Exchange data shall be used only for legitimate, fair, and transparent trading. The Exchange data shall
not be disseminated, supplied, and/or processed for any unauthorised or illegal purposes. The said data shall not be used for the development of
different charting, technical tools, paper trading, gaming, and virtual trading purposes."
Other points in the same document ([API Terms](https://fyers.in/terms-and-conditions-api)):
- Section 2: personal use as "a private interface for customizing personal trading and investment experience" is allowed.
- Section 12: no copying, caching for redistribution, public display or sub-licensing of API content without FYERS's written consent.
- No version or last-updated date appears on the terms page, so the assistant cannot tell how current it is. A FYERS product page separately advertises historical data for charting and backtesting, which sits uneasily with Section 11; the two statements are unreconciled.

## 4. Requirement-by-requirement comparison
"Doc" means documented; "?" means not established. Both providers were judged only on their own documentation and without credentials.

| Requirement | FYERS Standard (free) | Groww |
|---|---|---|
| ₹0 access to live market data | Documented (market data on Standard) | **Documented as excluded** from the free plan; paid ₹499 + tax/month |
| Needs a broker account | Yes, a FYERS account (unknown if you have one) | Yes (you have one; plan unknown) |
| NIFTY index value | Doc: index stream `NSE:NIFTY50-INDEX`; REST index quote not shown | Doc: quote with `segment=CASH`, `trading_symbol=NIFTY` (paid) |
| Single NIFTY option quote with bid/ask | Quote has bid/ask (symbol format for NIFTY options not shown on the pages read) | Quote has bid/ask; NIFTY option symbols in instrument file not confirmed |
| Option chain | Doc, includes bid/ask and OI; NIFTY not named | Doc, **no bid/ask, depth or timestamp** |
| Depth | Doc: 5 levels per side (REST and stream) | One level in the example; maximum not stated |
| Streaming | Doc: 100 symbols (Standard) | Doc: feed with timestamps (paid status of feed not stated) |
| True quote timestamp | **No.** REST quote `tt` ("today's time", unit unstated); depth `ltt` is last-traded time; stream depth has no timestamp; stream has an undocumented `exch_feed_time` | **No.** Only `last_trade_time` (epoch ms) |
| Real-time vs delayed stated | Pages say "live"/"real-time"; no explicit delay statement either way for Standard | Not stated |
| Daily login | Token expires 6:30 AM; browser login documented, automation not | Token expires 6:00 AM; token endpoint also offers key/TOTP flows |
| Rate limits (data) | 5/s, 50/min, 5,000/day | 10/s, 300/min |
| Terms vs paper trading | **Explicit prohibition in Section 11** | Not found. The assistant read only the API pages and did not locate or read Groww's full terms, so this is not evidence that none exist |
| Historical bid/ask | None (OHLCV only) | None (candles) |

## 5. Unresolved questions
1. **FYERS written permission.** May Standard-plan API data be used in a personal, local, non-redistributed paper-trading simulator, given Section 11? (Blocker.)
2. **Real-time or delayed** on the Standard plan, and whether exchange-data licensing fees ever apply to you.
3. **Quote time.** What `tt`, `ltt` and `exch_feed_time` mean and their units; whether any field is the time of the quote itself rather than of the last trade. This decides whether the V6-0.1 eligibility boundary can be fed honestly.
4. **NIFTY symbols** (index and option format) and whether the option chain supports NIFTY: the FYERS Appendix and symbol master were not read.
5. **Login automation** and refresh-token validity; daily manual login may be required.
6. **SEBI retail-algo changes** (a notice on the authentication page says usage changes may take effect 1 April 2026) and their effect on data-only apps. The pages read do not say.
7. **Whether you have a FYERS account**, and which Groww plan you hold. Both are for you to check; the assistant did not.
8. **Groww's full terms** on using API data for paper trading (not located).

## 6. Recommendation (lowest practical cost)
1. **Do not integrate any provider now.** No verified free source meets the requirements, so the dashboard stays on the synthetic provider.
2. **Ask FYERS first, at no cost** (support or your account's help channel, in writing). Suggested question: *"I run a private, local simulator that reads Standard-plan market data through a data-only API app, shows it only on my own computer, and never redistributes it or places orders. Your API Terms Section 11 says exchange data shall not be used for paper trading or virtual trading. Is this use permitted? Is Standard-plan market data real-time, and which field gives the time of a quote?"* Keep the reply. If the answer is yes in writing, a FYERS data-only app becomes the ₹0 candidate and a follow-up task could read the Appendix, confirm NIFTY symbols and design the adapter. If no or unclear, FYERS is ruled out.
3. **Opening a FYERS account is your decision**, and is not needed to ask the question above if you can reach their support without one. Creating an app means accepting the same API Terms, so do that only after the written answer.
4. **If FYERS says no**, the remaining paths cost money or lose realism: (a) Groww paid plan at ₹499 + tax per month (₹4,999 per year), which still has the timestamp and real-time gaps in the Groww report; (b) a different provider, not yet researched; (c) continue without live quotes, using synthetic data and end-of-day files, which have no bid/ask and so cannot support honest fill simulation (see V6-0.1).
5. **Whichever provider is chosen**, do not claim `LIVE` or feed the eligibility boundary until the quote-time question is answered in writing or by a prior-approved freshness rule.

## 7. Not done, by request
No account, credential, API call, installation, subscription, acceptance of terms, dashboard change, signal, paper order or broker order code, and no V5.1 rerun.
