"""Tests for the tracked-file safety scan. All "secrets" are synthetic and are assembled at run time from pieces, so this file
itself contains none of the strings the scanner looks for (the repository scan test below proves that)."""
import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from niftypaper.safety import scan
from tests import ROOT

# synthetic examples, deliberately split so they never appear literally in this file
AWS = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
ASSIGN_VALUE = "Zx9Qm4Lp2Vb7Nc5Rt8Wy3Hk6"
ASSIGNMENT = "api" + "_key = '" + ASSIGN_VALUE + "'"
PRIVATE_KEY = "-----BEGIN " + "RSA PRIVATE" + " KEY-----"
JWT = "ey" + "J" + "hbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + "." + "abcdefghij1234567890"
BEARER = "Authorization: Bear" + "er " + "A1b2C3d4E5f6G7h8I9j0K1l2"
URL_CRED = "post" + "gres://" + "alice:" + "pa55w0rdlong" + "@db.internal/main"
ORDER_FUNCTION = "def " + "place" + "_order(self, qty):"
ORDER_CALL = "client." + "cancel" + "Order(1)"
ORDER_URL = "https://" + "broker.example.com" + "/v2/" + "orders"
BROKER_HOST = "https://" + "api." + "dhan" + ".co" + "/x"
SDK_IMPORT = "import " + "kite" + "connect"
SDK_IMPORT2 = "from " + "upstox" + "_client import Api"


def rules_for(text):
    return {f.rule for f in scan.scan_text("x.py", text)}


class TestRules(unittest.TestCase):
    def test_each_synthetic_secret_is_detected(self):
        self.assertIn("S2", rules_for(f"key = '{AWS}'"))
        self.assertIn("S3", rules_for(ASSIGNMENT))
        self.assertIn("S1", rules_for(PRIVATE_KEY))
        self.assertIn("S4", rules_for(f"t = '{JWT}'"))
        self.assertIn("S5", rules_for(BEARER))
        self.assertIn("S6", rules_for(f"url = '{URL_CRED}'"))

    def test_each_synthetic_broker_order_pattern_is_detected(self):
        self.assertIn("B1", rules_for(ORDER_FUNCTION))
        self.assertIn("B1", rules_for(ORDER_CALL))
        self.assertIn("B2", rules_for(f"requests_target = '{ORDER_URL}'"))
        self.assertIn("B3", rules_for(f"base = '{BROKER_HOST}'"))
        self.assertIn("B4", rules_for(SDK_IMPORT))
        self.assertIn("B4", rules_for(SDK_IMPORT2))

    def test_findings_never_contain_the_full_secret(self):
        for text, secret in ((f"key = '{AWS}'", AWS), (ASSIGNMENT, ASSIGN_VALUE), (f"t = '{JWT}'", JWT), (BEARER, "A1b2C3d4E5f6G7h8I9j0K1l2"),
                             (f"url = '{URL_CRED}'", "pa55w0rdlong")):
            for finding in scan.scan_text("x.py", text):
                self.assertNotIn(secret, str(finding))
                self.assertIn("redacted", finding.snippet)

    def test_harmless_text_is_not_flagged(self):
        harmless = [
            "token = os.environ['SERVICE_TOKEN']", "api_key = 'YOUR_API_KEY_GOES_HERE_1234'", "password = ''", "secret = 'abcdabcdabcdabcd'",
            "tokens_per_day = 5000", "# order placement is out of scope", "the placement of orders is not implemented",
            "total_orders = 3", "orders = []", "ticker = 'NIFTY'", "see https://example.com/docs", "x = 'a' * 40",
        ]
        for line in harmless:
            with self.subTest(line=line):
                self.assertEqual(scan.scan_text("x.py", line), [])

    def test_known_limitation_split_or_encoded_strings_are_not_detected(self):
        """Documents what the scan cannot see (see docs/SAFETY.md): this test passing is not a security claim."""
        self.assertEqual(scan.scan_text("x.py", "k = 'AK' + 'IA' + 'ABCDEFGHIJKLMNOP'"), [])
        self.assertEqual(scan.scan_text("x.py", "cmd = 'pl' + 'ace_ord' + 'er'"), [])
        import base64
        encoded = base64.b64encode(AWS.encode()).decode()
        self.assertEqual(scan.scan_text("x.py", f"blob = '{encoded}'"), [])


class TestFiles(unittest.TestCase):
    def write(self, tmp, name, content, binary=False):
        path = Path(tmp) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if binary else content.encode("utf-8"))
        return name

    def test_sensitive_file_names_are_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            names = [self.write(tmp, n, "x") for n in (".env", ".env.production", "keys/server.pem", "id_rsa", "credentials.json", "my_token.json", "a/secrets.yaml")]
            names += [self.write(tmp, ".env.example", "x"), self.write(tmp, "notes.txt", "x")]
            report = scan.scan_paths(Path(tmp), names)
            flagged = {f.path for f in report.findings if f.rule == "F1"}
            self.assertEqual(flagged, {".env", ".env.production", "keys/server.pem", "id_rsa", "credentials.json", "my_token.json", "a/secrets.yaml"})

    def test_incomplete_scans_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = self.write(tmp, "blob.bin", b"\x00\x01\x02", binary=True)
            latin = self.write(tmp, "latin.txt", b"caf\xe9", binary=True)
            big = self.write(tmp, "big.txt", "a" * (scan.MAX_BYTES + 1))
            missing = "does_not_exist.txt"
            report = scan.scan_paths(Path(tmp), [binary, latin, big, missing])
            reasons = {path: why for path, why in report.skipped}
            self.assertEqual(set(reasons), {binary, latin, big, missing})
            self.assertEqual(report.exit_code, 2)               # skipped files are never treated as clean

    def test_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            clean = self.write(tmp, "ok.py", "x = 1\n")
            dirty = self.write(tmp, "bad.py", f"k = '{AWS}'\n")
            self.assertEqual(scan.scan_paths(Path(tmp), [clean]).exit_code, 0)
            self.assertEqual(scan.scan_paths(Path(tmp), [clean, dirty]).exit_code, 1)

    def test_the_host_exemption_is_narrow(self):
        host = "api-t1" + "." + "fy" + "ers.in"
        text = "HOST = " + repr(host) + chr(10) + ORDER_FUNCTION + chr(10)
        with tempfile.TemporaryDirectory() as tmp:
            exempt = self.write(tmp, "src/niftypaper/providers/fyers_http.py", text)
            other = self.write(tmp, "src/niftypaper/providers/other.py", text)
            doc = self.write(tmp, "docs/V6-0.5_RECORD.md", text)
            report = scan.scan_paths(Path(tmp), [exempt, other, doc])
            found = {(f.path, f.rule) for f in report.findings}
            self.assertEqual(found, {(exempt, "B1"), (doc, "B1"), (other, "B1"), (other, "B3")})   # B3 waived only for the two named files

    def test_gitignore_requirements(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([f.snippet for f in scan.check_gitignore(Path(tmp))], ["missing .gitignore"])
            (Path(tmp) / ".gitignore").write_text(".env\n*.db\n", encoding="utf-8")
            missing = {f.snippet for f in scan.check_gitignore(Path(tmp))}
            self.assertIn("required ignore entry missing: *.pem", missing)
            (Path(tmp) / ".gitignore").write_text("\n".join(scan.REQUIRED_GITIGNORE), encoding="utf-8")
            self.assertEqual(scan.check_gitignore(Path(tmp)), [])


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, check=True)


class TestGitIntegration(unittest.TestCase):
    def make_repo(self, tmp):
        git(tmp, "init", "-q")
        (Path(tmp) / ".gitignore").write_text("\n".join(scan.REQUIRED_GITIGNORE) + "\n", encoding="utf-8")
        return Path(tmp)

    def test_only_tracked_files_are_scanned_unless_untracked_is_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(tmp)
            (repo / "tracked.py").write_text(f"k = '{AWS}'\n", encoding="utf-8")
            (repo / "untracked.py").write_text(f"k = '{AWS}'\n", encoding="utf-8")
            (repo / "data").mkdir()
            (repo / "data" / "ignored.py").write_text(f"k = '{AWS}'\n", encoding="utf-8")
            git(repo, "add", ".gitignore", "tracked.py")
            tracked = scan.scan_repo(repo)
            self.assertEqual({f.path for f in tracked.findings}, {"tracked.py"})
            everything = scan.scan_repo(repo, include_untracked=True)
            self.assertEqual({f.path for f in everything.findings}, {"tracked.py", "untracked.py"})   # ignored data/ stays out

    def test_command_line_exit_codes_and_redacted_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(tmp)
            (repo / "clean.py").write_text("x = 1\n", encoding="utf-8")
            git(repo, "add", ".")
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(scan.main(["--root", str(repo)]), 0)
            (repo / "bad.py").write_text(f"{ORDER_FUNCTION}\n    k = '{AWS}'\n", encoding="utf-8")
            git(repo, "add", ".")
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(scan.main(["--root", str(repo)]), 1)
            text = out.getvalue()
            self.assertIn("FINDING", text)
            self.assertNotIn(AWS, text)

    def test_not_a_git_repository_is_an_error_not_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "x.py").write_text("x = 1\n", encoding="utf-8")
            with self.assertRaises(scan.SafetyScanError):
                scan.scan_repo(tmp)
            self.assertEqual(scan.main(["--root", tmp]), 2)


class TestThisRepository(unittest.TestCase):
    def test_this_repository_passes_its_own_scan_including_untracked_files(self):
        report = scan.scan_repo(ROOT, include_untracked=True)
        self.assertEqual([str(f) for f in report.findings], [])
        self.assertEqual(report.skipped, [])
        self.assertGreater(report.scanned, 8)


if __name__ == "__main__":
    unittest.main()
