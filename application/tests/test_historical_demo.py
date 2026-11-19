"""tools/replay_fixture_demo.py: the one-command fixture replay, its refusals, its success criteria and the commands it prints. Invented fixtures only."""
import ast
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from niftypaper.historical import anchor, audit, replay
from niftypaper.historical import manifest as mf
from tests import ROOT
from tests import historical_fixtures as fx

TOOLS = ROOT / "tools"


def load(name):
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Base(unittest.TestCase):
    def setUp(self):
        self.demo = load("replay_fixture_demo")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

    def go(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.demo.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def ok(self, name="run1"):
        code, out, err = self.go("--out-dir", str(self.tmp / name))
        self.assertEqual((code, err), (0, ""), out)
        return out

    @staticmethod
    def field(out, label):
        return re.search(rf"^\s+{re.escape(label)}\s+(\S+)", out, re.M).group(1)

    def tree(self, folder):
        return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file() and not p.name.endswith(".lock")}


class TestSuccessAndDeterminism(Base):
    def test_a_successful_run_reports_identity_count_head_and_path(self):
        out = self.ok()
        folder = self.tmp / "run1"
        data = (folder / "events.jsonl").read_bytes()
        events = [json.loads(line) for line in data.decode().splitlines()]
        self.assertEqual(self.field(out, "event count"), str(len(events)))
        self.assertEqual(self.field(out, "head hash"), events[-1]["hash"])
        self.assertEqual(self.field(out, "log sha256"), hashlib.sha256(data).hexdigest())
        self.assertIn("synthetic_fixture", out)
        self.assertIn("replay_synthetic_fixture", out)
        self.assertIn(str(folder / "events.jsonl"), out)
        self.assertIn("chain         VALID", out)
        self.assertIn("accounting    CONSISTENT (coverage FULL)", out)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["events.jsonl", "state"])
        report = audit.build_report(audit.AuditConfig(folder / "events.jsonl", folder / "state" / self.demo.STATE_NAME, self.demo.FIXTURE_DIR))
        self.assertEqual((report["verdict"], report["coverage"], report["chain"]["status"], report["chain"]["head"]), ("CONSISTENT", "FULL", "VALID", events[-1]["hash"]))
        self.assertEqual(self.field(out, "audit digest"), report["digest"])

    def test_the_replay_exercises_fills_exits_and_refusals(self):
        self.ok()
        events = [json.loads(line) for line in (self.tmp / "run1" / "events.jsonl").read_text().splitlines()]
        kinds = [(e["type"], e.get("code")) for e in events if e["type"] in ("FILL", "INTENT_REFUSED")]
        self.assertEqual(kinds, [("FILL", None), ("FILL", None), ("INTENT_REFUSED", "BOUNDARY_REJECTED"), ("FILL", None), ("FILL", None),
                                 ("INTENT_REFUSED", "NOT_FILL_ELIGIBLE"), ("INTENT_REFUSED", "NO_VISIBLE_QUOTE")])

    def test_separate_destinations_give_identical_bytes_and_head(self):
        first, second, third = self.ok("a"), self.ok("b"), self.ok("c" * 3)
        a, b, c = (self.tree(self.tmp / n) for n in ("a", "b", "ccc"))
        self.assertEqual(a, b)
        self.assertEqual(a, c)
        self.assertEqual({self.field(o, "head hash") for o in (first, second, third)}, {self.field(first, "head hash")})
        self.assertEqual({self.field(o, "audit digest") for o in (first, second, third)}, {self.field(first, "audit digest")})

    def test_event_bytes_hold_no_paths_or_current_times(self):
        self.ok("a")
        text = (self.tmp / "a" / "events.jsonl").read_text()
        state = (self.tmp / "a" / "state" / self.demo.STATE_NAME).read_text()
        for blob in (text, state):
            self.assertNotIn(str(self.tmp), blob)
            self.assertNotIn(self.tmp.name, blob)
            self.assertNotIn("2026-", blob)
        self.assertNotIn("PowerShell", text)

    def test_the_fixed_configuration_and_source(self):
        self.assertEqual((self.demo.EXPECTED_SOURCE, self.demo.EXPECTED_ACCOUNT), ("synthetic_fixture", "replay_synthetic_fixture"))
        self.assertEqual(self.demo.FIXTURE_DIR, ROOT / "tests" / "fixtures" / "historical_synthetic")
        self.assertTrue(all(i.at.year == 2031 for i in self.demo.script()))                          # invented dates
        self.assertEqual(self.demo.script(), self.demo.script())
        self.assertTrue(self.demo.DEFAULT_OUT_DIR.is_relative_to(ROOT / "data"))
        self.assertIn("data/", (ROOT / ".gitignore").read_text().splitlines())


class TestRefusals(Base):
    def test_an_existing_log_is_never_overwritten_and_a_new_folder_is_suggested(self):
        self.ok("run1")
        before = self.tree(self.tmp / "run1")
        code, out, err = self.go("--out-dir", str(self.tmp / "run1"))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("never overwritten", err)
        self.assertIn(f"--out-dir {self.tmp / 'run2'}", err)
        self.assertEqual(self.tree(self.tmp / "run1"), before)
        foreign = self.tmp / "other"
        foreign.mkdir()
        (foreign / "events.jsonl").write_bytes(b"precious\n")
        code, out, err = self.go("--out-dir", str(foreign))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("--out-dir", err)
        self.assertEqual(self.tree(foreign), {"events.jsonl": b"precious\n"})

    def test_saved_state_from_an_earlier_run_is_not_reused(self):
        self.ok("run1")
        folder = self.tmp / "half"
        (folder / "state").mkdir(parents=True)
        (folder / "state" / "leftover.jsonl").write_bytes(b"x\n")
        code, out, err = self.go("--out-dir", str(folder))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("never reused or deleted", err)
        self.assertFalse((folder / "events.jsonl").exists())
        self.assertEqual(self.tree(folder), {"state/leftover.jsonl": b"x\n"})

    def test_bad_folders(self):
        """Uses a copy of the fixtures under a temporary root, so a regression in the guard could never write into the committed fixtures."""
        root = self.tmp / "fx_root"
        copy = root / "set"
        shutil.copytree(self.demo.FIXTURE_DIR, copy)
        file = self.tmp / "afile"
        file.write_text("x")
        before = self.tree(root)
        with mock.patch.object(self.demo, "FIXTURE_DIR", copy):
            for target in (file, copy, copy / "inside", root):
                code, out, err = self.go("--out-dir", str(target))
                self.assertEqual((code, out), (2, ""), target)
        self.assertEqual(self.tree(root), before)
        self.assertFalse((copy / "inside").exists())

    def test_a_fixture_set_with_another_source_is_refused_before_anything_is_written(self):
        folder = self.tmp / "otherfix"
        shutil.copytree(self.demo.FIXTURE_DIR, folder)
        files = {e.path: e.record_type for e in mf.load_manifest(folder / mf.MANIFEST_NAME)}
        (folder / mf.MANIFEST_NAME).unlink()
        fx.write_manifest(folder, files, source_id="synthetic_other")
        with mock.patch.object(self.demo, "FIXTURE_DIR", folder):
            code, out, err = self.go("--out-dir", str(self.tmp / "run1"))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("synthetic_fixture", err)
        self.assertFalse((self.tmp / "run1").exists())
        (folder / mf.MANIFEST_NAME).write_bytes(b"{not json}\n")
        with mock.patch.object(self.demo, "FIXTURE_DIR", folder):
            self.assertEqual(self.go("--out-dir", str(self.tmp / "run1"))[0], 2)
        self.assertFalse((self.tmp / "run1").exists())

    def test_unknown_options_are_rejected_by_the_parser(self):
        for argv in (["--network"], ["--fixtures", "x"], ["--out-dir"], ["extra"]):
            with self.subTest(argv), self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()):
                self.demo.main(argv)
            self.assertEqual(caught.exception.code, 2)


class TestNeverReportsFalseSuccess(Base):
    SUCCESS = "finished and verified"

    def assertNoSuccess(self, code, out, err, expected_code):
        self.assertEqual(code, expected_code)
        self.assertNotIn(self.SUCCESS, out + err)
        self.assertNotIn("replay_anchor.py create", out + err)                                      # no commands are offered for an unverified log
        self.assertEqual(out, "")
        self.assertIn("No success is reported", err)

    def run_with(self, tamper, name="run1"):
        real = replay.ReplayRunner.write_events

        def write_then_tamper(runner, path):
            receipt = real(runner, path)
            tamper(Path(path), Path(path).parent / "state" / self.demo.STATE_NAME)
            return receipt

        with mock.patch.object(replay.ReplayRunner, "write_events", write_then_tamper):
            return self.go("--out-dir", str(self.tmp / name))

    def test_a_log_whose_chain_is_broken_after_publication(self):
        def tamper(events, state):
            events.write_bytes(events.read_bytes().replace(b'"FILL"', b'"FILX"', 1))
        code, out, err = self.run_with(tamper)
        self.assertNoSuccess(code, out, err, 4)
        self.assertIn("event chain is INVALID", err)
        self.assertIn("HASH_MISMATCH", err)
        self.assertTrue((self.tmp / "run1" / "events.jsonl").exists())                               # evidence kept, not repaired
        self.assertIn(b"FILX", (self.tmp / "run1" / "events.jsonl").read_bytes())

    def test_a_log_that_is_cut_short(self):
        def tamper(events, state):
            events.write_bytes(events.read_bytes()[:-30])
        code, out, err = self.run_with(tamper)
        self.assertNoSuccess(code, out, err, 4)
        self.assertIn("INCOMPLETE_FINAL_RECORD", err)

    def test_accounting_that_no_longer_reconciles(self):
        def tamper(events, state):
            lines = state.read_bytes().split(b"\n")
            trade = json.loads(lines[2])
            trade["realized_pnl"] = "99.00" if trade["realized_pnl"] is not None else None
            trade["cost"] = "99.99"
            lines[2] = json.dumps(trade, sort_keys=True).encode()
            state.write_bytes(b"\n".join(lines))
        code, out, err = self.run_with(tamper)
        self.assertNoSuccess(code, out, err, 4)
        self.assertIn("accounting reconciliation is INCONSISTENT", err)
        self.assertIn(b"99.99", (self.tmp / "run1" / "state" / self.demo.STATE_NAME).read_bytes())

    def test_missing_state_means_not_reconcilable(self):
        def tamper(events, state):
            state.unlink()
        code, out, err = self.run_with(tamper)
        self.assertNoSuccess(code, out, err, 4)
        self.assertIn("NOT_RECONCILABLE", err)

    def test_each_mismatch_with_the_writers_receipt_or_identity_is_reported(self):
        real = audit.build_report

        def patched(change):
            def build(config):
                report = copy.deepcopy(real(config))
                change(report)
                return report
            return build

        cases = {
            "chain status": (lambda r: r["chain"].update(status="INVALID", reason="X", detail="d"), "event chain is INVALID"),
            "verdict": (lambda r: r.update(verdict="INCONSISTENT"), "INCONSISTENT"),
            "verdict blocked": (lambda r: r.update(verdict="NOT_RECONCILABLE"), "NOT_RECONCILABLE"),
            "coverage": (lambda r: r.update(coverage="PARTIAL"), "PARTIAL coverage"),
            "account": (lambda r: r["account"].update(name="replay_other"), "replay_other"),
            "source": (lambda r: r["account"].update(source_id="other"), "expected"),
            "event count": (lambda r: r["chain"].update(events_verified=r["chain"]["events_verified"] + 1), "writer reported"),
            "head": (lambda r: r["chain"].update(head="0" * 64), "head hash differs"),
        }
        for name, (change, text) in cases.items():
            with self.subTest(name), mock.patch.object(audit, "build_report", patched(change)):
                code, out, err = self.go("--out-dir", str(self.tmp / ("m_" + name.replace(" ", "_"))))
            self.assertNoSuccess(code, out, err, 4)
            self.assertIn(text, err, name)
            self.assertTrue((self.tmp / ("m_" + name.replace(" ", "_")) / "events.jsonl").exists())

    def test_a_file_that_differs_from_the_receipt(self):
        real_receipt = replay.ReplayRunner.write_events

        def lying(runner, path):
            receipt = real_receipt(runner, path)
            return replay.EventLogReceipt(receipt.path, receipt.events, receipt.head, "0" * 64, receipt.bytes)

        with mock.patch.object(replay.ReplayRunner, "write_events", lying):
            code, out, err = self.go("--out-dir", str(self.tmp / "run1"))
        self.assertNoSuccess(code, out, err, 4)
        self.assertIn("differs from what the writer reported", err)

    def test_replay_or_write_failures_are_not_success_and_leave_evidence_alone(self):
        with mock.patch.object(replay.ReplayRunner, "write_events", side_effect=replay.EventLogError("WRITE_FAILED", "OSError")):
            code, out, err = self.go("--out-dir", str(self.tmp / "w"))
        self.assertEqual((code, out), (3, ""))
        self.assertIn("FAILED", err)
        self.assertNotIn(self.SUCCESS, err)
        self.assertFalse((self.tmp / "w" / "events.jsonl").exists())
        self.assertTrue((self.tmp / "w" / "state" / self.demo.STATE_NAME).exists())                  # the saved trades stay; nothing is deleted
        with mock.patch.object(replay.ReplayRunner, "open", side_effect=replay.ReplayRefused("INTEGRITY_FAILURE", "x")):
            code, out, err = self.go("--out-dir", str(self.tmp / "o"))
        self.assertEqual((code, out), (3, ""))
        self.assertNotIn(self.SUCCESS, err)
        with mock.patch.object(replay.ReplayRunner, "run", side_effect=replay.ReplayRefused("BAD_INTENT", "x")):
            code, out, err = self.go("--out-dir", str(self.tmp / "r"))
        self.assertEqual((code, out), (3, ""))

    def test_the_failed_run_can_be_followed_by_a_clean_one_elsewhere(self):
        with mock.patch.object(replay.ReplayRunner, "write_events", side_effect=replay.EventLogError("WRITE_FAILED")):
            self.go("--out-dir", str(self.tmp / "bad"))
        code, out, err = self.go("--out-dir", str(self.tmp / "bad"))
        self.assertEqual(code, 2)                                                                    # the earlier evidence blocks reuse
        self.assertEqual(self.go("--out-dir", str(self.tmp / "good"))[0], 0)


class TestPathHandling(Base):
    def test_spaces_relative_forward_slash_and_nested_paths(self):
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.tmp)
        for argv in (["--out-dir", "with space/run 1"], ["--out-dir", "rel\\sub\\run2"], ["--out-dir", "./dot/../run3/"], ["--out-dir", str(self.tmp / "abs" / "a b" / "run4") + os.sep]):
            code, out, err = self.go(*argv)
            self.assertEqual((code, err), (0, ""), argv)
            log = Path(re.search(r"event log\s+(.*events\.jsonl)", out).group(1))
            self.assertTrue(log.is_absolute())
            self.assertTrue(log.exists(), log)
        self.assertTrue((self.tmp / "with space" / "run 1" / "events.jsonl").exists())
        self.assertTrue((self.tmp / "run3" / "events.jsonl").exists())

    def test_powershell_quoting(self):
        quote = self.demo.ps_quote
        self.assertEqual(quote("C:\\a b\\c"), "'C:\\a b\\c'")
        self.assertEqual(quote("it's"), "'it''s'")
        self.assertEqual(quote("$env:USERNAME `n \"x\""), "'$env:USERNAME `n \"x\"'")                  # nothing inside single quotes is expanded
        self.assertEqual(quote(Path("x")), "'x'")

    def test_an_apostrophe_in_the_folder_name_is_quoted_safely(self):
        out = self.ok("it's here")
        self.assertIn("it''s here", out)
        self.assertEqual(len([line for line in out.splitlines() if line.startswith("$Events")]), 1)
        variables = self.variables(out)
        self.assertTrue(Path(variables["Events"]).exists())

    @unittest.skipUnless(os.name == "nt", "Windows path forms")
    def test_windows_forms(self):
        out = self.ok("win")
        variables = self.variables(out)
        for name in ("Events", "State", "Fixtures", "Anchor"):
            self.assertRegex(variables[name], r"^[A-Za-z]:\\", name)
            self.assertNotIn("/", variables[name])
        forward = (str(self.tmp / "fwd").replace("\\", "/"))
        code, out, err = self.go("--out-dir", forward)
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("/", self.variables(out)["Events"])
        self.assertIn("tools\\replay_anchor.py", out)

    @staticmethod
    def variables(out):
        found = {}
        for match in re.finditer(r"^\$(\w+)\s*=\s*'((?:[^']|'')*)'", out, re.M):
            found[match.group(1)] = match.group(2).replace("''", "'")
        return found


class TestPrintedCommands(Base):
    """The printed commands are run, in-process, through the real tools: they must be valid syntax and behave as described."""

    def commands(self, out):
        return [line for line in out.splitlines() if line.startswith("python tools")]

    def execute(self, line, variables):
        tokens = line.split()
        self.assertEqual(tokens[0], "python")
        script = tokens[1].replace("\\", "/")
        tool = load(Path(script).stem)
        argv = []
        for token in tokens[2:]:
            argv.append(variables[token[1:]] if token.startswith("$") else token)
        buffer, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(error):
            code = tool.main(argv)
        return code, buffer.getvalue(), error.getvalue(), script

    def test_anchor_creation_is_not_automatic(self):
        out = self.ok()
        found = list(self.tmp.rglob("*.anchor.json")) + list(self.tmp.rglob("*anchor*"))
        self.assertEqual(found, [])
        self.assertIn("No anchor has been created", out)
        self.assertIn("Nothing below has been run for you", out)
        text = (TOOLS / "replay_fixture_demo.py").read_text(encoding="utf-8")
        self.assertNotIn("anchor.create", text)

    def test_every_printed_command_runs_with_the_real_cli_syntax(self):
        out = self.ok()
        variables = TestPathHandling.variables(out)
        commands = self.commands(out)
        self.assertEqual([c.split()[2] for c in commands], ["create", "show", "--events", "--events"])
        results = []
        for line in commands[:3]:
            code, stdout, stderr, script = self.execute(line, variables)
            results.append((script, code))
            self.assertEqual(code, 0, (line, stderr))
        self.assertEqual(results, [("tools/replay_anchor.py", 0), ("tools/replay_anchor.py", 0), ("tools/replay_audit.py", 0)])
        created = Path(variables["Anchor"])
        self.assertTrue(created.exists())
        report = audit.build_report(audit.AuditConfig(Path(variables["Events"]), Path(variables["State"]), Path(variables["Fixtures"]), anchor_path=created))
        self.assertEqual((report["anchor"]["status"], report["anchor"]["protection"]), ("MATCH", "LOCAL_OR_UNDECLARED"))
        protected = self.tmp / "usb" / "copy.anchor.json"                                          # step 4 with a preserved copy
        protected.parent.mkdir()
        shutil.copy(created, protected)
        variables["Protected"] = str(protected)
        code, stdout, stderr, _ = self.execute(commands[3], variables)
        self.assertEqual((code, stderr), (0, ""), stdout)
        self.assertIn("declared independent by you, not verified", stdout)

    def test_the_strict_options_fail_when_the_preserved_copy_is_wrong(self):
        out = self.ok()
        variables = TestPathHandling.variables(out)
        commands = self.commands(out)
        self.execute(commands[0], variables)
        other = self.ok("run2")
        other_vars = TestPathHandling.variables(other)
        self.assertEqual(variables["Head"], other_vars["Head"])                                     # identical runs have identical heads...
        self.execute(self.commands(other)[0], other_vars)
        variables["Protected"] = other_vars["Anchor"]
        self.assertEqual(self.execute(commands[3], variables)[0], 0)                                # ...so an anchor from the identical run matches
        variables["Head"] = "0" * 64
        self.assertEqual(self.execute(commands[3], variables)[0], 3)
        variables["Head"] = other_vars["Head"]
        variables["Protected"] = str(self.tmp / "missing.anchor.json")
        self.assertEqual(self.execute(commands[3], variables)[0], 3)

    def test_create_refuses_to_overwrite_when_pasted_twice(self):
        out = self.ok()
        variables = TestPathHandling.variables(out)
        create = self.commands(out)[0]
        self.assertEqual(self.execute(create, variables)[0], 0)
        before = Path(variables["Anchor"]).read_bytes()
        code, _, stderr, _ = self.execute(create, variables)
        self.assertEqual(code, 2)
        self.assertIn("DESTINATION_EXISTS", stderr)
        self.assertEqual(Path(variables["Anchor"]).read_bytes(), before)

    def test_the_suggested_anchor_place_is_apart_from_the_log_and_the_state(self):
        variables = TestPathHandling.variables(self.ok())
        self.assertFalse(anchor.beside(Path(variables["Anchor"]), Path(variables["Events"]), Path(variables["State"])))
        self.assertNotEqual(Path(variables["Anchor"]).parent, Path(variables["Events"]).parent)

    def test_the_output_explains_what_is_local_and_what_is_not_shown(self):
        out = self.ok()
        for phrase in ("This anchor is LOCAL", "consistency check only", "write-protected USB stick", "YOUR", "declaration", "does NOT show",
                       "genuine", "Nothing is ever repaired", "CHANGE THIS"):
            self.assertIn(phrase, out)
        self.assertEqual(len([line for line in out.splitlines() if "python tools\\replay_anchor.py create" in line]), 1)
        self.assertEqual(self.demo.ps_quote("a"), "'a'")


class TestIsolation(Base):
    def test_no_network_access(self):
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network used")), mock.patch.object(socket, "create_connection", side_effect=AssertionError("network used")):
            self.ok()
        tree = ast.parse((TOOLS / "replay_fixture_demo.py").read_text(encoding="utf-8"))
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules.add(node.module.split(".")[0])
        banned = {"socket", "ssl", "http", "urllib", "urllib3", "requests", "httpx", "aiohttp", "websocket", "ftplib", "smtplib", "asyncio", "subprocess", "webbrowser", "tests"}
        self.assertEqual(modules & banned, set())
        self.assertTrue(modules <= set(__import__("sys").stdlib_module_names) | {"niftypaper"}, modules)

    def test_no_credentials_environment_or_clock_are_read(self):
        text = (TOOLS / "replay_fixture_demo.py").read_text(encoding="utf-8")
        for banned in ("os.environ", "getenv", "datetime.now", "utcnow", "time.time", "FYERS", "access_token", "api_key", "password"):
            self.assertNotIn(banned, text, banned)

    def test_only_the_output_folder_changes_and_the_fixtures_and_research_folders_are_untouched(self):
        watched = [self.demo.FIXTURE_DIR, ROOT / "config", ROOT / "src"]
        research = ROOT.parent / "ML"
        if research.is_dir():
            watched.append(research)

        def snapshot():
            return {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for folder in watched for p in folder.rglob("*") if p.is_file() and "__pycache__" not in p.parts}

        before = snapshot()
        self.ok()
        self.assertEqual(snapshot(), before)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["run1"])

    def test_the_default_output_location_is_ignored_by_git_and_outside_research_data(self):
        default = self.demo.DEFAULT_OUT_DIR
        self.assertEqual(default.relative_to(ROOT).as_posix(), "data/replay_demo/run1")
        self.assertFalse(str(default).startswith(str(ROOT.parent / "ML")))
        with mock.patch.object(self.demo, "DEFAULT_OUT_DIR", self.tmp / "default_run"):
            code, out, err = self.go()
        self.assertEqual((code, err), (0, ""))
        self.assertTrue((self.tmp / "default_run" / "events.jsonl").exists())

    def test_help_documents_the_behaviour(self):
        buffer = io.StringIO()
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stdout(buffer):
            self.demo.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("--out-dir", buffer.getvalue())


class TestDocumentation(unittest.TestCase):
    def test_readme_and_audit_doc_describe_the_one_command_workflow(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        doc = (ROOT / "docs" / "HISTORICAL_REPLAY_AUDIT.md").read_text(encoding="utf-8")
        for text in (readme, doc):
            self.assertIn("tools/replay_fixture_demo.py", text.replace("\\", "/"))
        for phrase in ("-m unittest discover -s tests -t .", "-W error::ResourceWarning"):
            self.assertIn(phrase, readme)
        for phrase in ("does not create an anchor", "LOCAL", "--require-anchor", "Exit codes"):
            self.assertIn(phrase, doc)


if __name__ == "__main__":
    unittest.main()
