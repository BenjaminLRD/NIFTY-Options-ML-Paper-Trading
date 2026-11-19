"""tools/replay_verify_later.py: strict, read-only verification of a saved replay. Invented fixtures and temporary folders only; the committed fixture folder
is only read."""
import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import unittest

from niftypaper.historical import anchor, identity
from tests import ROOT
from tests.test_historical_audit import SCRIPT
from tests.test_historical_chain import lines_of
from tests.test_historical_inputs import Base
from tests.test_historical_inputs import FIXTURE_DIR

TOOL = ROOT / "tools" / "replay_verify_later.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("verify_later_under_test", TOOL)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool


def snapshot(*roots):
    seen = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                seen[str(path)] = (path.read_bytes(), path.stat().st_mtime_ns)
        seen[str(root) + "|dirs"] = sorted(str(p) for p in root.rglob("*") if p.is_dir())
    return seen


class VerifyBase(Base):
    def setUp(self):
        super().setUp()
        self.tool = load_tool()
        self.run_ = self.run_variant("my run")                                       # a folder name with a space, on purpose
        self.anchor_path = self.tmp / "kept apart" / "anchor.json"
        anchor.create(self.run_.events, self.run_.state, self.anchor_path)
        self.head = json.loads(lines_of(self.run_.events.read_bytes())[-1])["hash"]

    def verify(self, run=None, fixtures=FIXTURE_DIR, *extra, anchor_file="default", head=None, trust="independent", detail=False):
        run = run or self.run_
        argv = ["--events", str(run.events), "--state", str(run.state), "--fixtures", str(fixtures)]
        if anchor_file == "default":
            anchor_file = self.anchor_path
        if anchor_file is not None:
            argv += ["--anchor", str(anchor_file)]
        if head is not None:
            argv += ["--expected-head", head]
        if trust:
            argv += ["--anchor-trust", trust]
        if detail:
            argv.append("--detail")
        argv += list(extra)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = self.tool.main(argv)
        return code, out.getvalue()

    def assertFails(self, result, *texts):
        code, out = result
        self.assertNotEqual(code, 0, out)
        self.assertEqual(out.rstrip("\n").splitlines()[-1], "FAIL", out)
        self.assertNotIn("PASS", out.splitlines(), out)
        for text in texts:
            self.assertIn(text, out)

    def anchored_variant(self, run, name):
        """An anchor and head for a (possibly doctored) log, so that only the other requirements can fail."""
        path = self.tmp / "anchors" / f"{name}.json"
        anchor.create(run.events, run.state, path)
        return path, json.loads(lines_of(run.events.read_bytes())[-1])["hash"]


class TestStrictPass(VerifyBase):
    def test_a_clean_replay_with_an_anchor_passes(self):
        code, out = self.verify()
        self.assertEqual(code, 0, out)
        self.assertEqual(out.rstrip("\n").splitlines()[-1], "PASS")
        for name in ("PLAN", "CHAIN", "ANCHOR", "IDENTITY", "PAIRING", "ACCOUNTING"):
            self.assertRegex(out, rf"ok\s+{name}")
        self.assertIn("YOUR declaration", out)
        self.assertIn("genuine", out)

    def test_a_clean_replay_with_only_an_expected_head_passes(self):
        code, out = self.verify(anchor_file=None, head=self.head)
        self.assertEqual((code, out.rstrip("\n").splitlines()[-1]), (0, "PASS"), out)

    def test_the_output_states_what_is_and_is_not_established(self):
        _, out = self.verify(detail=True)
        self.assertIn("Not verified by this tool", out)
        self.assertIn("genuine", out)
        self.assertIn("Authenticity: NOT ESTABLISHED", out)
        self.assertNotIn("1000000", "\n".join(l for l in out.splitlines() if l.startswith("  ok")))              # no state dump in the requirement lines

    def test_spaces_in_every_path_work_from_the_command_line_too(self):
        spaced = self.tmp / "fixtures with spaces"
        shutil.copytree(FIXTURE_DIR, spaced)
        done = subprocess.run([sys.executable, "-B", str(TOOL), "--events", str(self.run_.events), "--state", str(self.run_.state), "--fixtures", str(spaced),
                               "--anchor", str(self.anchor_path), "--anchor-trust", "independent"], capture_output=True, text=True, timeout=120)
        self.assertEqual((done.returncode, done.stdout.rstrip("\n").splitlines()[-1]), (0, "PASS"), done.stdout + done.stderr)


class TestEvidenceRequired(VerifyBase):
    def test_no_anchor_and_no_head_fails(self):
        self.assertFails(self.verify(anchor_file=None), "one of --anchor or --expected-head is required")

    def test_a_local_declaration_fails_even_with_a_matching_anchor(self):
        self.assertFails(self.verify(trust="local"), "--anchor-trust independent is required")
        self.assertFails(self.verify(trust=None), "--anchor-trust independent is required")

    def test_passing_independent_does_not_make_a_local_anchor_independent(self):
        beside = self.run_.events.parent / "anchor_copy.json"
        shutil.copyfile(self.anchor_path, beside)
        self.assertFails(self.verify(anchor_file=beside), "ANCHOR", "only a local consistency check")
        inside = self.fixture_copy("fx_anchor")
        shutil.copyfile(self.anchor_path, inside / "anchor.json")
        self.assertFails(self.verify(fixtures=inside, anchor_file=inside / "anchor.json"), "beside or inside the fixture folder")

    def test_a_wrong_expected_head_or_a_malformed_one_fails(self):
        self.assertFails(self.verify(anchor_file=None, head="0" * 64), "FAIL ANCHOR")
        self.assertFails(self.verify(anchor_file=None, head="abc"), "64 lowercase hex")
        self.assertFails(self.verify(head="1" * 64), "FAIL ANCHOR")                                  # a good anchor does not excuse a wrong head

    def test_a_missing_or_garbled_anchor_file_fails(self):
        self.assertFails(self.verify(anchor_file=self.tmp / "nope.json"), "does not exist")
        garbled = self.tmp / "other" / "garbled.json"
        garbled.parent.mkdir()
        garbled.write_text("{not an anchor", encoding="utf-8")
        self.assertFails(self.verify(anchor_file=garbled), "FAIL ANCHOR", "INVALID_ANCHOR")

    def test_missing_inputs_fail(self):
        gone = type("R", (), {"events": self.tmp / "gone.jsonl", "state": self.run_.state})
        self.assertFails(self.verify(gone), "--events file does not exist")
        self.assertFails(self.verify(fixtures=self.tmp / "no fixtures"), "--fixtures folder does not exist")

    def test_a_missing_required_argument_is_a_fail_line_not_a_pass(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.tool.main(["--events", "x"])
        self.assertEqual((code, out.getvalue().rstrip("\n").splitlines()[-1]), (2, "FAIL"))


class TestRequirements(VerifyBase):
    def forged_run(self, name, edit):
        made = self.rechained(self.run_, name, edit)
        home = self.tmp / f"forged {name}"                                                   # its own folder, so an anchor elsewhere is not "beside" it
        home.mkdir()
        moved = home / "events.jsonl"
        shutil.copyfile(made.events, moved)
        run = type("R", (), {"events": moved, "state": self.run_.state})
        path, head = self.anchored_variant(run, name)
        return run, path, head

    def test_a_chain_valid_but_accounting_inconsistent_replay_fails(self):
        def edit(objs):
            checkpoint = objs[-1]
            checkpoint["cash"] = "1.00"
        run, path, head = self.forged_run("cash", edit)
        result = self.verify(run, anchor_file=path)
        self.assertFails(result, "FAIL ACCOUNTING", "INCONSISTENT")
        self.assertRegex(result[1], r"ok\s+CHAIN")
        self.assertRegex(result[1], r"ok\s+ANCHOR")                                                  # the separate answers stay separate

    def test_no_plan_fails(self):
        runner = self.open("noplan")
        runner.run(SCRIPT)
        runner.write_events(self.tmp / "noplan" / "events.jsonl")
        runner.close()
        run = type("R", (), {"events": self.tmp / "noplan" / "events.jsonl", "state": self.tmp / "noplan" / "state" / f"replay_{runner.source_id}.jsonl"})
        path, _ = self.anchored_variant(run, "noplan")
        self.assertFails(self.verify(run, anchor_file=path), "FAIL PLAN", "NOT_DECLARED")

    def test_a_mismatched_plan_fails(self):
        def edit(objs):
            claim = objs[0]["inputs"]
            claim["plan"] = identity.plan(SCRIPT[:3] + SCRIPT[4:])
            claim["digest"] = identity.full_digest(claim)
        run, path, _ = self.forged_run("plan", edit)
        self.assertFails(self.verify(run, anchor_file=path), "FAIL PLAN", "MISMATCH")

    def test_a_partially_covered_unfinished_log_fails(self):
        lines = lines_of(self.run_.events.read_bytes())
        cut = next(n for n, x in enumerate(lines) if json.loads(x)["type"] == "INTENT_RECEIVED" and json.loads(x)["lots"] == 2)
        prefix = self.tmp / "prefix" / "events.jsonl"
        prefix.parent.mkdir()
        prefix.write_bytes(("\n".join(lines[:cut]) + "\n").encode("utf-8"))
        head = json.loads(lines[cut - 1])["hash"]
        run = type("R", (), {"events": prefix, "state": self.run_.state})
        result = self.verify(run, anchor_file=None, head=head)                                          # the head matches the prefix; it is still not a finished replay
        self.assertFails(result, "FAIL PLAN", "INCOMPLETE")

    def test_a_truncated_log_checked_against_the_original_head_or_anchor_fails(self):
        lines = lines_of(self.run_.events.read_bytes())
        short = self.tmp / "short" / "events.jsonl"
        short.parent.mkdir()
        short.write_bytes(("\n".join(lines[:-2]) + "\n").encode("utf-8"))
        run = type("R", (), {"events": short, "state": self.run_.state})
        self.assertFails(self.verify(run), "FAIL ANCHOR")
        self.assertFails(self.verify(run, anchor_file=None, head=self.head), "FAIL ANCHOR")

    def test_a_modified_log_fails(self):
        data = bytearray(self.run_.events.read_bytes())
        position = data.index(b"BUY")
        data[position:position + 3] = b"SEL"
        edited = self.tmp / "edited" / "events.jsonl"
        edited.parent.mkdir()
        edited.write_bytes(bytes(data))
        run = type("R", (), {"events": edited, "state": self.run_.state})
        self.assertFails(self.verify(run), "FAIL CHAIN")

    def test_a_rebuilt_chain_without_the_original_anchor_fails(self):
        run = self.rechained(self.run_, "rebuilt", lambda objs: objs[-1].update(cash="1.00"))
        self.assertFails(self.verify(run), "FAIL ANCHOR")

    def test_a_state_of_another_source_fails(self):
        other_fx = self.fixture_copy("other_source", source_id="other_source")
        other = self.run_variant("other", fixtures=other_fx)
        run = type("R", (), {"events": self.run_.events, "state": other.state})
        self.assertFails(self.verify(run), "FAIL PAIRING")

    def test_a_state_of_another_replay_of_the_same_source_fails(self):
        other = self.run_variant("slippage", slippage_ticks=2)
        run = type("R", (), {"events": self.run_.events, "state": other.state})
        self.assertFails(self.verify(run), "FAIL PAIRING")

    def test_a_missing_or_truncated_state_fails(self):
        gone = type("R", (), {"events": self.run_.events, "state": self.tmp / "state gone.jsonl"})
        self.assertFails(self.verify(gone), "--state file does not exist")
        short = self.tmp / "st" / f"replay_synthetic_fixture.jsonl"
        short.parent.mkdir()
        short.write_bytes(self.run_.state.read_bytes().splitlines(keepends=True)[0])
        self.assertFails(self.verify(type("R", (), {"events": self.run_.events, "state": short})))

    def test_changed_fixtures_fail(self):
        changed = self.fixture_copy("changed")
        target = next(p for p in sorted(changed.glob("SYNTHETIC_*.jsonl")))
        target.write_bytes(target.read_bytes() + b"\n")
        self.assertFails(self.verify(fixtures=changed), "FAIL IDENTITY")

    def test_a_different_account_name_fails(self):
        renamed = self.tmp / "acct" / "replay_other.jsonl"
        renamed.parent.mkdir()
        renamed.write_bytes(self.run_.state.read_bytes().replace(b"replay_synthetic_fixture", b"replay_other"))
        self.assertFails(self.verify(type("R", (), {"events": self.run_.events, "state": renamed})))

    def test_an_unexpected_audit_crash_is_a_fail(self):
        from unittest import mock
        with mock.patch.object(self.tool.audit, "build_report", side_effect=RuntimeError("boom")):
            result = self.verify()
        self.assertFails(result, "could not be completed")
        self.assertEqual(result[0], 2)


class TestReadOnlyAndExisting(VerifyBase):
    def test_nothing_is_created_changed_or_deleted_by_any_outcome(self):
        other = self.run_variant("slippage", slippage_ticks=2)
        before = snapshot(self.tmp)
        fixtures_before = snapshot(FIXTURE_DIR)
        for run, kwargs in ((self.run_, {}), (self.run_, {"anchor_file": None}), (type("R", (), {"events": self.run_.events, "state": other.state}), {}),
                            (self.run_, {"trust": "local"}), (self.run_, {"head": "2" * 64, "detail": True})):
            self.verify(run, **kwargs)
        self.assertEqual(snapshot(self.tmp), before)
        self.assertEqual(snapshot(FIXTURE_DIR), fixtures_before)

    def test_the_tool_source_has_no_way_to_write(self):
        text = TOOL.read_text(encoding="utf-8")
        for banned in ("unlink", "rmtree", "mkdir", "write_text", "write_bytes", "os.remove", "shutil", "rename", "replace(", "anchor_module.create", "'w'", '"w"', '"wb"', '"xb"'):
            self.assertNotIn(banned, text)
        self.assertNotRegex(text, r"open\(")

    def test_the_audit_cli_and_demo_behave_as_before(self):
        audit_tool = importlib.util.module_from_spec(importlib.util.spec_from_file_location("a", ROOT / "tools" / "replay_audit.py"))
        audit_tool.__spec__.loader.exec_module(audit_tool)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(audit_tool.main(["--events", str(self.run_.events), "--state", str(self.run_.state), "--fixtures", str(FIXTURE_DIR)]), 0)
        demo = importlib.util.module_from_spec(importlib.util.spec_from_file_location("d", ROOT / "tools" / "replay_fixture_demo.py"))
        demo.__spec__.loader.exec_module(demo)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(demo.main(["--out-dir", str(self.tmp / "demo run")]), 0)
        self.assertNotIn("replay_verify_later", out.getvalue())
        self.assertEqual(len([l for l in out.getvalue().splitlines() if l.startswith("python tools")]), 4)


if __name__ == "__main__":
    unittest.main()
