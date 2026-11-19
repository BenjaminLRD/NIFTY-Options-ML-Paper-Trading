"""Replay input identity, change detection and event/state/fixture pairing. Invented fixtures only."""
import contextlib
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from niftypaper.historical import audit, eventchain, identity, replay
from niftypaper.historical import manifest as mf
from niftypaper.paper.engine import ORIGIN_SYNTHETIC, CostModel, MarketView, PaperEngine, RiskLimits, TradeRequest
from niftypaper.paper.store import JsonlStore, StoreError
from tests import ROOT
from tests import historical_fixtures as fx
from tests.historical_fixtures import AS_OF, CE
from tests.test_historical_audit import SCRIPT
from tests.test_historical_chain import dump, lines_of
from tests.test_historical_replay import END, FIXTURE_DIR, START, STATE_FILE, at

KNOWN_ANSWER_DIGEST = "050a776405f99b2c3892e53995df5fd4d724ac6bfa30d8e79a4257411372ab1d"
KNOWN_ANSWER_BINDING = "9701852d6ceea528c97c602c5c472e20fdf67ebd6a3791e64af48270d679ff02"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.runners = []

    def fixture_copy(self, name, change=None, source_id=None):
        """A copy of the committed fixtures; `change(folder)` may edit files first; the manifest is rebuilt only if a source id is given or `rebuild` is True."""
        folder = self.tmp / ("fx_" + name)                      # never the run folder: logs and state next to fixtures would be unlisted files
        shutil.copytree(FIXTURE_DIR, folder)
        if change:
            change(folder)
        if source_id:
            files = {e.path: e.record_type for e in mf.load_manifest(folder / mf.MANIFEST_NAME)}
            (folder / mf.MANIFEST_NAME).unlink()
            fx.write_manifest(folder, files, source_id=source_id)
        return folder

    def config(self, name, fixtures=FIXTURE_DIR, **kw):
        base = dict(fixture_dir=fixtures, state_dir=self.tmp / name / "state", as_of=AS_OF, start=START, end=END, permitted=True)
        base.update(kw)
        return replay.ReplayConfig(**base)

    def open(self, name, fixtures=FIXTURE_DIR, intents=None, **kw):
        runner = replay.ReplayRunner.open(self.config(name, fixtures, **kw), intents=intents)
        self.addCleanup(runner.close)
        return runner

    def run_variant(self, name, fixtures=FIXTURE_DIR, script=SCRIPT, **kw):
        runner = self.open(name, fixtures, intents=script, **kw)
        runner.run(script)
        runner.write_events(self.tmp / name / "events.jsonl")
        runner.close()
        return type("Run", (), {"events": self.tmp / name / "events.jsonl", "state": self.tmp / name / "state" / f"replay_{runner.source_id}.jsonl"})

    def report(self, run, fixtures=FIXTURE_DIR, **kw):
        return audit.build_report(audit.AuditConfig(run.events, run.state, fixtures, **kw))

    def objs(self, run):
        return [json.loads(x) for x in lines_of(run.events.read_bytes())]

    def status(self, report, check):
        return next(c["status"] for c in report["checks"] if c["id"] == check)

    def notes(self, report, check):
        return " | ".join(next(c["notes"] for c in report["checks"] if c["id"] == check))

    def rechained(self, run, name, change):
        """The run's events with `change(list of dicts)` applied and the whole chain rebuilt (what a writer of the file could do)."""
        objs = [{k: v for k, v in o.items() if k not in eventchain.RESERVED} for o in self.objs(run)]
        change(objs)
        data, _ = dump(objs)
        path = self.tmp / f"{name}.jsonl"
        path.write_bytes(data)
        return type("Run", (), {"events": path, "state": run.state})


class TestIdentity(Base):
    def test_the_first_event_states_the_inputs(self):
        run = self.run_variant("a")
        opened = self.objs(run)[0]
        claim = opened["inputs"]
        self.assertEqual((claim["version"], set(claim)), ("1", {"version", "digest", "account_binding", "inputs", "accounting", "window", "plan"}))
        manifest_bytes = (FIXTURE_DIR / mf.MANIFEST_NAME).read_bytes()
        self.assertEqual(claim["inputs"]["manifest_sha256"], hashlib.sha256(manifest_bytes).hexdigest())
        self.assertEqual((claim["inputs"]["source_id"], claim["inputs"]["account"]), ("synthetic_fixture", "replay_synthetic_fixture"))
        entries = mf.load_manifest(FIXTURE_DIR / mf.MANIFEST_NAME)
        self.assertEqual(claim["inputs"]["fixtures"], [e.to_dict() for e in sorted(entries, key=lambda e: e.path)])
        for fixture in claim["inputs"]["fixtures"]:
            self.assertEqual(fixture["sha256"], hashlib.sha256((FIXTURE_DIR / fixture["path"]).read_bytes()).hexdigest())
        accounting = claim["accounting"]
        self.assertEqual((accounting["as_of"], accounting["starting_balance"], accounting["permitted"], accounting["slippage_ticks"], accounting["max_quote_age_seconds"],
                          accounting["fee_per_trade"], accounting["fee_turnover_fraction"], accounting["allowed_origin_classes"]),
                         (AS_OF.isoformat(), "1000000", True, 1, "5.0", "20", "0.0005", ["SYNTHETIC_FIXTURE"]))
        self.assertEqual(accounting["limits"]["max_lots_per_trade"], "2")
        self.assertEqual(claim["window"], {"start": START.isoformat(), "end": END.isoformat()})
        self.assertEqual({k: opened[k] for k in ("source_id", "as_of", "start", "end", "slippage_ticks")},
                         {"source_id": "synthetic_fixture", "as_of": accounting["as_of"], "start": claim["window"]["start"], "end": claim["window"]["end"], "slippage_ticks": 1})

    def test_no_paths_times_of_writing_or_unrelated_files_in_the_identity(self):
        run = self.run_variant("a")
        line = lines_of(run.events.read_bytes())[0]
        for text in (str(self.tmp), self.tmp.name, str(FIXTURE_DIR), str(ROOT), "\\\\", "2026-"):
            self.assertNotIn(text, line)
        other = self.run_variant("elsewhere", fixtures=self.fixture_copy("moved", source_id=None))
        self.assertEqual(self.objs(other)[0]["inputs"], self.objs(run)[0]["inputs"])                 # the same inputs at another path have the same identity

    def test_identical_inputs_give_identical_identity_and_bytes(self):
        first, second = self.run_variant("a"), self.run_variant("b")
        self.assertEqual(first.events.read_bytes(), second.events.read_bytes())
        self.assertEqual(self.objs(first)[0]["inputs"]["digest"], self.objs(second)[0]["inputs"]["digest"])

    def test_known_answer_vector_for_the_specification(self):
        entry = mf.Entry("a.jsonl", "1" * 64, 10, "src", "QUOTE_SNAPSHOT", "SYNTHETIC_FIXTURE", "invented", None, None, None)
        accounting, window = {"k": "v", "n": 1}, {"start": "s", "end": "e"}
        claim = identity.build(manifest_sha256="2" * 64, entries=[entry], source_id="src", accounting=accounting, window=window)
        inputs = {"manifest_sha256": "2" * 64, "source_id": "src", "account": "replay_src", "fixtures": [entry.to_dict()]}
        spec = lambda domain, body: hashlib.sha256(domain + json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()
        self.assertEqual(claim["digest"], spec(b"niftypaper.replay.inputs/1\n", {"version": "1", "inputs": inputs, "accounting": accounting, "window": window}))
        self.assertEqual(claim["account_binding"], spec(b"niftypaper.replay.account-binding/1\n", {"version": "1", "inputs": inputs, "accounting": accounting}))
        self.assertEqual((claim["digest"], claim["account_binding"]), (KNOWN_ANSWER_DIGEST, KNOWN_ANSWER_BINDING))
        self.assertEqual(identity.differences(claim, claim), [])

    def test_every_input_and_setting_that_matters_changes_the_identity(self):
        base = self.objs(self.run_variant("base"))[0]["inputs"]
        edited_quote = self.fixture_copy("edited", change=lambda f: (f / "SYNTHETIC_quotes.jsonl").write_bytes((f / "SYNTHETIC_quotes.jsonl").read_bytes().replace(b"100.20", b"100.25")))
        files = {e.path: e.record_type for e in mf.load_manifest(edited_quote / mf.MANIFEST_NAME)}
        (edited_quote / mf.MANIFEST_NAME).unlink()
        fx.write_manifest(edited_quote, files)
        respaced = self.fixture_copy("respaced")
        (respaced / mf.MANIFEST_NAME).write_bytes((respaced / mf.MANIFEST_NAME).read_bytes() + b"\n")
        variants = {
            "fixture content": dict(fixtures=edited_quote), "manifest bytes": dict(fixtures=respaced), "source id": dict(fixtures=self.fixture_copy("other", source_id="synthetic_other")),
            "as_of": dict(as_of=AS_OF - timedelta(days=1)), "balance": dict(starting_balance=Decimal("2000000")), "slippage": dict(slippage_ticks=2),
            "quote age": dict(max_quote_age=timedelta(seconds=6)), "limits": dict(limits=RiskLimits(max_lots_per_trade=1)), "costs": dict(costs=CostModel(fee_per_trade=Decimal("21"))),
        }
        digests = {base["digest"]}
        for name, change in variants.items():
            claim = self.objs(self.run_variant("v_" + name.replace(" ", "_"), script=[], **change))[0]["inputs"]
            self.assertNotEqual(claim["digest"], base["digest"], name)
            self.assertNotEqual(claim["account_binding"], base["account_binding"], name)
            digests.add(claim["digest"])
        self.assertEqual(len(digests), len(variants) + 1)
        moved = self.objs(self.run_variant("window", script=[], end=END - timedelta(minutes=1)))[0]["inputs"]
        self.assertNotEqual(moved["digest"], base["digest"])                                         # the window changes the run's identity...
        self.assertEqual(moved["account_binding"], base["account_binding"])                          # ...but not what the saved account was set up on

    def test_the_identity_module_has_no_environment_or_clock(self):
        text = Path(identity.__file__).read_text(encoding="utf-8")
        for banned in ("datetime.now", "os.environ", "getcwd", "abspath", "resolve(", "socket"):
            self.assertNotIn(banned, text)


class TestChangeDetection(Base):
    def baseline_fills(self):
        run = self.run_variant("baseline")
        return [{k: v for k, v in e.items() if k not in ("n", "prev", "hash", "inputs")} for e in self.objs(run) if e["type"] in ("FILL", "INTENT_REFUSED")]

    def test_a_normal_run_records_an_unchanged_recheck(self):
        run = self.run_variant("a")
        events = self.objs(run)
        rechecks = [e for e in events if e["type"] == "INPUTS_RECHECKED"]
        self.assertEqual(len(rechecks), 1)
        self.assertEqual((rechecks[0]["status"], rechecks[0]["differences"], rechecks[0]["identity_digest"]), ("UNCHANGED", [], events[0]["inputs"]["digest"]))
        self.assertEqual([e["type"] for e in events[-2:]], ["INPUTS_RECHECKED", "CHECKPOINT"])
        self.assertEqual(audit.verify_chain(run.events.read_bytes())["status"], "VALID")

    def test_a_fixture_changed_during_the_replay_does_not_alter_the_replay_but_is_not_reported_as_completed(self):
        folder = self.fixture_copy("live")
        runner = self.open("live", folder)
        quotes = folder / "SYNTHETIC_quotes.jsonl"
        original = quotes.read_bytes()
        runner.run(SCRIPT[:2])
        quotes.write_bytes(original.replace(b"100.20", b"199.99"))                                    # changed after validation, in the middle of the replay
        runner.run(SCRIPT[2:])
        with self.assertRaises(replay.InputsChanged) as caught:
            runner.write_events(self.tmp / "live" / "events.jsonl")
        self.assertIn("HASH_MISMATCH_WITH_MANIFEST SYNTHETIC_quotes.jsonl", caught.exception.differences)
        self.assertIsNone(runner.log_receipt)                                                         # never reported as a completed replay
        self.assertIsNotNone(runner.evidence_receipt)
        self.assertIs(caught.exception.receipt, runner.evidence_receipt)
        self.assertEqual(quotes.read_bytes(), original.replace(b"100.20", b"199.99"))                 # the changed input is left as found
        log = self.tmp / "live" / "events.jsonl"
        self.assertTrue(log.exists())
        events = [json.loads(x) for x in lines_of(log.read_bytes())]
        fills = [{k: v for k, v in e.items() if k not in ("n", "prev", "hash", "inputs")} for e in events if e["type"] in ("FILL", "INTENT_REFUSED")]
        self.assertEqual(fills, self.baseline_fills())                                                # the replay ran on the validated snapshot, not the changed file
        self.assertEqual(next(e for e in events if e["type"] == "INPUTS_RECHECKED")["status"], "CHANGED")
        run = type("Run", (), {"events": log, "state": self.tmp / "live" / "state" / STATE_FILE})
        report = self.report(run, folder)
        self.assertEqual((report["chain"]["status"], report["verdict"]), ("VALID", "INCONSISTENT"))
        self.assertEqual(self.status(report, "I1_INPUT_IDENTITY"), "FAIL")
        self.assertIn("changed when it finished", self.notes(report, "I1_INPUT_IDENTITY"))
        self.assertEqual(report["inputs_identity"]["completion_recheck"]["status"], "CHANGED")

    def test_other_kinds_of_change(self):
        cases = {
            "manifest edited": (lambda f: (f / mf.MANIFEST_NAME).write_bytes((f / mf.MANIFEST_NAME).read_bytes() + b"\n"), "the manifest file changed"),
            "fixture deleted": (lambda f: (f / "SYNTHETIC_bars.jsonl").unlink(), "MISSING_FILE SYNTHETIC_bars.jsonl"),
            "file added": (lambda f: (f / "extra.jsonl").write_bytes(b"{}\n"), "UNLISTED_FILE extra.jsonl"),
            "size changed": (lambda f: (f / "SYNTHETIC_index.jsonl").write_bytes((f / "SYNTHETIC_index.jsonl").read_bytes() + b"\n"), "SIZE_MISMATCH SYNTHETIC_index.jsonl"),
            "manifest deleted": (lambda f: (f / mf.MANIFEST_NAME).unlink(), "the manifest file is missing or unreadable"),
        }
        for name, (change, expected) in cases.items():
            with self.subTest(name):
                folder = self.fixture_copy("c_" + name.replace(" ", "_"))
                runner = self.open("c_" + name.replace(" ", "_"), folder)
                runner.run(SCRIPT)
                change(folder)
                with self.assertRaises(replay.InputsChanged) as caught:
                    runner.write_events(self.tmp / ("c_" + name.replace(" ", "_")) / "events.jsonl")
                self.assertIn(expected, caught.exception.differences)
                self.assertIsNone(runner.log_receipt)

    def test_a_change_made_and_put_back_between_the_checks_is_not_detected(self):
        folder = self.fixture_copy("restored")
        runner = self.open("restored", folder)
        quotes = folder / "SYNTHETIC_quotes.jsonl"
        original = quotes.read_bytes()
        quotes.write_bytes(original.replace(b"100.20", b"199.99"))
        runner.run(SCRIPT[:3])
        quotes.write_bytes(original)
        runner.run(SCRIPT[3:])
        receipt = runner.write_events(self.tmp / "restored" / "events.jsonl")                         # stated limitation: this passes
        self.assertEqual(runner.log_receipt, receipt)
        self.assertEqual(self.report(type("Run", (), {"events": receipt.path, "state": self.tmp / "restored" / "state" / STATE_FILE}), folder)["inputs_identity"]["status"], "MATCH")

    def test_the_manifest_is_hashed_and_parsed_from_the_same_bytes(self):
        folder = self.fixture_copy("race")
        original = (folder / mf.MANIFEST_NAME).read_bytes()
        real = mf.parse_manifest

        def parse_then_swap(data):
            entries = real(data)
            (folder / mf.MANIFEST_NAME).write_bytes(original + b"\n")                                 # the file changes right after it was read
            return entries

        with mock.patch.object(mf, "parse_manifest", parse_then_swap):
            runner = self.open("race", folder)
        self.assertEqual(runner.inputs["inputs"]["manifest_sha256"], hashlib.sha256(original).hexdigest())     # the claim describes the bytes that were validated
        runner.run(SCRIPT[:2])
        with self.assertRaises(replay.InputsChanged) as caught:
            runner.write_events(self.tmp / "race" / "events.jsonl")
        self.assertEqual(caught.exception.differences, ["the manifest file changed"])

    def test_a_mid_run_recheck_is_recorded_and_changes_nothing_else(self):
        runner = self.open("mid")
        runner.run(SCRIPT[:2])
        count = len(runner.events)
        self.assertEqual(runner.recheck_inputs(), [])
        self.assertEqual(len(runner.events), count + 1)
        self.assertEqual(runner.events[-1]["status"], "UNCHANGED")
        runner.run(SCRIPT[2:])
        receipt = runner.write_events(self.tmp / "mid" / "events.jsonl")
        self.assertEqual(audit.verify_chain(receipt.path.read_bytes(), expected_head=receipt.head)["status"], "VALID")
        self.assertEqual(len([e for e in runner.events if e["type"] == "INPUTS_RECHECKED"]), 2)

    def test_retrying_after_a_changed_run_neither_completes_it_nor_adds_events(self):
        folder = self.fixture_copy("retry")
        runner = self.open("retry", folder)
        runner.run(SCRIPT)
        (folder / "SYNTHETIC_index.jsonl").write_bytes(b"{}\n")
        with self.assertRaises(replay.InputsChanged):
            runner.write_events(self.tmp / "retry" / "first.jsonl")
        count = len(runner.events)
        with self.assertRaises(replay.InputsChanged):
            runner.write_events(self.tmp / "retry" / "second.jsonl")
        self.assertEqual(len(runner.events), count)
        self.assertIsNone(runner.log_receipt)
        self.assertEqual((self.tmp / "retry" / "first.jsonl").read_bytes(), (self.tmp / "retry" / "second.jsonl").read_bytes())
        with self.assertRaises(replay.EventLogError) as exists:
            runner.write_events(self.tmp / "retry" / "first.jsonl")
        self.assertEqual(exists.exception.code, "DESTINATION_EXISTS")

    def test_open_validates_everything_first_and_creates_nothing_when_it_refuses(self):
        for name, change, code in (("tampered", lambda f: (f / "SYNTHETIC_eod.jsonl").write_bytes((f / "SYNTHETIC_eod.jsonl").read_bytes().replace(b"101.00", b"102.00", 1)), "INTEGRITY_FAILURE"),
                                   ("no manifest", lambda f: (f / mf.MANIFEST_NAME).unlink(), "INVALID_MANIFEST"),
                                   ("junk manifest", lambda f: (f / mf.MANIFEST_NAME).write_bytes(b"{nope}\n"), "INVALID_MANIFEST"),
                                   ("unlisted", lambda f: (f / "extra.jsonl").write_bytes(b"{}\n"), "INTEGRITY_FAILURE")):
            folder = self.fixture_copy("o_" + name.replace(" ", "_"), change=change)
            with self.assertRaises(replay.ReplayRefused) as caught:
                replay.ReplayRunner.open(self.config("o_" + name.replace(" ", "_"), folder))
            self.assertEqual(caught.exception.code, code, name)
            self.assertFalse((self.tmp / ("o_" + name.replace(" ", "_")) / "state").exists(), name)

    def test_existing_logs_and_state_are_left_alone_when_inputs_change(self):
        folder = self.fixture_copy("keep")
        runner = self.open("keep", folder)
        runner.run(SCRIPT)
        state = self.tmp / "keep" / "state" / STATE_FILE
        state_before = state.read_bytes()
        (self.tmp / "keep" / "old.jsonl").write_bytes(b"older evidence\n")
        (folder / "SYNTHETIC_index.jsonl").write_bytes(b"{}\n")
        with self.assertRaises(replay.InputsChanged):
            runner.write_events(self.tmp / "keep" / "events.jsonl")
        self.assertEqual(state.read_bytes(), state_before)
        self.assertEqual((self.tmp / "keep" / "old.jsonl").read_bytes(), b"older evidence\n")
        self.assertEqual(sorted(p.name for p in (self.tmp / "keep").iterdir() if p.is_file()), ["events.jsonl", "old.jsonl"])


class TestStateBinding(Base):
    def test_the_state_header_carries_the_binding_and_a_restart_may_move_the_window(self):
        run = self.run_variant("a", script=SCRIPT[:2])
        header = json.loads(run.state.read_bytes().split(b"\n")[0])
        claim = self.objs(run)[0]["inputs"]
        self.assertEqual((header["account"], header["binding"]), ("replay_synthetic_fixture", claim["account_binding"]))
        again = self.open("a", start=at(10, 15, 31))                                                  # same inputs and settings, later window
        self.assertEqual((again.engine.lock_reason, again.engine.restored), (None, 2))
        self.assertNotEqual(again.inputs["digest"], claim["digest"])
        self.assertEqual(again.inputs["account_binding"], claim["account_binding"])

    def test_a_restart_on_different_settings_or_fixtures_is_locked_out_and_changes_nothing(self):
        self.run_variant("a", script=SCRIPT[:2])
        state = self.tmp / "a" / "state" / STATE_FILE
        before = state.read_bytes()
        changed_fixtures = self.fixture_copy("alt", change=lambda f: (f / "SYNTHETIC_index.jsonl").write_bytes((f / "SYNTHETIC_index.jsonl").read_bytes().replace(b"25000.00", b"25001.00")))
        files = {e.path: e.record_type for e in mf.load_manifest(changed_fixtures / mf.MANIFEST_NAME)}
        (changed_fixtures / mf.MANIFEST_NAME).unlink()
        fx.write_manifest(changed_fixtures, files)
        for name, kw in (("slippage", dict(slippage_ticks=2)), ("limits", dict(limits=RiskLimits(max_lots_per_trade=1))), ("quote age", dict(max_quote_age=timedelta(seconds=9))),
                         ("fixtures", dict(fixtures=changed_fixtures)), ("as_of", dict(as_of=AS_OF - timedelta(hours=1))), ("costs", dict(costs=CostModel(fee_per_trade=Decimal("1"))))):
            with self.subTest(name):
                runner = replay.ReplayRunner.open(self.config("a", start=at(10, 15, 31), **kw))
                self.assertIn("DIFFERENT_ACCOUNT", runner.engine.lock_reason or "", name)
                self.assertTrue([e for e in runner.events if e["type"] == "STATE_LOCKED"])
                runner.run([replay.Intent(at(10, 15, 33), CE, "SELL", 1)])
                self.assertEqual(runner.events[-1]["code"], "STATE_UNAVAILABLE")
                runner.close()
                self.assertEqual(state.read_bytes(), before)

    def test_binding_is_optional_for_other_accounts_and_required_when_asked_for(self):
        path = self.tmp / "plain.jsonl"
        engine = PaperEngine(Decimal("1000000"), store=JsonlStore(path, Decimal("1000000"), account="synthetic"), origin=ORIGIN_SYNTHETIC)
        view = MarketView(ORIGIN_SYNTHETIC, "synthetic", "SIMULATED", "SIMULATED", CE, Decimal("100.00"), Decimal("100.20"), None, 650, 650, Decimal("0.05"), 65, None, None, None, True, True)
        self.assertTrue(engine.simulate(TradeRequest(CE, "BUY", 1), view).accepted)
        engine.close()
        self.assertNotIn(b"binding", path.read_bytes())
        again = JsonlStore(path, Decimal("1000000"), account="synthetic")
        self.assertEqual(len(again.load()), 1)
        again.close()
        bound = JsonlStore(path, Decimal("1000000"), account="synthetic", binding="x" * 64)
        with self.assertRaises(StoreError) as caught:
            bound.load()
        bound.close()
        self.assertEqual(caught.exception.code, "DIFFERENT_ACCOUNT")


class TestAuditPairing(Base):
    def setUp(self):
        super().setUp()
        self.run_ = self.run_variant("main")

    def assertBlocked(self, report, expected_text):
        self.assertEqual(report["verdict"], "NOT_RECONCILABLE")
        self.assertEqual(self.status(report, "P1_PAIRING"), "BLOCKED")
        self.assertIn(expected_text, self.notes(report, "P1_PAIRING"))
        self.assertEqual(report["pairing"]["status"], "MISMATCH")
        self.assertEqual(report["summary"]["rebuilt_cash"], {"unavailable": "RECONCILIATION_BLOCKED"})             # nothing merged, nothing inferred, nothing shown as zero
        self.assertEqual(report["summary"]["rebuilt_open_positions"], {"unavailable": "RECONCILIATION_BLOCKED"})
        for row in report["rows"]:
            self.assertEqual(row["cash_after"], {"unavailable": "RECONCILIATION_BLOCKED"})

    def test_a_clean_pair(self):
        report = self.report(self.run_)
        self.assertEqual((report["verdict"], report["coverage"], report["pairing"]["status"], report["inputs_identity"]["status"]), ("CONSISTENT", "FULL", "PAIRED", "MATCH"))
        self.assertEqual(self.status(report, "P1_PAIRING"), "PASS")
        self.assertEqual(self.status(report, "I1_INPUT_IDENTITY"), "PASS")
        claimed = report["inputs_identity"]["claimed"]
        self.assertEqual(claimed["digest"], self.objs(self.run_)[0]["inputs"]["digest"])
        self.assertEqual(report["inputs_identity"]["recomputed"]["digest"], claimed["digest"])
        self.assertEqual([f["path"] for f in claimed["fixtures"]], sorted(e.path for e in mf.load_manifest(FIXTURE_DIR / mf.MANIFEST_NAME)))
        self.assertEqual(report["pairing"]["events"]["account_binding"], report["pairing"]["state"]["binding"])
        self.assertIn("Inputs: MATCH   Pairing (state with events): PAIRED", audit.render_text(report))
        self.assertEqual(self.report(self.run_), report)                                                           # deterministic

    def test_state_from_another_source(self):
        other = self.run_variant("other", fixtures=self.fixture_copy("othersrc", source_id="synthetic_other"))
        report = self.report(type("Run", (), {"events": self.run_.events, "state": other.state}))
        self.assertBlocked(report, "another source or account")
        self.assertEqual(report["chain"]["status"], "VALID")                                                       # the chain is a separate question
        self.assertEqual(report["anchor"]["status"], "NOT_SUPPLIED")
        reverse = self.report(type("Run", (), {"events": other.events, "state": self.run_.state}), FIXTURE_DIR)
        self.assertEqual(reverse["verdict"], "NOT_RECONCILABLE")

    def test_state_from_another_account(self):
        folder = self.tmp / "acct"
        folder.mkdir()
        path = folder / "paper_synthetic.jsonl"
        engine = PaperEngine(Decimal("1000000"), store=JsonlStore(path, Decimal("1000000"), account="synthetic"), origin=ORIGIN_SYNTHETIC)
        view = MarketView(ORIGIN_SYNTHETIC, "synthetic", "SIMULATED", "SIMULATED", CE, Decimal("100.00"), Decimal("100.20"), None, 650, 650, Decimal("0.05"), 65, None, None, None, True, True)
        self.assertTrue(engine.simulate(TradeRequest(CE, "BUY", 1), view).accepted)
        engine.close()
        report = self.report(type("Run", (), {"events": self.run_.events, "state": path}))
        self.assertEqual((report["verdict"], self.status(report, "S2_ACCOUNT_IDENTITY"), self.status(report, "P1_PAIRING")), ("NOT_RECONCILABLE", "BLOCKED", "BLOCKED"))
        self.assertEqual(report["summary"]["rebuilt_cash"], {"unavailable": "RECONCILIATION_BLOCKED"})

    def test_state_from_a_different_replay_of_the_same_source(self):
        different = self.run_variant("slip2", slippage_ticks=2)
        self.assertEqual(json.loads(different.state.read_bytes().split(b"\n")[0])["account"], "replay_synthetic_fixture")
        report = self.report(type("Run", (), {"events": self.run_.events, "state": different.state}))
        self.assertBlocked(report, "different inputs or settings")
        for name, kw in (("limits", dict(limits=RiskLimits(max_lots_per_trade=1))), ("balance", dict(starting_balance=Decimal("900000")))):
            report = self.report(type("Run", (), {"events": self.run_.events, "state": self.run_variant("d_" + name, **kw).state}))
            self.assertEqual(report["verdict"], "NOT_RECONCILABLE", name)

    def test_same_inputs_but_a_different_script_is_caught_by_the_trade_match_not_the_binding(self):
        shorter = self.run_variant("shorter", script=SCRIPT[:2])
        report = self.report(type("Run", (), {"events": self.run_.events, "state": shorter.state}))
        self.assertEqual(report["pairing"]["status"], "PAIRED")                                                    # stated limit: the binding covers inputs and settings, not the script
        self.assertEqual((report["verdict"], self.status(report, "X1_EVENT_TRADE_MATCH")), ("INCONSISTENT", "FAIL"))

    def test_missing_or_truncated_state(self):
        for name, state in (("missing", self.tmp / "nowhere" / STATE_FILE), ("truncated", None), ("header only", None)):
            if name != "missing":
                copy = self.tmp / ("st_" + name.replace(" ", "_"))
                copy.mkdir()
                state = copy / STATE_FILE
                data = self.run_.state.read_bytes()
                state.write_bytes(data[: len(data) // 2] if name == "truncated" else data.split(b"\n")[0] + b"\n")
            report = self.report(type("Run", (), {"events": self.run_.events, "state": state}))
            if name == "header only":
                self.assertEqual(self.status(report, "X1_EVENT_TRADE_MATCH"), "FAIL")                              # a header with no trades pairs but cannot match the fills
                self.assertEqual(report["verdict"], "INCONSISTENT")
            else:
                self.assertEqual((report["verdict"], self.status(report, "S1_STATE_PARSE")), ("NOT_RECONCILABLE", "BLOCKED"), name)
                if name == "missing":
                    self.assertEqual(report["pairing"]["status"], "NOT_ESTABLISHED")
                    self.assertEqual(report["account"]["starting_balance"], {"unavailable": "STATE_UNUSABLE"})

    def test_a_state_with_no_binding_cannot_be_paired(self):
        lines = self.run_.state.read_bytes().split(b"\n")
        header = json.loads(lines[0])
        del header["binding"]
        folder = self.tmp / "nobind"
        folder.mkdir()
        (folder / STATE_FILE).write_bytes(b"\n".join([json.dumps(header, sort_keys=True).encode()] + lines[1:]))
        report = self.report(type("Run", (), {"events": self.run_.events, "state": folder / STATE_FILE}))
        self.assertBlocked(report, "no binding")

    def test_changed_fixture_identity(self):
        content = self.fixture_copy("c1", change=lambda f: (f / "SYNTHETIC_quotes.jsonl").write_bytes((f / "SYNTHETIC_quotes.jsonl").read_bytes().replace(b"100.20", b"100.25")))
        report = self.report(self.run_, content)                                                                    # file changed, manifest not
        self.assertEqual(report["verdict"], "INCONSISTENT")
        self.assertEqual((self.status(report, "I1_INPUT_IDENTITY"), self.status(report, "F1_FIXTURES")), ("FAIL", "FAIL"))
        self.assertEqual(report["inputs_identity"]["status"], "MISMATCH")
        self.assertIn("SYNTHETIC_quotes.jsonl", self.notes(report, "I1_INPUT_IDENTITY"))
        regenerated = self.fixture_copy("c2", change=lambda f: (f / "SYNTHETIC_quotes.jsonl").write_bytes((f / "SYNTHETIC_quotes.jsonl").read_bytes().replace(b"100.20", b"100.25")))
        files = {e.path: e.record_type for e in mf.load_manifest(regenerated / mf.MANIFEST_NAME)}
        (regenerated / mf.MANIFEST_NAME).unlink()
        fx.write_manifest(regenerated, files)                                                                       # file and manifest changed together
        report = self.report(self.run_, regenerated)
        self.assertEqual(report["inputs_identity"]["status"], "MISMATCH")
        self.assertIn("inputs.manifest_sha256 differs", report["inputs_identity"]["differences"])
        self.assertTrue(any("SYNTHETIC_quotes.jsonl" in d for d in report["inputs_identity"]["differences"]))
        self.assertEqual(self.status(report, "I1_INPUT_IDENTITY"), "FAIL")
        other_source = self.fixture_copy("c3", source_id="synthetic_other")
        self.assertEqual(self.status(self.report(self.run_, other_source), "I1_INPUT_IDENTITY"), "FAIL")

    def test_valid_chain_but_mismatched_state(self):
        different = self.run_variant("slip3", slippage_ticks=3)
        report = self.report(type("Run", (), {"events": self.run_.events, "state": different.state}))
        self.assertEqual((report["chain"]["status"], report["verdict"], report["pairing"]["status"]), ("VALID", "NOT_RECONCILABLE", "MISMATCH"))
        self.assertEqual(report["trust"]["chain_integrity"], "VALID")
        self.assertEqual(report["trust"]["accounting"], "NOT_RECONCILABLE")
        self.assertEqual(report["trust"]["authenticity"], "NOT_ESTABLISHED")

    def test_matching_state_and_chain_but_incorrect_accounting_claims(self):
        def tamper(objs):
            next(o for o in objs if o["type"] == "FILL")["cash"] = "1.00"
        run = self.rechained(self.run_, "wrongcash", tamper)
        report = self.report(run)
        self.assertEqual((report["chain"]["status"], report["pairing"]["status"], report["inputs_identity"]["status"], report["verdict"]), ("VALID", "PAIRED", "MATCH", "INCONSISTENT"))
        self.assertEqual(self.status(report, "A1_CASH"), "FAIL")
        self.assertEqual(self.status(report, "P1_PAIRING"), "PASS")

    def test_an_edited_identity_claim_is_found_even_with_a_rebuilt_chain(self):
        cases = {
            "digest": lambda o: o[0]["inputs"].__setitem__("digest", "0" * 64),
            "binding": lambda o: o[0]["inputs"].__setitem__("account_binding", "0" * 64),
            "manifest hash": lambda o: o[0]["inputs"]["inputs"].__setitem__("manifest_sha256", "0" * 64),
            "fixture hash": lambda o: o[0]["inputs"]["inputs"]["fixtures"][0].__setitem__("sha256", "0" * 64),
            "fixture removed": lambda o: o[0]["inputs"]["inputs"]["fixtures"].pop(),
            "accounting": lambda o: o[0]["inputs"]["accounting"].__setitem__("as_of", "2031-03-09T12:00:00+05:30"),
            "slippage claim": lambda o: o[0]["inputs"]["accounting"].__setitem__("slippage_ticks", 2),
            "window": lambda o: o[0]["inputs"]["window"].__setitem__("end", "2031-03-03T10:29:00+05:30"),
            "source": lambda o: o[0]["inputs"]["inputs"].__setitem__("source_id", "synthetic_other"),
        }
        for name, change in cases.items():
            with self.subTest(name):
                report = self.report(self.rechained(self.run_, "e_" + name.replace(" ", "_"), change))
                self.assertEqual(report["chain"]["status"], "VALID", name)
                self.assertNotEqual(self.status(report, "I1_INPUT_IDENTITY") if report["verdict"] != "NOT_RECONCILABLE" else "FAIL", "PASS", name)
                self.assertNotEqual(report["verdict"], "CONSISTENT", name)

    def test_claims_that_contradict_the_replays_own_statements_fail_even_without_fixtures(self):
        cases = {
            "accounting cut-off": (lambda o: o[0]["inputs"]["accounting"].__setitem__("as_of", "2031-03-09T12:00:00+05:30"), "as_of"),
            "accounting slippage": (lambda o: o[0]["inputs"]["accounting"].__setitem__("slippage_ticks", 2), "slippage_ticks"),
            "window end": (lambda o: o[0]["inputs"]["window"].__setitem__("end", "2031-03-03T10:29:00+05:30"), "window end"),
            "source": (lambda o: o[0]["inputs"]["inputs"].__setitem__("source_id", "synthetic_other"), "names source"),
            "account": (lambda o: o[0]["inputs"]["inputs"].__setitem__("account", "replay_other"), "replay_<source>"),
            "recheck digest": (lambda o: next(e for e in o if e["type"] == "INPUTS_RECHECKED").__setitem__("identity_digest", "0" * 64), "different identity digest"),
            "recheck status": (lambda o: next(e for e in o if e["type"] == "INPUTS_RECHECKED").__setitem__("status", "MAYBE"), "unknown status"),
        }
        for name, (change, text) in cases.items():
            with self.subTest(name):
                report = self.report(self.rechained(self.run_, "k_" + name.replace(" ", "_"), change), fixtures=None)
                self.assertEqual(report["chain"]["status"], "VALID", name)
                if report["verdict"] != "NOT_RECONCILABLE":
                    self.assertEqual(self.status(report, "I1_INPUT_IDENTITY"), "FAIL", name)
                    self.assertIn(text, self.notes(report, "I1_INPUT_IDENTITY"), name)
                self.assertNotEqual(report["verdict"], "CONSISTENT", name)

    def test_logs_that_predate_the_identity_are_partial_not_failed(self):
        run = self.rechained(self.run_, "legacy", lambda objs: objs[0].pop("inputs"))
        report = self.report(run)
        self.assertEqual((report["verdict"], report["coverage"]), ("CONSISTENT", "PARTIAL"))
        self.assertEqual((self.status(report, "P1_PAIRING"), self.status(report, "I1_INPUT_IDENTITY")), ("NOT_CHECKED", "NOT_CHECKED"))
        self.assertEqual((report["pairing"]["status"], report["inputs_identity"]["status"]), ("NOT_ESTABLISHED", "NOT_CHECKED"))

    def test_without_fixtures_the_identity_is_claimed_but_not_compared(self):
        report = self.report(self.run_, fixtures=None)
        self.assertEqual((report["verdict"], report["coverage"], self.status(report, "I1_INPUT_IDENTITY")), ("CONSISTENT", "PARTIAL", "NOT_CHECKED"))
        self.assertEqual(report["inputs_identity"]["recomputed"], {"unavailable": "FIXTURES_NOT_SUPPLIED"})
        self.assertEqual(report["pairing"]["status"], "PAIRED")                                                   # pairing needs no fixtures

    def test_no_completion_recheck_is_partial_coverage(self):
        def drop(objs):
            objs.remove(next(o for o in objs if o["type"] == "INPUTS_RECHECKED"))
            for number, o in enumerate(objs, 1):
                o["n"] = number

        run = self.rechained(self.run_, "norecheck", drop)
        report = self.report(run)
        self.assertEqual(report["inputs_identity"]["completion_recheck"]["status"], "NOT_RECORDED")
        self.assertEqual(self.status(report, "I1_INPUT_IDENTITY"), "NOT_CHECKED")

    def test_unknown_event_types_are_still_a_blocking_problem_and_the_new_type_is_known(self):
        self.assertIn("INPUTS_RECHECKED", audit.EVENT_TYPES)
        run = self.rechained(self.run_, "weird", lambda objs: objs[3].__setitem__("type", "SOMETHING_NEW"))
        self.assertEqual(self.report(run)["verdict"], "NOT_RECONCILABLE")

    def test_independent_runs_are_never_merged(self):
        other = self.run_variant("other2", fixtures=self.fixture_copy("othersrc2", source_id="synthetic_other"))
        first, second = self.report(self.run_), self.report(other, self.tmp / "fx_othersrc2")
        self.assertEqual((first["verdict"], second["verdict"]), ("CONSISTENT", "CONSISTENT"))
        self.assertNotEqual(first["inputs_identity"]["claimed"]["digest"], second["inputs_identity"]["claimed"]["digest"])
        self.assertNotEqual(first["pairing"]["state"]["binding"], second["pairing"]["state"]["binding"])
        self.assertEqual((first["account"]["source_id"], second["account"]["source_id"]), ("synthetic_fixture", "synthetic_other"))

    def test_the_audit_stays_read_only_with_the_new_checks(self):
        files = [self.run_.events, self.run_.state] + list(FIXTURE_DIR.iterdir())
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}
        listing = sorted(p.name for p in self.run_.events.parent.iterdir()), sorted(p.name for p in FIXTURE_DIR.iterdir())
        self.report(self.run_)
        self.report(self.run_, fixtures=None)
        self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}, before)
        self.assertEqual((sorted(p.name for p in self.run_.events.parent.iterdir()), sorted(p.name for p in FIXTURE_DIR.iterdir())), listing)


class TestDemo(Base):
    def setUp(self):
        super().setUp()
        spec = __import__("importlib.util").util.spec_from_file_location("demo_under_test", ROOT / "tools" / "replay_fixture_demo.py")
        self.demo = __import__("importlib.util").util.module_from_spec(spec)
        spec.loader.exec_module(self.demo)

    def go(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.demo.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_the_demo_prints_the_identity_and_keeps_its_commands(self):
        code, out, err = self.go("--out-dir", str(self.tmp / "run1"))
        self.assertEqual((code, err), (0, ""))
        events = [json.loads(x) for x in lines_of((self.tmp / "run1" / "events.jsonl").read_bytes())]
        digest = events[0]["inputs"]["digest"]
        self.assertIn(f"input identity {digest}", out)
        self.assertIn("inputs        MATCH", out)
        self.assertIn("state pairing PAIRED", out)
        self.assertEqual(len([line for line in out.splitlines() if line.startswith("python tools")]), 4)
        self.assertNotIn("anchor created", out)

    def test_fixtures_changed_while_the_demo_runs_are_not_reported_as_success(self):
        folder = self.fixture_copy("demo_fx")
        real = replay.ReplayRunner.write_events

        def change_then_write(runner, path):
            (folder / "SYNTHETIC_index.jsonl").write_bytes(b"{}\n")
            return real(runner, path)

        with mock.patch.object(self.demo, "FIXTURE_DIR", folder), mock.patch.object(replay.ReplayRunner, "write_events", change_then_write):
            code, out, err = self.go("--out-dir", str(self.tmp / "run1"))
        self.assertEqual((code, out), (4, ""))
        self.assertIn("changed while the replay ran", err)
        self.assertIn("No success is reported", err)
        self.assertTrue((self.tmp / "run1" / "events.jsonl").exists())                                             # evidence kept
        self.assertTrue((self.tmp / "run1" / "state" / STATE_FILE).exists())

    def test_identity_or_pairing_problems_are_not_success(self):
        real = audit.build_report

        def patched(change):
            def build(config):
                report = json.loads(json.dumps(real(config)))
                change(report)
                return report
            return build

        for name, change, text in (("identity", lambda r: r["inputs_identity"].update(status="MISMATCH", differences=["x"]), "input identity is MISMATCH"),
                                   ("pairing", lambda r: r["pairing"].update(status="MISMATCH", reasons=["y"]), "state pairing is MISMATCH"),
                                   ("digest", lambda r: r["inputs_identity"]["claimed"].update(digest="0" * 64), "differs from what the replay reported"),
                                   ("recheck", lambda r: r["inputs_identity"]["completion_recheck"].update(status="NOT_RECORDED"), "completion recheck")):
            with self.subTest(name), mock.patch.object(audit, "build_report", patched(change)):
                code, out, err = self.go("--out-dir", str(self.tmp / ("d_" + name)))
            self.assertEqual((code, out), (4, ""), name)
            self.assertIn(text, err)


class TestDocumentation(unittest.TestCase):
    def test_the_policy_is_documented(self):
        doc = (ROOT / "docs" / "HISTORICAL_REPLAY_AUDIT.md").read_text(encoding="utf-8")
        for phrase in ("Input identity", "INPUTS_RECHECKED", "InputsChanged", "put back", "account_binding", "P1_PAIRING", "I1_INPUT_IDENTITY"):
            self.assertIn(phrase, doc)


if __name__ == "__main__":
    unittest.main()
