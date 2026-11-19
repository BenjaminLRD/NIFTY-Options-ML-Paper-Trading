"""Hash-chained replay event log: the writer, the audit's independent verification, adversarial edits to the file, and safe publishing. Invented fixtures only."""
import contextlib
import errno
import hashlib
import io
import json
import os
import threading
import unittest
from pathlib import Path
from unittest import mock

from niftypaper.historical import audit, eventchain, replay
from tests.historical_fixtures import AS_OF, CE, PE
from tests.test_historical_audit import Base, SCRIPT, load_tool
from tests.test_historical_replay import END, FIXTURE_DIR, START, STATE_FILE, at

GENESIS = "f610a30490733bbcf28fc1ba8f5dbc0c0adc9c45317def914e99e35e75d9cd32"
SAMPLE_LINE = ('{"chain":"1","clock":"2031-03-03T10:14:00+05:30","n":1,"note":"caf\\u00e9","prev":"' + GENESIS + '","type":"EXAMPLE"}')
SAMPLE_HASH = "91b7adb3e0124de4639f4dda8d90307b89d5ad66d1d27d62c723173434f923e1"


def lines_of(data):
    return data.decode("utf-8").split("\n")[:-1]


def dump(objs):
    """Re-chain a list of event dicts (without chain fields) from the genesis, exactly as the writer would."""
    head, out = eventchain.GENESIS, []
    for obj in objs:
        event = eventchain.seal({k: v for k, v in obj.items() if k not in eventchain.RESERVED}, head)
        head = event["hash"]
        out.append(eventchain.line(event))
    return "".join(out).encode("utf-8"), head


class ChainBase(Base):
    def setUp(self):
        super().setUp()
        self.run_ = self.make_run()
        self.data = self.run_.events.read_bytes()
        self.objs = [json.loads(x) for x in lines_of(self.data)]

    def verify(self, data, **kw):
        return audit.verify_chain(data, **kw)

    def assertInvalid(self, data, reason, line=None, **kw):
        result = self.verify(data, **kw)
        self.assertEqual((result["status"], result["reason"]), ("INVALID", reason), result)
        if line is not None:
            self.assertEqual(result["first_failure"]["line"], line, result)
        return result

    def mutated(self, fn):
        pieces = lines_of(self.data)
        fn(pieces)
        return ("\n".join(pieces) + "\n").encode("utf-8")

    def edit_line(self, index, fn, rehash=False):
        """Change one event's fields. With rehash=True the line gets a fresh valid hash of its own (but later lines are not updated)."""
        def change(pieces):
            obj = json.loads(pieces[index])
            fn(obj)
            if rehash:
                obj["hash"] = eventchain.event_hash(obj)
            pieces[index] = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return self.mutated(change)


class TestSpecification(unittest.TestCase):
    def test_genesis_and_a_known_answer_vector(self):
        self.assertEqual(eventchain.GENESIS, GENESIS)
        self.assertEqual(audit.CHAIN_GENESIS, GENESIS)
        event = {"chain": "1", "clock": "2031-03-03T10:14:00+05:30", "n": 1, "note": "café", "prev": GENESIS, "type": "EXAMPLE"}
        self.assertEqual(eventchain.canonical(event), SAMPLE_LINE)
        self.assertEqual(eventchain.event_hash(event), SAMPLE_HASH)
        sealed = eventchain.seal({"clock": event["clock"], "n": 1, "note": "café", "type": "EXAMPLE"}, GENESIS)
        self.assertEqual(sealed["hash"], SAMPLE_HASH)
        spec = hashlib.sha256(b"niftypaper.replay.chain.event/1\n" + SAMPLE_LINE.encode("ascii")).hexdigest()        # the specification, written out once more
        self.assertEqual(spec, SAMPLE_HASH)
        log = (eventchain.line(sealed)).encode()
        result = audit.verify_chain(log)
        self.assertEqual((result["status"], result["events_verified"], result["head"]), ("VALID", 1, SAMPLE_HASH))

    def test_the_hash_covers_every_field_except_itself(self):
        base = eventchain.seal({"n": 1, "clock": "2031-03-03T10:14:00+05:30", "type": "EXAMPLE", "x": "1"}, GENESIS)
        self.assertEqual(eventchain.event_hash(base), base["hash"])                       # an existing hash field is ignored, so it cannot hash itself
        for name, value in (("n", 2), ("clock", "2031-03-03T10:14:01+05:30"), ("type", "OTHER"), ("x", "2"), ("prev", "1" * 64), ("chain", "2"), ("new", "field")):
            self.assertNotEqual(eventchain.event_hash({**base, name: value}), base["hash"], name)

    def test_the_writer_refuses_what_it_cannot_chain(self):
        with self.assertRaises(eventchain.ChainError):
            eventchain.seal({"n": 1, "x": 5.0}, GENESIS)
        with self.assertRaises(eventchain.ChainError):
            eventchain.seal({"n": 1, "nested": {"y": [1, 2.5]}}, GENESIS)
        with self.assertRaises(eventchain.ChainError):
            eventchain.seal({"n": 1, "x": object()}, GENESIS)
        with self.assertRaises(eventchain.ChainError):
            eventchain.seal({"n": 1, "hash": "x"}, GENESIS)
        with self.assertRaises(eventchain.ChainError):
            eventchain.seal({"n": 1, "prev": "x"}, GENESIS)
        with self.assertRaises(eventchain.ChainError):
            eventchain.seal({1: "x"}, GENESIS)
        with self.assertRaises(ValueError):
            eventchain.canonical({"x": float("nan")})

    def test_unknown_event_types_are_chained_and_verified_not_skipped(self):
        data, head = dump([{"n": 1, "clock": "2031-03-03T10:14:00+05:30", "type": "SOMETHING_NEW", "payload": {"a": [1, 2, 3]}},
                           {"n": 2, "clock": "2031-03-03T10:14:00+05:30", "type": "ANOTHER_NEW_TYPE"}])
        result = audit.verify_chain(data)
        self.assertEqual((result["status"], result["events_verified"], result["head"]), ("VALID", 2, head))
        tampered = data.replace(b"ANOTHER_NEW_TYPE", b"ANOTHER_NEW_TYPX")
        self.assertEqual(audit.verify_chain(tampered)["reason"], "HASH_MISMATCH")


class TestWriter(ChainBase):
    def test_every_event_is_chained_and_the_log_verifies(self):
        self.assertEqual(self.objs[0]["prev"], GENESIS)
        for index, event in enumerate(self.objs, 1):
            self.assertEqual((event["n"], event["chain"]), (index, "1"))
            self.assertEqual(event["prev"], GENESIS if index == 1 else self.objs[index - 2]["hash"])
            self.assertEqual(event["hash"], eventchain.event_hash(event))
        types = {e["type"] for e in self.objs}
        self.assertTrue({"REPLAY_OPENED", "RECORD_AVAILABLE", "INTENT_RECEIVED", "INTENT_REFUSED", "FILL", "CHECKPOINT"} <= types, types)
        result = self.verify(self.data)
        self.assertEqual((result["status"], result["reason"], result["events_verified"], result["head"], result["first_failure"]),
                         ("VALID", None, len(self.objs), self.objs[-1]["hash"], None))
        self.assertFalse(result["anchored"])

    def test_lines_are_canonical_ascii_and_free_of_floats_and_paths(self):
        text = self.data.decode("ascii")
        for line in text.splitlines():
            self.assertEqual(line, eventchain.canonical(json.loads(line)))
        self.assertNotIn("\r", text)
        self.assertNotIn(str(self.tmp), text)
        self.assertNotRegex(text, r"[:,\[]-?\d+\.\d+[,}\]]")                         # no bare decimal numbers: decimals travel as strings
        self.assertIn('"max_quote_age_seconds":"5.0"', text)

    def test_identical_runs_give_identical_bytes_and_heads(self):
        other = self.make_run("again")
        self.assertEqual(other.events.read_bytes(), self.data)
        self.assertEqual(json.loads(lines_of(other.events.read_bytes())[-1])["hash"], self.objs[-1]["hash"])

    def test_receipt_and_head(self):
        runner = replay.ReplayRunner.open(replay.ReplayConfig(fixture_dir=FIXTURE_DIR, state_dir=self.tmp / "rs", as_of=AS_OF, start=START, end=END, permitted=True))
        self.addCleanup(runner.close)
        self.assertEqual(runner.head, runner.events[-1]["hash"])
        runner.run(SCRIPT)
        path = self.tmp / "receipt.jsonl"
        receipt = runner.write_events(path)
        data = path.read_bytes()
        self.assertEqual((receipt.path, receipt.events, receipt.head, receipt.sha256, receipt.bytes),
                         (path, len(runner.events), runner.head, hashlib.sha256(data).hexdigest(), len(data)))
        self.assertEqual(runner.log_receipt, receipt)
        self.assertEqual(runner.events[-1]["type"], "CHECKPOINT")
        self.assertEqual(audit.verify_chain(data, expected_head=receipt.head)["anchored"], True)

    def test_an_event_that_cannot_be_chained_is_never_half_recorded(self):
        runner = replay.ReplayRunner.open(replay.ReplayConfig(fixture_dir=FIXTURE_DIR, state_dir=self.tmp / "rs", as_of=AS_OF, start=START, end=END, permitted=True))
        self.addCleanup(runner.close)
        count, head = len(runner.events), runner.head
        with self.assertRaises(eventchain.ChainError):
            runner._emit("ODD", value=1.5)
        self.assertEqual((len(runner.events), runner.head), (count, head))
        runner._emit("ODD", value="1.5")
        self.assertEqual(runner.events[-1]["prev"], head)


class TestAdversarialEdits(ChainBase):
    def test_a_deleted_event(self):
        for index in (1, 3, 10, len(self.objs) - 2):
            data = self.mutated(lambda p, i=index: p.pop(i))
            self.assertInvalid(data, "SEQUENCE_GAP", index + 1)
        self.assertInvalid(self.mutated(lambda p: p.pop(0)), "FIRST_SEQUENCE_NOT_ONE", 1)

    def test_the_last_event_deleted_leaves_a_valid_chain_that_only_an_anchor_catches(self):
        data = self.mutated(lambda p: p.pop())
        self.assertEqual(self.verify(data)["status"], "VALID")                           # stated limitation: a cut at an event boundary is still a chain
        self.assertInvalid(data, "HEAD_MISMATCH", expected_head=self.objs[-1]["hash"])
        self.assertTrue(self.verify(self.data, expected_head=self.objs[-1]["hash"])["anchored"])
        report = self.report(type("Run", (), {"events": self.write("cut", data), "state": self.run_.state}))
        self.assertEqual(report["chain"]["status"], "VALID")
        self.assertEqual(report["verdict"], "INCONSISTENT")                              # the accounting checks still notice the missing final checkpoint

    def write(self, name, data):
        path = self.tmp / f"{name}.jsonl"
        path.write_bytes(data)
        return path

    def test_a_duplicated_event(self):
        self.assertInvalid(self.mutated(lambda p: p.insert(5, p[4])), "SEQUENCE_DUPLICATE", 6)
        self.assertInvalid(self.mutated(lambda p: p.append(p[-1])), "SEQUENCE_DUPLICATE", len(self.objs) + 1)

    def test_reordered_events(self):
        self.assertInvalid(self.mutated(lambda p: p.__setitem__(slice(4, 6), [p[5], p[4]])), "SEQUENCE_GAP", 5)
        self.assertInvalid(self.mutated(lambda p: p.__setitem__(slice(0, 2), [p[1], p[0]])), "FIRST_SEQUENCE_NOT_ONE", 1)

    def test_a_modified_event(self):
        for index in (0, 4, 9, len(self.objs) - 1):
            data = self.edit_line(index, lambda o: o.__setitem__("clock", "2031-03-03T10:15:09+05:30"))
            self.assertInvalid(data, "HASH_MISMATCH", index + 1)
        fill = next(i for i, o in enumerate(self.objs) if o["type"] == "FILL")
        self.assertInvalid(self.edit_line(fill, lambda o: o.__setitem__("cash", "1.00")), "HASH_MISMATCH", fill + 1)
        self.assertInvalid(self.edit_line(fill, lambda o: o.pop("quote_ref")), "HASH_MISMATCH", fill + 1)
        self.assertInvalid(self.edit_line(fill, lambda o: o.__setitem__("extra", "x")), "HASH_MISMATCH", fill + 1)

    def test_a_modified_event_with_its_own_hash_fixed_breaks_the_next_link(self):
        data = self.edit_line(6, lambda o: o.__setitem__("clock", "2031-03-03T10:15:09+05:30"), rehash=True)
        self.assertInvalid(data, "LINK_BROKEN", 8)

    def test_a_forged_event_inserted_with_a_valid_hash(self):
        forged = eventchain.seal({"n": 5, "clock": self.objs[3]["clock"], "type": "RECORD_AVAILABLE", "ref": "x#1"}, self.objs[3]["hash"])
        data = self.mutated(lambda p: p.insert(4, eventchain.canonical(forged)))
        self.assertInvalid(data, "SEQUENCE_DUPLICATE", 6)                                 # the original fifth event now repeats number 5

    def test_a_writer_who_rebuilds_the_whole_chain_passes_unless_the_head_was_recorded(self):
        edited = [dict(o) for o in self.objs]
        edited[7]["clock"] = "2031-03-03T10:15:09+05:30"
        data, head = dump(edited)
        self.assertEqual(self.verify(data)["status"], "VALID")                           # stated limitation, not a defect
        self.assertNotEqual(head, self.objs[-1]["hash"])
        self.assertInvalid(data, "HEAD_MISMATCH", expected_head=self.objs[-1]["hash"])

    def test_truncation_inside_a_line_and_incomplete_final_records(self):
        for cut in (1, 7, len(lines_of(self.data)[-1]) // 2):
            data = self.data[: len(self.data) - 1 - cut]
            result = self.assertInvalid(data, "INCOMPLETE_FINAL_RECORD", len(self.objs))
            self.assertEqual(result["events_verified"], len(self.objs) - 1)
            self.assertEqual(result["head"], self.objs[-2]["hash"])                      # how far the log can be trusted
        self.assertInvalid(self.data[:-1], "INCOMPLETE_FINAL_RECORD", len(self.objs))
        self.assertInvalid(b'{"n":', "INCOMPLETE_FINAL_RECORD", 1)

    def test_empty_missing_and_unreadable_input(self):
        self.assertInvalid(b"", "EMPTY_FILE")
        self.assertInvalid(b"\n", "BLANK_LINE", 1)
        self.assertInvalid(b"   \n", "BLANK_LINE", 1)
        self.assertInvalid(b"\xff\xfe\x00", "NOT_UTF8")
        missing = self.verify(None)
        self.assertEqual((missing["status"], missing["reason"]), ("UNAVAILABLE", "MISSING_FILE"))
        report = audit.build_report(audit.AuditConfig(self.tmp / "none.jsonl", self.run_.state, FIXTURE_DIR))
        self.assertEqual((report["chain"]["status"], report["verdict"]), ("UNAVAILABLE", "NOT_RECONCILABLE"))

    def test_malformed_lines(self):
        self.assertInvalid(self.mutated(lambda p: p.insert(3, "")), "BLANK_LINE", 4)
        self.assertInvalid(self.mutated(lambda p: p.insert(3, "junk")), "MALFORMED_JSON", 4)
        self.assertInvalid(self.mutated(lambda p: p.insert(3, "[1]")), "NOT_AN_OBJECT", 4)
        self.assertInvalid(self.mutated(lambda p: p.insert(3, '{"n":4,"n":5}')), "MALFORMED_JSON", 4)
        self.assertInvalid(self.mutated(lambda p: p.insert(3, '{"n":NaN}')), "MALFORMED_JSON", 4)
        self.assertInvalid(b"\xef\xbb\xbf" + self.data, "MALFORMED_JSON", 1)               # a byte order mark

    def test_non_canonical_serialisation_is_refused(self):
        self.assertInvalid(self.data.replace(b"\n", b"\r\n"), "NON_CANONICAL_LINE", 1)
        self.assertInvalid(self.mutated(lambda p: p.__setitem__(2, json.dumps(json.loads(p[2])))), "NON_CANONICAL_LINE", 3)                    # default spacing
        self.assertInvalid(self.mutated(lambda p: p.__setitem__(2, json.dumps(json.loads(p[2]), sort_keys=True, indent=1).replace("\n", ""))), "NON_CANONICAL_LINE", 3)
        self.assertInvalid(self.mutated(lambda p: p.__setitem__(2, p[2] + " ")), "NON_CANONICAL_LINE", 3)
        self.assertInvalid(self.mutated(lambda p: p.__setitem__(2, " " + p[2])), "NON_CANONICAL_LINE", 3)
        reordered = lambda p: p.__setitem__(2, json.dumps(dict(reversed(list(json.loads(p[2]).items()))), separators=(",", ":")))
        self.assertInvalid(self.mutated(reordered), "NON_CANONICAL_LINE", 3)
        self.assertInvalid(self.mutated(lambda p: p.__setitem__(2, p[2].replace('"type"', '"\\u0074ype"'))), "NON_CANONICAL_LINE", 3)               # an escaped key
        self.assertInvalid(self.mutated(lambda p: p.__setitem__(2, p[2][:-1] + ',"zz":1.5}')), "NON_CANONICAL_LINE", 3)                       # a float

    def test_version_and_chain_field_problems(self):
        self.assertInvalid(self.edit_line(2, lambda o: o.__setitem__("chain", "2")), "UNSUPPORTED_VERSION", 3)
        self.assertInvalid(self.edit_line(0, lambda o: o.__setitem__("chain", "2")), "UNSUPPORTED_VERSION", 1)
        for bad in (1, None, True, ["1"]):
            self.assertInvalid(self.edit_line(2, lambda o, b=bad: o.__setitem__("chain", b)), "MALFORMED_VERSION", 3)
        for name in ("chain", "prev", "hash"):
            self.assertInvalid(self.edit_line(2, lambda o, n=name: o.pop(n)), "MISSING_CHAIN_FIELDS", 3)
        for value in ("1", 1.0, True, None, "x", 0, -1, 4, 10 ** 9):
            self.assertIn(self.verify(self.edit_line(2, lambda o, v=value: o.__setitem__("n", v)))["reason"],
                          ("MALFORMED_SEQUENCE", "SEQUENCE_DUPLICATE", "SEQUENCE_REORDERED", "SEQUENCE_GAP"), value)
        self.assertInvalid(self.edit_line(2, lambda o: o.__setitem__("n", "3")), "MALFORMED_SEQUENCE", 3)
        self.assertInvalid(self.edit_line(2, lambda o: o.__setitem__("n", True)), "MALFORMED_SEQUENCE", 3)
        self.assertInvalid(self.edit_line(2, lambda o: o.__setitem__("n", 3.0)), "MALFORMED_SEQUENCE", 3)

    def test_hash_format_and_genesis(self):
        for bad in ("A" * 64, "a" * 63, "g" * 64, 5, None, ""):
            self.assertInvalid(self.edit_line(2, lambda o, b=bad: o.__setitem__("hash", b)), "MALFORMED_HASH", 3)
            self.assertInvalid(self.edit_line(2, lambda o, b=bad: o.__setitem__("prev", b)), "MALFORMED_HASH", 3)
        self.assertInvalid(self.edit_line(0, lambda o: o.__setitem__("prev", "0" * 64), rehash=True), "GENESIS_MISMATCH", 1)
        self.assertInvalid(self.edit_line(0, lambda o: o.__setitem__("prev", eventchain.event_hash({"x": 1})), rehash=True), "GENESIS_MISMATCH", 1)

    def test_logs_without_a_chain_are_unavailable_not_valid_and_mixed_logs_are_invalid(self):
        bare = [{k: v for k, v in o.items() if k not in eventchain.RESERVED} for o in self.objs]
        legacy = "".join(json.dumps(o, sort_keys=True) + "\n" for o in bare).encode()
        result = self.verify(legacy)
        self.assertEqual((result["status"], result["reason"]), ("UNAVAILABLE", "NO_CHAIN_FIELDS"))
        run = type("Run", (), {"events": self.write("legacy", legacy), "state": self.run_.state})
        report = self.report(run)
        self.assertEqual((report["chain"]["status"], report["verdict"]), ("UNAVAILABLE", "CONSISTENT"))        # accounting does not depend on the chain
        stripped_middle = self.mutated(lambda p: p.__setitem__(4, json.dumps({k: v for k, v in json.loads(p[4]).items() if k not in eventchain.RESERVED}, sort_keys=True)))
        self.assertInvalid(stripped_middle, "MISSING_CHAIN_FIELDS", 5)
        stripped_first = legacy.split(b"\n", 1)[0] + b"\n" + b"\n".join(self.data.split(b"\n")[1:])
        self.assertInvalid(stripped_first, "MISSING_CHAIN_FIELDS", 2)

    def test_the_expected_head_must_be_well_formed(self):
        for bad in ("abc", "A" * 64, 5):
            self.assertInvalid(self.data, "MALFORMED_EXPECTED_HEAD", expected_head=bad)


class TestSeparationFromAccounting(ChainBase):
    def test_the_report_adds_a_chain_section_and_keeps_its_old_shape(self):
        report = self.report(self.run_)
        self.assertEqual(set(report), {"report_version", "notice", "verdict", "coverage", "inputs", "account", "summary", "inputs_identity", "pairing", "chain", "anchor", "trust", "checks", "exclusions", "rows", "digest"})
        self.assertEqual([c["id"] for c in report["checks"]][:3], ["E1_EVENTS_PARSE", "E2_EVENTS_SEQUENCE", "E3_EVENTS_CLOCK"])        # no chain row among the checks
        self.assertEqual((report["verdict"], report["coverage"], report["chain"]["status"]), ("CONSISTENT", "FULL", "VALID"))
        self.assertEqual(set(report["chain"]), {"status", "version", "genesis", "reason", "detail", "first_failure", "events_verified", "head", "anchored", "notice"})
        self.assertIn("does not show that the events are genuine", report["chain"]["notice"])

    def test_chain_valid_but_accounting_wrong(self):
        edited = [{k: v for k, v in o.items() if k not in eventchain.RESERVED} for o in self.objs]
        next(o for o in edited if o["type"] == "FILL")["cash"] = "1.00"
        data, _ = dump(edited)
        report = self.report(type("Run", (), {"events": self.write_data("rechained", data), "state": self.run_.state}))
        self.assertEqual((report["chain"]["status"], report["verdict"]), ("VALID", "INCONSISTENT"))

    def write_data(self, name, data):
        path = self.tmp / f"{name}.jsonl"
        path.write_bytes(data)
        return path

    def test_accounting_consistent_but_chain_invalid(self):
        data = self.edit_line(5, lambda o: o.__setitem__("note", "x"))
        report = self.report(type("Run", (), {"events": self.write_data("noted", data), "state": self.run_.state}))
        self.assertEqual((report["verdict"], report["chain"]["status"], report["chain"]["reason"]), ("CONSISTENT", "INVALID", "HASH_MISMATCH"))
        self.assertEqual(report["chain"]["first_failure"], {"line": 6, "n": 6})
        self.assertEqual(report["chain"]["events_verified"], 5)
        self.assertEqual(report["chain"]["head"], self.objs[4]["hash"])

    def test_damaged_events_are_not_reconcilable_and_the_chain_says_where(self):
        report = self.report(type("Run", (), {"events": self.write_data("gap", self.mutated(lambda p: p.pop(4))), "state": self.run_.state}))
        self.assertEqual((report["verdict"], report["chain"]["status"], report["chain"]["reason"]), ("NOT_RECONCILABLE", "INVALID", "SEQUENCE_GAP"))

    def test_the_report_digest_depends_on_the_chain_section(self):
        base = self.report(self.run_)
        anchored = audit.build_report(audit.AuditConfig(self.run_.events, self.run_.state, FIXTURE_DIR, expected_head=self.objs[-1]["hash"]))
        wrong = audit.build_report(audit.AuditConfig(self.run_.events, self.run_.state, FIXTURE_DIR, expected_head="0" * 64))
        self.assertEqual((anchored["chain"]["anchored"], wrong["chain"]["status"], wrong["chain"]["reason"]), (True, "INVALID", "HEAD_MISMATCH"))
        self.assertEqual((wrong["verdict"], anchored["verdict"]), ("CONSISTENT", "CONSISTENT"))
        self.assertEqual(len({base["digest"], anchored["digest"], wrong["digest"]}), 3)
        self.assertEqual(self.report(self.run_)["digest"], base["digest"])

    def test_the_text_view_and_command_line(self):
        tool = load_tool()
        text = audit.render_text(self.report(self.run_))
        self.assertIn("Event chain: VALID", text)
        bad = self.write_data("badchain", self.edit_line(5, lambda o: o.__setitem__("note", "x")))
        buffer, error = io.StringIO(), io.StringIO()
        base = ["--events", str(bad), "--state", str(self.run_.state), "--fixtures", str(FIXTURE_DIR)]
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(error):
            self.assertEqual(tool.main(base), 0)                                                 # the contract is unchanged without the new flag
            self.assertEqual(tool.main(base + ["--require-valid-chain"]), 3)
            good = ["--events", str(self.run_.events), "--state", str(self.run_.state), "--fixtures", str(FIXTURE_DIR)]
            self.assertEqual(tool.main(good + ["--require-valid-chain", "--expected-head", self.objs[-1]["hash"]]), 0)
            self.assertEqual(tool.main(good + ["--require-valid-chain", "--expected-head", "0" * 64]), 3)
            self.assertEqual(tool.main(base + ["--text"]), 0)
        self.assertIn("Event chain: INVALID (HASH_MISMATCH at line 6)", buffer.getvalue())

    def test_verification_is_read_only(self):
        before = (self.run_.events.read_bytes(), self.run_.events.stat().st_mtime_ns, sorted(p.name for p in self.run_.events.parent.iterdir()))
        self.report(self.run_)
        self.verify(self.run_.events.read_bytes())
        self.assertEqual((self.run_.events.read_bytes(), self.run_.events.stat().st_mtime_ns, sorted(p.name for p in self.run_.events.parent.iterdir())), before)
        text = Path(audit.__file__).read_text(encoding="utf-8")
        self.assertNotIn("eventchain", text)                                                  # the verifier is written from the specification, not from the writer
        self.assertNotIn("import replay", text)


class TestSafePublishing(Base):
    def open_runner(self, name="w"):
        runner = replay.ReplayRunner.open(replay.ReplayConfig(fixture_dir=FIXTURE_DIR, state_dir=self.tmp / name / "state", as_of=AS_OF, start=START, end=END, permitted=True))
        self.addCleanup(runner.close)
        runner.run(SCRIPT)
        return runner

    def folder(self, name):
        path = self.tmp / name
        path.mkdir()
        return path

    def test_success_leaves_only_the_log(self):
        runner = self.open_runner()
        folder = self.folder("ok")
        receipt = runner.write_events(folder / "events.jsonl")
        self.assertEqual([p.name for p in folder.iterdir()], ["events.jsonl"])
        self.assertEqual((folder / "events.jsonl").read_bytes(), runner.event_lines().encode())
        self.assertEqual(audit.verify_chain((folder / "events.jsonl").read_bytes(), expected_head=receipt.head)["status"], "VALID")

    def test_an_existing_destination_is_never_replaced(self):
        runner = self.open_runner()
        folder = self.folder("exists")
        (folder / "events.jsonl").write_bytes(b"precious\n")
        with self.assertRaises(replay.EventLogError) as caught:
            runner.write_events(folder / "events.jsonl")
        self.assertEqual(caught.exception.code, "DESTINATION_EXISTS")
        self.assertEqual((folder / "events.jsonl").read_bytes(), b"precious\n")
        self.assertEqual([p.name for p in folder.iterdir()], ["events.jsonl"])
        self.assertIsNone(runner.log_receipt)
        (folder / "dir.jsonl").mkdir()
        with self.assertRaises(replay.EventLogError):
            runner.write_events(folder / "dir.jsonl")

    def test_a_failed_write_is_not_a_completed_replay_and_a_retry_adds_nothing(self):
        runner = self.open_runner()
        folder = self.folder("fail")
        with mock.patch("os.fsync", side_effect=OSError(errno.ENOSPC, "disk full")):
            with self.assertRaises(replay.EventLogError) as caught:
                runner.write_events(folder / "events.jsonl")
        self.assertEqual(caught.exception.code, "WRITE_FAILED")
        self.assertEqual(list(folder.iterdir()), [])
        self.assertIsNone(runner.log_receipt)
        count = len(runner.events)
        receipt = runner.write_events(folder / "events.jsonl")                                  # the retry
        self.assertEqual((receipt.events, len(runner.events)), (count, count))                   # no second checkpoint, no omitted event
        self.assertEqual(audit.verify_chain((folder / "events.jsonl").read_bytes())["events_verified"], count)

    def test_failures_at_each_stage_leave_nothing_behind(self):
        runner = self.open_runner()
        cases = {
            "READBACK_MISMATCH": mock.patch.object(Path, "read_bytes", return_value=b"x"),
            "CHAIN_NOT_VALID": mock.patch.object(audit, "verify_chain", return_value={"status": "INVALID", "reason": "HASH_MISMATCH", "detail": "x", "events_verified": 0}),
            "CANNOT_CREATE": mock.patch("os.open", side_effect=PermissionError(errno.EACCES, "no")),
            "WRITE_FAILED": mock.patch("os.fdopen", side_effect=OSError(errno.EIO, "io")),
        }
        for code, patch in cases.items():
            folder = self.folder("stage_" + code)
            with patch, self.assertRaises(replay.EventLogError) as caught:
                runner.write_events(folder / "events.jsonl")
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(list(folder.iterdir()), [], code)
            self.assertIsNone(runner.log_receipt)

    def test_a_foreign_partial_file_is_left_alone(self):
        runner = self.open_runner()
        folder = self.folder("partial")
        partial = folder / f".events.jsonl.{os.getpid()}.partial"
        partial.write_bytes(b"someone else's")
        with self.assertRaises(replay.EventLogError) as caught:
            runner.write_events(folder / "events.jsonl")
        self.assertEqual(caught.exception.code, "PARTIAL_FILE_EXISTS")
        self.assertEqual(partial.read_bytes(), b"someone else's")
        self.assertFalse((folder / "events.jsonl").exists())

    def test_a_missing_folder_creates_nothing(self):
        runner = self.open_runner()
        with self.assertRaises(replay.EventLogError) as caught:
            runner.write_events(self.tmp / "no" / "such" / "events.jsonl")
        self.assertEqual(caught.exception.code, "CANNOT_CREATE")
        self.assertFalse((self.tmp / "no").exists())

    def test_a_destination_created_during_the_write_is_not_replaced(self):
        runner = self.open_runner()
        folder = self.folder("race")
        destination = folder / "events.jsonl"
        real_link = os.link

        def racing_link(source, target, **kw):
            Path(target).write_bytes(b"winner\n")                                                # another writer publishes first
            return real_link(source, target, **kw)

        with mock.patch("os.link", racing_link), self.assertRaises(replay.EventLogError) as caught:
            runner.write_events(destination)
        self.assertEqual(caught.exception.code, "DESTINATION_EXISTS")
        self.assertEqual(destination.read_bytes(), b"winner\n")
        self.assertEqual([p.name for p in folder.iterdir()], ["events.jsonl"])

    def test_without_hard_links_the_fallback_still_never_replaces(self):
        runner = self.open_runner()
        no_links = mock.patch("os.link", side_effect=OSError(errno.EPERM, "hard links unavailable"))
        free = self.folder("nolink")
        with no_links:
            if os.name == "nt":
                receipt = runner.write_events(free / "events.jsonl")                              # Windows: a rename, which refuses an existing destination
                self.assertEqual(audit.verify_chain((free / "events.jsonl").read_bytes(), expected_head=receipt.head)["status"], "VALID")
                self.assertEqual([p.name for p in free.iterdir()], ["events.jsonl"])
            else:
                with self.assertRaises(replay.EventLogError) as caught:
                    runner.write_events(free / "events.jsonl")
                self.assertEqual(caught.exception.code, "PUBLISH_UNSUPPORTED")
                self.assertEqual(list(free.iterdir()), [])
        taken = self.folder("nolink_taken")
        real_rename = os.rename

        def late_winner(source, target):
            Path(target).write_bytes(b"winner\n")
            return real_rename(source, target)

        with no_links, mock.patch("os.rename", late_winner), self.assertRaises(replay.EventLogError):
            runner.write_events(taken / "events.jsonl")
        self.assertEqual((taken / "events.jsonl").read_bytes(), b"winner\n")
        self.assertEqual([p.name for p in taken.iterdir()], ["events.jsonl"])

    def test_two_writers_to_one_destination_exactly_one_wins(self):
        runners = [self.open_runner("w1"), self.open_runner("w2")]
        folder = self.folder("two")
        outcomes = []

        def attempt(runner):
            try:
                outcomes.append(runner.write_events(folder / "events.jsonl"))
            except replay.EventLogError as error:
                outcomes.append(error)

        threads = [threading.Thread(target=attempt, args=(r,)) for r in runners]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wins = [o for o in outcomes if isinstance(o, replay.EventLogReceipt)]
        self.assertEqual((len(wins), len(outcomes)), (1, 2))
        self.assertEqual(audit.verify_chain((folder / "events.jsonl").read_bytes(), expected_head=wins[0].head)["status"], "VALID")
        self.assertEqual([p.name for p in folder.iterdir()], ["events.jsonl"])

    def test_on_this_platform(self):
        runner = self.open_runner()
        folder = self.folder("platform")
        runner.write_events(folder / "events.jsonl")
        self.assertTrue((folder / "events.jsonl").is_file())
        (folder / "other").write_bytes(b"x")
        if os.name == "nt":
            with self.assertRaises(FileExistsError):                  # the property the Windows fallback relies on: rename never replaces
                os.rename(folder / "other", folder / "events.jsonl")
        with self.assertRaises(FileExistsError):                      # and the primary path: a hard link never replaces
            os.link(folder / "other", folder / "events.jsonl")
        self.assertEqual((folder / "other").read_bytes(), b"x")


if __name__ == "__main__":
    unittest.main()
