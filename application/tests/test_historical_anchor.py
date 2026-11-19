"""External trust anchors for replay event logs: format, creation, comparison in the audit, and what an anchor does and does not prove. Invented fixtures only.

Four things are kept apart throughout: accounting reconciliation (the verdict), chain integrity, anchor consistency, and authenticity (never established).
"""
import contextlib
import errno
import hashlib
import io
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from niftypaper.historical import anchor, audit, eventchain, replay
from niftypaper.historical import manifest as mf
from niftypaper.paper.engine import ORIGIN_SYNTHETIC, MarketView, PaperEngine, TradeRequest
from niftypaper.paper.store import JsonlStore
from decimal import Decimal
from tests import ROOT
from tests import historical_fixtures as fx
from tests.historical_fixtures import AS_OF, CE, PE
from tests.test_historical_audit import SCRIPT, Base
from tests.test_historical_chain import dump, lines_of
from tests.test_historical_replay import END, FIXTURE_DIR, START, STATE_FILE, at

import importlib.util


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool


class AnchorBase(Base):
    def setUp(self):
        super().setUp()
        self.run_ = self.make_run()
        self.vault = self.tmp / "vault"                      # a folder away from the log and the replay state
        self.vault.mkdir()
        self.data = self.run_.events.read_bytes()
        self.objs = [json.loads(x) for x in lines_of(self.data)]

    def anchor_file(self, name="anchor.json", run=None):
        run = run or self.run_
        anchor.create(run.events, run.state, self.vault / name)
        return self.vault / name

    def audit(self, run=None, **kw):
        run = run or self.run_
        return audit.build_report(audit.AuditConfig(run.events, run.state, FIXTURE_DIR, **kw))

    def write_log(self, name, data):
        folder = self.tmp / name
        folder.mkdir()
        path = folder / "events.jsonl"
        path.write_bytes(data)
        return type("Run", (), {"events": path, "state": self.run_.state})

    def replay_other_source(self):
        folder = self.tmp / "otherfix"
        shutil.copytree(FIXTURE_DIR, folder)
        files = {e.path: e.record_type for e in mf.load_manifest(folder / mf.MANIFEST_NAME)}
        (folder / mf.MANIFEST_NAME).unlink()
        fx.write_manifest(folder, files, source_id="synthetic_other")
        config = replay.ReplayConfig(fixture_dir=folder, state_dir=self.tmp / "other" / "state", as_of=AS_OF, start=START, end=END, permitted=True)
        runner = replay.ReplayRunner.open(config)
        runner.run(SCRIPT[:2])
        runner.write_events(self.tmp / "other" / "events.jsonl")
        runner.close()
        return type("Run", (), {"events": self.tmp / "other" / "events.jsonl", "state": self.tmp / "other" / "state" / "replay_synthetic_other.jsonl"})

    def rewritten(self, record, name="a2.json"):
        path = self.vault / name
        path.write_bytes(anchor.canonical_line(record).encode("ascii"))
        return path

    def record(self, **change):
        record = anchor.build(self.data, "synthetic_fixture")
        record.update(change)
        return record


class TestFormat(AnchorBase):
    def test_the_record_binds_the_log_and_the_replay(self):
        path = self.anchor_file()
        raw = path.read_bytes()
        record = anchor.parse(raw)
        self.assertEqual(list(record), sorted(anchor.FIELDS)) if False else self.assertEqual(set(record), set(anchor.FIELDS))
        self.assertEqual((record["anchor"], record["kind"], record["source_id"], record["account"], record["chain"]),
                         ("1", "niftypaper.replay.event_log_anchor", "synthetic_fixture", "replay_synthetic_fixture", "1"))
        self.assertEqual((record["events"], record["head"], record["log_sha256"], record["log_bytes"], record["final_event"]),
                         (len(self.objs), self.objs[-1]["hash"], hashlib.sha256(self.data).hexdigest(), len(self.data), "CHECKPOINT"))
        self.assertEqual((record["first_clock"], record["last_clock"], record["trades"]), (self.objs[0]["clock"], self.objs[-1]["clock"], self.objs[-1]["trades"]))
        self.assertEqual(record["genesis"], eventchain.GENESIS)
        self.assertEqual(anchor.GENESIS, audit.CHAIN_GENESIS)
        self.assertIn("not an independent trust anchor", record["notice"])

    def test_canonical_one_line_ascii_without_paths_times_or_secrets(self):
        raw = self.anchor_file().read_bytes()
        text = raw.decode("ascii")
        self.assertEqual((text.count("\n"), text.endswith("\n")), (1, True))
        self.assertEqual(text, anchor.canonical_line(json.loads(text)))
        self.assertNotIn(str(self.tmp), text)
        self.assertNotIn("2026-", text)
        for word in ("token", "secret", "password", "key", "credential"):
            self.assertNotIn(word, text.lower().replace("keys", ""), word)

    def test_identical_replays_give_identical_anchor_bytes(self):
        first = self.anchor_file().read_bytes()
        second_run = self.make_run("again")
        self.assertEqual(self.anchor_file("second.json", second_run).read_bytes(), first)

    def test_malformed_and_unsupported_anchors_are_refused(self):
        good = self.record()
        line = lambda r: anchor.canonical_line(r).encode("ascii")
        spaced = json.dumps(good).encode() + b"\n"
        reordered = json.dumps(dict(reversed(list(good.items()))), separators=(",", ":")).encode() + b"\n"
        cases = {
            "empty": (b"", "EMPTY_ANCHOR"), "not utf8": (b"\xff\xfe\n", "MALFORMED_ANCHOR"), "no newline": (line(good)[:-1], "MALFORMED_ANCHOR"),
            "two lines": (line(good) + line(good), "MALFORMED_ANCHOR"), "junk": (b"junk\n", "MALFORMED_ANCHOR"), "array": (b"[1]\n", "MALFORMED_ANCHOR"),
            "duplicate key": (b'{"anchor":"1","anchor":"1"}\n', "MALFORMED_ANCHOR"), "nan": (b'{"anchor":"1","events":NaN}\n', "MALFORMED_ANCHOR"),
            "version 2": (line({**good, "anchor": "2"}), "UNSUPPORTED_VERSION"), "version int": (line({**good, "anchor": 1}), "MALFORMED_VERSION"),
            "no version": (line({k: v for k, v in good.items() if k != "anchor"}), "MALFORMED_VERSION"),
            "missing field": (line({k: v for k, v in good.items() if k != "head"}), "MALFORMED_ANCHOR"), "extra field": (line({**good, "extra": 1}), "MALFORMED_ANCHOR"),
            "kind": (line({**good, "kind": "other"}), "MALFORMED_ANCHOR"), "notice": (line({**good, "notice": "x"}), "MALFORMED_ANCHOR"),
            "account": (line({**good, "account": "replay_other"}), "MALFORMED_ANCHOR"), "source": (line({**good, "source_id": "Bad Id", "account": "replay_Bad Id"}), "MALFORMED_ANCHOR"),
            "chain": (line({**good, "chain": "2"}), "MALFORMED_ANCHOR"), "genesis": (line({**good, "genesis": "0" * 64}), "MALFORMED_ANCHOR"),
            "head upper": (line({**good, "head": "A" * 64}), "MALFORMED_ANCHOR"), "head short": (line({**good, "head": "a" * 63}), "MALFORMED_ANCHOR"),
            "sha int": (line({**good, "log_sha256": 5}), "MALFORMED_ANCHOR"), "zero events": (line({**good, "events": 0}), "MALFORMED_ANCHOR"),
            "negative": (line({**good, "events": -1}), "MALFORMED_ANCHOR"), "bool count": (line({**good, "events": True}), "MALFORMED_ANCHOR"),
            "float count": (line(good).replace(b'"events":%d' % good["events"], b'"events":%d.0' % good["events"]), "MALFORMED_ANCHOR"),
            "float string": (line({**good, "log_bytes": "9"}), "MALFORMED_ANCHOR"), "zero bytes": (line({**good, "log_bytes": 0}), "MALFORMED_ANCHOR"),
            "naive clock": (line({**good, "first_clock": "2031-03-03T10:14:00"}), "MALFORMED_ANCHOR"),
            "reversed clocks": (line({**good, "first_clock": good["last_clock"], "last_clock": good["first_clock"]}), "MALFORMED_ANCHOR"),
            "final event": (line({**good, "final_event": "FILL"}), "MALFORMED_ANCHOR"), "spaced": (spaced, "NON_CANONICAL_ANCHOR"), "reordered": (reordered, "NON_CANONICAL_ANCHOR"),
            "crlf": (line(good)[:-1] + b"\r\n", "NON_CANONICAL_ANCHOR"),
        }
        for name, (data, code) in cases.items():
            with self.subTest(name), self.assertRaises(anchor.AnchorError) as caught:
                anchor.parse(data)
            self.assertEqual(caught.exception.code, code, name)
            path = self.vault / "bad.json"
            path.write_bytes(data)
            if path.exists():
                report = self.audit(anchor_path=path)
                self.assertEqual((report["anchor"]["status"], report["anchor"]["parts"]["anchor_file"]["reason"]), ("INVALID_ANCHOR", code), name)
                self.assertEqual((report["verdict"], report["chain"]["status"]), ("CONSISTENT", "VALID"), name)               # a bad anchor changes neither
            path.unlink()


class TestComparison(AnchorBase):
    def test_a_matching_anchor(self):
        path = self.anchor_file()
        report = self.audit(anchor_path=path)
        section = report["anchor"]
        self.assertEqual((section["status"], section["protection"], section["authenticity"]), ("MATCH", "LOCAL_OR_UNDECLARED", "NOT_ESTABLISHED"))
        entry = section["parts"]["anchor_file"]
        self.assertEqual(set(entry["comparisons"].values()), {"MATCH"})
        self.assertEqual(set(entry["comparisons"]), {"source_id", "events", "head", "log_sha256", "log_bytes", "first_clock", "last_clock"})
        self.assertIn("NOT independently protected", section["reading"])
        self.assertIn("Not an independent trust anchor", section["protection_note"])
        self.assertEqual(report["trust"], {"accounting": "CONSISTENT", "chain_integrity": "VALID", "anchor_consistency": "MATCH", "anchor_protection": "LOCAL_OR_UNDECLARED",
                                           "authenticity": "NOT_ESTABLISHED", "reading": report["trust"]["reading"]})
        self.assertFalse(any(isinstance(v, bool) for v in report["trust"].values()))                  # no single yes/no that merges the four questions

    def test_without_an_anchor_nothing_is_claimed(self):
        section = self.audit()["anchor"]
        self.assertEqual((section["status"], section["protection"], section["authenticity"]), ("NOT_SUPPLIED", "NOT_APPLICABLE", "NOT_ESTABLISHED"))

    def test_a_modified_log_with_an_unchanged_anchor(self):
        path = self.anchor_file()
        pieces = lines_of(self.data)
        pieces[5] = pieces[5].replace('"clock":"2031-03-03T10:15', '"clock":"2031-03-03T10:16', 1)
        self.assertNotEqual(pieces[5], lines_of(self.data)[5])
        run = self.write_log("mod", ("\n".join(pieces) + "\n").encode())
        report = self.audit(run, anchor_path=path)
        entry = report["anchor"]["parts"]["anchor_file"]
        self.assertEqual((report["chain"]["status"], report["anchor"]["status"], entry["reason"]), ("INVALID", "MISMATCH", "CHAIN_NOT_VALID"))
        self.assertEqual((entry["comparisons"]["log_sha256"], entry["comparisons"]["head"]), ("MISMATCH", "UNAVAILABLE"))

    def test_clean_boundary_truncation_is_valid_chain_but_anchor_mismatch(self):
        path = self.anchor_file()
        run = self.write_log("cut", ("\n".join(lines_of(self.data)[:-1]) + "\n").encode())
        report = self.audit(run, anchor_path=path)
        self.assertEqual(report["chain"]["status"], "VALID")                                        # the chain alone cannot see this
        self.assertEqual((report["anchor"]["status"], report["anchor"]["parts"]["anchor_file"]["reason"]), ("MISMATCH", "FIELDS_DIFFER"))
        comparisons = report["anchor"]["parts"]["anchor_file"]["comparisons"]
        self.assertEqual([k for k, v in comparisons.items() if v == "MISMATCH"], ["events", "head", "log_sha256", "log_bytes"])
        self.assertEqual(report["verdict"], "INCONSISTENT")

    def test_a_rebuilt_chain_with_an_unchanged_anchor(self):
        path = self.anchor_file()
        edited = [{k: v for k, v in o.items() if k not in eventchain.RESERVED} for o in self.objs]
        edited[2]["note"] = "inserted by someone"
        data, _ = dump(edited)
        run = self.write_log("rebuilt", data)
        report = self.audit(run, anchor_path=path)
        self.assertEqual(report["chain"]["status"], "VALID")                                        # a rebuilt chain is valid...
        self.assertEqual(report["anchor"]["status"], "MATCH" if False else "MISMATCH")               # ...but not the log that was anchored
        self.assertEqual(report["anchor"]["parts"]["anchor_file"]["comparisons"]["head"], "MISMATCH")
        self.assertEqual(report["verdict"], "CONSISTENT")                                           # and the accounting alone would not have noticed

    def test_mismatched_source_identity(self):
        other = self.replay_other_source()
        other_anchor = self.anchor_file("other.json", other)
        report = self.audit(anchor_path=other_anchor)
        comparisons = report["anchor"]["parts"]["anchor_file"]["comparisons"]
        self.assertEqual((report["anchor"]["status"], comparisons["source_id"], comparisons["head"]), ("MISMATCH", "MISMATCH", "MISMATCH"))
        forged = self.rewritten(self.record(source_id="synthetic_other", account="replay_synthetic_other"))
        report = self.audit(anchor_path=forged)
        self.assertEqual(report["anchor"]["parts"]["anchor_file"]["comparisons"]["source_id"], "MISMATCH")
        self.assertEqual(report["anchor"]["status"], "MISMATCH")

    def test_each_field_that_differs_is_reported(self):
        for field, value in (("events", len(self.objs) + 1), ("head", "0" * 64), ("log_sha256", "0" * 64), ("log_bytes", len(self.data) + 1),
                             ("first_clock", "2031-03-03T10:00:00+05:30"), ("last_clock", "2031-03-03T11:00:00+05:30")):
            with self.subTest(field):
                report = self.audit(anchor_path=self.rewritten(self.record(**{field: value})))
                comparisons = report["anchor"]["parts"]["anchor_file"]["comparisons"]
                self.assertEqual((report["anchor"]["status"], comparisons[field]), ("MISMATCH", "MISMATCH"))
                self.assertEqual([k for k, v in comparisons.items() if v == "MISMATCH"], [field])

    def test_a_missing_anchor_file_or_log(self):
        report = self.audit(anchor_path=self.vault / "nothing.json")
        self.assertEqual((report["anchor"]["status"], report["anchor"]["parts"]["anchor_file"]["reason"]), ("UNAVAILABLE", "ANCHOR_FILE_MISSING"))
        self.assertFalse((self.vault / "nothing.json").exists())                                     # auditing never creates an anchor
        path = self.anchor_file()
        gone = type("Run", (), {"events": self.tmp / "gone.jsonl", "state": self.run_.state})
        report = self.audit(gone, anchor_path=path)
        self.assertEqual(report["anchor"]["status"], "UNAVAILABLE")

    def test_anchor_consistency_and_accounting_are_independent_questions(self):
        path = self.anchor_file()
        state = self.tmp / "tampered"
        state.mkdir()
        target = state / STATE_FILE
        lines = self.run_.state.read_bytes().split(b"\n")
        trade = json.loads(lines[3])
        trade["realized_pnl"] = "1.00"
        lines[3] = json.dumps(trade, sort_keys=True).encode()
        target.write_bytes(b"\n".join(lines))
        report = audit.build_report(audit.AuditConfig(self.run_.events, target, FIXTURE_DIR, anchor_path=path))
        self.assertEqual((report["verdict"], report["chain"]["status"], report["anchor"]["status"]), ("INCONSISTENT", "VALID", "MATCH"))
        self.assertEqual(report["trust"]["authenticity"], "NOT_ESTABLISHED")

    def test_expected_head_keeps_its_behaviour_and_is_reported_in_the_anchor_section(self):
        head = self.objs[-1]["hash"]
        good = self.audit(expected_head=head)
        self.assertEqual((good["chain"]["anchored"], good["chain"]["status"], good["anchor"]["status"]), (True, "VALID", "MATCH"))
        self.assertEqual(good["anchor"]["parts"]["expected_head"]["protection"], "LOCAL_OR_UNDECLARED")
        bad = self.audit(expected_head="0" * 64)
        self.assertEqual((bad["chain"]["status"], bad["chain"]["reason"], bad["anchor"]["status"], bad["verdict"]), ("INVALID", "HEAD_MISMATCH", "MISMATCH", "CONSISTENT"))
        malformed = self.audit(expected_head="xyz")
        self.assertEqual((malformed["chain"]["reason"], malformed["anchor"]["status"]), ("MALFORMED_EXPECTED_HEAD", "MISMATCH"))
        declared = self.audit(expected_head=head, anchor_trust="independent")
        self.assertEqual((declared["anchor"]["protection"], declared["anchor"]["parts"]["expected_head"]["downgraded"]), ("DECLARED_INDEPENDENT", False))

    def test_both_an_anchor_file_and_a_head_must_match(self):
        path = self.anchor_file()
        report = self.audit(anchor_path=path, expected_head="0" * 64)
        self.assertEqual((report["anchor"]["parts"]["anchor_file"]["status"], report["anchor"]["parts"]["expected_head"]["status"], report["anchor"]["status"]),
                         ("MATCH", "MISMATCH", "MISMATCH"))
        report = self.audit(anchor_path=path, expected_head=self.objs[-1]["hash"])
        self.assertEqual(report["anchor"]["status"], "MATCH")


class TestProtectionIsNeverAssumed(AnchorBase):
    def test_independent_is_only_ever_a_declaration(self):
        path = self.anchor_file()
        local = self.audit(anchor_path=path)["anchor"]
        self.assertEqual(local["protection"], "LOCAL_OR_UNDECLARED")
        declared = self.audit(anchor_path=path, anchor_trust="independent")["anchor"]
        self.assertEqual(declared["protection"], "DECLARED_INDEPENDENT")
        self.assertIn("did not verify the declaration", declared["protection_note"])
        self.assertIn("not verified", audit.render_text(self.audit(anchor_path=path, anchor_trust="independent")).lower().replace("by you, ", ""))
        with self.assertRaises(ValueError):
            self.audit(anchor_path=path, anchor_trust="trusted")

    def test_a_declaration_is_downgraded_when_the_anchor_sits_beside_the_log_or_state(self):
        path = self.anchor_file()
        for name, folder in (("beside_log", self.run_.events.parent), ("beside_state", self.run_.state.parent)):
            copy = folder / f"{name}.json"
            copy.write_bytes(path.read_bytes())
            section = self.audit(anchor_path=copy, anchor_trust="independent")["anchor"]
            self.assertEqual(section["protection"], "LOCAL_OR_UNDECLARED", name)
            entry = section["parts"]["anchor_file"]
            self.assertEqual((entry["downgraded"], entry["beside_log_or_state"], entry["status"]), (True, True, "MATCH"), name)
            copy.unlink()

    def test_a_local_anchor_can_be_rewritten_by_whoever_can_rewrite_the_log_so_it_is_not_a_trust_boundary(self):
        original = self.anchor_file()
        preserved = self.tmp / "usb"                                                      # stands for a copy the writer could not touch
        preserved.mkdir()
        shutil.copy(original, preserved / "anchor.json")
        # the "attacker" can write the log, the state and the local anchor: rebuild the log with an edit, then make a fresh anchor for it
        edited = [{k: v for k, v in o.items() if k not in eventchain.RESERVED} for o in self.objs]
        edited[2]["note"] = "changed by the writer"
        data, _ = dump(edited)
        self.run_.events.write_bytes(data)
        original.unlink()
        anchor.create(self.run_.events, self.run_.state, original)
        fooled = self.audit(anchor_path=original)["anchor"]
        self.assertEqual((fooled["status"], fooled["protection"]), ("MATCH", "LOCAL_OR_UNDECLARED"))     # passes, and says plainly that it proves little
        self.assertIn("NOT independently protected", fooled["reading"])
        caught = self.audit(anchor_path=preserved / "anchor.json", anchor_trust="independent")["anchor"]
        self.assertEqual((caught["status"], caught["protection"]), ("MISMATCH", "DECLARED_INDEPENDENT"))  # the preserved copy is what detects it
        self.assertEqual(caught["authenticity"], "NOT_ESTABLISHED")


class TestCreation(AnchorBase):
    def refused(self, code, events=None, state=None, out=None):
        out = out or self.vault / "new.json"
        with self.assertRaises(anchor.AnchorError) as caught:
            anchor.create(events or self.run_.events, state or self.run_.state, out)
        self.assertEqual(caught.exception.code, code)
        self.assertFalse(out.exists())
        self.assertEqual([p.name for p in self.vault.iterdir()], [p.name for p in self.vault.iterdir() if not p.name.startswith(".")])     # no partial left behind
        return caught.exception

    def test_creation_is_explicit_and_audit_never_creates(self):
        self.audit(anchor_path=self.vault / "never.json")
        self.assertEqual(list(self.vault.iterdir()), [])
        text = Path(audit.__file__).read_text(encoding="utf-8")
        for banned in ("anchor_module.create", "publish", "canonical_line"):
            self.assertNotIn(banned, text, banned)

    def test_an_existing_anchor_is_never_overwritten(self):
        path = self.anchor_file()
        before = path.read_bytes()
        with self.assertRaises(anchor.AnchorError) as caught:
            anchor.create(self.run_.events, self.run_.state, path)
        self.assertEqual(caught.exception.code, "DESTINATION_EXISTS")
        self.assertEqual(path.read_bytes(), before)
        (self.vault / "dir.json").mkdir()
        with self.assertRaises(anchor.AnchorError):
            anchor.create(self.run_.events, self.run_.state, self.vault / "dir.json")

    def test_invalid_incomplete_or_missing_logs_are_not_anchored(self):
        pieces = lines_of(self.data)
        variants = {
            "modified": ("\n".join(pieces[:3] + [pieces[3].replace('"clock":"2031-03-03T10:1', '"clock":"2031-03-03T10:2', 1)] + pieces[4:]) + "\n").encode(),
            "deleted": ("\n".join(pieces[:3] + pieces[4:]) + "\n").encode(), "cut in a line": self.data[:-20], "empty": b"", "junk": b"junk\n",
            "legacy": "".join(json.dumps({k: v for k, v in o.items() if k not in eventchain.RESERVED}) + "\n" for o in self.objs).encode(),
        }
        for name, data in variants.items():
            run = self.write_log("v_" + name.replace(" ", "_"), data)
            self.refused("LOG_NOT_VALID", events=run.events)
        self.refused("LOG_NOT_VALID", events=self.tmp / "no" / "events.jsonl")
        no_checkpoint = self.write_log("unfinished", ("\n".join(pieces[:-1]) + "\n").encode())
        self.assertEqual(audit.verify_chain(no_checkpoint.events.read_bytes())["status"], "VALID")           # a valid chain, but an unfinished log
        self.assertEqual(self.refused("LOG_INCOMPLETE", events=no_checkpoint.events).code, "LOG_INCOMPLETE")
        headless, _ = dump([{**{k: v for k, v in o.items() if k not in eventchain.RESERVED}, "n": number} for number, o in enumerate(self.objs[1:], 1)])
        self.refused("LOG_INCOMPLETE", events=self.write_log("headless", headless).events)

    def test_the_log_must_belong_to_the_replay_account(self):
        other = self.replay_other_source()
        self.refused("SOURCE_MISMATCH", state=other.state)
        self.refused("STATE_UNREADABLE", state=self.tmp / "nostate" / "none.jsonl")
        engine_dir = self.tmp / "synth"
        engine_dir.mkdir()
        path = engine_dir / "paper_synthetic.jsonl"
        engine = PaperEngine(Decimal("1000000"), store=JsonlStore(path, Decimal("1000000"), account="synthetic"), origin=ORIGIN_SYNTHETIC)
        view = MarketView(ORIGIN_SYNTHETIC, "synthetic", "SIMULATED", "SIMULATED", CE, Decimal("100.00"), Decimal("100.20"), None, 650, 650, Decimal("0.05"), 65, None, None, None, True, True)
        self.assertTrue(engine.simulate(TradeRequest(CE, "BUY", 1), view).accepted)
        engine.close()
        self.refused("NOT_A_REPLAY_ACCOUNT", state=path)

    def test_an_anchor_beside_the_log_or_state_is_refused(self):
        run = self.run_
        for out in (run.events.parent / "a.json", run.state.parent / "a.json", run.events.parent / "sub" / "a.json", run.events.parent.parent / "a.json",
                    run.events.parent / ".." / "run" / "a.json"):
            with self.subTest(str(out)), self.assertRaises(anchor.AnchorError) as caught:
                anchor.create(run.events, run.state, out)
            self.assertEqual(caught.exception.code, "ANCHOR_BESIDE_LOG")
        self.assertEqual(sorted(p.name for p in run.events.parent.iterdir()), ["events.jsonl", "state"])
        self.assertFalse(anchor.beside(self.vault / "a.json", run.events, run.state))

    def test_failures_while_writing_leave_nothing_and_never_report_success(self):
        with mock.patch("os.fsync", side_effect=OSError(errno.ENOSPC, "full")), self.assertRaises(anchor.AnchorError) as caught:
            anchor.create(self.run_.events, self.run_.state, self.vault / "x.json")
        self.assertEqual(caught.exception.code, "WRITE_FAILED")
        self.assertEqual(list(self.vault.iterdir()), [])
        real_link = os.link

        def racing(source, target, **kw):
            Path(target).write_bytes(b"someone else's\n")
            return real_link(source, target, **kw)

        with mock.patch("os.link", racing), self.assertRaises(anchor.AnchorError) as caught:
            anchor.create(self.run_.events, self.run_.state, self.vault / "x.json")
        self.assertEqual(caught.exception.code, "DESTINATION_EXISTS")
        self.assertEqual((self.vault / "x.json").read_bytes(), b"someone else's\n")
        self.assertEqual([p.name for p in self.vault.iterdir()], ["x.json"])

    def test_creation_reads_only_and_writes_one_new_file(self):
        files = [self.run_.events, self.run_.state]
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}
        listing = sorted(p.name for p in self.run_.events.parent.iterdir()), sorted(p.name for p in self.run_.state.parent.iterdir())
        self.anchor_file()
        self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}, before)
        self.assertEqual((sorted(p.name for p in self.run_.events.parent.iterdir()), sorted(p.name for p in self.run_.state.parent.iterdir())), listing)
        self.assertEqual([p.name for p in self.vault.iterdir()], ["anchor.json"])


class TestReadOnlyAudit(AnchorBase):
    def test_auditing_with_an_anchor_changes_nothing(self):
        path = self.anchor_file()
        files = [self.run_.events, self.run_.state, path]
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}
        listings = [sorted(p.name for p in d.iterdir()) for d in (self.run_.events.parent, self.run_.state.parent, self.vault)]
        self.audit(anchor_path=path)
        self.audit(anchor_path=path, anchor_trust="independent", expected_head=self.objs[-1]["hash"])
        self.audit(anchor_path=self.vault / "missing.json")
        self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}, before)
        self.assertEqual([sorted(p.name for p in d.iterdir()) for d in (self.run_.events.parent, self.run_.state.parent, self.vault)], listings)

    def test_reports_are_deterministic_and_the_digest_covers_the_anchor_section(self):
        path = self.anchor_file()
        first = self.audit(anchor_path=path)
        self.assertEqual(self.audit(anchor_path=path), first)
        self.assertEqual(self.audit(anchor_path=self.anchor_file("copy.json"))["digest"], first["digest"])         # same content, different file name
        self.assertNotEqual(self.audit()["digest"], first["digest"])
        self.assertNotEqual(self.audit(anchor_path=path, anchor_trust="independent")["digest"], first["digest"])
        self.assertNotIn(str(self.tmp), json.dumps(first))


class TestCommandLines(AnchorBase):
    def setUp(self):
        super().setUp()
        self.anchor_tool, self.audit_tool = load_tool("replay_anchor"), load_tool("replay_audit")

    def call(self, tool, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(argv)
        return code, out.getvalue(), err.getvalue()

    def create_args(self, out, *extra):
        return ["create", "--events", str(self.run_.events), "--state", str(self.run_.state), "--out", str(out), *extra]

    def test_create_says_what_the_location_is_worth(self):
        code, out, err = self.call(self.anchor_tool, self.create_args(self.vault / "a.json"))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("LOCAL", out)
        self.assertIn("NOT an independent trust anchor", out)
        self.assertIn("does not show the events are genuine", out)
        code, out, _ = self.call(self.anchor_tool, self.create_args(self.vault / "b.json", "--declare-protected"))
        self.assertEqual(code, 0)
        self.assertIn("you declared", out)
        self.assertIn("did not and cannot check", out)
        self.assertEqual((self.vault / "a.json").read_bytes(), (self.vault / "b.json").read_bytes())                  # the declaration is not stored in the anchor

    def test_create_refusals_exit_2_with_a_reason(self):
        self.call(self.anchor_tool, self.create_args(self.vault / "a.json"))
        for argv, reason in ((self.create_args(self.vault / "a.json"), "DESTINATION_EXISTS"), (self.create_args(self.run_.events.parent / "a.json"), "ANCHOR_BESIDE_LOG"),
                             (["create", "--events", str(self.tmp / "nolog" / "none"), "--state", str(self.run_.state), "--out", str(self.vault / "c.json")], "LOG_NOT_VALID")):
            code, out, err = self.call(self.anchor_tool, argv)
            self.assertEqual((code, out), (2, ""))
            self.assertIn(reason, err)
        self.assertEqual(sorted(p.name for p in self.vault.iterdir()), ["a.json"])

    def test_show_is_read_only(self):
        path = self.anchor_file()
        before = path.read_bytes()
        code, out, _ = self.call(self.anchor_tool, ["show", "--anchor", str(path)])
        self.assertEqual(code, 0)
        self.assertIn('"head":', out)
        self.assertEqual(path.read_bytes(), before)
        (self.vault / "bad.json").write_bytes(b"junk\n")
        code, _, err = self.call(self.anchor_tool, ["show", "--anchor", str(self.vault / "bad.json")])
        self.assertEqual(code, 2)
        self.assertIn("MALFORMED_ANCHOR", err)

    def audit_args(self, *extra, run=None):
        run = run or self.run_
        return ["--events", str(run.events), "--state", str(run.state), "--fixtures", str(FIXTURE_DIR), *extra]

    def test_audit_with_an_anchor_and_the_exit_codes(self):
        path = self.anchor_file()
        code, out, _ = self.call(self.audit_tool, self.audit_args("--anchor", str(path), "--out", str(self.tmp / "r1.json")))
        self.assertEqual(code, 0)
        self.assertIn("anchor MATCH (LOCAL: not an independent anchor)", out)
        self.assertIn("authenticity NOT_ESTABLISHED", out)
        code, out, _ = self.call(self.audit_tool, self.audit_args("--anchor", str(path), "--anchor-trust", "independent", "--out", str(self.tmp / "r2.json")))
        self.assertIn("declared independent, not verified", out)
        code, out, _ = self.call(self.audit_tool, self.audit_args("--anchor", str(path), "--text"))
        self.assertEqual(code, 0)
        self.assertIn("Anchor: MATCH (LOCAL or undeclared: NOT an independent trust anchor)", out)
        self.assertIn("Authenticity: NOT ESTABLISHED", out)
        cut = self.write_log("cutcli", ("\n".join(lines_of(self.data)[:-1]) + "\n").encode())
        base = self.audit_args("--anchor", str(path), run=cut)
        for flags, expected in (([], 1), (["--require-anchor"], 3), (["--require-valid-chain"], 1)):
            self.assertEqual(self.call(self.audit_tool, base + flags)[0], expected, flags)                           # INCONSISTENT stays 1; the anchor flag turns it into 3
        tampered = self.write_log("modcli", self.data.replace(b'"FILL"', b'"FILX"', 1))
        self.assertEqual(self.call(self.audit_tool, self.audit_args("--anchor", str(path), run=tampered))[0], 2)    # damaged events keep their exit code
        self.assertEqual(self.call(self.audit_tool, self.audit_args())[0], 0)                                       # no anchor flags: contract unchanged
        self.assertEqual(self.call(self.audit_tool, self.audit_args("--require-anchor"))[0], 3)                    # required but not supplied
        self.assertEqual(self.call(self.audit_tool, self.audit_args("--anchor", str(path), "--require-anchor"))[0], 0)
        with self.assertRaises(SystemExit):
            self.call(self.audit_tool, self.audit_args("--anchor-trust", "trusted"))

    def test_existing_expected_head_option_still_works_the_same_way(self):
        head = self.objs[-1]["hash"]
        self.assertEqual(self.call(self.audit_tool, self.audit_args("--expected-head", head, "--require-valid-chain"))[0], 0)
        self.assertEqual(self.call(self.audit_tool, self.audit_args("--expected-head", "0" * 64, "--require-valid-chain"))[0], 3)
        self.assertEqual(self.call(self.audit_tool, self.audit_args("--expected-head", "0" * 64))[0], 0)


class TestConsistencyOfConstants(unittest.TestCase):
    def test_module_properties(self):
        text = Path(anchor.__file__).read_text(encoding="utf-8")
        for banned in ("datetime.now", "os.environ", "socket", "urllib", "eventchain", "token", "password"):
            self.assertNotIn(banned, text, banned)
        self.assertEqual(anchor.GENESIS, eventchain.GENESIS)
        self.assertEqual(len(anchor.FIELDS), len(set(anchor.FIELDS)))


if __name__ == "__main__":
    unittest.main()
