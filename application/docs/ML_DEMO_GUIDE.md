# Demo guide (invented data, about 5 minutes)

**No real orders are placed, no real market data or provider is used, no credentials are needed, and no profitability is established.** Everything below runs on invented 2031 data and virtual money.

## Setup

* Python **3.12 or newer** (check with `python --version`). Nothing else to install: the project uses only the standard library.
* Get the repository folder (clone or copy). Open a terminal in it (Windows PowerShell; the commands are the same elsewhere).

## Start

```powershell
python run_ml_demo.py
```

It makes the first replay (40 invented trading days) if none is saved, prints a short summary, and serves the dashboard on 127.0.0.1 only. Open **http://127.0.0.1:8765/ml** (the main dashboard is `http://127.0.0.1:8765/`). Stop with Ctrl+C.

| Command | What it does |
|---|---|
| `python run_ml_demo.py` | Shows the newest saved replay (makes one if there is none). Nothing is overwritten. |
| `python run_ml_demo.py --new` | Makes another replay (`run2`, `run3`, ...). Earlier ones stay untouched. |
| `python run_ml_demo.py --list` | Lists saved replays (model, decisions, fills, audit result) and exits. |
| `python run_ml_demo.py --run run1` | Shows a specific saved replay. |
| `python run_ml_demo.py --model b0_training_prior` | Replays with the research baseline: 40 NO TRADE decisions (no demonstrated edge, so no trade). |
| `python run_ml_demo.py --no-serve` | Runs the replay and prints the summary only. |
| `python run_ml_demo.py --state-dir some\folder` | Keeps the main dashboard's virtual account in that folder instead of `data/paper_state` (use it to try trading without touching your saved account). |
| `--port N`, `--days N` (5 to 250) | Change the port or the replay length. |

Replays are folders `data/ml_replay/run1`, `run2`, ... (git-ignored). A replay is written to `<run>.partial` and renamed only when it is complete, so an interrupted run never leaves a half result; the `.partial` folder is not a result, nothing reads it, and you delete it yourself before running again. A replay saved by an older version of the program is kept but shown as "made by another version".

## Navigating

* **Top bar, "Main dashboard" / "ML replay"** switches pages. On the main page the chip "ML replay (invented data)" opens the ML page.
* **Main dashboard** (Synthetic Mode): generated quotes, a chart of SIMULATED HISTORY, an order form for virtual trades, your virtual cash, open positions, the daily risk budget, refusal reasons and recent activity. Real-Time Mode needs provider access and stays off: no provider is approved.
* **ML replay page**, top to bottom: *What am I looking at?*; *Market and data-source status* (replay vs synthetic vs provider); *Saved replays*; *System health and current account*; *Predictions and decisions* with a recorded explanation per day; *Risk limits and refusals*; *Model performance*; *Portfolio history*; *Simulated trades*; *Simulator assumptions*; *Limitations*.
* `/ml?run=run2` shows a specific replay; `/api/ml` and `/api/ledger` give the same data as JSON.

## What to try

1. On `/ml`, read the first two decisions: NO TRADE, "INSUFFICIENT_HISTORY" (a model never guesses without data).
2. Find a BUY, then the SELL that closes it, in *Simulated trades*; compare the quote bid/ask with the fill (touch plus one tick, with a fee).
3. Find a decision marked "REFUSED at ENGINE: RISK_DAILY_LOSS": the risk limit refused it, and it is absent from *Simulated trades*.
4. Under *System health*, see that only `demo_synthetic_ur1` may trade; the research models cannot (their standing is NONE and B1/B2 have no parameters).
5. On the main page, place a virtual trade, then try 9 lots to see a refusal reason. Submitting the same form twice trades once.

## Start errors and what they mean

| Message | Meaning |
|---|---|
| `Python 3.12 or newer is required` | Use a newer Python. |
| `cannot listen on 127.0.0.1:PORT` | The port is in use: add `--port 8800`. |
| `PARTIAL_EXISTS` | An interrupted run left `<run>.partial`; inspect or delete it, then run again. |
| `DESTINATION_EXISTS` | The folder already has files; the program never overwrites. |
| `BAD_DAYS` / `UNKNOWN_MODEL` | Fix the argument; the message lists valid values. |

Every number comes from invented data and says nothing about any market or any model's skill. More: `docs/ML_INTEGRATION.md` (architecture, data flow), `docs/V6-0.4_RECORD.md` (what was audited and fixed).

## Five-minute presentation script

1. (30 s) Say the one-line truth: a simulation-only paper-trading platform; invented data; no real orders; the research found no edge.
2. (60 s) Main page: banner, synthetic status, place a BUY, try 9 lots to show a refusal reason, show cash, position and the risk bar.
3. (90 s) `/ml`: data-kind panel, a decision with its probability and explanation, then a `REFUSED at ENGINE: RISK_DAILY_LOSS` row.
4. (60 s) Performance section: point at the research record (no model beat the prior) and say the replay numbers are from invented data.
5. (30 s) Stop the app, start it again, show the account and ledger are still there (use `--state-dir` with a temporary folder if you do not want to touch your saved account).
6. (30 s) Tests: `python -B -W error::ResourceWarning -m unittest discover -s tests -t .` (904 tests, 5 skipped without Node.js).

Expected behaviour on a fresh clone: the first run creates `data/ml_replay/run1` (40 decisions, 16 fills, audit CONSISTENT / FULL for the demo model); the same command again shows it unchanged.
