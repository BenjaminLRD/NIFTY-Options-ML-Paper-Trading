# Provider feasibility: NIFTY index and option quotes for a local paper-trading dashboard

Date of research: 2026-10-29. Method: public documentation, pricing and terms pages read, plus web search results. **No credentials were requested, stored or used. No provider API, FYERS or otherwise, was called. No data was downloaded.** Nothing here shows that any provider's data is real-time, correct, or permitted for this project. Search-result and third-party statements are labelled as such and are not evidence of a provider's policy.

## 1. Bottom line

- **No provider can be recommended on the evidence found.** Every candidate fails at least one of: a documented right to use the data for simulation, a documented quote timestamp, or a documented real-time status.
- **FYERS Standard** is the cheapest option (free) and has the most complete documentation read, but its API terms (section 11) *explicitly name* "paper trading" and "virtual trading" as prohibited uses of exchange data. It stays blocked until FYERS gives written permission.
- **Zerodha Kite Connect (Connect plan, 500 rupees/month per app)** has the best documented quote timestamp (`timestamp`, described as the exchange time of the quote packet) and five-level depth. Its permitted-use text was **not found**; the official pricing page is silent on personal use and simulation.
- **Upstox** documents a `timestamp` ("feed update time") and five-level depth, but pricing, rate limits for the quote call and usage terms were not on the page read.
- **Dhan** documents depth but only `last_trade_time`; the Data API is reported (third party) at 499 rupees plus tax per 30 days; usage terms were not on the pages read.
- **Angel One (its API documentation)**: the documentation page returned no content to the reader tool, so **nothing was established**. Not "no" - not examined.
- **NSE direct / authorised vendors** are the only route where redistribution and display licences are formally defined, but prices were not retrieved and a personal paper-trading dashboard is probably not their target customer.

**The exact next step** is a written question to a provider (section 6), not code and not credentials.

## 2. What written permission is required (personal, local, simulated use)

Separate three things the provider might grant. A grant of one does not imply the others.

| # | Right | Needed here? | Why |
|---|---|---|---|
| A | API access / a data subscription (technical entitlement, plan, quota) | Yes | Without it no quote is returned. Evidenced by plan pages. |
| B | Permission to *use* the data for this purpose: private charting, simulation / paper trading, personal research | **Yes, explicitly** | The dashboard compares live quotes against a virtual account. FYERS terms single this use out. |
| C | Permission to store, copy or build a database from the data | Only if the app persists quotes | The current app keeps virtual trades, not quote history, but fill prices derive from quotes. Ask anyway. |
| D | Redistribution / display to others / commercial use | **No** - the project must never do this | Out of scope; keep it out. |

**Explicitly stated by FYERS** (API terms, read 2026-10-29, no version or date shown on the page): personal use is allowed (section 2); exchange data may not be used for "charting or technical tools" or for "paper trading, gaming, and virtual trading purposes" (section 11); credentials are the client's alone, and scraping, building databases or keeping copies needs FYERS's written consent (section 12). The terms state no data fee.

**What remains uncertain for FYERS:** whether section 11 is a blanket ban or aimed at redistribution; whether it applies to Standard-plan data; whether it can be waived in writing; and whether FYERS's written reply must come from a named department. Only FYERS can answer. The confirmation table in `docs/provider_terms/FYERS_TERMS_AND_CONFIRMATION.md` is still blank.

**Minimum written permission to record** (for any provider): the data product and plan; that the user may display quotes privately on their own machine; that the user may compute simulated fills and profit and loss from them; that short-term local storage for audit is allowed (state the retention); that no redistribution occurs; the date, the sender and the terms version. Record it in `config/local/provider_permissions.json` (git-ignored; none exists today).

## 3. Candidate comparison (official statements only unless marked)

"Not found" means the page read did not say; it does not mean the provider lacks it.

| Provider / product | Quote timestamp | Bid / ask | Index + options | Cost | Rate limits | Use-permission text |
|---|---|---|---|---|---|---|
| **FYERS Standard** quotes REST | `tt` "Today's time"; no timezone or unit; no exchange or last-trade time | `bid`, `ask` fields | Index symbol and NIFTY option bid/ask **not verified**; option chain documented without host or entitlement | Free (Standard) | 5/s, 50/min, 5,000/day; 50 symbols/request | **Prohibits** charting and paper/virtual trading (section 11) |
| **FYERS Prime** | same as above | same | same | Paid, price not listed | 10/s, 500/min, 500,000/day; 1,000 symbols POST | Same terms; plan does not change section 11 as far as shown |
| **Zerodha Kite Connect** Personal | no market data | none | none | Free | n/a | Not found |
| **Zerodha Kite Connect** Connect | `timestamp` = exchange time of quote packet; `last_trade_time` may be null | Five depth levels with price, quantity, orders | NIFTY 50 index appears in examples; options carry `oi`; not stated outright | 500 rupees/month per app (official support page); GST not stated | Quote rate limit not stated on the quote page; up to 500 instruments (full quote) | Official pricing page is silent. A forum post (unofficial; reply not from confirmed staff) says personal use is permitted and redistribution needs a licensed vendor - **do not rely on it** |
| **Upstox** full quote | `timestamp` (feed update time) and `last_trade_time`, in milliseconds | Five depth levels | Index support only partly shown (India VIX, global indices); options carry `oi` | Not stated on page; third party says free standard feed | Not stated on page; 500 instruments/call | Not found |
| **Dhan** market quote | `last_trade_time` only; no quote-level timestamp | Depth in quote call (five levels in example) | Options in examples; index not stated | Data API reportedly 499 rupees + tax per 30 days (third party, unverified) | Quote API 1/s; data API 5/s and 100,000/day (official page); 1,000 instruments/call | No terms on page read |
| **Groww** (from the V6-0.4 research, 2026-10-26) | `last_trade_time` only | Not re-checked | Not re-checked | 499 rupees + tax/month or 4,999/year | Not re-checked | Full terms not read |
| **Angel One** API | **Not established** (docs page returned no content) | - | - | - | - | - |
| **NSE Data & Analytics / authorised vendors** | Product-specific (snapshot files over SFTP are documented by NSE) | Product-specific | Yes | 2026 tariff PDFs exist; **figures not retrieved**. Third-party table: TrueData and Global Datafeeds API "tailored quote only" | Product-specific | Licences are formal; display and redistribution licences are separate |

## 4. Evidence by theme

**Timestamps.** Only two documentation pages read describe a time field with a meaning: Kite Connect (`timestamp`: exchange time of the quote packet) and Upstox (`timestamp`: feed update time). Neither says what happens when the market is closed or a feed pauses, and neither was observed against a live response (no call was made). FYERS and Dhan expose no quote-level time with a defined meaning. This project's staleness gate needs a clock that is not our own receive time, so those two are better matched to it. A meaning on a page is a claim, not a measurement; the first live response must be compared against a trusted reference before any state above `UNVERIFIED`.

**Bid and ask.** All four documented APIs return depth or `bid`/`ask`. Whether *NIFTY weekly option* quotes carry non-zero bid and ask during the session has not been observed anywhere. The engine already fails closed on missing, crossed, locked, zero or off-tick quotes.

**Cost.** Observed figures: FYERS Standard 0; Kite Connect 500 rupees/month per app; Dhan Data API 499 rupees + tax/month (third party); Groww 499 + tax/month. Prime, NSE, TrueData and Global Datafeeds prices were not established. Tax treatment is unknown for every paid plan, so none of these is a final bill.

**Rate limits.** The dashboard needs one index quote and a handful of option quotes every few seconds. FYERS Standard (5/s) and Dhan (quote 1/s) both suffice for that; the others are unspecified on the pages read. Rate is not the binding constraint. Permission and timestamp meaning are.

**Integration complexity.** The provider adapter boundary already exists (`FyersProvider` plus the shared snapshot and evidence model). A second REST adapter would be similar work: authentication, symbol mapping, a response parser, tests with fixtures. Token handling differs: FYERS tokens expire daily at 06:30; other brokers' token lifetimes were not read.

**Subscription is not permission.** A paid or free *entitlement* (A) tells you the API will answer. The exchange-data rules the broker passes on (B, C, D) are separate. FYERS's free plan is the clearest example: access is free, yet use for paper trading is named as prohibited. Kite Connect's paid plan is the converse risk: payment is documented, use rights are silent. This document does not infer one from the other.

## 5. Unresolved questions

1. FYERS: does written permission exist for private, local, non-redistributed paper-trading use (section 11)? Is Standard data real-time or delayed? What does `tt` mean?
2. Kite Connect: where is the binding data-use policy, and does it permit private simulation and local storage? Is `timestamp` populated for index and option quotes outside exact-trade moments?
3. Upstox and Dhan: usage terms, token lifetime, confirmation that NIFTY index and option quotes with bid/ask are returned, and plan pricing.
4. Angel One: everything (documentation could not be read).
5. All: do any of these quotes arrive with a delay (exchange-mandated delayed feeds exist for some retail products)? No page read states it plainly for a REST quote.
6. SEBI retail algorithmic-trading rules and how they treat read-only data apps: not examined.
7. Contract specifications (lot size, tick size) still need an exchange circular; they are `UNVERIFIED_OBSERVED`.

## 6. Exact question to put to the provider

Send in writing and keep the reply, with its date and sender, outside the repository's tracked files:

(Template in the owner's voice, for the owner to adapt and send; it has not been sent.)

> I am an individual retail client. I want to use your market-data API (product and plan: ______) to receive NSE index and option quotes on my own computer for a private, non-commercial **simulated (paper) trading** application that displays the quotes and computes virtual fills and virtual profit and loss. Nothing is redistributed or shown to anyone else, and no real orders are sent. Please confirm in writing: (1) whether this use is permitted, including section 11 of your API terms where it names charting, paper trading and virtual trading; (2) whether quotes on this plan are real-time or delayed, and by how much; (3) the meaning, timezone and unit of the time field in each quote, and whether it is the exchange time; (4) whether index and NIFTY option quotes include best bid and ask; (5) whether keeping a local copy of the quotes for audit, for ______ days, is permitted; and (6) any fee, tax or condition attached.

For **FYERS** this is the question to ask first, because its terms name the use outright. For a comparison, ask the same text of **Zerodha Kite Connect** (it has the better-documented timestamp). Whichever replies in writing with an unambiguous yes becomes the recommendation. A silent or ambiguous reply means no provider, and the app stays synthetic.

## 7. Expected costs

| Route | Documented or reported cost | Status |
|---|---|---|
| FYERS Standard | 0 | Official; but use blocked by section 11 |
| Kite Connect Connect plan | 500 rupees/month per app (GST not stated) | Official support page |
| Dhan Data API | 499 rupees + tax / 30 days | Third party, unverified |
| Groww | 499 + tax/month or 4,999/year | From V6-0.4 research |
| Upstox | "Free standard feed" | Third party, unverified |
| NSE / vendors | Quote needed | Not established |

Budget conservatively at about 500 rupees per month plus tax for the realistic paid route. This is an estimate from the documents above, not a quote.

## 8. What this milestone did not change

No engine, provider, dashboard or data code was modified. No frozen research artifact was touched. The synthetic mode remains the only trading mode. Provider data can at best reach `UNVERIFIED`, and nothing in this document upgrades it.

## 9. Sources (pages read 2026-10-29 unless stated)

- Existing project records: `docs/FYERS_VERIFICATION.md`, `docs/provider_terms/FYERS_TERMS_AND_CONFIRMATION.md`, `docs/V6_0_4_FREE_DATA_PROVIDER_RESEARCH.md`.
- Kite Connect market quotes documentation (kite.trade/docs/connect/v3/market-quotes) and the Zerodha support article "What are the charges for Kite APIs" (support.zerodha.com).
- Kite forum thread on personal-use data questions (kite.trade/forum/discussion/16250): reply not from confirmed staff and answers nothing; **not evidence**.
- Upstox full market quote documentation (upstox.com/developer/api-documentation/get-full-market-quote).
- Dhan's market quote and API overview documentation pages (the v2 docs on Dhan's developer site).
- Angel One's developer documentation landing page: returned no usable content.
- NSE Data & Analytics (nseindia.com/market-data/real-time-data-subscription): fetch timed out; only search-result summaries seen.
- Third party, unverified: onetradejournal.com cost comparison page (checked by its author 14 October 2026).
