# Replay reconciliation and audit report (read-only, fixture replays)

Date: 2026-10-27. Builds on `docs/HISTORICAL_REPLAY_PHASE3.md`. Invented fixtures only: no NSE or broker data, no credentials, no network, no registry source enabled, no strategy. Nothing is committed.

**What a clean report means.** The supplied files agree with each other and with the stated rules. It does **not** prove that the files, the fixtures or the quotes they describe are genuine: whoever can write all the files consistently passes every check (a test shows a coherent set passing). It is a consistency check, not an authenticity check.

## 1. Use

```
python tools/replay_audit.py --events events.jsonl --state replay_<source>.jsonl [--fixtures DIR] [--out report.json] [--text]
```

Library: `niftypaper.historical.audit.build_report(AuditConfig(events_path, state_path, fixture_dir=None, fee_per_trade=None, fee_fraction=None, mark_max_age=None))` returns a plain dict; `render_text(report)` gives a short table. Exit codes: 0 `CONSISTENT`, 1 `INCONSISTENT`, 2 `NOT_RECONCILABLE` or unusable arguments. `--out` writes a new file only (never overwrites, never over or inside an input).

Events file: `ReplayRunner.write_events(path)` adds a final `CHECKPOINT` and writes a **new** file. The runner's events now also carry what the audit needs: the configuration echo and the engine's cash and positions at open (`REPLAY_OPENED`), `INTENT_RECEIVED` before every decision, and the engine's account claims (`cash`, `positions`, `realized_total`) on every fill, refusal and checkpoint. The audit treats all of these as claims to verify, not as truth.

## 2. Read-only and isolated

The audit reads each input with a plain read. It never opens anything for writing, takes no lock on the state file (a replay that is still running can be audited, and keeps its lock), creates no lock file, and repairs nothing. It does not import the engine, the state store, the attestation, any provider or dashboard module; a static test enforces that. A state file whose account is not `replay_<source>` (synthetic, provider, anything else) is refused (`S2`, blocked). The report has no generation time, machine path or random value, and its `digest` is the SHA-256 of the canonical JSON of everything else, so identical inputs give an identical report and digest.

## 3. Report format (JSON, `report_version` 1)

Top level: `verdict` (`CONSISTENT`, `INCONSISTENT`, `NOT_RECONCILABLE`), `coverage` (`FULL`, or `PARTIAL` when a check could not run), `notice`, `inputs` (SHA-256 and size of each file, the fixture manifest hash and re-ingest digest), `account`, `summary` (rebuilt cash, realised total, fees, open positions, decision counts), `checks`, `exclusions`, `rows`, `digest`.

**Unavailable is never zero.** A value that cannot be known is `{"unavailable": "<REASON>"}` (for example `NOT_A_FILL`, `NO_QUOTE_FOR_THIS_EVENT`, `RECONCILIATION_BLOCKED`, `FIXTURES_NOT_VERIFIED`) and open P&L is `{"status": "UNAVAILABLE", "reason": ...}`. A real zero is the string `"0.00"` (open P&L with no open positions says so in a `note`).

One row per event, in file order, with: `seq`, `n`, `clock`, `type`, `source_id`, `instrument`, `intent` (contract, side, lots), `decision` (outcome, stage, code), `quote` (reference, bid, ask, observation time, from the saved trade), `fill` (trade number, side, lots, lot size, quantity, fill price, turnover, fee), `assumptions` (slippage per unit and in ticks, fee model, fill basis, stored assumptions), `cash_before`, `cash_after`, `positions_before`, `positions_after` (quantity, premium basis, buy fees, lot size), `realized_pnl` (this trade; cumulative), `unrealized_pnl`, `reason` (the refusal code or detail, or the open note). `exclusions` lists every record or file excluded at ingestion with reason, source file, row and the other copy, taken from a re-ingestion of the fixtures.

## 4. Reconciliation: what is recomputed and the equations

The account is rebuilt from the saved trade records in sequence, starting from the state header's starting balance, with the audit's own code. Money is in rupees to the paisa, rounding half up.

| Quantity | Rule |
|---|---|
| Quantity | `quantity = lots x lot_size`, above zero |
| Turnover | `turnover = round(fill_price x quantity, 0.01)` |
| Fee | `fee = round(F + f x turnover, 0.01)`, `F` and `f` from the replay's stated fee model (or the audit's override) |
| Fill price | BUY: `ask + slippage`; SELL: `bid - slippage`; `slippage > 0`; `0 < bid < ask` on the stored quote |
| Cash | BUY: `cash -= turnover + fee`; SELL: `cash += turnover - fee`. So `ending cash = starting balance - sum(buy turnover + fee) + sum(sell turnover - fee)`; every fee appears once, in exactly one cash movement |
| Position | BUY adds quantity, `basis += turnover`, `buy_fees += fee`; SELL may not exceed the held quantity and the lot size must match (no short selling) |
| Exit split | full exit: the exact remainder of basis and buy fees; partial exit: `basis_out = round(basis x q/held, 0.01)`, `fees_out` likewise (the same convention the engine documents) |
| Realised P&L | `realised = sell turnover - basis_out - fees_out - sell fee`; a BUY stores none |
| Cost-basis conservation | per position lifecycle, `sum(buy turnover) = sum(basis_out) + remaining basis`, and the same for buy fees (an internal invariant of the rebuild; input tampering cannot reach it, so it guards the audit's own arithmetic rather than detecting edits) |
| Open P&L | `bid x quantity - basis` for each open position, using the newest quote that was available and observed at or before the event clock, fill-eligible, from a fixture-origin file, and no older than the quote-age limit stated at open (default 5 s). Indicative: before exit slippage and exit fees. If any open position lacks such a mark the total is unavailable and the reason names each position (`NO_VISIBLE_QUOTE`, `NEWEST_QUOTE_NOT_USABLE`, `MARK_STALE`) |

Checks (`checks` in the report; each is `PASS`, `FAIL`, `BLOCKED`, `NOT_CHECKED` or `SKIPPED`):

- `E1` event file present, complete lines, valid JSON objects; `E2` event numbers `1..N` (a gap, duplicate or swap is blocking); `E3` clocks aware and never backwards, one `REPLAY_OPENED` first, records announced only at or after their `available_at`; `E4` every intent has exactly one decision and every decision its intent; `E5` events end with a checkpoint and account for every saved trade (catches a file cut at a line boundary).
- `PL1` the intent plan the replay claims equals the ordered intents the events record (section 11).
- `P1` the state belongs to the same replay account and input binding as the events; `I1` the claimed inputs are consistent, equal the fixtures supplied and were unchanged at completion (section 10).
- `S1` state file present, complete, header and trade records valid; `S2` account is `replay_<source>` and every trade is a historical replay trade of that source; `S3` trade numbers `1..N` and times never backwards; `S4` the arithmetic above per trade.
- `A1` engine cash claims (open, every fill, every refusal, every checkpoint) equal the rebuild; `A2` position claims equal the rebuild, no negative or short position, one lot size; `A3` each fee equals the fee model and is charged once; `A4` cost basis conserved; `A5` stored realised P&L and the engine's realised total equal the rebuild; `A6` a refused intent leaves cash and positions unchanged and has no trade.
- `X1` each `FILL` event matches exactly one saved trade (every field, and the clock equals the trade's time) and every saved trade has a `FILL`; `X2` events, state header, trades and fixture manifest name the same source.
- `F1` fixtures verify against their manifest, re-ingest to the digest recorded at open, every announced record exists with the stated attributes, every record available by the last event was announced, and each fill's quote reference is a fill-eligible fixture quote of this source that was available at the fill's clock, matches the quote stored with the trade, was the newest visible quote for that instrument, and has slippage in whole ticks; `M1` open P&L marks (above). Both are `NOT_CHECKED` if no fixture folder is given.

`BLOCKED` means the inputs cannot be trusted enough to reconcile (verdict `NOT_RECONCILABLE`); dependent checks are `SKIPPED` and every accounting column in the rows is `{"unavailable": "RECONCILIATION_BLOCKED"}`. `FAIL` means they could be read but disagree (verdict `INCONSISTENT`). Nothing is repaired, skipped silently or defaulted to make a record fit.

## 5. Tests (`tests/test_historical_audit.py`, 43 tests)

Success with hand-computed figures (fees, partial and final exits, cost basis, realised P&L); every required row column; unavailable versus zero; the no-fixtures case; exclusions listing; restart; an engine that is consistently wrong (extra rupee of realised P&L, inflated fee, double-charged buy fee) caught although its own balances agree with themselves; mismatches (realised P&L, fee, turnover, quote, cash claim, position claim, realised total, fill price, refusal claim, extra saved trade, missing trade, cut-off events, older/early quote reference, ingest digest, fee model, source identity, changed fixtures, modified or missing announcements); corruption (gap, duplicate, swapped, truncated, blank, junk, empty, non-object and duplicate-key event lines; backwards, naive or unknown clocks; the same for state lines; missing files; other-account state); read-only behaviour (bytes, mtimes, no new files, a live replay audited without disturbing its lock); determinism (repeat, separate run, moved files, digest changes with any byte); independence from the engine; the command line. Every audit rule was mutated once and the tests caught all but the cost-basis conservation line, which is an internal invariant (see above).

## 6. Limitations

- Consistency only, not authenticity (above). Files written together by the same hand pass.
- Event lines are hash-chained (section 8), which finds edits, insertions, deletions, duplicates and reorders inside the file, but not a rebuilt chain or a cut at an event boundary without a recorded head. Beyond the chain, tampering is found through disagreement with another file: a modified `RECORD_AVAILABLE` line is invisible without the fixtures (the report is then `PARTIAL`, a test shows it); a deleted event is caught by sequence numbers, the final checkpoint or the fixtures, but an edit that keeps every cross-reference consistent is not.
- The fee model and quote-age limit are read from the replay's own `REPLAY_OPENED` event unless overridden; a tampered event can state a different model, which `A3` then compares against the saved fees but cannot verify against anything external.
- One event file is audited against the state file's trades up to its last checkpoint; events from an earlier run audited against a later state are reported as inconsistent, not merged.
- Open P&L needs the fixtures; it is indicative, at the bid, and ignores exit slippage and fees (the engine's risk report is more conservative).
- Day-level and candle records are shown only as announcements; they carry no marks.

## 8. Hash-chained event log (chain version "1")

**Format.** Every event line is one JSON object: the event's own fields plus `chain` (the string `"1"`), `prev` and `hash`. The sequence number is the existing field `n` (1, 2, 3, ...). Every event is chained, whatever its type, including `INTENT_RECEIVED`, `RECORD_AVAILABLE`, `FILL`, `INTENT_REFUSED`, `CHECKPOINT` and types this version does not know. Decimals, times and durations travel as strings (the one float, the quote-age limit, is now the string `"5.0"`); the writer refuses floats and any value it cannot canonicalise, and an event that cannot be chained is never recorded.

**Canonical serialization.** The object with keys sorted, separators `,` and `:` with no spaces, ASCII only (non-ASCII escaped as `\uXXXX`), no NaN or infinity, and a single `\n` ending the line. The log file is exactly these lines. Nothing path-dependent and no generation time is included.

**Hash.** `hash = SHA-256( "niftypaper.replay.chain.event/1\n" + canonical(event without its "hash" field) )`, lower-case hex. It covers every field of the line except `hash` itself (so `chain`, `n`, `prev` and all payload fields), and never includes itself. `prev` is the previous line's `hash`.

**Genesis.** The first event's `prev` is `GENESIS = SHA-256("niftypaper.replay.chain.genesis/1")` = `f610a30490733bbcf28fc1ba8f5dbc0c0adc9c45317def914e99e35e75d9cd32`. A known-answer test pins the genesis value and one event hash.

**Independent verification.** `audit.verify_chain(bytes, expected_head=None)` is written from this specification and does not import the writer (a test checks that, and the known-answer vector ties the two together). It examines raw lines in order and reports the first failure with its line and sequence number, the length of the valid prefix and that prefix's head: empty file; blank, non-JSON, non-object, duplicate-key or NaN lines; chain fields missing on some lines; unsupported or malformed version (treated as not verified, so `INVALID`); a malformed or non-consecutive sequence number (first not 1, gap, duplicate, reorder); malformed hashes; any line that is not in the canonical serialization (spacing, key order, escapes, CRLF, BOM, floats); a wrong genesis; a broken `prev` link; a hash that does not recompute (modified line); a final line with no line ending (truncated). A log with no chain fields at all is `UNAVAILABLE` (`NO_CHAIN_FIELDS`), as is a missing file; neither is called valid.

**Reported separately.** The report has a new top-level `chain` section: `status` (`VALID`, `INVALID`, `UNAVAILABLE`), `reason`, `detail`, `first_failure` (`line`, `n`), `events_verified`, `head`, `anchored`, `genesis`, `version`, `notice`. It never changes `verdict` (`CONSISTENT`, `INCONSISTENT`, `NOT_RECONCILABLE` keep their meanings), the `checks` table or the exit codes. All four combinations occur and are tested: chain valid and accounting consistent; chain valid but accounting wrong (an attacker re-chained an edited log); accounting consistent but chain invalid; neither. The CLI gains `--expected-head HASH` and `--require-valid-chain` (exit 3 only if the accounting verdict is `CONSISTENT` and the chain is not `VALID`); without the flag the exit codes are unchanged.

**What the chain does not do.** It is tamper evidence, not proof of authenticity. A valid chain does not show that the events are genuine or that the replay ran as described. Anyone who can write the file can recompute the whole chain from an edit onward and produce a valid one (a test does this and it passes). A log cut cleanly at an event boundary is still a valid chain; only a head hash recorded somewhere the writer cannot change (`receipt.head`, then `--expected-head`) detects that, and the accounting check `E5` (no final checkpoint) catches many cuts but not all. The chain covers the event file only, not the state file or the fixtures.

**Writing the log.** `ReplayRunner.write_events(path)` is still new-file-only and now returns an `EventLogReceipt(path, events, head, sha256, bytes)` or raises `EventLogError` (`DESTINATION_EXISTS`, `PARTIAL_FILE_EXISTS`, `CANNOT_CREATE`, `WRITE_FAILED`, `READBACK_MISMATCH`, `CHAIN_NOT_VALID`, `PUBLISH_UNSUPPORTED`). A replay is complete only when it has returned a receipt; `runner.log_receipt` stays `None` after any failure. Steps: add a final checkpoint (not again if the last event is already one at the same clock, so a retry adds nothing), write a temporary file `.<name>.<pid>.partial` in the same folder created exclusively, flush and `fsync`, read it back and compare, chain-verify it with the audit's verifier against the in-memory head and event count, then publish with a hard link, which fails if the destination exists. The temporary file is removed on every path; an existing destination or a foreign partial file is never touched. Platform notes: on Windows (NTFS) hard links work and the tests exercise them; if they are unavailable there the fallback is `os.rename`, which on Windows refuses an existing destination (tested with a simulated late winner); on other systems without hard links the write is refused (`PUBLISH_UNSUPPORTED`) because a plain rename could replace a log. The publish is atomic on local NTFS and POSIX file systems; on POSIX the folder is flushed afterwards, on Windows that is not possible, so a power loss just after a reported success can still lose the file. Network file systems and unusual file systems are not covered.

**Read-only audit.** `verify_chain` takes bytes, and `build_report` only reads: it takes no lock on the state file and never repairs, truncates or rewrites a damaged log.

## 9. Trust anchors for event-log head hashes

The hash chain finds edits inside a log. It cannot see a log cut cleanly at an event boundary, or a log whose whole chain was rebuilt after an edit. An **anchor** records what a finished log looked like (head hash, event count, size, SHA-256, replay identity) so a later audit can compare the log with it. How much that comparison is worth depends entirely on where the anchor is kept.

### 9.1 Trust model: three cases, never confused

| Case | What it is | What a MATCH means |
|---|---|---|
| A | A head hash given directly with `--expected-head` | Trustworthy only if you got the value from a source the log's writer cannot change (a note written down when the replay finished, a value kept by someone else). The audit cannot tell where it came from. |
| B | An anchor file kept anywhere the same user or process can write, including next to the log or in the same account's folders | A consistency check and a convenience. **Not an independent trust anchor**: whoever can rewrite the log can rewrite this file to match (a test does exactly that and the audit reports MATCH, labelled `LOCAL_OR_UNDECLARED`). |
| C | A copy held where the log's writer cannot modify it: a medium or share that is read-only to that writer, another account with its own permissions, a hardware write-protected USB stick, a value recorded on paper or held by another person | If the declaration is true, the log is unchanged since the anchor was made. This program cannot create such a place, cannot see who can write to a path, and does not assume one exists. |

The audit never promotes B to C. Protection is reported as `LOCAL_OR_UNDECLARED` unless you pass `--anchor-trust independent`, and then as `DECLARED_INDEPENDENT`: your declaration, not something the audit checked. Even a declared-independent anchor is downgraded back to `LOCAL_OR_UNDECLARED` (with `downgraded: true`) when the file sits in the same folder tree as the event log or the replay state. Marking a file read-only (`attrib +R`) does not make it independent: the same user can undo that. No check here shows that the events or quotes are genuine; `authenticity` is always `NOT_ESTABLISHED`.

### 9.2 Anchor record (version "1")

One canonical line (keys sorted, compact separators, ASCII, no floats, one line ending), exactly these fields: `anchor` ("1"), `kind` (`niftypaper.replay.event_log_anchor`), `notice` (fixed text saying a local copy is not an independent anchor), `source_id`, `account` (`replay_<source_id>`), `chain` ("1"), `genesis`, `events` (count), `head` (hash of the last event), `log_sha256` and `log_bytes` (the whole event file), `first_clock`, `last_clock`, `trades` (from the final checkpoint) and `final_event` ("CHECKPOINT"). Nothing else: no paths, generation time, user name or secrets, so identical logs give identical anchor bytes. Parsing is strict: unsupported or malformed version, unknown or missing fields, wrong types, malformed hashes, bad counts or clocks, a wrong genesis and any non-canonical serialization are refused (`MALFORMED_ANCHOR`, `UNSUPPORTED_VERSION`, `MALFORMED_VERSION`, `NON_CANONICAL_ANCHOR`, `EMPTY_ANCHOR`).

### 9.3 Creating and comparing

Creating is a separate, explicit operation (`tools/replay_anchor.py create`; it makes the destination folder if it is missing, after its checks pass). Auditing never creates, rewrites or repairs an anchor or a log. Creation first verifies the event chain and refuses (exit 2, reason printed) a missing, invalid or unreadable log (`LOG_NOT_VALID`), a valid chain that is unfinished or lacks its opening event or final checkpoint (`LOG_INCOMPLETE`), a log whose source differs from the replay account's (`SOURCE_MISMATCH`), an unreadable or non-replay state file (`STATE_UNREADABLE`, `NOT_A_REPLAY_ACCOUNT`), a destination in the same folder tree as the log or the state, above or below it (`ANCHOR_BESIDE_LOG`), and an existing destination (`DESTINATION_EXISTS`; the same temporary-file, read-back and no-replace publishing as the event log, now in `historical/publish.py`).

The audit compares field by field (`source_id`, `events`, `head`, `log_sha256`, `log_bytes`, `first_clock`, `last_clock`) and reports `anchor.status`: `MATCH`, `MISMATCH` (with the differing fields, or `CHAIN_NOT_VALID` when the chain is broken), `INVALID_ANCHOR` (with the parse reason), `UNAVAILABLE` (anchor file or log missing) or `NOT_SUPPLIED`. `--expected-head` keeps its behaviour (the chain section's `anchored` and `HEAD_MISMATCH`) and is also shown in the anchor section; with both given, both must match. A valid comparison with an independently protected anchor detects a clean-boundary cut (events, head, size and digest differ) and a rebuilt chain (head and digest differ). The report's new `trust` block lists four separate answers, never one boolean: `accounting` (the verdict), `chain_integrity`, `anchor_consistency` with `anchor_protection`, and `authenticity` (`NOT_ESTABLISHED`). Verdicts, checks and default exit codes are unchanged; `--require-anchor` exits 3 unless the anchor comparison is a `MATCH` (when the accounting exit would be 0 or 1).

### 9.4 Windows-friendly workflow (no cloud, subscription or administrator rights)

1. Run a synthetic replay from a script of your own with `ReplayRunner` on the invented fixtures; keep the state folder and the event log in one working folder, for example `C:\replay\run1`.
2. Save the event log: `receipt = runner.write_events(r"C:\replay\run1\events.jsonl")`. Write down `receipt.head` now (paper, a note someone else holds, or a message to yourself that you cannot edit later).
3. Verify the chain and the books:
   `python tools\replay_audit.py --events C:\replay\run1\events.jsonl --state C:\replay\run1\state\replay_synthetic_fixture.jsonl --fixtures tests\fixtures\historical_synthetic --text`
4. Create the anchor explicitly, in a folder that is not the run folder (it is refused there):
   `python tools\replay_anchor.py create --events C:\replay\run1\events.jsonl --state C:\replay\run1\state\replay_synthetic_fixture.jsonl --out C:\replay\anchors\run1.anchor.json`
   The tool prints that this copy is LOCAL.
5. Copy `run1.anchor.json` (and the head hash) to a place the replay writer cannot change: a USB stick with a hardware write-protect switch, another Windows user's folder that only that user can write, or a printout. Then lock the working copy away from the run folder if you like, but remember that does not make it independent.
6. Audit later with the preserved copy:
   `python tools\replay_audit.py --events ... --state ... --fixtures ... --anchor E:\run1.anchor.json --anchor-trust independent --require-anchor --text`
   Use `--anchor-trust independent` only if step 5 is true. Without it the result is labelled as a local consistency check.
7. Read failures; nothing is repaired automatically. `MISMATCH` with `FIELDS_DIFFER`: the log is not the one that was anchored (cut, replaced or edited), or you are comparing the wrong anchor (check `source_id`, `events`). `CHAIN_NOT_VALID`: the log is damaged inside; the report gives the first bad line and the length of the valid prefix. `INVALID_ANCHOR`: the anchor file itself is damaged or from another version; use another copy. A mismatch does not say who changed what, and a match does not say the events were genuine. Keep the damaged files as evidence; make a fresh replay rather than editing logs.

### 9.5 Limits

- Anyone who can write both the log and the anchor (or the head you compare with) defeats this; the program cannot enforce file permissions or tell where a copy lives.
- The location test (`ANCHOR_BESIDE_LOG`, `beside_log_or_state`) is a path check only: a network share, a junction or a mapped drive can still be writable by the same user.
- An anchor is made once, after the replay; it does not protect the replay while it is running, and a log with no anchor is as trustworthy as before.
- The anchor says nothing about the state file, the fixtures, or whether the invented data resembles any market.

### 9.6 One-command demonstration: `tools/replay_fixture_demo.py`

Steps 1 to 3 of the workflow above (run, save, verify) are one command on the invented fixtures (`tests/fixtures/historical_synthetic`, source `synthetic_fixture`):

```
python tools\replay_fixture_demo.py
python tools\replay_fixture_demo.py --out-dir C:\replay\run2
```

It runs a fixed scripted replay (cut-off, clock, script and fixtures are constants, so there is no strategy and nothing volatile), publishes `events.jsonl` with the same never-overwrite writer, and audits what it wrote with the read-only audit. The default folder is `data\replay_demo\run1` under the repository (git-ignored, away from the research folders); inside it are `events.jsonl` and `state\replay_synthetic_fixture.jsonl`. Runs written to separate folders have identical event bytes, head hash and audit digest. It never overwrites or deletes: if `events.jsonl` or a non-empty `state` folder already exists it stops and suggests another `--out-dir`.

It prints a success summary only if all of these held: the replay finished; the log was published; the chain is `VALID`; the accounting verdict is `CONSISTENT` with `FULL` coverage; the report names account `replay_synthetic_fixture` and source `synthetic_fixture`; and the chain's event count, head hash and the file's SHA-256 and size equal the writer's receipt. The summary lists source, event count, head hash and log path, then copyable PowerShell lines (`Set-Location`, the variables `$Events`, `$State`, `$Fixtures`, `$Head`, `$Anchor`) and four commands: create the anchor, show it, audit against it, and the strict audit against a protected copy (`--anchor-trust independent --expected-head $Head --require-valid-chain --require-anchor`). The summary says plainly that the anchor is LOCAL until you copy it somewhere the replay writer cannot modify. The script does not create an anchor: that stays your deliberate step, and the suggested anchor folder (`<run folder>_anchor`) is chosen apart from the log and the state so the create command is accepted. The created anchor folder is made by `replay_anchor.py create` itself, after its checks pass.

Exit codes: 0 everything passed; 2 refused before doing anything (existing output, a folder that is a file or the fixture folder, an unexpected fixture source); 3 the replay or the write failed; 4 the log was written but a check failed. On 3 and 4 the files are left exactly as they are, nothing is repaired, and neither a success message nor any anchor command is printed.

What a success shows: the invented replay ran, its log was published without replacing anything, the hash chain is intact, the books rebuild from the saved trades, and the log matches its writer's receipt. What it does not show: that any event is genuine, or that nobody who can rewrite every file changed them; for that you need a protected copy of the anchor or head (9.1), and even that shows only that the log is unchanged since it was made. The replay state folder keeps a `.lock` file beside the saved trades; it is the single-writer lock's marker and is harmless.

Run the whole test suite with `python -B -W error::ResourceWarning -m unittest discover -s tests -t .` and the safety scan with `$env:PYTHONPATH = "src"; python -B -m niftypaper.safety.scan --root . --include-untracked`.

## 10. Input identity, change detection and event/state/fixture pairing

### 10.1 Input identity (identity version "1")

The first event (`REPLAY_OPENED`) carries an `inputs` claim built by `historical/identity.py` from values the runner already holds in memory, in the canonical serialization of section 8 (no floats, no paths, no times of writing, nothing the manifest does not list):

- `inputs`: `manifest_sha256` (SHA-256 of the manifest file's bytes), `source_id`, `account` (`replay_<source_id>`) and `fixtures`, one object per manifest line in path order (`path` relative, `sha256`, `bytes`, `source_id`, `record_type`, `origin_class`, `origin`, `retrieved_at_utc`, `terms_url`, `terms_checked_on`);
- `accounting`: everything that changes what the replay does with them: ingest cut-off, starting balance, permission flag, slippage, quote-age limit, fee model, every risk limit, allowed origin classes, and the parser, schema, policy and chain versions;
- `window`: start and end instants.

`digest = SHA-256("niftypaper.replay.inputs/1\n" + canonical({version, inputs, accounting, window}))` is the whole claim. `account_binding = SHA-256("niftypaper.replay.account-binding/1\n" + canonical({version, inputs, accounting}))` leaves out the window, so a restart on the same inputs and settings but a later window has the same binding. The binding is written into the replay state file's header; a state file whose binding differs (other fixtures, another cut-off, slippage, limits, fee model, balance, quote-age limit, permission) is refused when a replay opens (`DIFFERENT_ACCOUNT`: the account is locked, nothing is mixed, the file is not touched). A known-answer test pins both hashes for a small made-up input. The audit report exposes the claim in `inputs_identity.claimed` (digest, binding, manifest hash, source, account, window and each fixture's path, hash, size and record type) so a reviewer can see which inputs the run says it used.

### 10.2 Integrity policy (what is and is not guaranteed)

1. **Before the replay.** The manifest is read once; those exact bytes are hashed and parsed. Every listed fixture is read once, checked against the manifest and parsed. Any changed, missing or unlisted file, or an invalid manifest, refuses the open (`INTEGRITY_FAILURE`, `INVALID_MANIFEST`) before anything is created.
2. **During the replay.** The replay runs only on the records parsed from the bytes it validated (an in-memory snapshot; the records are immutable). A fixture edited, deleted or added while the replay runs therefore cannot change what the replay does or mix into it. It is also not noticed until the next recheck.
3. **At completion.** `write_events` calls `recheck_inputs()`: the manifest and every listed fixture are re-read and re-hashed against the open-time snapshot, and unlisted files are reported. The result is an `INPUTS_RECHECKED` event (status `UNCHANGED` or `CHANGED`, the differences, the identity digest) inside the hash chain, followed by the final checkpoint. `recheck_inputs()` can also be called at any point. If anything differs, the log is still published as evidence, but `write_events` raises `InputsChanged` (carrying the receipt and the differences), `runner.log_receipt` stays `None` and `runner.evidence_receipt` holds the published log: the run is never reported as completed. Existing logs, state and the changed inputs are left exactly as they are; nothing is repaired or deleted.
4. **Limit.** The start and end checks detect many changes. They cannot prove an input was never changed and then put back between them: a test shows a change reverted before the recheck passes. Nor do they show who changed anything or that the original inputs were genuine.

### 10.3 Pairing the event log, the state and the fixtures

Two new checks in the report (all other checks, verdict meanings and exit codes are unchanged):

- `P1_PAIRING`: the state header's `account` must equal the account the events claim, and its `binding` must equal the claimed `account_binding`. A state of another source, another account, other inputs or other settings, or one with no binding, makes this check `BLOCKED`: the verdict is `NOT_RECONCILABLE`, no accounting is attempted against it, and every balance column reads `{"unavailable": "RECONCILIATION_BLOCKED"}` (never zero, never inferred, never merged). Events that carry no input identity (written before it existed) give `NOT_CHECKED` and `PARTIAL` coverage, not a failure. The report's `pairing` section lists both sides.
- `I1_INPUT_IDENTITY`: the claim must agree with the replay's own stated configuration and source; with a fixture folder supplied, the identity recomputed from it (manifest bytes, each listed fixture) must equal the claim and every listed file must still match its manifest line (`FAIL` otherwise, naming the difference); the completion recheck must be recorded and `UNCHANGED` (`CHANGED` is a `FAIL`; an absent recheck, as in an unfinished log, is `NOT_CHECKED`). The report's `inputs_identity` section gives `claimed`, `recomputed`, `differences`, `completion_recheck` and a status (`MATCH`, `MISMATCH`, `INCONSISTENT_CLAIM`, `NOT_CHECKED`). Without fixtures the claim is shown but not compared.

The binding covers inputs and settings, not the scripted intents (section 11): a state from a different script run on the same inputs pairs, and is then caught by the event-to-trade match (`X1`) and the completeness check (`E5`). Chain, anchor, accounting, pairing, identity and authenticity remain separate answers; a chain-valid log can be paired with the wrong state, and a correctly paired, chain-valid log can still carry wrong accounting claims (tested).

### 10.4 Demonstration script

`tools/replay_fixture_demo.py` prints the input identity digest and fixture count, and reports success only if the audit also finds `inputs_identity` = `MATCH`, `pairing` = `PAIRED`, the claimed digest equal to the one the replay reported, and the completion recheck `UNCHANGED`. Fixtures changed while it runs end in exit code 4 with the log kept as evidence and no success message or commands. It still never creates an anchor.

### 10.5 Limits

- Everything above is consistency, not authenticity: whoever can write the manifest, the fixtures, the log and the state together can make them agree.
- Fixture identity is the manifest's description plus the hashes of the listed files; a fixture folder that was wrong from the start is not detected.
- The recheck reads the files again; it does not lock them.
- Old logs and state files without this identity still audit (partial coverage); an old state file without a binding is refused by a new replay.

## 11. Intent-plan identity

### 11.1 What the plan is

`ReplayRunner.open(config, intents=script, until=None)` declares the exact scripted intents up front. `historical/identity.py` turns them into a canonical plan, in the serialization of section 8 (no floats, no paths, no times of writing):

- `intents`: in **execution order** (a stable sort by instant, exactly as `run()` orders its script). Each is `{at, contract, side, lots}`, `at` in UTC ISO-8601 so two spellings of one instant give one digest. These four fields are every field of an `Intent`; nothing else can change how an intent is read, executed or refused.
- `until`: the explicit end of the script, or the last intent's instant (`null` for an empty script).
- `digest = SHA-256("niftypaper.replay.plan/1\n" + canonical({version, intents, until}))`.

List order matters only between intents at the same instant (that order decides which runs first); reordering the list across different instants does not change what happens and does not change the digest. Changing a field, the instants' order, the number of intents or `until` does. A known-answer test pins the format, with the value computed independently of the code.

### 11.2 Where it is recorded, and how the contract is split

- The plan (all intents and its digest) is part of the `inputs` claim in the first event, and the **full digest** covers it. A claim without a plan hashes exactly as before plans existed, so older logs verify unchanged.
- The **account binding** is unchanged and does **not** cover the plan. The binding decides whether a saved account may continue; a continuation that uses a different script (a later window, a new experiment on the same inputs) is legitimate, and putting the plan in the binding would refuse it. This keeps state pairing exactly as strict as before, not more permissive. The cost is stated plainly: a state file produced under another plan pairs with these events; it is caught by the trade-level checks (`X1`, `E5`), not by pairing (tested).
- With a declared plan `run()` accepts only that script (`PLAN_MISMATCH` otherwise) and only once (`PLAN_ALREADY_RUN`). Intents or `until` outside the window, or malformed intents, are refused at open (`PLAN_OUTSIDE_WINDOW`, `BAD_INTENT`) before any file is created. `try_trade` can still be called directly; the audit then sees an intent the plan does not contain.

### 11.3 What the audit checks (`PL1_PLAN_IDENTITY`, reported under `inputs_identity.plan`)

The claim is not trusted on its own. The audit recomputes the plan digest from the claimed intents, checks they are in time order and inside the claimed window, and then walks the events: the n-th `INTENT_RECEIVED` must be the plan's n-th intent at the plan's instant with the same contract, side and lots; an intent beyond the plan (or one the runner could not read) fails; a finished log that recorded fewer intents than planned fails; an event later than `until` fails. `I1` additionally recomputes the full digest and the binding from the claim's own parts, so a claim that contradicts itself fails even without fixtures.

Statuses: `MATCH`, `MISMATCH`, `MALFORMED`, `INCOMPLETE` (an unfinished log whose recorded intents match so far: `NOT_CHECKED`, never a pass), `NOT_DECLARED` (no plan claimed: a legacy log, or a run that did not declare one; `NOT_CHECKED`, `PARTIAL` coverage, never reported as matching). Chain, anchor, input identity, pairing, accounting and authenticity remain separate answers; a valid chain does not make a false plan claim acceptable (tested). The demo declares its plan and reports success only if the plan is `MATCH`.

### 11.4 What this does not show

The plan claim and the events are written by the same process into the same file. Anyone who can rewrite the log can rewrite the plan and the events together and rebuild the chain; only an independently kept head or anchor (section 9) would expose that, and even then nothing here shows the intents were ever genuine decisions of anyone. The plan identifies what was scripted; it does not show who scripted it.

## 12. Integrity review of the replay workflow

Reviewed: chain and anchors, input identity and snapshots, plan identity, event/state pairing, accounting reconstruction, legacy and incomplete logs, failure behaviour and retained evidence.

**Fixed in this milestone**
1. *An earlier changed recheck was hidden by a later clean one.* `recheck_inputs()` can be called at any time; if it found a change that was then undone, the final recheck was clean and `write_events` reported success, and the audit looked only at the last recheck. Now a change seen at any recheck makes `write_events` raise `InputsChanged` and makes `I1` fail (tested).
2. *A claim's digest was never recomputed from its own parts* unless fixtures were supplied. `I1` now recomputes the digest and binding from the claim (tested).
3. *A flaky test:* the attestation tamper test replaced the tag's last character with `0`, which left the tag unchanged one time in sixteen. It now always changes it.

**Remaining limits (documented, not fixed; each would need a larger design)**
- *Same-writer rewrite.* A writer of the log, state, fixtures and manifest can make all of them agree and rebuild the chain. Detected only by an independent head or anchor, never as authenticity.
- *A local anchor is not independent* (section 9.1); the audit reports the user's declaration, not a verified fact.
- *Start/end rechecks miss a change that is undone before the next check* (section 10.2). Only a change seen at some check counts.
- *State persists even when the run is not completed.* A replay that ends in `InputsChanged` or a failed write has already saved its trades to the state file; a later run restores them. The evidence log shows the failure, and the state is neither repaired nor removed.
- *Plan not in the binding* (11.2): a state from another plan on the same inputs pairs; trade-level checks catch it.
- *Direct `try_trade` and `advance_to` calls* are not scripted; with a declared plan the audit catches extra intents, but clock moves that trade nothing are not part of the plan.
- *Runs that do not declare a plan* (including every log written before plans existed) audit as `PARTIAL`.
- *Unfinished logs* check only their recorded prefix; nothing says whether the missing part ever existed.
- *Accounting is recomputed from the state and events the same actor may have written*; it shows internal consistency with the stated rules, not that any quote was real.

## 13. Strict "verify later": `tools/replay_verify_later.py`

A read-only wrapper around the same audit code (`historical/audit.py`; nothing is re-implemented). It creates, repairs, rewrites and deletes nothing, takes no lock and has no output option. PowerShell, paths with spaces are fine when quoted:

```
python tools/replay_verify_later.py --events "D:\my runs\run1\events.jsonl" --state "D:\my runs\run1\state\replay_synthetic_fixture.jsonl" --fixtures tests\fixtures\historical_synthetic --anchor "E:\kept apart\anchor.json" --anchor-trust independent
python tools/replay_verify_later.py ... --expected-head <64-hex head you wrote down> --anchor-trust independent --detail
```

PASS needs **all** of the following; everything else is FAIL, with the reason printed per requirement (`--detail` adds the audit's text report; no state contents are printed):

| Requirement | Met only when |
|---|---|
| PLAN | the replay declared an intent plan and the events record exactly that plan (`PL1`, plan status `MATCH`) |
| CHAIN | the event hash chain is `VALID` |
| ANCHOR | an anchor file and/or `--expected-head` was supplied, every comparison is `MATCH`, and each is declared independent and not beside the log, the state or (for an anchor file) the fixture folder |
| IDENTITY | the identity recomputed from the fixtures equals the claim and every recheck was `UNCHANGED` |
| PAIRING | the state belongs to the same account and input binding as the events |
| ACCOUNTING | verdict `CONSISTENT`, coverage `FULL`, and every check `PASS` (none `NOT_CHECKED` or `SKIPPED`) |

Also FAIL: no anchor and no head; `--anchor-trust` other than `independent` (the declaration is required, never defaulted); a malformed head; a missing input; an unexpected audit error. The last output line is exactly `PASS` or `FAIL`. Exit codes: 0 PASS; 1 a requirement failed; 2 unusable arguments or audit error (also FAIL).

Trust: `--anchor-trust independent` is only the caller's declaration. The tool cannot see filesystem permissions or where a hash came from; it refuses the declaration when the anchor sits beside the log, state or fixtures (the audit's existing rule, plus the fixture folder), and it prints that protection is unverified. A head given on the command line is taken as given. PASS shows that the supplied files agree with each other and with that head/anchor; it does not show the events or quotes were genuine, nor who made the fixtures, log or state.

Tests: `tests/test_historical_verify_later.py`.

## 14. Zero-trade runs: the initialized state file

Before this change the saved state file was created at the first accepted trade, so a valid replay in which nothing was filled had an event log and no state file, which the audit could only call `NOT_RECONCILABLE`. Now a replay account is **initialized when the replay opens**: `JsonlStore.initialize()` (used only by the replay runner, with `require_initialized=True`) creates a header-only file with an exclusive create, flushed to disk, naming the account and the input binding (section 10). It never overwrites, truncates or repairs anything, takes the same single-writer lock as before, and appends to the same file as trades arrive. Other stores (the dashboard's, synthetic accounts) behave exactly as before.

What the contract distinguishes (the store checks it at every start, the audit re-checks it from the file):

| State on disk | Meaning | Replay | Audit |
|---|---|---|---|
| header only, right account and binding | initialized account, zero trades | opens | reconciles; `account.state_kind` = `INITIALIZED_NO_TRADES` |
| header plus trades | account with trades | opens | reconciles; `INITIALIZED_WITH_TRADES` |
| no file | never initialized (or deleted) | cannot exist after open; a later load is `MISSING` | `NOT_RECONCILABLE` |
| zero bytes, truncated, corrupt, not UTF-8 | damaged | account locked (`EMPTY_FILE`, `TRUNCATED`, `CORRUPT`), file untouched, no trade | `NOT_RECONCILABLE` |
| other account, source, binding or starting balance, or a header with no account/binding | not this replay | locked (`DIFFERENT_ACCOUNT`), file untouched | `NOT_RECONCILABLE` (pairing mismatch) or `INCONSISTENT` (balance) |

Re-opening the same account with the same inputs is idempotent (the file is validated, not rewritten). A missing or empty file is never treated as an empty account for a replay.

A zero-trade run is `CONSISTENT` with `FULL` coverage only under the usual conditions: declared plan present and matching, valid chain, input identity matching, state paired, every intent/refusal/checkpoint accounted for, and cash, positions, fees and P&L rebuilt from the state (balance from the header, zero trades) agreeing with the checkpoints. A fill the state does not hold, or trades in the state the events never recorded, is `INCONSISTENT`; a wrong claim about cash or trades is `INCONSISTENT`. Zero figures are reported only because the valid state and events establish them. Strict verification (`tools/replay_verify_later.py`) therefore passes a genuinely complete zero-trade run when the independently kept anchor/head matches, and fails missing or damaged state like any other. No anchor is created automatically and a local anchor is still not independent.

Limits: a state file written before this change (created at the first trade) still has a header with account and binding and works as before; a replay run that never reached `open` has no state, as before. The header says the account existed, not that anyone watched it: whoever can write the files can write a header-only state too.

## 15. Smallest next step

Only once the NSE terms question is answered: a read-only reader for one local end-of-day layout.
